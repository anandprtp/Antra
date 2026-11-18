package main

import (
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// TestFFTCorrectness checks the FFT itself against a known signal before any
// audio is involved — if this is wrong, every downstream verdict is wrong.
func TestFFTCorrectness(t *testing.T) {
	const n = 1024
	const sr = 48000.0
	const tone = 3000.0
	re := make([]float64, n)
	im := make([]float64, n)
	for i := 0; i < n; i++ {
		re[i] = math.Sin(2 * math.Pi * tone * float64(i) / sr)
	}
	fftInPlace(re, im)

	peakBin, peakMag := 0, 0.0
	for b := 1; b < n/2; b++ {
		m := re[b]*re[b] + im[b]*im[b]
		if m > peakMag {
			peakMag, peakBin = m, b
		}
	}
	gotHz := float64(peakBin) * sr / float64(n)
	if math.Abs(gotHz-tone) > sr/float64(n) {
		t.Fatalf("FFT peak at %.1f Hz, expected %.1f Hz", gotHz, tone)
	}
}

func ffmpegPath() string {
	if p, err := exec.LookPath("ffmpeg"); err == nil {
		return p
	}
	return ""
}

// genTone writes a WAV containing a sweep low-passed at the given frequency, so
// the analyser has a KNOWN cutoff to find.
func genLowPassed(t *testing.T, dir, name string, cutoff int) string {
	ff := ffmpegPath()
	if ff == "" {
		t.Skip("ffmpeg not on PATH")
	}
	out := filepath.Join(dir, name)
	args := []string{
		"-y", "-v", "error",
		"-f", "lavfi", "-i", "anoisesrc=d=12:c=white:r=48000",
	}
	if cutoff > 0 {
		// ONE 2-pole stage: Butterworth is -3 dB exactly at the corner, so the
		// fixture's true cutoff equals `cutoff`. Cascading stages would move the
		// -3 dB point well below the nominal corner and make the expectation wrong.
		args = append(args, "-af", "lowpass=f="+itoa(cutoff)+":poles=2")
	}
	args = append(args, "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", out)
	cmd := exec.Command(ff, args...)
	if b, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("ffmpeg fixture failed: %v\n%s", err, b)
	}
	return out
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var b []byte
	for i > 0 {
		b = append([]byte{byte('0' + i%10)}, b...)
		i /= 10
	}
	if neg {
		return "-" + string(b)
	}
	return string(b)
}

// encodeMP3 writes a real LAME encode with the low-pass pinned, so the fixture's
// ground truth is exact rather than inferred from the bitrate default.
func encodeMP3(t *testing.T, dir, name string, bitrate, cutoff int) string {
	t.Helper()
	ff := ffmpegPath()
	src := genLowPassed(t, dir, "src_"+name+".wav", 0)
	out := filepath.Join(dir, name)
	args := []string{"-y", "-v", "error", "-i", src,
		"-c:a", "libmp3lame", "-b:a", itoa(bitrate) + "k", "-cutoff", itoa(cutoff), out}
	if b, err := exec.Command(ff, args...).CombinedOutput(); err != nil {
		t.Fatalf("%s encode failed: %v\n%s", name, err, b)
	}
	return out
}

// TestCutoffDetection: given audio with a KNOWN brick wall, does the analyser
// land on it?
//
// This used to use synthetic `lowpass` fixtures and assert the filter's corner
// frequency. Those fixtures are not valid ground truth for what CutoffHz now
// means. CutoffHz is "the highest frequency still carrying content", which is
// what a person reads off a Spek plot — and a single 2-pole low-pass is only
// -12 dB/octave, so a 15 kHz fixture genuinely still has energy at 24 kHz, far
// above the -70 dB content floor. The analyser reporting 24 kHz there was
// correct and the expectation was wrong; that is the third time a synthetic
// filter has produced a bad expectation in this file. Real encoders brick-wall,
// so they are the only fixtures with a defensible answer.
func TestCutoffDetection(t *testing.T) {
	if ffmpegPath() == "" {
		t.Skip("ffmpeg not on PATH")
	}
	dir := t.TempDir()

	for _, tc := range []struct {
		name    string
		bitrate int
		cutoff  int
		tolHz   float64
	}{
		{"lp15k.mp3", 128, 15000, 1800},
		{"lp19k.mp3", 320, 19000, 1800},
	} {
		path := encodeMP3(t, dir, tc.name, tc.bitrate, tc.cutoff)
		res, err := analyseSpectrum(path, ffmpegPath(), 48000, 12)
		if err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		diff := math.Abs(float64(res.CutoffHz) - float64(tc.cutoff))
		if diff > tc.tolHz {
			t.Errorf("%s: cutoff %d Hz, wanted ~%d Hz (diff %.0f)",
				tc.name, res.CutoffHz, tc.cutoff, diff)
		} else {
			t.Logf("%s: cutoff %d Hz (wanted ~%d), wall %.1f dB, verdict=%s conf=%.2f",
				tc.name, res.CutoffHz, tc.cutoff, res.ShelfDropDb, res.Verdict, res.CutoffConfidence)
		}
		if len(res.Evidence) == 0 {
			t.Errorf("%s: no evidence recorded", tc.name)
		}
	}
}

// TestHiResNotCalledLossy is the regression guard for the reported bug: a
// Spek-verified lossless 96 kHz FLAC was branded "Fake Lossless".
//
// The cause was judging the cutoff as a FRACTION OF NYQUIST. A genuine hi-res
// master has no content above ~24 kHz — that is the recording chain and human
// hearing, not the encoding — so at 96 kHz its ratio is ~0.5 however perfect it
// is, and the old rule ("steep shelf AND ratio < 0.90 => lossy") condemned every
// 88.2/96/192 kHz file on sight. Both fixtures below have a steep wall that a
// ratio test would damn; neither may be called lossy, because the wall sits
// above any frequency a consumer lossy encoder can reach.
func TestHiResNotCalledLossy(t *testing.T) {
	ff := ffmpegPath()
	if ff == "" {
		t.Skip("ffmpeg not on PATH")
	}
	dir := t.TempDir()

	// (a) The classic "hi-res" shape: 48 kHz full-band content resampled up to
	//     96 kHz, so the resampler's anti-alias filter leaves a very steep wall
	//     at 24 kHz landing in a deep floor.
	src48 := genLowPassed(t, dir, "hires_src.wav", 0)
	up96 := filepath.Join(dir, "hires_upsampled.wav")
	if b, err := exec.Command(ff, "-y", "-v", "error", "-i", src48,
		"-ar", "96000", "-c:a", "pcm_s16le", up96).CombinedOutput(); err != nil {
		t.Fatalf("resample failed: %v\n%s", err, b)
	}

	// (b) A 96 kHz master band-limited around 22 kHz, as real recordings are.
	lp96 := filepath.Join(dir, "hires_lp22k.wav")
	if b, err := exec.Command(ff, "-y", "-v", "error",
		"-f", "lavfi", "-i", "anoisesrc=d=12:c=white:r=96000",
		"-af", "lowpass=f=22000:poles=2,lowpass=f=22000:poles=2",
		"-ac", "1", "-ar", "96000", "-c:a", "pcm_s16le", lp96).CombinedOutput(); err != nil {
		t.Fatalf("hi-res fixture failed: %v\n%s", err, b)
	}

	for _, path := range []string{up96, lp96} {
		res, err := analyseSpectrum(path, ff, 96000, 12)
		if err != nil {
			t.Fatalf("%s: %v", filepath.Base(path), err)
		}
		if res.Verdict == "lossy" {
			t.Errorf("%s: hi-res lossless wrongly judged LOSSY (content to %d Hz, wall %.1f dB at %d Hz, floor %.1f dB)",
				filepath.Base(path), res.CutoffHz, res.ShelfDropDb, res.CliffHz, res.EnergyAboveCutoffDb)
		}
		t.Logf("%s: content to %d Hz, wall %.1f dB at %d Hz -> %s (conf %.2f)",
			filepath.Base(path), res.CutoffHz, res.ShelfDropDb, res.CliffHz, res.Verdict, res.CutoffConfidence)
	}
}

// TestGentleRollOffNotCalledLossy: programme material with little treble rolls
// off gradually and never lands in a digital floor. It must not be branded
// lossy no matter how far down the content ends up.
func TestGentleRollOffNotCalledLossy(t *testing.T) {
	ff := ffmpegPath()
	if ff == "" {
		t.Skip("ffmpeg not on PATH")
	}
	dir := t.TempDir()
	path := genLowPassed(t, dir, "gentle15k.wav", 15000)
	res, err := analyseSpectrum(path, ff, 48000, 12)
	if err != nil {
		t.Fatal(err)
	}
	if res.Verdict == "lossy" {
		t.Errorf("gentle 2-pole roll-off wrongly judged lossy (wall %.1f dB at %d Hz, floor %.1f dB)",
			res.ShelfDropDb, res.CliffHz, res.EnergyAboveCutoffDb)
	}
	t.Logf("gentle roll-off: content to %d Hz, wall %.1f dB, floor %.1f dB -> %s",
		res.CutoffHz, res.ShelfDropDb, res.EnergyAboveCutoffDb, res.Verdict)
}

// TestFullBandNotCalledLossy is the regression guard that matters most: real
// full-band audio must NEVER be branded lossy. Wrongly calling a genuine master
// "Fake Lossless" is the worse error and was the reported complaint.
func TestFullBandNotCalledLossy(t *testing.T) {
	ff := ffmpegPath()
	if ff == "" {
		t.Skip("ffmpeg not on PATH")
	}
	dir := t.TempDir()
	path := genLowPassed(t, dir, "full.wav", 0) // no low-pass at all
	res, err := analyseSpectrum(path, ff, 48000, 12)
	if err != nil {
		t.Fatal(err)
	}
	if res.Verdict == "lossy" {
		t.Errorf("full-band audio wrongly judged lossy (cutoff %d Hz, shelf %.1f dB)",
			res.CutoffHz, res.ShelfDropDb)
	}
	t.Logf("full-band: cutoff %d Hz, verdict=%s, conf=%.2f", res.CutoffHz, res.Verdict, res.CutoffConfidence)
}

// TestResolutionBeatsOldProbe: the old probe could only report one of
// {8k,12k,16k,17k,19k,21k}. Verify we can land on a value it could never produce.
func TestResolutionBeatsOldProbe(t *testing.T) {
	ff := ffmpegPath()
	if ff == "" {
		t.Skip("ffmpeg not on PATH")
	}
	dir := t.TempDir()
	// Real encoder, not a synthetic filter — see the note on TestCutoffDetection.
	path := encodeMP3(t, dir, "lp14k.mp3", 128, 14000)
	res, err := analyseSpectrum(path, ff, 48000, 12)
	if err != nil {
		t.Fatal(err)
	}
	old := map[int]bool{0: true, 8000: true, 12000: true, 16000: true, 17000: true, 19000: true, 21000: true}
	if old[res.CutoffHz] {
		t.Errorf("cutoff %d Hz is a legacy-probe value — resolution not actually improved", res.CutoffHz)
	}
	if math.Abs(float64(res.CutoffHz)-14000) > 2000 {
		t.Errorf("cutoff %d Hz not near the true 14000 Hz", res.CutoffHz)
	}
	t.Logf("14 kHz fixture -> %d Hz (old probe could only say 12000 or 16000)", res.CutoffHz)
}

func TestMain(m *testing.M) {
	// proc_tree_kill_test.go re-enters this binary as a fake process tree.
	helperProcessDispatch()
	os.Exit(m.Run())
}

// TestRealEncoders is the test that actually matters: encode with real lossy
// encoders and confirm the analyser both finds a plausible cutoff AND calls it
// lossy. Synthetic filters have gentle slopes; real encoders brick-wall, which
// is precisely the signal this feature is meant to detect.
func TestRealEncoders(t *testing.T) {
	ff := ffmpegPath()
	if ff == "" {
		t.Skip("ffmpeg not on PATH")
	}
	dir := t.TempDir()
	src := genLowPassed(t, dir, "src.wav", 0) // full-band white noise

	for _, tc := range []struct {
		name   string
		args   []string
		wantLo int
		wantHi int
	}{
		// -cutoff pins LAME's low-pass so the ground truth is known exactly.
		// Relying on the bitrate default was wrong: LAME did NOT low-pass white
		// noise at 128k where expected, and the analyser was correctly reporting
		// the ~20.6 kHz content that was really there.
		{"mp3_16k.mp3", []string{"-c:a", "libmp3lame", "-b:a", "128k", "-cutoff", "16000"}, 14500, 17500},
		{"mp3_20k.mp3", []string{"-c:a", "libmp3lame", "-b:a", "320k", "-cutoff", "20000"}, 18500, 21500},
	} {
		out := filepath.Join(dir, tc.name)
		a := append([]string{"-y", "-v", "error", "-i", src}, tc.args...)
		a = append(a, out)
		if b, err := exec.Command(ff, a...).CombinedOutput(); err != nil {
			t.Fatalf("%s encode failed: %v\n%s", tc.name, err, b)
		}
		res, err := analyseSpectrum(out, ff, 48000, 12)
		if err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		t.Logf("%s -> cutoff %d Hz, shelf %.1f dB, verdict=%s conf=%.2f likely=%q",
			tc.name, res.CutoffHz, res.ShelfDropDb, res.Verdict, res.CutoffConfidence, res.LikelySource)
		if res.CutoffHz < tc.wantLo || res.CutoffHz > tc.wantHi {
			t.Errorf("%s: cutoff %d Hz outside expected %d–%d Hz",
				tc.name, res.CutoffHz, tc.wantLo, tc.wantHi)
		}
		if res.Verdict != "lossy" {
			t.Errorf("%s: verdict %q, expected \"lossy\" for a real lossy encode",
				tc.name, res.Verdict)
		}
	}
}

// TestSpectrogramColumns validates the interactive-spectrogram payload (FEAT-5
// UI). A UI that draws wrong data is worse than no UI, so the matrix is checked
// against a known low-passed fixture: rows above the cutoff must be dark.
func TestSpectrogramColumns(t *testing.T) {
	ff := ffmpegPath()
	if ff == "" {
		t.Skip("ffmpeg not on PATH")
	}
	dir := t.TempDir()
	// Use a REAL encoder, not a synthetic filter. A single 2-pole lowpass is only
	// -12 dB/octave, so at Nyquist it is ~24 dB down — genuinely still visible,
	// and asserting a large contrast against it would be testing the wrong
	// physics (the same mistake the LAME -cutoff fixture corrected earlier).
	// Real encoders brick-wall, which is what the spectrogram must show.
	src := genLowPassed(t, dir, "spec_src.wav", 0)
	path := filepath.Join(dir, "spec_mp3_12k.mp3")
	if b, err := exec.Command(ff, "-y", "-v", "error", "-i", src,
		"-c:a", "libmp3lame", "-b:a", "128k", "-cutoff", "12000", path).CombinedOutput(); err != nil {
		t.Fatalf("encode failed: %v - %s", err, b)
	}
	res, err := analyseSpectrum(path, ff, 48000, 12)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Columns) == 0 {
		t.Fatal("no spectrogram columns produced")
	}
	rows := len(res.Columns[0])
	if rows != specRowCount {
		t.Errorf("got %d frequency rows, want %d", rows, specRowCount)
	}
	if res.FreqBinHz <= 0 {
		t.Errorf("FreqBinHz not set (%v)", res.FreqBinHz)
	}
	if res.TimeEndSec <= res.TimeStartSec {
		t.Errorf("bad time window: %v..%v", res.TimeStartSec, res.TimeEndSec)
	}

	// Average brightness below vs above the 12 kHz cutoff. Nyquist is 24 kHz, so
	// the cutoff sits at row rows/2.
	half := rows / 2
	var lo, hi, nlo, nhi float64
	for _, col := range res.Columns {
		for r, v := range col {
			if r < half-10 {
				lo += float64(v)
				nlo++
			} else if r > half+10 {
				hi += float64(v)
				nhi++
			}
		}
	}
	loAvg, hiAvg := lo/nlo, hi/nhi
	t.Logf("mean brightness below cutoff=%.1f, above=%.1f (0-255)", loAvg, hiAvg)
	if !(loAvg > hiAvg*2) {
		t.Errorf("spectrogram does not show the low-pass: below=%.1f above=%.1f", loAvg, hiAvg)
	}

	// Payload must stay small — the whole point of quantising to bytes.
	total := len(res.Columns) * rows
	t.Logf("payload: %d columns x %d rows = %d bytes (~%.1f KB)", len(res.Columns), rows, total, float64(total)/1024)
	if total > 1<<21 { // 2 MB
		t.Errorf("spectrogram payload too large: %d bytes", total)
	}
}
