package main

/* ── Artwork theme (v2.0.0 THEME-3) ───────────────────────────────────────────

   Derives the whole palette from the cover art of whatever is on screen, in the
   Material You sense: the ENTIRE surface is tinted by the cover, not just the
   buttons.

   ─── WHY THIS RUNS IN GO ──────────────────────────────────────────────────

   Antra-Web does this in the browser with a canvas. The port question was
   whether getImageData taints in a Wails webview, where the page origin is a
   custom scheme rather than https. Measured rather than assumed: every artwork
   CDN the app actually uses answers `Access-Control-Allow-Origin: *` and
   `Content-Type: image/jpeg` (i.scdn.co, is1-ssl.mzstatic.com,
   resources.tidal.com, cdns-images.dzcdn.net), so a JS port with
   crossorigin="anonymous" would probably have worked.

   It lives here anyway, because Go has no origin concept at all, so the whole
   question stops existing -- and because the acceptance criteria demand
   contrast MEASURED across real covers in both modes, which `go test` can do
   deterministically and a non-compositing webview cannot. Local library covers
   (the Downloaded Music player) are served over the app's own media server, so
   one http.Get covers remote CDNs and local files alike.

   ─── WHY THIS DRIVES SEEDS, NOT A COLOUR ──────────────────────────────────

   style.css is three tiers: a MODE ramp that owns lightness, THEME seeds that
   own hue and chroma, and ~30 semantic tokens derived from both. So this
   extracts a HUE and a CHROMA and writes them into the seed slots -- the same
   ~9 values every static theme declares. Two things fall out for free:

     - Contrast is preserved by construction. Lightness never comes from the
       artwork; it stays on the mode ramp, and OKLCH is perceptually uniform, so
       swapping hue at fixed L cannot move a contrast ratio. A washed-out pale
       cover yields a pale-HUED theme that is still readable, where a raw
       extracted hex would be near-white text on near-white chrome.
     - THEME-2's mode caps do light mode for us. --a1-l-cap / --a-cf already
       clamp any accent for the light ramp, so unlike the web module -- which
       has to hand-maintain a second pair of light lightness constants -- this
       one emits a single seed that both modes consume. That is the direct
       payoff of keeping lightness on the ramp.

   ─── WHY OKLCH AND NOT HSL ────────────────────────────────────────────────

   The seeds are OKLCH hues, and an HSL hue is NOT the same number. Extracting
   in HSL and writing the result into --a1-h would be silently wrong, so pixels
   go sRGB -> linear -> OKLab -> OKLCH and the hue that comes out is in the
   space it is going in to.

   ─── WHY A HISTOGRAM AND NOT THE TOP PIXEL ────────────────────────────────

   Sorting candidates by saturation and taking the first — the obvious
   approach — lets one logo pixel or one JPEG ringing artifact decide the
   whole theme. Here pixels are bucketed by hue and weighted by chroma, so a
   colour has to actually be PRESENT to win. TestHistogramBeatsHotPixels pins
   that 3 magenta pixels do not outvote 400 blue ones.
   ────────────────────────────────────────────────────────────────────────── */

import (
	"encoding/json"
	"fmt"
	"image"
	_ "image/gif"
	_ "image/jpeg"
	_ "image/png"
	"io"
	"math"
	"net/http"
	"strings"
	"time"

	_ "golang.org/x/image/webp"
)

/* ── Constants ─────────────────────────────────────────────────────────────
   Every value below is MEASURED from this app's own twelve seed blocks in
   style.css or from the sRGB gamut, never inherited from the web module --
   the desktop seeds are a different population (a1-l 0.599-0.874 here against
   the web 0.64-0.72). See the v2.0.0 changelog for the tables. */

const (
	// Accent lightness. Desktop's twelve static themes sit at a1-l 0.599-0.874
	// (median 0.712) and a2-l 0.659-0.921 (median 0.772), with a median gap of
	// 0.075. Measured over all hues, a1-l 0.72 also sits near the peak of
	// in-gamut chroma headroom (min 0.122, median 0.163) while giving >=6.88:1
	// accent contrast in dark; light is capped at 0.509 by the ramp regardless.
	artA1L = 0.72
	artA2L = 0.79

	// Chroma band, from the same twelve themes (a1-c 0.015-0.244, median 0.137;
	// a2-c 0.012-0.212, median 0.121). Clamping into a band is what stops a neon
	// cover producing a theme that vibrates. Every value is ALSO clamped into
	// the sRGB gamut per hue -- see artClampChroma; at a1-l 0.72 the ceiling
	// runs 0.121 (cyan, h~195) to 0.300 (magenta, h~330), so a flat band would
	// clip channels and distort the hue on most hues.
	artA1CMin = 0.13
	artA1CMax = 0.21
	artA2CMin = 0.10
	artA2CMax = 0.16
	// Measured a2-c/a1-c across the twelve static themes: median 0.80.
	artA2CRatio = 0.80

	// Neutral chroma factor -- how far the near-black (or near-white) chrome is
	// tinted toward the cover. THIS is the row that makes it read as Material
	// You rather than "the accent colour changed". The static themes run
	// 0.000-1.517 (median 1.177). Bounded above by gamut: light --bg-color is
	// oklch(0.974, 0.009*n-cf, h) and L=0.974 admits at most 0.0120 chroma at
	// its worst hue, so n-cf <= 1.33.
	artNCF = 1.15
	// The near-white tier is clipped separately for the same reason the six
	// tinted static themes clip theirs: dark --text-primary is
	// oklch(0.983, 0.019*n-cf-hi, h) and L=0.983 admits at most 0.0078 chroma,
	// so n-cf-hi <= 0.41 for a theme whose hue is not known in advance.
	artNCFHi = 0.40

	// The accent PAIR. The web module rotates by -30 when the cover has no
	// usable second hue. Recorded as a deliberate departure: desktop's own
	// twelve themes separate a1 and a2 by only 0-8.6 degrees, so this reads as a
	// slightly wider gradient than the static set. That is intended -- a
	// dynamic theme should look like its cover, and the M3 tertiary palette is a
	// ~60 degree rotation, so -30 is already the conservative half of it.
	artSpreadMin     = 15.0
	artSpreadMax     = 80.0
	artDefaultSpread = -30.0

	artBuckets = 24 // 15 degrees each
	artMaxDim  = 96 // ~9k samples; plenty for a hue histogram

	artFetchTimeout = 12 * time.Second
	artMaxBytes     = 12 << 20 // 12 MiB
)

// The chroma range real album art actually occupies. MEASURED with THIS
// extractor over the twelve covers in realCovers -- not guessed, and not
// carried over from the web module, whose 0.05-0.15 comes from a canvas
// downscale that area-averages pixels and therefore reports different numbers
// than this sampler does.
//
// Observed dominant-bucket chroma: 0.0160 (Rumours, washed beige) .. 0.1044
// (Adele 21, heavy sepia), median ~0.064. The first cut of this file guessed
// 0.045-0.150 and TestRealCoverChromaBand failed it, which is the same trap the
// web module records: with the band set too high the whole real population
// lands in its lower half and a muted cover stops being distinguishable from a
// saturated one. The band below is the observed run opened slightly either
// side, so the twelve covers spread across --a1-c 0.131 .. 0.205.
var artCLo, artCHi = 0.015, 0.110

// Mode ramp values mirrored from style.css. KEEP IN STEP -- the test walks
// these, so a drift there fails the suite rather than shipping a pre-paint
// colour that disagrees with the painted one.
type artRamp struct {
	name                               string
	lBg0, cBg0, lBg1, cBg1             float64
	lFg0, cFg0, lFg1, cFg1             float64
	lFg2, cFg2, lFg3, cFg3, lFg4, cFg4 float64
	a1Cap, a2Cap, aCF                  float64
}

var artDarkRamp = artRamp{
	name: "dark",
	lBg0: 0.179, cBg0: 0.018, lBg1: 0.239, cBg1: 0.026,
	lFg0: 0.983, cFg0: 0.019, lFg1: 0.842, cFg1: 0.049,
	lFg2: 0.797, cFg2: 0.046, lFg3: 0.598, cFg3: 0.034,
	lFg4: 0.497, cFg4: 0.030,
	a1Cap: 1, a2Cap: 1, aCF: 1,
}

var artLightRamp = artRamp{
	name: "light",
	lBg0: 0.974, cBg0: 0.009, lBg1: 0.930, cBg1: 0.016,
	lFg0: 0.265, cFg0: 0.018, lFg1: 0.440, cFg1: 0.024,
	lFg2: 0.470, cFg2: 0.026, lFg3: 0.542, cFg3: 0.021,
	lFg4: 0.643, cFg4: 0.017,
	a1Cap: 0.509, a2Cap: 0.611, aCF: 0.66,
}

// ArtworkSeed is exactly the set of seed slots a static theme declares, plus
// the two pre-paint backgrounds and a diagnostic. The frontend writes these
// onto documentElement.style; every derived token follows with no extra wiring.
type ArtworkSeed struct {
	H           float64 `json:"h"`             // --n-h   (Material You: the neutral)
	AH          float64 `json:"ah"`            // --a1-h  (primary; EQUALS H for artwork, not for custom)
	H2          float64 `json:"h2"`            // --a2-h  (tertiary)
	C1          float64 `json:"c1"`            // --a1-c
	C2          float64 `json:"c2"`            // --a2-c
	L1          float64 `json:"l1"`            // --a1-l
	L2          float64 `json:"l2"`            // --a2-l
	NCF         float64 `json:"n_cf"`          // --n-cf
	NCFHi       float64 `json:"n_cf_hi"`       // --n-cf-hi
	Swatch      string  `json:"swatch"`        // picker chip, dark-mode accent
	BgDark      string  `json:"bg_dark"`       // pre-paint ground, dark
	BgLight     string  `json:"bg_light"`      // pre-paint ground, light
	BgElevDark  string  `json:"bg_elev_dark"`  // --bg-elevated, dark  (picker preview only)
	BgElevLight string  `json:"bg_elev_light"` // --bg-elevated, light (picker preview only)
	ArtC        float64 `json:"art_c"`         // raw dominant-bucket chroma (diagnostic)
}

/* ── Colour conversion ───────────────────────────────────────────────────── */

func artSrgbToLinear(c float64) float64 {
	c /= 255
	if c <= 0.04045 {
		return c / 12.92
	}
	return math.Pow((c+0.055)/1.055, 2.4)
}

func artLinearToSrgb01(c float64) float64 {
	if c <= 0.0031308 {
		return c * 12.92
	}
	return 1.055*math.Pow(c, 1/2.4) - 0.055
}

func artRgbToOklch(r8, g8, b8 float64) (L, C, H float64) {
	r, g, b := artSrgbToLinear(r8), artSrgbToLinear(g8), artSrgbToLinear(b8)
	l := math.Cbrt(0.4122214708*r + 0.5363325363*g + 0.0514459929*b)
	m := math.Cbrt(0.2119034982*r + 0.6806995451*g + 0.1073969566*b)
	s := math.Cbrt(0.0883024619*r + 0.2817188376*g + 0.6299787005*b)
	L = 0.2104542553*l + 0.7936177850*m - 0.0040720468*s
	A := 1.9779984951*l - 2.4285922050*m + 0.4505937099*s
	B := 0.0259040371*l + 0.7827717662*m - 0.8086757660*s
	C = math.Hypot(A, B)
	H = math.Atan2(B, A) * 180 / math.Pi
	if H < 0 {
		H += 360
	}
	return
}

// artOklchToLinearRGB returns UNCLAMPED linear RGB, so callers can test gamut.
func artOklchToLinearRGB(L, C, H float64) (float64, float64, float64) {
	hr := H * math.Pi / 180
	A, B := C*math.Cos(hr), C*math.Sin(hr)
	lp := L + 0.3963377774*A + 0.2158037573*B
	mp := L - 0.1055613458*A - 0.0638541728*B
	sp := L - 0.0894841775*A - 1.2914855480*B
	l, m, s := lp*lp*lp, mp*mp*mp, sp*sp*sp
	r := 4.0767416621*l - 3.3077115913*m + 0.2309699292*s
	g := -1.2684380046*l + 2.6097574011*m - 0.3413193965*s
	b := -0.0041960863*l - 0.7034186147*m + 1.7076147010*s
	return r, g, b
}

func artInGamut(L, C, H float64) bool {
	const eps = 1e-4
	r, g, b := artOklchToLinearRGB(L, C, H)
	for _, v := range [3]float64{r, g, b} {
		if v < -eps || v > 1+eps {
			return false
		}
	}
	return true
}

func artOklchToRGB255(L, C, H float64) (int, int, int) {
	r, g, b := artOklchToLinearRGB(L, C, H)
	conv := func(v float64) int {
		v = math.Max(0, math.Min(1, v))
		n := int(math.Round(artLinearToSrgb01(v) * 255))
		if n < 0 {
			n = 0
		}
		if n > 255 {
			n = 255
		}
		return n
	}
	return conv(r), conv(g), conv(b)
}

func artOklchToHex(L, C, H float64) string {
	r, g, b := artOklchToRGB255(L, C, H)
	return fmt.Sprintf("#%02x%02x%02x", r, g, b)
}

// artMaxChroma binary-searches the largest in-gamut chroma at (L, H).
func artMaxChroma(L, H float64) float64 {
	lo, hi := 0.0, 0.45
	if !artInGamut(L, lo, H) {
		return 0
	}
	for i := 0; i < 24; i++ {
		mid := (lo + hi) / 2
		if artInGamut(L, mid, H) {
			lo = mid
		} else {
			hi = mid
		}
	}
	return lo
}

// artClampChroma clamps c so the accent is in gamut in BOTH modes. Dark binds
// at every hue measured, but light is checked rather than assumed.
func artClampChroma(c, lDark, lCap, aCF, h float64) float64 {
	if c < 0 {
		c = 0
	}
	if m := artMaxChroma(lDark, h); c > m {
		c = m
	}
	lLight := math.Min(lDark, lCap)
	if aCF > 0 {
		if m := artMaxChroma(lLight, h) / aCF; c > m {
			c = m
		}
	}
	return c
}

/* ── Extraction ──────────────────────────────────────────────────────────── */

type artPixel struct{ r, g, b float64 }

type artAnalysis struct {
	H  float64
	C  float64
	H2 float64
	Ok bool
	// HasH2 distinguishes "the cover has a genuine second hue" from "we had to
	// rotate", so artToSeed does not have to encode a sentinel in a float.
	HasH2 bool
}

type artHist struct {
	w, cnt, sx, sy [artBuckets]float64
	n              int
}

func artHistogram(px []artPixel, minC, minL, maxL float64) *artHist {
	h := &artHist{}
	for _, p := range px {
		L, C, H := artRgbToOklch(p.r, p.g, p.b)
		if C < minC || L < minL || L > maxL {
			continue
		}
		b := int(H / 360 * artBuckets)
		if b >= artBuckets {
			b = artBuckets - 1
		}
		if b < 0 {
			b = 0
		}
		rad := H * math.Pi / 180
		// Weighted by chroma, so a large flat wall of near-grey cannot outvote a
		// smaller genuinely colourful region.
		h.w[b] += C
		h.cnt[b]++
		h.sx[b] += math.Cos(rad) * C
		h.sy[b] += math.Sin(rad) * C
		h.n++
	}
	if h.n == 0 {
		return nil
	}
	return h
}

func (h *artHist) hue(i int) float64 {
	a := math.Atan2(h.sy[i], h.sx[i]) * 180 / math.Pi
	if a < 0 {
		a += 360
	}
	return a
}

func artArc(a, b float64) float64 {
	d := math.Mod(math.Abs(a-b), 360)
	if d > 180 {
		d = 360 - d
	}
	return d
}

func artAnalyse(px []artPixel) artAnalysis {
	h := artHistogram(px, 0.04, 0.25, 0.90)
	// Relaxed pass for covers that are almost entirely dark, blown out, or
	// nearly monochrome. Failing outright would leave the user staring at the
	// previous theme with no explanation.
	if h == nil {
		h = artHistogram(px, 0.015, 0.12, 0.96)
	}
	if h == nil {
		return artAnalysis{}
	}
	best := 0
	for i := 1; i < artBuckets; i++ {
		if h.w[i] > h.w[best] {
			best = i
		}
	}
	if h.w[best] <= 0 {
		return artAnalysis{}
	}
	hue := h.hue(best)
	// Mean chroma of the winning bucket -- how vivid that hue actually is,
	// independent of how much of the image it covers.
	chroma := h.w[best] / math.Max(1, h.cnt[best])

	second := -1
	for i := 0; i < artBuckets; i++ {
		if i == best || h.w[i] <= 0 {
			continue
		}
		d := artArc(h.hue(i), hue)
		if d < artSpreadMin || d > artSpreadMax {
			continue
		}
		if second < 0 || h.w[i] > h.w[second] {
			second = i
		}
	}
	out := artAnalysis{H: hue, C: chroma, Ok: true}
	if second >= 0 {
		out.H2, out.HasH2 = h.hue(second), true
	}
	return out
}

func artRound(v float64, places int) float64 {
	p := math.Pow(10, float64(places))
	return math.Round(v*p) / p
}

// artFloorTo truncates rather than rounds. Chroma is emitted at 4 decimal
// places, and rounding a gamut-clamped value UP puts it back outside sRGB --
// which is how the first cut of this file failed its own all-hue sweep on ~90
// of 360 hues. Truncating can only ever move a value further inside.
func artFloorTo(v float64, places int) float64 {
	p := math.Pow(10, float64(places))
	return math.Floor(v*p) / p
}

// artToSeed turns an analysis into the seed values style.css expects.
func artToSeed(a artAnalysis) (ArtworkSeed, bool) {
	if !a.Ok {
		return ArtworkSeed{}, false
	}
	h1 := a.H
	h2 := a.H2
	if !a.HasH2 {
		h2 = math.Mod(h1+artDefaultSpread+360, 360)
	}
	// Map the cover's own chroma onto the band the static themes use. An
	// over-eager multiplier here is the bug the web module records: every real
	// cover it tested landed past the clamp, so a muted cover was
	// indistinguishable from a saturated one and the vividness signal was being
	// computed and then thrown away. artCLo/artCHi are measured on real covers
	// with THIS extractor for exactly that reason.
	t := (math.Max(artCLo, math.Min(artCHi, a.C)) - artCLo) / (artCHi - artCLo)
	c1 := artA1CMin + t*(artA1CMax-artA1CMin)
	c1 = artClampChroma(c1, artA1L, artLightRamp.a1Cap, artLightRamp.aCF, h1)
	c2 := math.Max(artA2CMin, math.Min(artA2CMax, c1*artA2CRatio))
	c2 = artClampChroma(c2, artA2L, artLightRamp.a2Cap, artLightRamp.aCF, h2)

	// Hue is re-clamped after rounding for the same reason: a hue nudged by
	// 0.05 degrees changes the gamut ceiling slightly, and the emitted chroma
	// has to be in gamut at the hue actually emitted, not at the one measured.
	h1 = artRound(h1, 1)
	h2 = artRound(h2, 1)
	c1 = artFloorTo(artClampChroma(c1, artA1L, artLightRamp.a1Cap, artLightRamp.aCF, h1), 4)
	c2 = artFloorTo(artClampChroma(c2, artA2L, artLightRamp.a2Cap, artLightRamp.aCF, h2), 4)

	return ArtworkSeed{
		// AH == H here on purpose: Material You derives the primary and the
		// neutral from ONE cover hue. A custom theme separates them, which is
		// why the painter reads --a1-h from AH rather than from H.
		H: h1, AH: h1, H2: h2, C1: c1, C2: c2,
		L1: artA1L, L2: artA2L,
		NCF: artNCF, NCFHi: artNCFHi,
		Swatch: artOklchToHex(artA1L, c1, h1),
		// Stored so the pre-paint block in index.html can paint the right ground
		// before the stylesheet has even loaded -- a colour only known at
		// runtime cannot be in that file's static table.
		BgDark:      artOklchToHex(artDarkRamp.lBg0, artDarkRamp.cBg0*artNCF, h1),
		BgLight:     artOklchToHex(artLightRamp.lBg0, artLightRamp.cBg0*artNCF, h1),
		BgElevDark:  artOklchToHex(artDarkRamp.lBg1, artDarkRamp.cBg1*artNCF, h1),
		BgElevLight: artOklchToHex(artLightRamp.lBg1, artLightRamp.cBg1*artNCF, h1),
		ArtC:        artRound(a.C, 4),
	}, true
}

// artSamplePixels walks the decoded image on an even grid. Sampling rather than
// area-averaging is deliberate: averaging mixes neighbouring hues and pushes
// chroma toward grey, which is the very signal the histogram is weighing.
func artSamplePixels(img image.Image) []artPixel {
	b := img.Bounds()
	w, h := b.Dx(), b.Dy()
	if w <= 0 || h <= 0 {
		return nil
	}
	stepX, stepY := 1, 1
	if w > artMaxDim {
		stepX = (w + artMaxDim - 1) / artMaxDim
	}
	if h > artMaxDim {
		stepY = (h + artMaxDim - 1) / artMaxDim
	}
	out := make([]artPixel, 0, (w/stepX+1)*(h/stepY+1))
	for y := b.Min.Y; y < b.Max.Y; y += stepY {
		for x := b.Min.X; x < b.Max.X; x += stepX {
			r16, g16, b16, a16 := img.At(x, y).RGBA()
			if a16 < 32000 { // ~alpha 125/255, matching the web module
				continue
			}
			// RGBA() is alpha-premultiplied; undo it so a semi-transparent pixel
			// reports its real colour rather than a darkened one.
			if a16 > 0 && a16 < 0xffff {
				s := float64(0xffff) / float64(a16)
				r16 = uint32(math.Min(0xffff, float64(r16)*s))
				g16 = uint32(math.Min(0xffff, float64(g16)*s))
				b16 = uint32(math.Min(0xffff, float64(b16)*s))
			}
			out = append(out, artPixel{
				r: float64(r16) / 257, g: float64(g16) / 257, b: float64(b16) / 257,
			})
		}
	}
	return out
}

func artSeedFromImage(img image.Image) (ArtworkSeed, bool) {
	px := artSamplePixels(img)
	if len(px) == 0 {
		return ArtworkSeed{}, false
	}
	return artToSeed(artAnalyse(px))
}

var artHTTP = &http.Client{Timeout: artFetchTimeout}

func artFetchImage(url string) (image.Image, error) {
	url = strings.TrimSpace(url)
	if url == "" {
		return nil, fmt.Errorf("empty artwork url")
	}
	if !strings.HasPrefix(url, "http://") && !strings.HasPrefix(url, "https://") {
		return nil, fmt.Errorf("unsupported artwork url scheme")
	}
	resp, err := artHTTP.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("artwork fetch returned %d", resp.StatusCode)
	}
	img, _, err := image.Decode(io.LimitReader(resp.Body, artMaxBytes))
	if err != nil {
		return nil, fmt.Errorf("decode artwork: %w", err)
	}
	return img, nil
}

/* ── Fallback seed ───────────────────────────────────────────────────────── */

// artFallbackHue is the default theme's own neutral hue, so selecting the
// artwork theme before any cover has been seen lands on something recognisably
// Antra rather than on grey mud.
const artFallbackHue = 180.389

// artFallbackArtC is the median dominant-bucket chroma of the twelve covers in
// realCovers, so the placeholder is as vivid as a typical sleeve.
const artFallbackArtC = 0.064

// ArtworkFallbackSeed is what style.css declares under [data-theme="artwork"].
// TestFallbackSeedMatchesCSS asserts the stylesheet still agrees with it, so
// the CSS block cannot drift away from the extractor.
func ArtworkFallbackSeed() ArtworkSeed {
	s, _ := artToSeed(artAnalysis{H: artFallbackHue, C: artFallbackArtC, Ok: true})
	return s
}

/* ── Wails binding ───────────────────────────────────────────────────────── */

// ExtractArtworkSeed downloads a cover and returns the theme seed as JSON.
// A failure is never an error the user should see -- the frontend keeps the
// previous seed (or the CSS fallback block) rather than inventing a colour.
func (a *App) ExtractArtworkSeed(artworkURL string) string {
	fail := func(msg string) string {
		b, _ := json.Marshal(map[string]any{"ok": false, "error": msg})
		return string(b)
	}
	img, err := artFetchImage(artworkURL)
	if err != nil {
		return fail(err.Error())
	}
	seed, ok := artSeedFromImage(img)
	if !ok {
		return fail("no usable hue in artwork")
	}
	b, err := json.Marshal(map[string]any{"ok": true, "seed": seed})
	if err != nil {
		return fail(err.Error())
	}
	return string(b)
}
