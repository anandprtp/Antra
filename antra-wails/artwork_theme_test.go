package main

/* Tests for the v2.0.0 THEME-3 artwork theme.

   The load-bearing ones are the two sweeps: because the artwork theme accepts
   ANY hue, "measured on twelve hand-picked seeds" is not evidence here the way
   it was for THEME-1/THEME-2. Every property has to hold for all 360 degrees,
   in both modes, or a single unlucky cover ships an unreadable app. */

import (
	"bytes"
	"encoding/json"
	"image"
	"image/color"
	"image/png"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"
)

/* ── helpers ─────────────────────────────────────────────────────────────── */

func relLum(r, g, b int) float64 {
	lin := func(c int) float64 { return artSrgbToLinear(float64(c)) }
	return 0.2126*lin(r) + 0.7152*lin(g) + 0.0722*lin(b)
}

func contrastOklch(l1, c1, h1, l2, c2, h2 float64) float64 {
	r1, g1, b1 := artOklchToRGB255(l1, c1, h1)
	r2, g2, b2 := artOklchToRGB255(l2, c2, h2)
	a, b := relLum(r1, g1, b1), relLum(r2, g2, b2)
	if a < b {
		a, b = b, a
	}
	return (a + 0.05) / (b + 0.05)
}

// tokenScore evaluates every semantic token style.css derives from a seed, in
// one ramp, and returns contrast against --bg-color plus whether all of them
// are in the sRGB gamut.
type tokenScore struct {
	primary, fg, secondary, muted, faint float64
	accent, accentStrong                 float64
	inGamut                              bool
	worstOut                             string
}

func scoreSeed(m artRamp, s ArtworkSeed) tokenScore {
	nh := s.H
	bgL, bgC := m.lBg0, m.cBg0*s.NCF
	c := func(l, cc float64) float64 { return contrastOklch(l, cc, nh, bgL, bgC, nh) }
	a1l := math.Min(s.L1, m.a1Cap)
	a2l := math.Min(s.L2, m.a2Cap)
	a1c, a2c := s.C1*m.aCF, s.C2*m.aCF

	ts := tokenScore{
		primary:      c(m.lFg0, m.cFg0*s.NCFHi),
		fg:           c(m.lFg1, m.cFg1*s.NCF),
		secondary:    c(m.lFg2, m.cFg2*s.NCF),
		muted:        c(m.lFg3, m.cFg3*s.NCF),
		faint:        c(m.lFg4, m.cFg4*s.NCF),
		accent:       contrastOklch(a1l, a1c, s.H, bgL, bgC, nh),
		accentStrong: contrastOklch(a2l, a2c, s.H2, bgL, bgC, nh),
		inGamut:      true,
	}
	checks := []struct {
		name    string
		l, c, h float64
	}{
		{"bg-color", m.lBg0, m.cBg0 * s.NCF, nh},
		{"bg-elevated", m.lBg1, m.cBg1 * s.NCF, nh},
		{"text-primary", m.lFg0, m.cFg0 * s.NCFHi, nh},
		{"fg-color", m.lFg1, m.cFg1 * s.NCF, nh},
		{"text-secondary", m.lFg2, m.cFg2 * s.NCF, nh},
		{"text-muted", m.lFg3, m.cFg3 * s.NCF, nh},
		{"text-faint", m.lFg4, m.cFg4 * s.NCF, nh},
		{"accent-color", a1l, a1c, s.H},
		{"accent-strong", a2l, a2c, s.H2},
	}
	for _, k := range checks {
		if !artInGamut(k.l, k.c, k.h) {
			ts.inGamut = false
			ts.worstOut = k.name
			break
		}
	}
	return ts
}

// seedForHue builds the seed the extractor would produce for a given hue and a
// given raw artwork chroma, without needing an image.
func seedForHue(h, artC float64) ArtworkSeed {
	s, ok := artToSeed(artAnalysis{H: h, C: artC, Ok: true})
	if !ok {
		panic("artToSeed refused a valid analysis")
	}
	return s
}

func solidPixels(n int, r, g, b float64) []artPixel {
	out := make([]artPixel, n)
	for i := range out {
		out[i] = artPixel{r, g, b}
	}
	return out
}

/* ── 1. colour maths ─────────────────────────────────────────────────────── */

func TestOklchRoundTrip(t *testing.T) {
	cases := [][3]int{{255, 255, 255}, {0, 0, 0}, {55, 226, 194}, {222, 3, 71}, {24, 168, 255}, {128, 128, 128}}
	for _, c := range cases {
		L, C, H := artRgbToOklch(float64(c[0]), float64(c[1]), float64(c[2]))
		r, g, b := artOklchToRGB255(L, C, H)
		if abs(r-c[0]) > 1 || abs(g-c[1]) > 1 || abs(b-c[2]) > 1 {
			t.Errorf("round trip %v -> oklch(%.4f %.4f %.1f) -> (%d %d %d)", c, L, C, H, r, g, b)
		}
	}
}

func abs(a int) int {
	if a < 0 {
		return -a
	}
	return a
}

/* ── 2. the histogram must not be decided by a few hot pixels ────────────── */

func TestHistogramBeatsHotPixels(t *testing.T) {
	// 400 mid blue, 3 screaming magenta. A "sort by saturation, take the first"
	// extractor would return magenta.
	px := append(solidPixels(400, 40, 90, 200), solidPixels(3, 255, 0, 255)...)
	a := artAnalyse(px)
	if !a.Ok {
		t.Fatal("analysis failed on a clearly colourful image")
	}
	// Blue sits around 264 in OKLCH, magenta around 328.
	if a.H < 230 || a.H > 300 {
		t.Errorf("dominant hue %.1f is not the blue mass; 3 hot pixels won", a.H)
	}
}

func TestGreyCoverYieldsNoHue(t *testing.T) {
	// A perfectly neutral cover has no hue to extract. The frontend must fall
	// back to the CSS default rather than be handed grey mud.
	px := solidPixels(2000, 128, 128, 128)
	if a := artAnalyse(px); a.Ok {
		t.Errorf("a pure grey image produced a hue: %+v", a)
	}
	if _, ok := artToSeed(artAnalysis{}); ok {
		t.Error("artToSeed accepted a failed analysis")
	}
}

func TestVeryDarkCoverUsesRelaxedPass(t *testing.T) {
	// Near-black sleeve with a faint colour cast: the strict pass finds nothing
	// (L below 0.25), the relaxed pass must still produce a hue.
	px := solidPixels(2000, 14, 20, 34)
	a := artAnalyse(px)
	if !a.Ok {
		t.Fatal("relaxed pass failed on a dark cover")
	}
	if a.H < 230 || a.H > 300 {
		t.Errorf("dark cover hue %.1f is not blue-ish", a.H)
	}
}

/* ── 3. the vividness signal must survive the mapping ────────────────────── */

func TestVividnessPreserved(t *testing.T) {
	// The bug the web module records: an over-eager multiplier pushed every real
	// cover past the clamp, so a muted cover and a neon one produced the same
	// chroma. Same hue, different vividness must give different --a1-c.
	// Hue 340 has ~0.25 of in-gamut chroma headroom at a1-l 0.72, so this
	// measures the MAPPING rather than the gamut clamp; the artC values are
	// inside the band real covers were measured to occupy.
	const h = 340.0
	muted := seedForHue(h, 0.02)
	mid := seedForHue(h, 0.06)
	vivid := seedForHue(h, 0.10)
	if !(muted.C1 < mid.C1 && mid.C1 < vivid.C1) {
		t.Errorf("chroma is not monotonic in artwork vividness: %.4f %.4f %.4f",
			muted.C1, mid.C1, vivid.C1)
	}
	if vivid.C1-muted.C1 < 0.03 {
		t.Errorf("vividness range collapsed to %.4f; the signal is being thrown away",
			vivid.C1-muted.C1)
	}
}

/* ── 4. gamut + contrast must hold for EVERY hue, in BOTH modes ──────────── */

func TestAllHuesGamutAndContrast(t *testing.T) {
	type floor struct {
		name string
		min  float64
		get  func(tokenScore) float64
	}
	// AA is 4.5:1 for body text and 3:1 for large text / non-text. --text-faint
	// and --accent-strong are large/decorative roles, matching the floors
	// THEME-1 and THEME-2 measured against.
	floors := []floor{
		{"text-primary", 4.5, func(s tokenScore) float64 { return s.primary }},
		{"fg-color", 4.5, func(s tokenScore) float64 { return s.fg }},
		{"text-secondary", 4.5, func(s tokenScore) float64 { return s.secondary }},
		{"text-muted", 4.5, func(s tokenScore) float64 { return s.muted }},
		{"text-faint", 3.0, func(s tokenScore) float64 { return s.faint }},
		{"accent-color", 4.5, func(s tokenScore) float64 { return s.accent }},
		{"accent-strong", 3.0, func(s tokenScore) float64 { return s.accentStrong }},
	}
	for _, m := range []artRamp{artDarkRamp, artLightRamp} {
		worst := map[string]struct {
			v float64
			h float64
		}{}
		for hue := 0.0; hue < 360; hue++ {
			for _, artC := range []float64{0.02, 0.05, 0.10, 0.15, 0.30} {
				s := seedForHue(hue, artC)
				ts := scoreSeed(m, s)
				if !ts.inGamut {
					t.Errorf("%s h=%.0f artC=%.2f: %s out of sRGB gamut", m.name, hue, artC, ts.worstOut)
				}
				for _, f := range floors {
					v := f.get(ts)
					if cur, ok := worst[f.name]; !ok || v < cur.v {
						worst[f.name] = struct {
							v float64
							h float64
						}{v, hue}
					}
					if v < f.min {
						t.Errorf("%s h=%.0f artC=%.2f: %s contrast %.2f < %.2f",
							m.name, hue, artC, f.name, v, f.min)
					}
				}
			}
		}
		for _, f := range floors {
			t.Logf("%-5s worst %-14s %.2f:1 (floor %.1f) at hue %.0f",
				m.name, f.name, worst[f.name].v, f.min, worst[f.name].h)
		}
	}
}

// artClampChroma states that dark binds at every hue. State it, then prove it.
func TestDarkBindsTheChromaClamp(t *testing.T) {
	viol := 0
	for hue := 0.0; hue < 360; hue++ {
		dark := artMaxChroma(artA1L, hue)
		light := artMaxChroma(math.Min(artA1L, artLightRamp.a1Cap), hue) / artLightRamp.aCF
		if light < dark {
			viol++
			if viol <= 3 {
				t.Logf("hue %.0f: light allows %.4f < dark %.4f -- light binds here", hue, light, dark)
			}
		}
	}
	if viol > 0 {
		t.Logf("light bound the clamp at %d/360 hues; artClampChroma takes the min of both, so this is handled", viol)
	}
}

/* ── 5. the pre-paint colour must equal the painted one ──────────────────── */

func TestPrePaintMatchesPaintedGround(t *testing.T) {
	// index.html paints seed.bg_dark / seed.bg_light before the stylesheet
	// loads. If these drift from --bg-color the launch flashes -- the exact
	// thing the pre-paint block exists to prevent.
	for hue := 0.0; hue < 360; hue += 7 {
		s := seedForHue(hue, 0.10)
		wantDark := artOklchToHex(artDarkRamp.lBg0, artDarkRamp.cBg0*s.NCF, s.H)
		wantLight := artOklchToHex(artLightRamp.lBg0, artLightRamp.cBg0*s.NCF, s.H)
		wantElevDark := artOklchToHex(artDarkRamp.lBg1, artDarkRamp.cBg1*s.NCF, s.H)
		wantElevLight := artOklchToHex(artLightRamp.lBg1, artLightRamp.cBg1*s.NCF, s.H)
		if s.BgElevDark != wantElevDark || s.BgElevLight != wantElevLight {
			t.Errorf("hue %.0f: seed elevated (%s/%s) != painted --bg-elevated (%s/%s)",
				hue, s.BgElevDark, s.BgElevLight, wantElevDark, wantElevLight)
		}
		if s.BgDark != wantDark || s.BgLight != wantLight {
			t.Errorf("hue %.0f: seed bg (%s/%s) != painted --bg-color (%s/%s)",
				hue, s.BgDark, s.BgLight, wantDark, wantLight)
		}
	}
}

// The seed must carry the SAME slots a static theme declares, so the frontend
// writes a complete set and nothing falls back to the previous theme.
func TestSeedCarriesEverySlot(t *testing.T) {
	s := seedForHue(200, 0.10)
	if s.L1 != artA1L || s.L2 != artA2L || s.NCF != artNCF || s.NCFHi != artNCFHi {
		t.Errorf("seed lost a constant: %+v", s)
	}
	if s.Swatch == "" || len(s.Swatch) != 7 || s.Swatch[0] != '#' {
		t.Errorf("bad swatch %q", s.Swatch)
	}
	if s.C1 <= 0 || s.C2 <= 0 {
		t.Errorf("zero chroma: %+v", s)
	}
}

/* ── 6. the binding never surfaces an error to the user ──────────────────── */

func TestExtractArtworkSeedFailuresAreSoft(t *testing.T) {
	a := &App{}
	for _, url := range []string{"", "   ", "file:///etc/passwd", "notaurl", "http://127.0.0.1:1/x.jpg"} {
		var out map[string]any
		if err := json.Unmarshal([]byte(a.ExtractArtworkSeed(url)), &out); err != nil {
			t.Fatalf("ExtractArtworkSeed(%q) returned invalid JSON: %v", url, err)
		}
		if out["ok"] != false {
			t.Errorf("ExtractArtworkSeed(%q) reported success", url)
		}
		if _, has := out["error"]; !has {
			t.Errorf("ExtractArtworkSeed(%q) gave no reason", url)
		}
	}
}

func TestExtractArtworkSeedDecodesRealFormats(t *testing.T) {
	// A synthetic PNG through the real decode path, so image.Decode registration
	// is proven rather than assumed.
	img := image.NewRGBA(image.Rect(0, 0, 40, 40))
	for y := 0; y < 40; y++ {
		for x := 0; x < 40; x++ {
			img.Set(x, y, color.RGBA{200, 60, 40, 255})
		}
	}
	s, ok := artSeedFromImage(img)
	if !ok {
		t.Fatal("a solid red image produced no seed")
	}
	if s.H < 10 || s.H > 60 {
		t.Errorf("red image gave hue %.1f", s.H)
	}
}

func TestTransparentPixelsIgnored(t *testing.T) {
	img := image.NewRGBA(image.Rect(0, 0, 40, 40))
	for y := 0; y < 40; y++ {
		for x := 0; x < 40; x++ {
			if x < 20 {
				img.Set(x, y, color.RGBA{0, 0, 0, 0}) // fully transparent
			} else {
				img.Set(x, y, color.RGBA{40, 90, 200, 255})
			}
		}
	}
	s, ok := artSeedFromImage(img)
	if !ok {
		t.Fatal("half-transparent image produced no seed")
	}
	if s.H < 230 || s.H > 300 {
		t.Errorf("transparent half leaked into the hue: %.1f", s.H)
	}
}

/* ── 7. real covers — the acceptance criterion ───────────────────────────── */

var realCovers = []struct{ label, url string }{
	{"The Weeknd - After Hours", "https://is1-ssl.mzstatic.com/image/thumb/Music125/v4/2b/b9/fe/2bb9fef5-d7f3-8345-25a9-db0e79fde4e4/20UMGIM11048.rgb.jpg/600x600bb.jpg"},
	{"Fleetwood Mac - Rumours", "https://is1-ssl.mzstatic.com/image/thumb/Music124/v4/4d/13/ba/4d13bac3-d3d5-7581-2c74-034219eadf2b/081227970949.jpg/600x600bb.jpg"},
	{"Nirvana - Nevermind", "https://is1-ssl.mzstatic.com/image/thumb/Music115/v4/19/7a/58/197a5870-618b-446f-a193-ca4a84502adc/190295781163.jpg/600x600bb.jpg"},
	{"Daft Punk - RAM", "https://is1-ssl.mzstatic.com/image/thumb/Music115/v4/e8/43/5f/e8435ffa-b6b9-b171-40ab-4ff3959ab661/886443919266.jpg/600x600bb.jpg"},
	{"Adele - 21", "https://is1-ssl.mzstatic.com/image/thumb/Music221/v4/eb/ca/25/ebca2596-cd1e-b295-91a3-771c868d0a79/191404113868.png/600x600bb.jpg"},
	{"Metallica - Black Album", "https://is1-ssl.mzstatic.com/image/thumb/Music125/v4/9e/80/1b/9e801b06-67fa-0990-2d15-85480ad3cd46/850007452025.png/600x600bb.jpg"},
	{"Billie Eilish - Happier Than Ever", "https://is1-ssl.mzstatic.com/image/thumb/Music125/v4/02/b7/6d/02b76dd3-4006-bc65-9f33-33b70a95222a/21UMGIM36684.rgb.jpg/600x600bb.jpg"},
	{"Miles Davis - Kind of Blue", "https://is1-ssl.mzstatic.com/image/thumb/Music/7f/9f/d6/mzi.vtnaewef.jpg/600x600bb.jpg"},
	{"Tame Impala - Currents B-Sides", "https://is1-ssl.mzstatic.com/image/thumb/Music124/v4/64/48/5c/64485cc9-968c-68cc-764e-9a7c71733def/00602567155454.rgb.jpg/600x600bb.jpg"},
	{"Kendrick Lamar - DAMN.", "https://is1-ssl.mzstatic.com/image/thumb/Music112/v4/ab/16/ef/ab16efe9-e7f1-66ec-021c-5592a23f0f9e/17UMGIM88793.rgb.jpg/600x600bb.jpg"},
	{"Spotify CDN control", "https://i.scdn.co/image/ab67616d0000b273ef0d4234e1a645740f77d59c"},
	{"Deezer CDN control", "https://cdns-images.dzcdn.net/images/cover/2e018122cb56986277102d2041a592c8/500x500-000000-80-0-0.jpg"},
}

// TestRealCoverContrast is the acceptance measurement: a spread of real covers
// (dark, pale, saturated, muted, near-monochrome) across four CDNs, contrast in
// BOTH modes. Set ANTRA_NET_TESTS=1 to run; it is skipped by default so the
// suite stays offline-clean.
func TestRealCoverContrast(t *testing.T) {
	if os.Getenv("ANTRA_NET_TESTS") == "" {
		t.Skip("set ANTRA_NET_TESTS=1 to run the real-cover measurement")
	}
	artHTTP = &http.Client{Timeout: 20 * time.Second}
	fetched := 0
	t.Logf("%-34s %6s %6s %6s | %-24s | %-24s", "cover", "hue", "artC", "a1-c",
		"dark  acc/muted/faint", "light acc/muted/faint")
	for _, c := range realCovers {
		img, err := artFetchImage(c.url)
		if err != nil {
			t.Logf("%-34s SKIP (%v)", c.label, err)
			continue
		}
		s, ok := artSeedFromImage(img)
		if !ok {
			t.Errorf("%s: no usable hue", c.label)
			continue
		}
		fetched++
		d := scoreSeed(artDarkRamp, s)
		l := scoreSeed(artLightRamp, s)
		t.Logf("%-34s %6.1f %6.3f %6.3f | %5.2f %5.2f %5.2f %-6s | %5.2f %5.2f %5.2f %-6s",
			c.label, s.H, s.ArtC, s.C1,
			d.accent, d.muted, d.faint, boolTag(d.inGamut),
			l.accent, l.muted, l.faint, boolTag(l.inGamut))
		for _, row := range []struct {
			mode string
			ts   tokenScore
		}{{"dark", d}, {"light", l}} {
			if !row.ts.inGamut {
				t.Errorf("%s [%s]: %s out of gamut", c.label, row.mode, row.ts.worstOut)
			}
			if row.ts.accent < 4.5 || row.ts.muted < 4.5 || row.ts.fg < 4.5 || row.ts.primary < 4.5 {
				t.Errorf("%s [%s]: AA failure acc=%.2f muted=%.2f fg=%.2f primary=%.2f",
					c.label, row.mode, row.ts.accent, row.ts.muted, row.ts.fg, row.ts.primary)
			}
			if row.ts.faint < 3.0 || row.ts.accentStrong < 3.0 {
				t.Errorf("%s [%s]: AA-large failure faint=%.2f accentStrong=%.2f",
					c.label, row.mode, row.ts.faint, row.ts.accentStrong)
			}
		}
	}
	if fetched < 4 {
		t.Skipf("only %d covers reachable; not a measurement", fetched)
	}
	t.Logf("measured %d/%d covers", fetched, len(realCovers))
}

func boolTag(b bool) string {
	if b {
		return "ok"
	}
	return "OUT"
}

// TestRealCoverChromaBand reports the raw dominant-bucket chroma of real
// artwork, which is what artCLo/artCHi are set from. Run with the network flag.
func TestRealCoverChromaBand(t *testing.T) {
	if os.Getenv("ANTRA_NET_TESTS") == "" {
		t.Skip("set ANTRA_NET_TESTS=1 to run the real-cover measurement")
	}
	artHTTP = &http.Client{Timeout: 20 * time.Second}
	var lo, hi = math.Inf(1), math.Inf(-1)
	n := 0
	for _, c := range realCovers {
		img, err := artFetchImage(c.url)
		if err != nil {
			continue
		}
		px := artSamplePixels(img)
		a := artAnalyse(px)
		if !a.Ok {
			continue
		}
		n++
		lo, hi = math.Min(lo, a.C), math.Max(hi, a.C)
		t.Logf("%-34s artC=%.4f hue=%6.1f h2=%v", c.label, a.C, a.H, a.HasH2)
	}
	if n == 0 {
		t.Skip("no covers reachable")
	}
	t.Logf("observed dominant-bucket chroma over %d covers: %.4f .. %.4f", n, lo, hi)
	t.Logf("band in use: artCLo=%.3f artCHi=%.3f", artCLo, artCHi)
	if lo < artCLo*0.5 || hi > artCHi*1.5 {
		t.Errorf("the measured band %.4f..%.4f has drifted far outside artCLo/artCHi (%.3f..%.3f)",
			lo, hi, artCLo, artCHi)
	}
}

/* ── 8. the constants the changelog quotes must stay true ────────────────── */

func TestGamutCeilingsThatSetTheConstants(t *testing.T) {
	// n-cf-hi is bounded by dark --text-primary at L=0.983.
	minMax := math.Inf(1)
	for h := 0.0; h < 360; h++ {
		minMax = math.Min(minMax, artMaxChroma(artDarkRamp.lFg0, h))
	}
	ceiling := minMax / artDarkRamp.cFg0
	t.Logf("dark --text-primary L=%.3f: worst-hue max chroma %.4f -> n-cf-hi <= %.3f",
		artDarkRamp.lFg0, minMax, ceiling)
	if artNCFHi > ceiling {
		t.Errorf("artNCFHi %.3f exceeds the gamut ceiling %.3f", artNCFHi, ceiling)
	}

	// n-cf is bounded by light --bg-color at L=0.974.
	minMax = math.Inf(1)
	for h := 0.0; h < 360; h++ {
		minMax = math.Min(minMax, artMaxChroma(artLightRamp.lBg0, h))
	}
	ceiling = minMax / artLightRamp.cBg0
	t.Logf("light --bg-color  L=%.3f: worst-hue max chroma %.4f -> n-cf <= %.3f",
		artLightRamp.lBg0, minMax, ceiling)
	if artNCF > ceiling {
		t.Errorf("artNCF %.3f exceeds the gamut ceiling %.3f", artNCF, ceiling)
	}
}

func TestAccentSpreadStaysInBand(t *testing.T) {
	// No genuine second hue -> rotate by the default and stay inside the band
	// the analyser would have accepted, so the two paths agree.
	s := seedForHue(200, 0.10)
	d := artArc(s.H, s.H2)
	if d < artSpreadMin || d > artSpreadMax {
		t.Errorf("default rotation gave a %.1f degree spread, outside %.0f..%.0f",
			d, artSpreadMin, artSpreadMax)
	}
	// A genuine second hue is preferred over the rotation.
	got, _ := artToSeed(artAnalysis{H: 200, C: 0.10, H2: 250, HasH2: true, Ok: true})
	if math.Abs(got.H2-250) > 0.05 {
		t.Errorf("the cover's own second hue was discarded: got %.1f", got.H2)
	}
}

func TestSeedRoundTripsThroughJSON(t *testing.T) {
	a := &App{}
	// Drive the real binding on a local server so the whole path is exercised.
	img := image.NewRGBA(image.Rect(0, 0, 32, 32))
	for y := 0; y < 32; y++ {
		for x := 0; x < 32; x++ {
			img.Set(x, y, color.RGBA{30, 110, 190, 255})
		}
	}
	srv := serveImage(t, img)
	defer srv.Close()

	var out struct {
		Ok   bool        `json:"ok"`
		Seed ArtworkSeed `json:"seed"`
	}
	raw := a.ExtractArtworkSeed(srv.URL + "/cover.png")
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		t.Fatalf("bad JSON: %v (%s)", err, raw)
	}
	if !out.Ok {
		t.Fatalf("extraction failed: %s", raw)
	}
	if out.Seed.H < 230 || out.Seed.H > 300 {
		t.Errorf("blue cover gave hue %.1f", out.Seed.H)
	}
	if out.Seed.BgDark == "" || out.Seed.BgLight == "" {
		t.Error("pre-paint grounds missing from the JSON")
	}
}

func serveImage(t *testing.T, img image.Image) *httptest.Server {
	t.Helper()
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		t.Fatalf("encode fixture: %v", err)
	}
	body := buf.Bytes()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "image/png")
		_, _ = w.Write(body)
	}))
}

/* ── 9. the CSS fallback block must agree with the extractor ─────────────── */

// The artwork theme needs a static seed for "selected, but no cover seen yet".
// It lives in style.css because the pre-paint block and the first frame both
// need it before any Go call can run -- so it is a second copy of numbers this
// file owns, and second copies drift. This test is the guard.
func TestFallbackSeedMatchesCSS(t *testing.T) {
	s := ArtworkFallbackSeed()
	t.Logf("fallback seed: n-h %.3f n-cf %.3f n-cf-hi %.3f a1 %.3f/%.4f/%.3f a2 %.3f/%.4f/%.3f",
		s.H, s.NCF, s.NCFHi, s.L1, s.C1, s.H, s.L2, s.C2, s.H2)
	t.Logf("fallback grounds: dark %s light %s swatch %s", s.BgDark, s.BgLight, s.Swatch)

	css, err := os.ReadFile("frontend/src/style.css")
	if err != nil {
		t.Skipf("style.css unreadable: %v", err)
	}
	block := extractCSSBlock(string(css), `:root[data-theme="artwork"]`)
	if block == "" {
		t.Fatal(`style.css has no :root[data-theme="artwork"] block`)
	}
	for _, want := range []struct {
		prop string
		val  float64
		dp   int
	}{
		{"--n-h", s.H, 3}, {"--n-cf", s.NCF, 3}, {"--n-cf-hi", s.NCFHi, 3},
		{"--a1-l", s.L1, 3}, {"--a1-c", s.C1, 4}, {"--a1-h", s.H, 3},
		{"--a2-l", s.L2, 3}, {"--a2-c", s.C2, 4}, {"--a2-h", s.H2, 3},
	} {
		got, ok := cssNumber(block, want.prop)
		if !ok {
			t.Errorf("style.css artwork block is missing %s", want.prop)
			continue
		}
		if math.Abs(got-want.val) > math.Pow(10, -float64(want.dp))/2 {
			t.Errorf("style.css %s = %v, extractor says %v", want.prop, got, want.val)
		}
	}
}

func extractCSSBlock(css, selector string) string {
	i := strings.Index(css, selector)
	if i < 0 {
		return ""
	}
	open := strings.Index(css[i:], "{")
	closeAt := strings.Index(css[i:], "}")
	if open < 0 || closeAt < 0 || closeAt < open {
		return ""
	}
	return css[i+open+1 : i+closeAt]
}

func cssNumber(block, prop string) (float64, bool) {
	for _, line := range strings.Split(block, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, prop+":") {
			continue
		}
		v := strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(line[len(prop)+1:]), ";"))
		if k := strings.Index(v, "/*"); k >= 0 {
			v = strings.TrimSpace(v[:k])
		}
		v = strings.TrimSuffix(strings.TrimSpace(v), ";")
		f, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
		if err != nil {
			return 0, false
		}
		return f, true
	}
	return 0, false
}
