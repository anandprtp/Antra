package main

import (
	"encoding/json"
	"math"
	"strings"
)

/*
Custom themes — base neutral x accent (v2.0.0 THEME-4)
======================================================

Separating 7 base neutrals from 17 accents gives 119 combinations. Writing
those out as full token blocks would mean 7 bases x 2 modes x 18 tokens +
17 accents x 2 x 2 = 320 hand-written values. That is precisely the pattern
THEME-1 replaced.

Here the two axes are what they already were in the token system:

    base neutral  ->  --n-h, --n-cf, --n-cf-hi     (the tint of every surface)
    accent        ->  --a1-l/c/h, --a2-l/c/h       (the accent pair)

so a custom theme is EXACTLY the nine-slot seed a static theme declares, which
is EXACTLY what ArtworkSeed already is. The seed is therefore reused verbatim:
the frontend paints it with the same painter, index.html replays it with the
same validator, and no new CSS exists anywhere. That reuse is the point --
THEME-4's own note is that once THEME-1 landed this is "nearly free".

WHY THIS FILE IS IN GO AND NOT IN THE FRONTEND
----------------------------------------------
The gamut clamping, the OKLCH conversions and the hex emitters all live in
artwork_theme.go. A JS copy would be a second implementation of the same maths
free to drift from the first -- the duplicated-helper trap this project has
recorded three times. Everything below calls into that file.

WHAT WAS MEASURED, AND WHAT IT CHANGED
--------------------------------------
Every constant is derived from THIS app's own twelve static themes, not carried
over from the reference palette the hue tables came from, which sits at a
different lightness and a much subtler tint (its base grounds are chroma
0.004-0.008; Antra's own run up to 0.027). Importing its numbers would have
produced 119 themes that all look grey, which defeats the axis.

  * a2-h = a1-h. Measured across the twelve static themes, a2-h - a1-h runs
    -6.1 to +8.6 degrees with a MEDIAN of -0.35 -- i.e. the static set does not
    rotate its accent pair at all. This is a deliberate departure from the
    artwork theme's -30, which exists there because a cover supplies a genuine
    second hue; a named accent does not, and "Violet" should not quietly become
    violet-and-blue.

  * Accent lightness is FIXED at artA1L / artA2L for all 17. Holding L fixed
    while swapping H is contrast-neutral in OKLCH, which is what makes contrast
    a property of the ramp rather than something re-tuned per combination --
    the central claim of THEME-1. Measured over all 119 x 2 modes the accent
    lands at 6.76-8.13 dark and 5.07-5.86 light, inside the static envelope.

  * Tinted bases all share ONE neutral chroma factor (Antra's own median,
    1.18). The reference palette varies tint strength per base, but that is an
    artifact of it rather than a signal: a user switching Stone -> Zinc expects the
    hue to change, not the intensity. "Neutral" is the untinted member and is
    the only one that differs, at 0.

  * n-cf 1.18 and n-cf-hi 0.40 are both under their WORST-HUE gamut ceilings
    (1.351 and 0.417 respectively, measured over all 360 hues), so a flat
    constant is provably safe at every base hue and no per-hue clamping is
    needed for the neutral. The accent DOES need it -- at artA1L the sRGB
    ceiling runs 0.126 (teal, h~186) to 0.264 (fuchsia, h~324), so a flat
    chroma would clip channels and distort the hue on most of the wheel.
*/

// customTintedNCF is the neutral chroma factor every tinted base uses.
// Antra's twelve static themes run 0.000-1.517 with a median of 1.177; 1.18 is
// that median, and it sits under the 1.351 worst-hue ceiling imposed by light
// --bg-color (oklch(0.974, 0.009*n-cf, h), and L=0.974 admits at most 0.01216
// chroma at its worst hue). Asserted by TestCustomNeutralFactorsInGamut.
const customTintedNCF = 1.18

// customNCFHi is the near-white tier, clipped separately for the same reason
// the six tinted static themes clip theirs and the artwork theme clips its own:
// dark --text-primary is oklch(0.983, 0.019*n-cf-hi, h) and L=0.983 admits at
// most 0.00793 chroma, so n-cf-hi <= 0.417 for a hue not known in advance.
const customNCFHi = artNCFHi

// customAccentCMax caps a requested accent chroma at the most saturated value
// this app's own themes already use (deezer, a1-c 0.2441). Beyond the gamut
// clamp, this is what stops a custom theme being more vivid than anything the
// static set ships. It binds for exactly one accent (fuchsia, whose gamut
// ceiling of 0.2639 is higher).
const customAccentCMax = 0.2441

const customThemeID = "custom"

// CustomBase is one entry on the neutral axis: the hue every surface, border
// and neutral text token is tinted toward.
type CustomBase struct {
	ID    string  `json:"id"`
	Label string  `json:"label"`
	Hue   float64 `json:"hue"`
	NCF   float64 `json:"ncf"`
	// Preview grounds, so the picker chip is the colour the user would get.
	BgDark  string `json:"bg_dark"`
	BgLight string `json:"bg_light"`
}

// CustomAccent is one entry on the accent axis.
type CustomAccent struct {
	ID    string  `json:"id"`
	Label string  `json:"label"`
	Hue   float64 `json:"hue"`
	SrcC  float64 `json:"src_c"`
	// Swatch is the accent as it renders in dark mode, after gamut clamping.
	Swatch      string `json:"swatch"`
	SwatchLight string `json:"swatch_light"`
}

// The hue tables are the familiar neutral and accent colour families (their
// `background` and `primary` values, read in OKLCH). Only hue and RELATIVE chroma are
// taken -- the lightness and tint strength are this app's, for the reasons in
// the header. The order is the order the picker renders.
var customBaseTable = []struct {
	id, label string
	hue, ncf  float64
}{
	// "Neutral" is genuinely achromatic: hue is meaningless at chroma 0, and 0
	// is what makes it the untinted member of the axis rather than a seventh
	// tint. graphite/spotify already sit here among the static themes.
	{"neutral", "Neutral", 0, 0},
	{"stone", "Stone", 56.3, customTintedNCF},
	{"taupe", "Taupe", 41.2, customTintedNCF},
	{"olive", "Olive", 106.9, customTintedNCF},
	{"mist", "Mist", 214.4, customTintedNCF},
	{"zinc", "Zinc", 286.1, customTintedNCF},
	{"mauve", "Mauve", 323.0, customTintedNCF},
}

var customAccentTable = []struct {
	id, label string
	hue, c    float64
}{
	{"red", "Red", 27.518, 0.213},
	{"rose", "Rose", 16.935, 0.222},
	{"pink", "Pink", 3.958, 0.223},
	{"fuchsia", "Fuchsia", 323.949, 0.253},
	{"purple", "Purple", 301.924, 0.265},
	{"violet", "Violet", 292.581, 0.270},
	{"indigo", "Indigo", 277.023, 0.240},
	{"blue", "Blue", 264.376, 0.243},
	{"sky", "Sky", 242.749, 0.134},
	{"cyan", "Cyan", 223.128, 0.105},
	{"teal", "Teal", 186.391, 0.096},
	{"emerald", "Emerald", 165.612, 0.118},
	{"green", "Green", 150.069, 0.154},
	{"lime", "Lime", 128.850, 0.238},
	{"yellow", "Yellow", 91.936, 0.199},
	{"amber", "Amber", 48.998, 0.163},
	{"orange", "Orange", 38.402, 0.195},
}

// customDefaultBase / customDefaultAccent are what an unconfigured custom theme
// resolves to, and are what style.css declares under [data-theme="custom"].
// Chosen to read as Antra rather than as an arbitrary pick: teal is the closest
// accent to the default theme's aqua, and Mist is the coolest neutral.
const (
	customDefaultBase   = "mist"
	customDefaultAccent = "teal"
)

func customFindBase(id string) (float64, float64, string, bool) {
	id = strings.ToLower(strings.TrimSpace(id))
	for _, b := range customBaseTable {
		if b.id == id {
			return b.hue, b.ncf, b.label, true
		}
	}
	return 0, 0, "", false
}

func customFindAccent(id string) (float64, float64, string, bool) {
	id = strings.ToLower(strings.TrimSpace(id))
	for _, a := range customAccentTable {
		if a.id == id {
			return a.hue, a.c, a.label, true
		}
	}
	return 0, 0, "", false
}

// customSeed builds the nine-slot seed for a (base, accent) pair.
//
// An unknown id falls back to the default rather than failing: this is a
// cosmetic setting read from a config file that a user can hand-edit, and a
// theme that refuses to render is a far worse outcome than one that renders the
// default. The second return reports whether both ids were recognised, so the
// caller can tell a deliberate choice from a fallback.
func customSeed(baseID, accentID string) (ArtworkSeed, bool) {
	nh, ncf, _, okB := customFindBase(baseID)
	if !okB {
		nh, ncf, _, _ = customFindBase(customDefaultBase)
	}
	ah, ac, _, okA := customFindAccent(accentID)
	if !okA {
		ah, ac, _, _ = customFindAccent(customDefaultAccent)
	}

	// Hue is rounded FIRST and the chroma clamped at the hue actually emitted,
	// not at the one requested -- a hue nudged by a fraction of a degree moves
	// the gamut ceiling slightly. Chroma is then TRUNCATED rather than rounded,
	// because rounding a gamut-clamped value up puts it back outside sRGB;
	// that is how THEME-3's first cut failed its own all-hue sweep on ~90 of
	// 360 hues.
	ah = artRound(ah, 1)
	nh = artRound(nh, 1)

	c1 := artClampChroma(math.Min(ac, customAccentCMax), artA1L,
		artLightRamp.a1Cap, artLightRamp.aCF, ah)
	c1 = artFloorTo(c1, 4)
	c2 := artClampChroma(math.Min(c1*artA2CRatio, customAccentCMax), artA2L,
		artLightRamp.a2Cap, artLightRamp.aCF, ah)
	c2 = artFloorTo(c2, 4)

	// A base with no tint has no meaningful hue; pin it to 0 so the emitted
	// seed is unambiguous rather than carrying a hue that multiplies by zero.
	if ncf == 0 {
		nh = 0
	}
	ncfHi := customNCFHi
	if ncf == 0 {
		ncfHi = 0
	}

	return ArtworkSeed{
		// The two axes are independent, so the neutral hue and the accent hue
		// are different slots. This is the case that made ArtworkSeed carry an
		// explicit AH: the painter used to derive --a1-h from H, which silently
		// gave Mauve x Amber a mauve accent and an amber accent-strong.
		H: nh, AH: ah, H2: ah, C1: c1, C2: c2,
		L1: artA1L, L2: artA2L,
		NCF: ncf, NCFHi: ncfHi,
		Swatch: artOklchToHex(artA1L, c1, ah),
		// The pre-paint block in index.html has to paint the ground before the
		// stylesheet loads, and a colour assembled at runtime cannot live in
		// that file's static table -- so it is carried on the seed, exactly as
		// the artwork theme carries its own.
		BgDark:      artOklchToHex(artDarkRamp.lBg0, artDarkRamp.cBg0*ncf, nh),
		BgLight:     artOklchToHex(artLightRamp.lBg0, artLightRamp.cBg0*ncf, nh),
		BgElevDark:  artOklchToHex(artDarkRamp.lBg1, artDarkRamp.cBg1*ncf, nh),
		BgElevLight: artOklchToHex(artLightRamp.lBg1, artLightRamp.cBg1*ncf, nh),
		ArtC:        c1,
	}, okB && okA
}

// CustomFallbackSeed is what style.css declares under [data-theme="custom"].
// TestCustomFallbackMatchesCSS asserts the stylesheet still agrees with it, so
// the CSS block cannot drift away from this table.
func CustomFallbackSeed() ArtworkSeed {
	s, _ := customSeed(customDefaultBase, customDefaultAccent)
	return s
}

/* ── Wails bindings ──────────────────────────────────────────────────────── */

// CustomThemeTable returns both axes with their preview colours, so the picker
// renders 7 + 17 = 24 chips rather than 119 cards. The two axes are
// independent -- a base chip's colour depends only on the base and an accent
// chip's only on the accent -- which is the whole reason a two-axis picker is
// worth having.
func (a *App) CustomThemeTable() string {
	bases := make([]CustomBase, 0, len(customBaseTable))
	for _, b := range customBaseTable {
		hue := b.hue
		if b.ncf == 0 {
			hue = 0
		}
		bases = append(bases, CustomBase{
			ID: b.id, Label: b.label, Hue: hue, NCF: b.ncf,
			BgDark:  artOklchToHex(artDarkRamp.lBg0, artDarkRamp.cBg0*b.ncf, hue),
			BgLight: artOklchToHex(artLightRamp.lBg0, artLightRamp.cBg0*b.ncf, hue),
		})
	}
	accents := make([]CustomAccent, 0, len(customAccentTable))
	for _, ac := range customAccentTable {
		s, _ := customSeed(customDefaultBase, ac.id)
		accents = append(accents, CustomAccent{
			ID: ac.id, Label: ac.label, Hue: artRound(ac.hue, 1), SrcC: ac.c,
			Swatch: s.Swatch,
			// The light swatch has to go through the mode scalars, or the chip
			// would advertise a colour light mode never actually paints.
			SwatchLight: artOklchToHex(
				math.Min(artA1L, artLightRamp.a1Cap), s.C1*artLightRamp.aCF, s.H2),
		})
	}
	b, _ := json.Marshal(map[string]any{
		"bases": bases, "accents": accents,
		"default_base": customDefaultBase, "default_accent": customDefaultAccent,
	})
	return string(b)
}

// CustomThemeSeed returns the seed for one (base, accent) pair as JSON, in the
// same shape ExtractArtworkSeed returns, so the frontend paints it with the
// same painter and stores it with the same validator.
func (a *App) CustomThemeSeed(baseID, accentID string) string {
	seed, known := customSeed(baseID, accentID)
	b, err := json.Marshal(map[string]any{"ok": true, "seed": seed, "known": known})
	if err != nil {
		e, _ := json.Marshal(map[string]any{"ok": false, "error": err.Error()})
		return string(e)
	}
	return string(b)
}
