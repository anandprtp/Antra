/* ═══════════════════════════════════════════════════════════════════════════
   ANTRA — internationalisation runtime  (v2.0.0 SF-3)

   WHAT THIS IS FOR, AND WHAT IT DELIBERATELY IS NOT
   ------------------------------------------------
   Antra shipped English hardcoded into 10,858 lines of `App.svelte`. The
   expensive, irreversible half of shipping more locales is not the
   translating — it is pulling the strings out of the markup, once, so that every
   later locale is a JSON file and nothing more. That extraction is what this
   module exists to serve.

   ENGLISH IS BUNDLED STATICALLY AND EVERY OTHER LOCALE IS LAZY. That asymmetry
   is deliberate: English is the fallback, and a fallback that can itself fail to
   load is not a fallback. It is imported at the top of this file so it is in the
   bundle before first paint; the rest arrive through `import.meta.glob`, so
   adding a language is dropping a file into `locales/` and adding one row to
   `LOCALES` — no build wiring, no import list to keep in step.

   THE TWO MISSING-KEY CASES ARE NOT THE SAME BUG AND ARE NOT TREATED ALIKE:

     * missing from a translation  →  fall back to English, silently. A partial
       translation is the normal state of every community-translated app, and
       showing a raw dotted key to a French user because one string is untranslated
       would be worse than showing them English.
     * missing from English too    →  return the key and warn. That is not a
       translation gap, it is a hole in the extraction — a string the markup asks
       for that the catalogue never got. It should be loud, and `test_i18n.cjs`
       fails the build on it rather than waiting for someone to notice.

   INTERPOLATION IS SUBSTITUTION INTO AN ALREADY-RESOLVED STRING — never `eval`,
   never `new Function`, and never HTML. Catalogue files are the one place in this
   app that a translator (i.e. someone who is not the maintainer) writes content
   that reaches the DOM, so the value of `{name}` is inserted as a text value by
   Svelte and a locale file cannot introduce markup.

   PLURALS GO THROUGH `Intl.PluralRules`, NOT `n === 1 ? a : b`. English needs two
   forms so the ternary would look fine today and be wrong the moment the first
   Slavic locale lands — Russian and Polish need `one` / `few` / `many` / `other`,
   and a two-branch ternary cannot express that no matter what the JSON says. The
   catalogue carries `key_one` / `key_other` (and `_few` / `_many` where a locale
   needs them) and the runtime asks the platform which one applies.

   This module is DOM-free on purpose, exactly like `palette.ts`, so the test can
   execute it under plain `node` instead of asserting on its source text.
   ═══════════════════════════════════════════════════════════════════════════ */

import { derived, get, writable } from 'svelte/store';
import en from '../locales/en.json';

/** A locale the picker offers. `nativeName` is what that language calls itself —
 *  a French speaker looks for "Français", not for "French". */
export interface LocaleMeta {
  id: string;
  /** Endonym, shown in the picker. */
  nativeName: string;
  /** English name, for the maintainer reading a bug report. */
  englishName: string;
}

/* Only English is listed until a real translation exists. Listing a language we
   cannot serve would be advertising support we do not have — the spec is explicit
   that machine-translating and calling it support is the thing not to do. */
export const LOCALES: LocaleMeta[] = [
  { id: 'en', nativeName: 'English', englishName: 'English' },
];

export const FALLBACK_LOCALE = 'en';

type Catalogue = Record<string, string>;

/** `{ a: { b: 'x' } }` → `{ 'a.b': 'x' }`. The catalogue is authored nested
 *  because that is what a translator can read; it is looked up flat because that
 *  is what a dotted key is. */
export function flatten(obj: unknown, prefix = '', out: Catalogue = {}): Catalogue {
  if (obj === null || typeof obj !== 'object') return out;
  for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === 'object' && !Array.isArray(v)) flatten(v, key, out);
    else if (typeof v === 'string') out[key] = v;
  }
  return out;
}

const EN: Catalogue = flatten(en);

/* Every non-English catalogue, resolved lazily. `eager: false` means none of them
   is in the initial bundle. */
const LAZY: Record<string, () => Promise<unknown>> =
  // `import.meta.glob` is a Vite build-time construct; under plain `node` (the
  // test) it does not exist, so the whole lookup degrades to "English only",
  // which is exactly what the test wants to exercise anyway.
  (() => {
    try {
      // @ts-ignore — Vite-only.
      return import.meta.glob('../locales/*.json');
    } catch {
      return {};
    }
  })();

export const locale = writable<string>(FALLBACK_LOCALE);
const catalogue = writable<Catalogue>(EN);

/** Keys asked for that exist in NO catalogue. Surfaced by the test; warned once
 *  each at runtime so a dev sees it without the console filling up. */
const missing = new Set<string>();
export function missingKeys(): string[] {
  return [...missing];
}

function interpolate(template: string, vars?: Record<string, unknown>): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (whole, name) =>
    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole,
  );
}

function pluralSuffixes(localeId: string, count: number): string[] {
  /* `select()` gives the ONE category that applies; `_other` is always kept as a
     second chance because a catalogue is allowed to define only `_other` for a
     string whose plural never actually varies. */
  try {
    const cat = new Intl.PluralRules(localeId).select(count);
    return cat === 'other' ? ['_other'] : [`_${cat}`, '_other'];
  } catch {
    return [count === 1 ? '_one' : '_other', '_other'];
  }
}

function lookup(dict: Catalogue, localeId: string, key: string, vars?: Record<string, unknown>): string | null {
  const count = vars && typeof vars.count === 'number' ? (vars.count as number) : null;
  if (count !== null) {
    for (const suffix of pluralSuffixes(localeId, count)) {
      const hit = dict[key + suffix];
      if (typeof hit === 'string') return hit;
    }
  }
  const hit = dict[key];
  return typeof hit === 'string' ? hit : null;
}

/** Resolve a key against a specific catalogue. Exported so the test can drive the
 *  resolution rules without standing up a store. */
export function translate(
  dict: Catalogue,
  localeId: string,
  key: string,
  vars?: Record<string, unknown>,
): string {
  const direct = lookup(dict, localeId, key, vars);
  if (direct !== null) return interpolate(direct, vars);

  /* Untranslated string in a real locale: English, quietly. */
  const fallback = lookup(EN, FALLBACK_LOCALE, key, vars);
  if (fallback !== null) return interpolate(fallback, vars);

  /* Not in English either — an extraction hole, not a translation gap. */
  if (!missing.has(key)) {
    missing.add(key);
    try {
      // eslint-disable-next-line no-console
      console.warn(`[i18n] missing key: ${key}`);
    } catch {
      /* ignore */
    }
  }
  return key;
}

/** Reactive translator. Use as `{$t('some.key')}` in markup so a language change
 *  re-renders without a reload. */
export const t = derived([catalogue, locale], ([$dict, $loc]) => {
  return (key: string, vars?: Record<string, unknown>): string => translate($dict, $loc, key, vars);
});

/** Non-reactive translator, for script-side strings that are produced once and
 *  handed to state (an error message, a toast). Those do NOT re-render on a
 *  language change — they were generated in the language that was active when the
 *  event happened, which for a transient message is the honest thing to show. */
export function tr(key: string, vars?: Record<string, unknown>): string {
  return translate(get(catalogue), get(locale), key, vars);
}

/** Pick the best supported locale for a browser/OS language list.
 *  `en-GB` matches `en`; `pt-BR` prefers an exact `pt-BR` over a bare `pt`. */
export function negotiate(preferred: readonly string[], supported: readonly string[]): string {
  const have = new Set(supported);
  for (const raw of preferred) {
    if (!raw) continue;
    const tag = String(raw).replace('_', '-');
    if (have.has(tag)) return tag;
    const lower = tag.toLowerCase();
    const exact = supported.find((s) => s.toLowerCase() === lower);
    if (exact) return exact;
    const base = lower.split('-')[0];
    const baseHit = supported.find((s) => s.toLowerCase().split('-')[0] === base);
    if (baseHit) return baseHit;
  }
  return FALLBACK_LOCALE;
}

/** What the OS says, via the webview. WebView2 / WKWebView / WebKitGTK all report
 *  the system UI language here, so this needs no Go binding on any platform. */
export function systemLocale(): string {
  try {
    const nav = typeof navigator !== 'undefined' ? navigator : null;
    const prefs: string[] = nav
      ? ([] as string[]).concat((nav.languages as string[]) || [], nav.language || [])
      : [];
    return negotiate(prefs, LOCALES.map((l) => l.id));
  } catch {
    return FALLBACK_LOCALE;
  }
}

/** Load and activate a locale. `''` / `'auto'` means "follow the OS".
 *  A locale whose file fails to load leaves the previous one in place rather than
 *  blanking the UI. */
export async function setLocale(id: string | undefined | null): Promise<string> {
  const wanted = !id || id === 'auto' ? systemLocale() : id;
  const chosen = LOCALES.some((l) => l.id === wanted) ? wanted : FALLBACK_LOCALE;

  if (chosen === FALLBACK_LOCALE) {
    catalogue.set(EN);
    locale.set(chosen);
    return chosen;
  }

  const loader = LAZY[`../locales/${chosen}.json`];
  if (!loader) {
    catalogue.set(EN);
    locale.set(FALLBACK_LOCALE);
    return FALLBACK_LOCALE;
  }
  try {
    const mod: any = await loader();
    /* Merged over English so a partial translation shows English for the rest
       instead of raw keys — the fallback is applied at load, not per lookup, so a
       hit is one dictionary read. */
    catalogue.set({ ...EN, ...flatten(mod && mod.default ? mod.default : mod) });
    locale.set(chosen);
    return chosen;
  } catch {
    catalogue.set(EN);
    locale.set(FALLBACK_LOCALE);
    return FALLBACK_LOCALE;
  }
}
