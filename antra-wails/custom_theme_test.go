package main

import (
	"encoding/json"
	"math"
	"os"
	"regexp"
	"strings"
	"testing"
)

/*
THEME-4 test suite.

The interesting assertion here is not "custom themes pass WCAG" in the
abstract -- it is "custom themes are no worse than the twelve that already
ship". So the envelope is MEASURED from style.css itself rather than hardcoded:
if a future ramp change moves the static themes, the control moves with them
and the comparison stays honest.
*/

// staticSeed is one of the twelve hand-authored theme blocks in style.css,
// read back as a seed so it can go through exactly the same scorer as a
// custom one.
type staticSeed struct {
	name string
	seed ArtworkSeed
}

var reThemeBlock = regexp.MustCompile(`(?s):root\[data-theme="([\w-]+)"\] \{(.*?)\n\}`)

func loadStaticSeeds(t *testing.T) []staticSeed {
	t.Helper()
	raw, err := os.ReadFile("frontend/src/style.css")
	if err != nil {
		t.Skipf("style.css unreadable: %v", err)
	}
	css := string(raw)
	out := []staticSeed{}
	// The default theme shares its selector with bare :root, so it is not
	// matched by the block regex and has to be picked up explicitly.
	if b := extractCSSBlock(css, `:root,
:root[data-theme="antra"]`); b != "" {
		if s, ok := seedFromCSSBlock(b); ok {
			out = append(out, staticSeed{"antra", s})
		}
	}
	for _, m := range reThemeBlock.FindAllStringSubmatch(css, -1) {
		name := m[1]
		// artwork and custom are DYNAMIC: their blocks are only the fallback
		// the generator emits, so including them would be comparing this
		// feature against itself rather than against the hand-authored set.
		if name == "artwork" || name == customThemeID {
			continue
		}
		if s, ok := seedFromCSSBlock(m[2]); ok {
			out = append(out, staticSeed{name, s})
		}
	}
	if len(out) < 10 {
		t.Fatalf("expected the full static theme set as a control, parsed only %d", len(out))
	}
	return out
}

func seedFromCSSBlock(block string) (ArtworkSeed, bool) {
	get := func(p string) (float64, bool) { return cssNumber(block, p) }
	nh, ok1 := get("--n-h")
	ncf, ok2 := get("--n-cf")
	nchi, ok3 := get("--n-cf-hi")
	a1l, ok4 := get("--a1-l")
	a1c, ok5 := get("--a1-c")
	a1h, ok6 := get("--a1-h")
	a2l, ok7 := get("--a2-l")
	a2c, ok8 := get("--a2-c")
	a2h, ok9 := get("--a2-h")
	if !(ok1 && ok2 && ok3 && ok4 && ok5 && ok6 && ok7 && ok8 && ok9) {
		return ArtworkSeed{}, false
	}
	return ArtworkSeed{
		H: nh, NCF: ncf, NCFHi: nchi,
		L1: a1l, C1: a1c, AH: a1h, H2: a2h,
		L2: a2l, C2: a2c,
	}, true
}

// scoreSeedSplit is scoreSeed with all three hue slots read separately:
// neutral from H, --a1-h from AH and --a2-h from H2. artwork_theme.go's own
// scorer collapses the first two because Material You derives them from ONE
// cover hue; a custom theme's whole point is that it does not, and reading
// --a1-h from H is exactly the bug that gave Mauve x Amber a mauve accent.
func scoreSeedSplit(m artRamp, s ArtworkSeed) tokenScore {
	nh := s.H
	bgL, bgC := m.lBg0, m.cBg0*s.NCF
	c := func(l, cc float64) float64 { return contrastOklch(l, cc, nh, bgL, bgC, nh) }
	a1l := math.Min(s.L1, m.a1Cap)
	a2l := math.Min(s.L2, m.a2Cap)
	a1c, a2c := s.C1*m.aCF, s.C2*m.aCF
	ah := s.AH   // --a1-h
	ah2 := s.H2  // --a2-h

	ts := tokenScore{
		primary:      c(m.lFg0, m.cFg0*s.NCFHi),
		fg:           c(m.lFg1, m.cFg1*s.NCF),
		secondary:    c(m.lFg2, m.cFg2*s.NCF),
		muted:        c(m.lFg3, m.cFg3*s.NCF),
		faint:        c(m.lFg4, m.cFg4*s.NCF),
		accent:       contrastOklch(a1l, a1c, ah, bgL, bgC, nh),
		accentStrong: contrastOklch(a2l, a2c, ah2, bgL, bgC, nh),
		inGamut:      true,
	}
	for _, k := range []struct {
		name    string
		l, c, h float64
	}{
		{"bg-color", m.lBg0, m.cBg0 * s.NCF, nh},
		{"bg-elevated", m.lBg1, m.cBg1 * s.NCF, nh},
		{"bg-overlay", 0.146, 0.013 * s.NCF, nh},
		{"text-primary", m.lFg0, m.cFg0 * s.NCFHi, nh},
		{"fg-color", m.lFg1, m.cFg1 * s.NCF, nh},
		{"text-secondary", m.lFg2, m.cFg2 * s.NCF, nh},
		{"text-muted", m.lFg3, m.cFg3 * s.NCF, nh},
		{"text-faint", m.lFg4, m.cFg4 * s.NCF, nh},
		{"accent-color", a1l, a1c, ah},
		{"accent-strong", a2l, a2c, ah2},
	} {
		if !artInGamut(k.l, k.c, k.h) {
			ts.inGamut = false
			ts.worstOut = k.name
			break
		}
	}
	return ts
}

/* ── 1. the full 119 x 2 sweep ───────────────────────────────────────────── */

// TestEveryCombinationInGamut is the assertion the per-hue chroma clamp exists
// for. A custom theme accepts any of 17 hues at a fixed lightness, and at
// artA1L the sRGB ceiling runs 0.126 (teal) to 0.264 (fuchsia) -- so a flat
// chroma band would clip channels and distort the hue on most of the wheel.
func TestEveryCombinationInGamut(t *testing.T) {
	bad := 0
	for _, b := range customBaseTable {
		for _, a := range customAccentTable {
			s, known := customSeed(b.id, a.id)
			if !known {
				t.Fatalf("customSeed(%q,%q) reported an unknown id for a table entry", b.id, a.id)
			}
			for _, m := range []artRamp{artDarkRamp, artLightRamp} {
				ts := scoreSeedSplit(m, s)
				if !ts.inGamut {
					bad++
					t.Errorf("%s/%s %s: %s out of sRGB", b.id, a.id, m.name, ts.worstOut)
				}
			}
		}
	}
	t.Logf("%d combinations x 2 modes, %d out of gamut",
		len(customBaseTable)*len(customAccentTable), bad)
}

// TestCombinationsStayInsideStaticEnvelope is the honest comparison: a custom
// theme must be no worse than the twelve hand-authored ones on any token. The
// envelope is measured from style.css, so it tracks the shipped set rather
// than a number frozen into this file.
func TestCombinationsStayInsideStaticEnvelope(t *testing.T) {
	statics := loadStaticSeeds(t)

	type band struct{ lo, hi float64 }
	envelope := map[string]*band{}
	note := func(mode, tok string, v float64) {
		k := mode + "/" + tok
		if b, ok := envelope[k]; ok {
			b.lo, b.hi = math.Min(b.lo, v), math.Max(b.hi, v)
		} else {
			envelope[k] = &band{v, v}
		}
	}
	for _, ss := range statics {
		for _, m := range []artRamp{artDarkRamp, artLightRamp} {
			ts := scoreSeedSplit(m, ss.seed)
			note(m.name, "muted", ts.muted)
			note(m.name, "faint", ts.faint)
			note(m.name, "accent", ts.accent)
			note(m.name, "accent-strong", ts.accentStrong)
		}
	}
	for k, b := range envelope {
		t.Logf("static envelope %-22s %5.2f .. %5.2f", k, b.lo, b.hi)
	}

	worst := map[string]float64{}
	for _, bse := range customBaseTable {
		for _, a := range customAccentTable {
			s, _ := customSeed(bse.id, a.id)
			for _, m := range []artRamp{artDarkRamp, artLightRamp} {
				ts := scoreSeedSplit(m, s)
				for tok, v := range map[string]float64{
					"muted": ts.muted, "faint": ts.faint,
					"accent": ts.accent, "accent-strong": ts.accentStrong,
				} {
					k := m.name + "/" + tok
					if cur, ok := worst[k]; !ok || v < cur {
						worst[k] = v
					}
					// The floor is the static set's own floor. A custom theme
					// dipping below it would be a regression against something
					// that already ships; staying above it is the claim.
					if v < envelope[k].lo-0.005 {
						t.Errorf("%s/%s %s %s = %.2f, below the static floor %.2f",
							bse.id, a.id, m.name, tok, v, envelope[k].lo)
					}
				}
			}
		}
	}
	for k, v := range worst {
		t.Logf("custom  floor  %-22s %5.2f (static floor %5.2f)", k, v, envelope[k].lo)
	}
}

// TestCustomTextTokensMeetWCAG pins the absolute bar as well as the relative
// one. accent-strong is deliberately excluded: measured, the static set runs
// 3.28-3.71 there in light because --a2-l-cap is intentionally lighter for the
// emphasis role, so it is an AA-LARGE token (3.0) in both this feature and the
// eleven themes that preceded it.
func TestCustomTextTokensMeetWCAG(t *testing.T) {
	for _, b := range customBaseTable {
		for _, a := range customAccentTable {
			s, _ := customSeed(b.id, a.id)
			for _, m := range []artRamp{artDarkRamp, artLightRamp} {
				ts := scoreSeedSplit(m, s)
				for _, k := range []struct {
					name string
					v    float64
					min  float64
				}{
					{"text-primary", ts.primary, 4.5},
					{"fg-color", ts.fg, 4.5},
					{"text-secondary", ts.secondary, 4.5},
					{"text-muted", ts.muted, 4.5},
					{"text-faint", ts.faint, 3.0},
					{"accent-color", ts.accent, 4.5},
					{"accent-strong", ts.accentStrong, 3.0},
				} {
					if k.v < k.min {
						t.Errorf("%s/%s %s %s = %.2f < %.1f",
							b.id, a.id, m.name, k.name, k.v, k.min)
					}
				}
			}
		}
	}
}

/* ── 2. the emitted numbers ──────────────────────────────────────────────── */

// TestEmittedChromaSurvivesRounding is THEME-3's recorded bug, re-asserted for
// this table: chroma is emitted at 4 decimal places, and ROUNDING a
// gamut-clamped value up puts it back outside sRGB. Every emitted value must be
// in gamut at the hue actually emitted, not at the one requested.
func TestEmittedChromaSurvivesRounding(t *testing.T) {
	for _, b := range customBaseTable {
		for _, a := range customAccentTable {
			s, _ := customSeed(b.id, a.id)
			// Asserted as "carries at most 4 decimal places", NOT as
			// artFloorTo(v) == v: that would be testing the idempotency of a
			// float operation, and it is not idempotent -- 0.0768*1e4 is
			// 767.9999999999999 in float64, so re-flooring its own output
			// drops a digit. The property that matters is the emitted
			// precision plus the gamut checks below.
			for _, k := range []struct {
				name string
				v    float64
			}{{"a1-c", s.C1}, {"a2-c", s.C2}} {
				if math.Abs(k.v*1e4-math.Round(k.v*1e4)) > 1e-6 {
					t.Errorf("%s/%s %s = %v carries more than 4 decimal places", b.id, a.id, k.name, k.v)
				}
			}
			if !artInGamut(s.L1, s.C1, s.AH) {
				t.Errorf("%s/%s emitted a1 (%v,%v,%v) is out of gamut", b.id, a.id, s.L1, s.C1, s.AH)
			}
			if !artInGamut(s.L2, s.C2, s.H2) {
				t.Errorf("%s/%s emitted a2 (%v,%v,%v) is out of gamut", b.id, a.id, s.L2, s.C2, s.H2)
			}
			// ...and in light, where the caps and the chroma factor both apply.
			if !artInGamut(math.Min(s.L1, artLightRamp.a1Cap), s.C1*artLightRamp.aCF, s.AH) {
				t.Errorf("%s/%s a1 out of gamut in LIGHT", b.id, a.id)
			}
			if !artInGamut(math.Min(s.L2, artLightRamp.a2Cap), s.C2*artLightRamp.aCF, s.H2) {
				t.Errorf("%s/%s a2 out of gamut in LIGHT", b.id, a.id)
			}
		}
	}
}

// TestCustomNeutralFactorsInGamut is what lets this file use FLAT neutral
// constants instead of clamping per hue. Both must sit under the worst-hue
// ceiling, so a future ramp change fails here rather than shipping a clipped
// surface at some base hue nobody happened to try.
func TestCustomNeutralFactorsInGamut(t *testing.T) {
	worst := func(l float64) float64 {
		m := math.Inf(1)
		for h := 0; h < 360; h++ {
			m = math.Min(m, artMaxChroma(l, float64(h)))
		}
		return m
	}
	bgCeil := worst(artLightRamp.lBg0) / artLightRamp.cBg0
	bg1Ceil := worst(artLightRamp.lBg1) / artLightRamp.cBg1
	hiCeil := worst(artDarkRamp.lFg0) / artDarkRamp.cFg0
	t.Logf("worst-hue ceilings: n-cf <= %.3f (light bg-0), %.3f (light bg-1); n-cf-hi <= %.3f (dark text-primary)",
		bgCeil, bg1Ceil, hiCeil)

	if customTintedNCF > bgCeil {
		t.Errorf("customTintedNCF %.3f exceeds the light --bg-color ceiling %.3f", customTintedNCF, bgCeil)
	}
	if customTintedNCF > bg1Ceil {
		t.Errorf("customTintedNCF %.3f exceeds the light --bg-elevated ceiling %.3f", customTintedNCF, bg1Ceil)
	}
	if customNCFHi > hiCeil {
		t.Errorf("customNCFHi %.3f exceeds the dark --text-primary ceiling %.3f", customNCFHi, hiCeil)
	}
}

// TestAccentPairIsNotRotated pins the measured departure from the artwork
// theme. Across the twelve static themes a2-h - a1-h runs -6.1..+8.6 with a
// median of -0.35 -- the static set does not rotate its pair, and a named
// accent should not either. artwork's -30 exists because a COVER supplies a
// genuine second hue.
func TestAccentPairIsNotRotated(t *testing.T) {
	for _, a := range customAccentTable {
		s, _ := customSeed(customDefaultBase, a.id)
		// H2 carries the accent hue and both accent tokens read it, so the
		// invariant is that there is only one accent hue on the seed at all.
		if s.AH != artRound(a.hue, 1) || s.H2 != s.AH {
			t.Errorf("%s accent hue a1=%v a2=%v, table says %v", a.id, s.AH, s.H2, a.hue)
		}
	}
	// The artwork theme, by contrast, DOES rotate -- a regression guard so a
	// future edit cannot collapse the two features into one behaviour.
	as := ArtworkFallbackSeed()
	if math.Abs(as.H2-as.H) < 1 {
		t.Errorf("artwork seed no longer rotates its accent pair (h=%v h2=%v)", as.H, as.H2)
	}
}

// TestNeutralBaseIsAchromatic: "Neutral" is the untinted member of the axis.
// A hue that multiplies by a zero chroma is meaningless, so it must be emitted
// as 0 rather than carried.
func TestNeutralBaseIsAchromatic(t *testing.T) {
	s, known := customSeed("neutral", "teal")
	if !known {
		t.Fatal("neutral/teal should be a known pair")
	}
	if s.NCF != 0 || s.NCFHi != 0 || s.H != 0 {
		t.Errorf("neutral base is not achromatic: n-h %v n-cf %v n-cf-hi %v", s.H, s.NCF, s.NCFHi)
	}
	r, g, b := artOklchToRGB255(artDarkRamp.lBg0, artDarkRamp.cBg0*s.NCF, s.H)
	if r != g || g != b {
		t.Errorf("neutral ground is not grey: rgb(%d,%d,%d)", r, g, b)
	}
	// ...while a tinted base must actually be tinted, or the axis does nothing.
	ts, _ := customSeed("mauve", "teal")
	r2, g2, b2 := artOklchToRGB255(artDarkRamp.lBg0, artDarkRamp.cBg0*ts.NCF, ts.H)
	if r2 == g2 && g2 == b2 {
		t.Errorf("mauve ground is grey: rgb(%d,%d,%d) -- the base axis is a no-op", r2, g2, b2)
	}
	t.Logf("neutral ground rgb(%d,%d,%d)  mauve ground rgb(%d,%d,%d)", r, g, b, r2, g2, b2)
}

// TestBasesAndAccentsAreDistinguishable: 119 combinations are only worth
// having if a user can tell them apart. Every base must paint a different
// ground and every accent a different swatch.
func TestBasesAndAccentsAreDistinguishable(t *testing.T) {
	seenBg := map[string]string{}
	for _, b := range customBaseTable {
		s, _ := customSeed(b.id, customDefaultAccent)
		if prev, dup := seenBg[s.BgDark]; dup {
			t.Errorf("bases %q and %q paint the same ground %s", prev, b.id, s.BgDark)
		}
		seenBg[s.BgDark] = b.id
	}
	seenAcc := map[string]string{}
	for _, a := range customAccentTable {
		s, _ := customSeed(customDefaultBase, a.id)
		if prev, dup := seenAcc[s.Swatch]; dup {
			t.Errorf("accents %q and %q paint the same swatch %s", prev, a.id, s.Swatch)
		}
		seenAcc[s.Swatch] = a.id
	}
	t.Logf("%d distinct grounds, %d distinct accent swatches", len(seenBg), len(seenAcc))
}

// TestVividnessSurvivesTheClamp is the trap THEME-3 recorded and had to fix:
// map the source chroma too aggressively and every entry lands on the clamp,
// so a muted accent becomes indistinguishable from a saturated one and the
// vividness signal is computed and then thrown away.
func TestVividnessSurvivesTheClamp(t *testing.T) {
	teal, _ := customSeed(customDefaultBase, "teal")     // source chroma 0.096, the least saturated
	violet, _ := customSeed(customDefaultBase, "violet") // source chroma 0.270, the most
	if !(violet.C1 > teal.C1+0.02) {
		t.Errorf("vividness collapsed: teal a1-c %.4f vs violet %.4f", teal.C1, violet.C1)
	}
	lo, hi := math.Inf(1), 0.0
	for _, a := range customAccentTable {
		s, _ := customSeed(customDefaultBase, a.id)
		lo, hi = math.Min(lo, s.C1), math.Max(hi, s.C1)
	}
	t.Logf("emitted a1-c spread %.4f .. %.4f across %d accents", lo, hi, len(customAccentTable))
	if hi-lo < 0.05 {
		t.Errorf("emitted chroma spread is only %.4f -- the accents are all the same vividness", hi-lo)
	}
	// ...and nothing may exceed what the static set already ships.
	if hi > customAccentCMax+1e-9 {
		t.Errorf("emitted a1-c %.4f exceeds the app's own maximum %.4f", hi, customAccentCMax)
	}
}

/* ── 3. the bindings the frontend actually calls ─────────────────────────── */

// TestTableAgreesWithSeed: a chip must advertise the colour the seed paints.
// The picker renders 7 + 17 chips from CustomThemeTable and then paints from
// CustomThemeSeed, so a disagreement between the two is a chip that lies.
func TestTableAgreesWithSeed(t *testing.T) {
	app := &App{}
	var tbl struct {
		Bases   []CustomBase   `json:"bases"`
		Accents []CustomAccent `json:"accents"`
		DefBase string         `json:"default_base"`
		DefAcc  string         `json:"default_accent"`
	}
	if err := json.Unmarshal([]byte(app.CustomThemeTable()), &tbl); err != nil {
		t.Fatalf("CustomThemeTable is not valid JSON: %v", err)
	}
	if len(tbl.Bases) != len(customBaseTable) || len(tbl.Accents) != len(customAccentTable) {
		t.Fatalf("table has %d bases / %d accents, want %d / %d",
			len(tbl.Bases), len(tbl.Accents), len(customBaseTable), len(customAccentTable))
	}
	for _, b := range tbl.Bases {
		s, known := customSeed(b.ID, tbl.DefAcc)
		if !known {
			t.Errorf("table advertises base %q that customSeed does not know", b.ID)
		}
		if b.BgDark != s.BgDark || b.BgLight != s.BgLight {
			t.Errorf("base %q chip says %s/%s, seed paints %s/%s",
				b.ID, b.BgDark, b.BgLight, s.BgDark, s.BgLight)
		}
	}
	for _, a := range tbl.Accents {
		s, known := customSeed(tbl.DefBase, a.ID)
		if !known {
			t.Errorf("table advertises accent %q that customSeed does not know", a.ID)
		}
		if a.Swatch != s.Swatch {
			t.Errorf("accent %q chip says %s, seed paints %s", a.ID, a.Swatch, s.Swatch)
		}
		want := artOklchToHex(math.Min(artA1L, artLightRamp.a1Cap), s.C1*artLightRamp.aCF, s.AH)
		if a.SwatchLight != want {
			t.Errorf("accent %q light chip says %s, light mode paints %s", a.ID, a.SwatchLight, want)
		}
	}
}

// TestCustomSeedJSONMatchesArtworkShape is load-bearing: the frontend paints a
// custom seed with paintArtworkSeed() and index.html replays it with the
// artwork validator, so the two payloads must be the same shape. A renamed
// field here would silently stop the theme painting.
func TestCustomSeedJSONMatchesArtworkShape(t *testing.T) {
	app := &App{}
	var custom, artwork map[string]any
	if err := json.Unmarshal([]byte(app.CustomThemeSeed("olive", "amber")), &custom); err != nil {
		t.Fatalf("CustomThemeSeed is not valid JSON: %v", err)
	}
	ab, _ := json.Marshal(map[string]any{"ok": true, "seed": ArtworkFallbackSeed()})
	if err := json.Unmarshal(ab, &artwork); err != nil {
		t.Fatal(err)
	}
	cs, _ := custom["seed"].(map[string]any)
	as, _ := artwork["seed"].(map[string]any)
	if cs == nil || as == nil {
		t.Fatal("one of the payloads has no seed object")
	}
	for k := range as {
		if _, ok := cs[k]; !ok {
			t.Errorf("custom seed is missing field %q that the artwork painter writes", k)
		}
	}
	// Every field the pre-paint validator range-checks must be present and of
	// the right kind, or the replay silently falls through to the CSS block.
	for _, k := range []string{"h", "h2", "c1", "c2", "l1", "l2", "n_cf", "n_cf_hi"} {
		if _, ok := cs[k].(float64); !ok {
			t.Errorf("custom seed field %q is not a number", k)
		}
	}
	for _, k := range []string{"bg_dark", "bg_light"} {
		v, ok := cs[k].(string)
		if !ok || len(v) != 7 || v[0] != '#' {
			t.Errorf("custom seed field %q = %v, want a #rrggbb string", k, cs[k])
		}
	}
	if custom["ok"] != true {
		t.Errorf("CustomThemeSeed did not report ok")
	}
}

// TestUnknownIdsFallBackSoftly: this is a cosmetic setting read from a config
// file a user can hand-edit. A theme that refuses to render is a much worse
// outcome than one that renders the default -- but the caller still has to be
// able to tell the two apart, which is what `known` is for.
func TestUnknownIdsFallBackSoftly(t *testing.T) {
	def, _ := customSeed(customDefaultBase, customDefaultAccent)
	for _, c := range [][2]string{
		{"", ""}, {"nope", "teal"}, {"mist", "nope"}, {"nope", "nope"},
		{"  MIST  ", "TEAL"}, // trimmed + case-folded, so it IS known
	} {
		s, known := customSeed(c[0], c[1])
		if !artInGamut(s.L1, s.C1, s.AH) {
			t.Errorf("fallback for (%q,%q) is out of gamut", c[0], c[1])
		}
		if s.BgDark == "" || s.Swatch == "" {
			t.Errorf("fallback for (%q,%q) produced no colours", c[0], c[1])
		}
		wantKnown := c[0] == "  MIST  "
		if known != wantKnown {
			t.Errorf("customSeed(%q,%q) known=%v, want %v", c[0], c[1], known, wantKnown)
		}
	}
	if s, _ := customSeed("nope", "nope"); s != def {
		t.Errorf("an unknown pair did not fall back to the default seed")
	}
	if s, _ := customSeed("  MIST  ", "TEAL"); s != def {
		t.Errorf("case/whitespace normalisation is broken: %+v vs %+v", s, def)
	}
}

/* ── 4. the stylesheet cannot drift ──────────────────────────────────────── */

// TestCustomFallbackMatchesCSS pins the generated [data-theme="custom"] block
// against this table, the same way TestFallbackSeedMatchesCSS pins artwork's.
// The block is what "custom selected, nothing configured yet" looks like, and
// it is also what paints before the seed is replayed.
func TestCustomFallbackMatchesCSS(t *testing.T) {
	s := CustomFallbackSeed()
	t.Logf("custom fallback (%s x %s): n-h %.1f n-cf %.2f n-cf-hi %.2f a1 %.2f/%.4f/%.1f a2 %.2f/%.4f/%.1f",
		customDefaultBase, customDefaultAccent, s.H, s.NCF, s.NCFHi, s.L1, s.C1, s.AH, s.L2, s.C2, s.H2)
	t.Logf("custom fallback grounds: dark %s light %s swatch %s", s.BgDark, s.BgLight, s.Swatch)

	css, err := os.ReadFile("frontend/src/style.css")
	if err != nil {
		t.Skipf("style.css unreadable: %v", err)
	}
	block := extractCSSBlock(string(css), `:root[data-theme="`+customThemeID+`"]`)
	if block == "" {
		t.Fatalf(`style.css has no :root[data-theme=%q] block`, customThemeID)
	}
	for _, want := range []struct {
		prop string
		val  float64
		dp   int
	}{
		{"--n-h", s.H, 1}, {"--n-cf", s.NCF, 2}, {"--n-cf-hi", s.NCFHi, 2},
		{"--a1-l", s.L1, 2}, {"--a1-c", s.C1, 4}, {"--a1-h", s.AH, 1},
		{"--a2-l", s.L2, 2}, {"--a2-c", s.C2, 4}, {"--a2-h", s.H2, 1},
	} {
		got, ok := cssNumber(block, want.prop)
		if !ok {
			t.Errorf("style.css custom block is missing %s", want.prop)
			continue
		}
		if math.Abs(got-want.val) > math.Pow(10, -float64(want.dp))/2 {
			t.Errorf("style.css %s = %v, table says %v", want.prop, got, want.val)
		}
	}
}

// TestPrePaintGroundMatchesPaintedGround: the pre-paint block writes an inline
// background before the stylesheet loads, and the stylesheet then paints
// --bg-color. If they disagree the launch flashes, which is the entire reason
// the pre-paint block exists.
func TestPrePaintGroundMatchesPaintedGround(t *testing.T) {
	for _, b := range customBaseTable {
		s, _ := customSeed(b.id, customDefaultAccent)
		for _, m := range []artRamp{artDarkRamp, artLightRamp} {
			painted := artOklchToHex(m.lBg0, m.cBg0*s.NCF, s.H)
			pre := s.BgDark
			if m.name == "light" {
				pre = s.BgLight
			}
			if pre != painted {
				t.Errorf("%s %s: pre-paint %s but --bg-color paints %s", b.id, m.name, pre, painted)
			}
		}
	}
}

// TestCustomThemeIsRegisteredInTheFrontend guards the dead-code failure this
// project has shipped before: a perfect Go implementation the UI never calls.
func TestCustomThemeIsRegisteredInTheFrontend(t *testing.T) {
	svelte, err := os.ReadFile("frontend/src/App.svelte")
	if err != nil {
		t.Skipf("App.svelte unreadable: %v", err)
	}
	src := string(svelte)
	for _, needle := range []string{
		"CustomThemeTable(", // the table is fetched
		"CustomThemeSeed(",  // the seed is fetched
		"custom_base",       // and both axes are persisted
		"custom_accent",
		// --a1-h must come from the seed's OWN accent-hue slot. Deriving it
		// from the neutral hue is correct for artwork and silently wrong here:
		// it gave Mauve x Amber a mauve accent and an amber accent-strong.
		"seed.ah",
		// The markup must read the reactive mirrors, not the functions: Svelte
		// cannot see through a call, so a function-call test in the template
		// never re-renders when config changes.
		"customBaseSel", "customAccentSel",
	} {
		if !strings.Contains(src, needle) {
			t.Errorf("App.svelte never references %q -- the picker cannot reach the Go side", needle)
		}
	}
	// The template must reference the reactive mirrors and NOT the functions.
	// Asserting the mirror is merely "present" is too weak -- the $: line alone
	// satisfies it. customBaseId/customAccentId legitimately appear twice each
	// (the definition, and the call inside chooseCustomAxis where $: has not
	// run yet); a THIRD occurrence means one crept back into the markup, where
	// Svelte cannot see through the call and the chips stop re-rendering.
	for _, fn := range []string{"customBaseId()", "customAccentId()"} {
		if n := strings.Count(src, fn); n != 2 {
			t.Errorf("App.svelte calls %s %d times, want 2 (definition + chooseCustomAxis) -- "+
				"a template must use the reactive mirror instead", fn, n)
		}
	}

	html, err := os.ReadFile("frontend/index.html")
	if err != nil {
		t.Skipf("index.html unreadable: %v", err)
	}
	h := string(html)
	// The BG whitelist entry alone is not enough: without the seed replay the
	// launch paints the FALLBACK pair and then repaints the user's, which is
	// the flash the pre-paint block exists to prevent.
	for _, needle := range []string{
		`"` + customThemeID + `":[`,                        // whitelisted in the BG table
		`t === "artwork" || t === "` + customThemeID + `"`, // ...and its seed is replayed
		`"antra:customSeed"`,                               // from its own storage key
	} {
		if !strings.Contains(h, needle) {
			t.Errorf("index.html pre-paint is missing %q -- the custom theme flashes on launch", needle)
		}
	}
}
