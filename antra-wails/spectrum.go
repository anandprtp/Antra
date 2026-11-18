package main

// Real FFT-based spectral analysis (v1.1.8 FEAT-5).
//
// Replaces the previous six-point frequency probe, which could only ever report
// a cutoff from the fixed set {8k, 12k, 16k, 17k, 19k, 21k}. That resolution
// cannot distinguish a 320 kbps MP3 (~20 kHz) from a 256 kbps AAC (~19 kHz) from
// a genuine lossless master (Nyquist), which is the "incorrect information"
// being reported — and it produced a bare integer with no confidence attached,
// so the UI asserted "Fake Lossless" from what was often a very weak signal.
//
// This decodes real PCM through ffmpeg, windows it, runs an FFT, and averages
// the magnitude spectrum over many frames. Every reported conclusion carries a
// confidence and the evidence behind it, and the analysis explicitly reports
// "inconclusive" rather than guessing.

import (
	"encoding/binary"
	"fmt"
	"io"
	"math"
	"os"
	"os/exec"
	"sort"
)

const (
	fftSize    = 4096 // ~11.7 Hz/bin at 48 kHz — fine enough to place a cutoff precisely
	fftHop     = 2048 // 50% overlap
	maxFrames  = 900  // cap work on long tracks; ~40 s of audio at 48 kHz
	analyseSec = 45.0 // window of audio to analyse
	// Spectrogram transport resolution. 256 frequency rows is plenty for a
	// display a few hundred pixels tall, and keeps the payload small enough to
	// hand to the frontend without shipping the ~170MB of raw PCM it came from.
	specRowCount = 256
	specFloorDb  = -100.0 // dB floor for the byte quantisation
)

// SpectralAnalysis is the honest, evidence-carrying result of the FFT pass.
type SpectralAnalysis struct {
	SampleRate int `json:"sampleRate"`
	NyquistHz  int `json:"nyquistHz"`

	// CutoffHz is the highest frequency carrying sustained energy. 0 = unknown.
	CutoffHz int `json:"cutoffHz"`
	// Confidence 0..1 for the cutoff. Low values MUST NOT be presented as fact.
	CutoffConfidence float64 `json:"cutoffConfidence"`
	// CliffHz is where the steepest drop was found — the low-pass wall itself.
	// Reported separately from CutoffHz (the -3 dB point) because they are
	// different measurements and conflating them hid a bug.
	CliffHz int `json:"cliffHz"`
	// ShelfDropDb is how sharply energy falls across the cutoff. A steep drop is
	// the signature of a lossy encoder's low-pass; a gentle roll-off is normal
	// programme material.
	ShelfDropDb float64 `json:"shelfDropDb"`
	// EnergyAboveCutoffDb: residual energy above the cutoff, relative to peak.
	EnergyAboveCutoffDb float64 `json:"energyAboveCutoffDb"`

	// Verdict is deliberately one of: "lossless", "lossy", "inconclusive".
	Verdict string `json:"verdict"`
	// LikelySource e.g. "MP3 ~320 kbps" — empty when inconclusive.
	LikelySource string `json:"likelySource"`
	// Evidence is human-readable justification shown in the UI.
	Evidence []string `json:"evidence"`

	// AvgSpectrumDb is the averaged magnitude spectrum in dB relative to peak,
	// downsampled for transport/rendering.
	AvgSpectrumDb []float64 `json:"avgSpectrumDb"`
	// FramesAnalysed is how much evidence the verdict rests on.
	FramesAnalysed int `json:"framesAnalysed"`

	// ── Interactive spectrogram data (FEAT-5 UI) ──────────────────────────
	// Columns is a time x frequency matrix of dB values, already quantised to
	// bytes for transport: a full float64 matrix would be tens of MB, whereas
	// the display only ever resolves ~1 dB steps. 0 = SpecFloorDb, 255 = 0 dB.
	Columns     [][]byte `json:"columns,omitempty"`
	SpecFloorDb float64  `json:"specFloorDb"`
	// TimeStartSec / TimeEndSec locate the analysed window inside the track, so
	// the hover readout can report a real timestamp rather than an offset.
	TimeStartSec float64 `json:"timeStartSec"`
	TimeEndSec   float64 `json:"timeEndSec"`
	// FreqBinHz is the width of one row, for the hover frequency readout.
	FreqBinHz float64 `json:"freqBinHz"`
}

// ---------------------------------------------------------------------------
// FFT
// ---------------------------------------------------------------------------

// fftInPlace performs an iterative in-place radix-2 Cooley–Tukey FFT.
// len(re) must be a power of two.
func fftInPlace(re, im []float64) {
	n := len(re)
	if n <= 1 {
		return
	}
	// Bit-reversal permutation.
	for i, j := 1, 0; i < n; i++ {
		bit := n >> 1
		for ; j&bit != 0; bit >>= 1 {
			j ^= bit
		}
		j ^= bit
		if i < j {
			re[i], re[j] = re[j], re[i]
			im[i], im[j] = im[j], im[i]
		}
	}
	for length := 2; length <= n; length <<= 1 {
		ang := -2 * math.Pi / float64(length)
		wRe, wIm := math.Cos(ang), math.Sin(ang)
		for i := 0; i < n; i += length {
			curRe, curIm := 1.0, 0.0
			half := length / 2
			for j := 0; j < half; j++ {
				uRe, uIm := re[i+j], im[i+j]
				vRe := re[i+j+half]*curRe - im[i+j+half]*curIm
				vIm := re[i+j+half]*curIm + im[i+j+half]*curRe
				re[i+j], im[i+j] = uRe+vRe, uIm+vIm
				re[i+j+half], im[i+j+half] = uRe-vRe, uIm-vIm
				nRe := curRe*wRe - curIm*wIm
				curIm = curRe*wIm + curIm*wRe
				curRe = nRe
			}
		}
	}
}

// hannWindow returns a precomputed Hann window of size n.
func hannWindow(n int) []float64 {
	w := make([]float64, n)
	for i := range w {
		w[i] = 0.5 * (1 - math.Cos(2*math.Pi*float64(i)/float64(n-1)))
	}
	return w
}

// ---------------------------------------------------------------------------
// Decode + analyse
// ---------------------------------------------------------------------------

// decodePCM streams mono float32 PCM at the file's native sample rate.
func decodePCM(filePath, ffmpegExe string, sampleRate int, startSec float64) ([]float64, error) {
	cmd := exec.Command(
		resolveExe(ffmpegExe, "ffmpeg"),
		"-v", "error",
		"-ss", fmt.Sprintf("%.2f", startSec),
		"-i", filePath,
		"-t", fmt.Sprintf("%.2f", analyseSec),
		"-ac", "1",
		"-ar", fmt.Sprintf("%d", sampleRate),
		"-f", "f32le",
		"-",
	)
	hideProcess(cmd)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	defer func() { _ = cmd.Wait() }()

	need := int(analyseSec*float64(sampleRate)) + fftSize
	samples := make([]float64, 0, need)
	buf := make([]byte, 4*8192)
	for len(samples) < need {
		n, rerr := io.ReadFull(stdout, buf)
		if n > 0 {
			for i := 0; i+4 <= n; i += 4 {
				bits := binary.LittleEndian.Uint32(buf[i : i+4])
				samples = append(samples, float64(math.Float32frombits(bits)))
			}
		}
		if rerr != nil {
			break
		}
	}
	if len(samples) < fftSize {
		return nil, fmt.Errorf("not enough audio decoded (%d samples)", len(samples))
	}
	return samples, nil
}

// analyseSpectrum runs the full FFT pass and derives an evidence-backed verdict.
func analyseSpectrum(filePath, ffmpegExe string, sampleRate int, durationSec float64) (*SpectralAnalysis, error) {
	if sampleRate <= 0 {
		sampleRate = 44100
	}
	// Skip intros/outros — analyse from a point that is likely to be dense.
	start := durationSec * 0.25
	if durationSec < 20 {
		start = 0
	} else if start > 60 {
		start = 60
	}

	samples, err := decodePCM(filePath, ffmpegExe, sampleRate, start)
	if err != nil {
		return nil, err
	}

	win := hannWindow(fftSize)
	bins := fftSize / 2
	acc := make([]float64, bins)
	frames := 0

	// Per-frame spectra kept for the interactive spectrogram (FEAT-5 UI).
	// Quantised to bytes at the end rather than stored as float64, because a
	// full matrix would be tens of megabytes for a single track and the display
	// resolves roughly 1 dB steps anyway.
	specRows := specRowCount
	if specRows > bins {
		specRows = bins
	}
	columns := make([][]float64, 0, maxFrames)

	re := make([]float64, fftSize)
	im := make([]float64, fftSize)

	for off := 0; off+fftSize <= len(samples) && frames < maxFrames; off += fftHop {
		// Skip near-silent frames: they carry no spectral information and would
		// drag the average down, making a real cutoff look lower than it is.
		var energy float64
		for i := 0; i < fftSize; i++ {
			s := samples[off+i]
			energy += s * s
		}
		if math.Sqrt(energy/float64(fftSize)) < 1e-4 {
			continue
		}
		for i := 0; i < fftSize; i++ {
			re[i] = samples[off+i] * win[i]
			im[i] = 0
		}
		fftInPlace(re, im)
		col := make([]float64, bins)
		for b := 0; b < bins; b++ {
			p := re[b]*re[b] + im[b]*im[b]
			acc[b] += p
			col[b] = p
		}
		columns = append(columns, col)
		frames++
	}
	if frames == 0 {
		return nil, fmt.Errorf("no non-silent audio frames to analyse")
	}

	// Average power -> dB relative to the loudest bin.
	peak := 0.0
	for b := range acc {
		acc[b] /= float64(frames)
		if acc[b] > peak {
			peak = acc[b]
		}
	}
	if peak <= 0 {
		return nil, fmt.Errorf("silent spectrum")
	}
	db := make([]float64, bins)
	for b := range acc {
		db[b] = 10 * math.Log10(acc[b]/peak+1e-20)
	}

	res := &SpectralAnalysis{
		SampleRate:     sampleRate,
		NyquistHz:      sampleRate / 2,
		FramesAnalysed: frames,
		AvgSpectrumDb:  downsampleSpectrum(db, 512),
		SpecFloorDb:    specFloorDb,
		FreqBinHz:      float64(sampleRate) / 2 / float64(specRows),
		TimeStartSec:   start,
		TimeEndSec:     start + float64(frames*fftHop)/float64(sampleRate),
	}
	res.Columns = quantiseColumns(columns, specRows)
	deriveVerdict(res, db, sampleRate)
	return res, nil
}

// quantiseColumns turns per-frame power spectra into byte rows for transport.
// Each column is reduced to `rows` frequency bands by MAX-pooling — averaging
// would hide a narrow but real high-frequency component, which is exactly the
// signal a spectrogram exists to show.
func quantiseColumns(cols [][]float64, rows int) [][]byte {
	if len(cols) == 0 || rows <= 0 {
		return nil
	}
	// Reference level must come from the RAW per-frame values. Using the
	// averaged-spectrum peak here was a scale mismatch (raw frame power is much
	// larger than the frame-averaged power), which pushed every cell towards 255
	// and made the spectrogram a near-uniform bright block — the low-pass was
	// invisible. Caught by TestSpectrogramColumns.
	peak := 0.0
	for _, col := range cols {
		for _, v := range col {
			if v > peak {
				peak = v
			}
		}
	}
	if peak <= 0 {
		return nil
	}
	bins := len(cols[0])
	out := make([][]byte, len(cols))
	group := float64(bins) / float64(rows)
	for i, col := range cols {
		row := make([]byte, rows)
		for r := 0; r < rows; r++ {
			lo := int(float64(r) * group)
			hi := int(float64(r+1) * group)
			if hi > bins {
				hi = bins
			}
			m := 0.0
			for j := lo; j < hi; j++ {
				if col[j] > m {
					m = col[j]
				}
			}
			d := 10 * math.Log10(m/peak+1e-20)
			// Map [specFloorDb, 0] dB onto [0, 255].
			v := (d - specFloorDb) / (0 - specFloorDb)
			if v < 0 {
				v = 0
			} else if v > 1 {
				v = 1
			}
			row[r] = byte(v * 255)
		}
		out[i] = row
	}
	return out
}

// downsampleSpectrum reduces the spectrum to `target` points by max-pooling, so
// a narrow but real high-frequency component is not averaged into invisibility.
func downsampleSpectrum(db []float64, target int) []float64 {
	if len(db) <= target {
		out := make([]float64, len(db))
		copy(out, db)
		return out
	}
	out := make([]float64, target)
	group := float64(len(db)) / float64(target)
	for i := 0; i < target; i++ {
		lo := int(float64(i) * group)
		hi := int(float64(i+1) * group)
		if hi > len(db) {
			hi = len(db)
		}
		m := math.Inf(-1)
		for j := lo; j < hi; j++ {
			if db[j] > m {
				m = db[j]
			}
		}
		out[i] = m
	}
	return out
}

// Thresholds separating a codec's low-pass from ordinary programme roll-off.
// They are all ABSOLUTE frequencies and levels, deliberately NOT fractions of
// Nyquist.
//
// Judging against Nyquist is what made every hi-res file read as fake. A genuine
// 96 kHz master has no content above roughly 24 kHz — that is a property of
// microphones, mastering and human hearing, not of the encoding — so its
// cutoff/Nyquist ratio is around 0.5 however perfect the master is. The old rule
// ("steep shelf AND ratio < 0.90 => lossy") therefore condemned every 88.2/96/
// 192 kHz file on sight. Measured: a Spek-verified lossless 96 kHz FLAC scored
// ratio 0.29 with a 25.5 dB shelf and was labelled "Fake Lossless".
const (
	wallSearchLoHz = 8000.0  // no consumer encoder places a low-pass below this
	codecMaxWallHz = 21000.0 // ...nor passes content above this
	wallDropMinDb  = 20.0    // a codec wall is abrupt; programme material is not
	codecFloorDb   = -88.0   // a codec ZEROES the bins above its wall
	contentFloorDb = -70.0   // "content reaches this far", relative to peak
	losslessTopHz  = 20500.0 // content past here is out of reach of lossy codecs
)

// deriveVerdict measures where content actually ends, looks for a codec-shaped
// wall, and attaches confidence + evidence.
func deriveVerdict(res *SpectralAnalysis, db []float64, sampleRate int) {
	bins := len(db)
	binHz := float64(sampleRate) / 2 / float64(bins)
	nyquist := float64(sampleRate) / 2

	// Smooth over ~200 Hz to suppress bin-level noise without blurring a shelf.
	smoothN := int(200/binHz + 0.5)
	if smoothN < 1 {
		smoothN = 1
	}
	sm := movingAverage(db, smoothN)

	// ── 1. How far up the band does real content reach? ──────────────────────
	// This is the number a person reads off a Spek plot: where the picture goes
	// black. The previous definition — the first point 3 dB below the median of
	// the whole 2 kHz..mid band — is meaningless for music, because that median
	// is dominated by bass and mids sitting 20-30 dB above the treble. It
	// reported 11-14 kHz for *every* file measured, lossless and lossy alike.
	contentTop := 0.0
	for b := bins - 1; b >= 0; b-- {
		if sm[b] > contentFloorDb {
			contentTop = float64(b+1) * binHz
			break
		}
	}

	// ── 2. The steepest 1 kHz drop, and the level it lands in ────────────────
	// The landing level is the decisive measurement and the one that was missing:
	// a codec zeroes every bin above its wall, so the level collapses to the
	// numerical floor, whereas a mastering roll-off — however steep — leaves
	// noise behind. Measured: MP3 320 lands at -98 dB, MP3 128 at -91 dB, while
	// a lossless 96 kHz master's much steeper-looking roll-off lands at -85 dB.
	step := int(1000/binHz + 0.5)
	if step < 1 {
		step = 1
	}
	loBin := int(wallSearchLoHz/binHz + 0.5)
	wallBin, wallDrop, floorAbove := -1, 0.0, 0.0
	for b := loBin; b+step < bins; b++ {
		d := sm[b] - sm[b+step]
		if d <= wallDrop {
			continue
		}
		hi := b + 3*step
		if hi > bins {
			hi = bins
		}
		if hi <= b+step {
			continue
		}
		wallBin, wallDrop = b, d
		floorAbove = percentile(sm[b+step:hi], 0.5)
	}
	wallHz := 0.0
	if wallBin > 0 {
		wallHz = float64(wallBin+1) * binHz
	}

	res.CutoffHz = int(contentTop)
	res.CliffHz = int(wallHz)
	res.ShelfDropDb = wallDrop
	res.EnergyAboveCutoffDb = floorAbove

	if dbg := os.Getenv("ANTRA_SPECTRUM_DEBUG"); dbg != "" {
		fmt.Printf("[spectrum] contentTop=%.0f wallHz=%.0f drop=%.1f floorAbove=%.1f nyq=%.0f\n",
			contentTop, wallHz, wallDrop, floorAbove, nyquist)
	}

	// ── 3. Verdict ───────────────────────────────────────────────────────────
	// "lossy" requires all three independent signals to agree: the wall is
	// abrupt, it sits where a codec could have put it, and it lands in a true
	// digital floor. Requiring all three is what stops a steep but natural
	// mastering roll-off — the hi-res case — from being condemned.
	isCodecWall := wallDrop >= wallDropMinDb &&
		wallHz > 0 && wallHz <= codecMaxWallHz &&
		floorAbove <= codecFloorDb

	conf := 0.30
	switch {
	case isCodecWall:
		res.Verdict = "lossy"
		// Guess from where content actually ends, not from the steepest-drop bin:
		// the latter sits below the corner and biases every guess a tier low.
		res.LikelySource = guessEncoder(res.CutoffHz)
		switch {
		case wallDrop >= 35:
			conf = 0.95
		case wallDrop >= 27:
			conf = 0.85
		default:
			conf = 0.75
		}
	case contentTop >= losslessTopHz:
		res.Verdict = "lossless"
		if contentTop >= 21500 {
			conf = 0.90
		} else {
			conf = 0.75
		}
	default:
		res.Verdict = "inconclusive"
	}

	if res.FramesAnalysed < 60 {
		conf *= 0.6
		res.Evidence = append(res.Evidence,
			fmt.Sprintf("Only %d analysis frames were available — confidence reduced.",
				res.FramesAnalysed))
	}
	res.CutoffConfidence = conf

	res.Evidence = append(res.Evidence,
		fmt.Sprintf("Content reaches %.1f kHz (Nyquist for this file is %.1f kHz).",
			contentTop/1000, nyquist/1000))
	if wallHz > 0 {
		res.Evidence = append(res.Evidence,
			fmt.Sprintf("Steepest roll-off is %.1f dB per kHz at %.1f kHz, settling at %.0f dB.",
				wallDrop, wallHz/1000, floorAbove))
	}
	res.Evidence = append(res.Evidence,
		fmt.Sprintf("Averaged over %d FFT frames at %d-point resolution (%.0f Hz/bin).",
			res.FramesAnalysed, fftSize, binHz))
	if nyquist > 25000 {
		res.Evidence = append(res.Evidence,
			"At this sample rate the band above ~24 kHz is expected to be empty — that "+
				"reflects the recording, not the encoding, and is not evidence of a transcode.")
	}

	switch res.Verdict {
	case "lossless":
		res.Evidence = append(res.Evidence,
			"Content extends past 20.5 kHz, which is beyond the reach of any consumer "+
				"lossy encoder — consistent with a true lossless master.")
	case "lossy":
		res.Evidence = append(res.Evidence,
			"An abrupt wall this low in the band, falling into a near-silent floor, is "+
				"characteristic of a lossy encoder rather than programme material.")
	default:
		res.Evidence = append(res.Evidence,
			"The roll-off is not sharp enough, or does not fall far enough, to "+
				"distinguish a lossy encoder from programme material with little "+
				"high-frequency content.")
	}
}

// guessEncoder maps a cutoff to the encoder settings that typically produce it.
// Ranges overlap in reality, so the result is always phrased as approximate.
func guessEncoder(cutoffHz int) string {
	switch {
	case cutoffHz < 11500:
		return "≈64 kbps or lower"
	case cutoffHz < 15000:
		return "MP3 ≈128 kbps"
	case cutoffHz < 16500:
		return "MP3 ≈160 kbps / AAC ≈96 kbps"
	case cutoffHz < 18000:
		return "MP3 ≈192 kbps / AAC ≈128 kbps"
	case cutoffHz < 19500:
		return "AAC ≈256 kbps"
	case cutoffHz < 20500:
		return "MP3 ≈320 kbps"
	default:
		return "high-bitrate lossy"
	}
}

func movingAverage(x []float64, n int) []float64 {
	if n <= 1 {
		out := make([]float64, len(x))
		copy(out, x)
		return out
	}
	out := make([]float64, len(x))
	half := n / 2
	for i := range x {
		lo, hi := i-half, i+half
		if lo < 0 {
			lo = 0
		}
		if hi >= len(x) {
			hi = len(x) - 1
		}
		s := 0.0
		for j := lo; j <= hi; j++ {
			s += x[j]
		}
		out[i] = s / float64(hi-lo+1)
	}
	return out
}

func percentile(x []float64, p float64) float64 {
	if len(x) == 0 {
		return 0
	}
	c := make([]float64, len(x))
	copy(c, x)
	sort.Float64s(c)
	i := int(p * float64(len(c)-1))
	if i < 0 {
		i = 0
	}
	if i >= len(c) {
		i = len(c) - 1
	}
	return c[i]
}
