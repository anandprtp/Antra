/*
 * SF-3 (v2.0.0) — internationalisation tests.
 *
 * Two jobs, and the second is the one that matters long-term:
 *
 *   1. drive the REAL `i18n.ts` (compiled with the esbuild Vite already ships,
 *      never re-implemented) and assert on what it returns;
 *   2. cross-check the catalogue against the markup in both directions, because
 *      an extraction is only as good as the guarantee that it stays complete.
 *      `App.svelte` asking for a key the catalogue does not have renders a raw
 *      dotted key to the user, and nothing else in the build catches it.
 *
 * The catalogue↔markup check is the same shape as `test_palette.cjs`'s 1:1
 * catalog↔`data-setting` check, and carries the same honest caveat: it is true
 * while this test is run, not true by construction.
 *
 * Run: node test_i18n.cjs      (from antra-wails/frontend)
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const esbuild = require('esbuild');

const SRC_DIR = path.join(__dirname, 'src');
const APP = path.join(SRC_DIR, 'App.svelte');
// Normalised to LF for scanning only — App.svelte is pure CRLF.
const app = fs.readFileSync(APP, 'utf8').split('\r\n').join('\n');
const EN = JSON.parse(fs.readFileSync(path.join(SRC_DIR, 'locales', 'en.json'), 'utf8'));

let pass = 0;
const failures = [];
function check(label, cond, detail) {
  if (cond) { pass++; return; }
  failures.push(detail ? `${label}\n      ${detail}` : label);
}
function eq(label, got, want) {
  check(label, got === want, `got:  ${JSON.stringify(got)}\n      want: ${JSON.stringify(want)}`);
}
function fatal(msg) { console.error('FATAL: ' + msg); process.exit(1); }

/* ── Load the real module ─────────────────────────────────────────────────
   `i18n.ts` imports `svelte/store` and the English catalogue, and probes
   `import.meta.glob` (a Vite-only construct) behind a try/catch. Under node the
   glob is absent, which is exactly the "English only" path this test wants. */
function loadI18n() {
  const ts = fs.readFileSync(path.join(SRC_DIR, 'lib', 'i18n.ts'), 'utf8');
  const js = esbuild.transformSync(ts, { loader: 'ts', format: 'cjs' }).code;
  const mod = { exports: {} };
  const shimRequire = (spec) => {
    if (spec === 'svelte/store') return require('svelte/store');
    if (spec.endsWith('en.json')) return EN;
    return require(spec);
  };
  const sandbox = { console, module: mod, exports: mod.exports, require: shimRequire };
  vm.createContext(sandbox);
  vm.runInContext(js, sandbox);
  return mod.exports;
}

const I = loadI18n();
if (typeof I.translate !== 'function') fatal('i18n.ts did not load');

const flat = I.flatten(EN);
if (Object.keys(flat).length < 200) fatal('en.json looks empty (' + Object.keys(flat).length + ' keys)');

/* ═══════════════════════════════════════════════════════════════════════════
   1 — RESOLUTION RULES
   ═══════════════════════════════════════════════════════════════════════════ */
console.log('\n1 — resolution');

eq('flatten nests dotted keys',
  I.flatten({ a: { b: 'x' } })['a.b'], 'x');
eq('flatten ignores non-strings',
  I.flatten({ a: 1, b: 'y' })['a'], undefined);

eq('known key resolves to its English value',
  I.translate(flat, 'en', 'common.close'), flat['common.close']);

eq('key missing everywhere returns the key itself',
  I.translate(flat, 'en', 'no.such.key.anywhere'), 'no.such.key.anywhere');
check('…and is reported by missingKeys()',
  I.missingKeys().indexOf('no.such.key.anywhere') !== -1);

// A partial translation must show English, never a raw key.
const partial = { 'common.close': 'Fermer' };
eq('translated key uses the translation',
  I.translate(partial, 'fr', 'common.close'), 'Fermer');
eq('untranslated key falls back to English, not to the key',
  I.translate(partial, 'fr', 'common.browse'), flat['common.browse']);

/* ═══════════════════════════════════════════════════════════════════════════
   2 — INTERPOLATION
   ═══════════════════════════════════════════════════════════════════════════ */
console.log('2 — interpolation');

const D = { 'x.hi': 'Hello {name}!', 'x.two': '{a} and {b}', 'x.none': 'plain' };
eq('substitutes a named placeholder',
  I.translate(D, 'en', 'x.hi', { name: 'Ada' }), 'Hello Ada!');
eq('substitutes several',
  I.translate(D, 'en', 'x.two', { a: '1', b: '2' }), '1 and 2');
eq('leaves an unsupplied placeholder visible rather than blanking it',
  I.translate(D, 'en', 'x.hi', {}), 'Hello {name}!');
eq('a string with no placeholder is untouched',
  I.translate(D, 'en', 'x.none', { name: 'x' }), 'plain');
eq('numbers stringify',
  I.translate(D, 'en', 'x.hi', { name: 7 }), 'Hello 7!');

// The catalogue is the one place a non-maintainer writes content that reaches
// the DOM. Substitution must be substitution — not evaluation.
const EVIL = { 'x.e': 'v={v}' };
eq('a placeholder value is inserted literally, never evaluated',
  I.translate(EVIL, 'en', 'x.e', { v: '${1+1}' }), 'v=${1+1}');
eq('a catalogue value containing a template literal is inert',
  I.translate({ 'x.t': '${process}' }, 'en', 'x.t'), '${process}');

/* ═══════════════════════════════════════════════════════════════════════════
   3 — PLURALS  (Intl.PluralRules, not n === 1)
   ═══════════════════════════════════════════════════════════════════════════ */
console.log('3 — plurals');

const P = { 'p.t_one': '{count} track', 'p.t_other': '{count} tracks' };
eq('count 1 selects _one', I.translate(P, 'en', 'p.t', { count: 1 }), '1 track');
eq('count 2 selects _other', I.translate(P, 'en', 'p.t', { count: 2 }), '2 tracks');
eq('count 0 selects _other in English', I.translate(P, 'en', 'p.t', { count: 0 }), '0 tracks');

eq('missing _one degrades to _other rather than to the key',
  I.translate({ 'p.t_other': '{count} tracks' }, 'en', 'p.t', { count: 1 }), '1 tracks');

// The reason this goes through Intl at all: a two-branch ternary cannot express
// Russian, and would look correct in English right up until the first Slavic
// locale arrived.
const RU = { 'p.t_one': '{count} трек', 'p.t_few': '{count} трека', 'p.t_many': '{count} треков' };
eq('ru count 2 selects _few', I.translate(RU, 'ru', 'p.t', { count: 2 }), '2 трека');
eq('ru count 5 selects _many', I.translate(RU, 'ru', 'p.t', { count: 5 }), '5 треков');
eq('ru count 1 selects _one', I.translate(RU, 'ru', 'p.t', { count: 1 }), '1 трек');

eq('a non-count var does not trigger plural lookup',
  I.translate({ 'p.x': 'hi {name}' }, 'en', 'p.x', { name: 'a' }), 'hi a');

/* ═══════════════════════════════════════════════════════════════════════════
   4 — LOCALE NEGOTIATION
   ═══════════════════════════════════════════════════════════════════════════ */
console.log('4 — negotiation');

eq('exact match wins', I.negotiate(['fr'], ['en', 'fr']), 'fr');
// Deliberately NOT `['en-GB'] -> 'en'`: that answer is also what the final
// fallback returns, so it passes whether or not base-matching works at all. The
// negative control caught exactly that. A non-English target is what makes the
// base-language step observable.
eq('region variant falls back to its base language',
  I.negotiate(['fr-CA'], ['en', 'fr']), 'fr');
eq('region variant of an unsupported language still falls back to English',
  I.negotiate(['en-GB'], ['en']), 'en');
eq('underscore form is accepted', I.negotiate(['pt_BR'], ['en', 'pt-BR']), 'pt-BR');
eq('exact regional beats base when both exist',
  I.negotiate(['pt-BR'], ['pt', 'pt-BR']), 'pt-BR');
eq('first supported entry in the preference list wins',
  I.negotiate(['zz', 'fr', 'de'], ['en', 'de', 'fr']), 'fr');
eq('nothing supported falls back to English', I.negotiate(['zz'], ['en']), 'en');
eq('empty preference list falls back to English', I.negotiate([], ['en']), 'en');
check('LOCALES advertises only languages that exist',
  I.LOCALES.every((l) => l.id === 'en' || fs.existsSync(path.join(SRC_DIR, 'locales', l.id + '.json'))),
  'a locale is offered in the picker with no catalogue file behind it');

/* ═══════════════════════════════════════════════════════════════════════════
   5 — CATALOGUE ↔ MARKUP, BOTH DIRECTIONS
   ═══════════════════════════════════════════════════════════════════════════ */
console.log('5 — catalogue vs markup');

const used = new Set();
for (const m of app.matchAll(/\$?\bt(?:r)?\(\s*'([a-z0-9_]+(?:\.[a-z0-9_]+)+)'/g)) used.add(m[1]);

/* SF-3 script side: the option tables (THEMES, FONTS, SOUNDS, TAG_GROUPS,
   formatOptions …) and `lib/settings-catalog.ts` store the catalogue KEY in the
   slot that used to hold English, and it is resolved at the render site with
   `$t(f.label)` or at palette-registration time with `tr(s.label)`. The key is
   still a literal in the source, so it is still checkable — it is simply not
   written inside the call.

   Only strings whose FIRST segment is a real top-level namespace of en.json are
   collected, so an ordinary dotted string (a filename, a CSS value) is not
   mistaken for a key. A typo in the LEAF still fails the "key exists" check
   below, which is the direction that matters. */
const NAMESPACES = new Set(Object.keys(EN));
const CATALOG_TS = fs.readFileSync(
  path.join(SRC_DIR, 'lib', 'settings-catalog.ts'), 'utf8');
for (const src of [app, CATALOG_TS]) {
  for (const m of src.matchAll(/'([a-z0-9_]+(?:\.[a-z0-9_]+)+)'/g)) {
    if (NAMESPACES.has(m[1].split('.')[0])) used.add(m[1]);
  }
}
check('markup actually uses the catalogue', used.size > 200, `only ${used.size} keys used`);

/* A plural string is asked for by its BASE key and stored under `_one` / `_other`
   (and `_few` / `_many` where a locale needs them), so both directions have to
   understand the suffix — otherwise every plural reads as a missing key in one
   direction and an orphan in the other. */
const PLURAL_SUFFIXES = ['_zero', '_one', '_two', '_few', '_many', '_other'];
const hasKey = (k) => k in flat || PLURAL_SUFFIXES.some((sfx) => k + sfx in flat);
const baseOf = (k) => {
  const sfx = PLURAL_SUFFIXES.find((s) => k.endsWith(s));
  return sfx ? k.slice(0, -sfx.length) : k;
};

const unknown = [...used].filter((k) => !hasKey(k));
check('every key App.svelte asks for exists in en.json', unknown.length === 0,
  unknown.slice(0, 10).join(', '));

const orphans = Object.keys(flat).filter((k) => !used.has(k) && !used.has(baseOf(k)));
check('every key in en.json is used by App.svelte', orphans.length === 0,
  `${orphans.length} orphan(s): ` + orphans.slice(0, 10).join(', '));

/* A plural key that defines only `_one` would fall through to the base key and
   then to nothing for count 2 — `_other` is the form every locale must have. */
/* A slug can end with the word "one" by accident (`…_one_by_one`), so a suffix
   alone does not make a key plural. What does is the markup asking for the BASE:
   that is the only reason a `_one` variant exists at all. */
const pluralBases = new Set(
  Object.keys(flat)
    .filter((k) => PLURAL_SUFFIXES.some((s) => k.endsWith(s)) && used.has(baseOf(k)))
    .map(baseOf),
);
const missingOther = [...pluralBases].filter((b) => !(b + '_other' in flat));
check('every plural key defines _other', missingOther.length === 0,
  missingOther.join(', '));

/* ═══════════════════════════════════════════════════════════════════════════
   6 — CATALOGUE HYGIENE
   ═══════════════════════════════════════════════════════════════════════════ */
console.log('6 — catalogue hygiene');

const vals = Object.entries(flat);
check('no value carries a newline (unreadable to a translator)',
  vals.every(([, v]) => v.indexOf('\n') === -1),
  vals.filter(([, v]) => v.indexOf('\n') !== -1).slice(0, 3).map(([k]) => k).join(', '));

// `{$t(...)}` renders a TEXT value, so an entity left in the catalogue would be
// shown literally as `&ldquo;` instead of as a quote mark.
const ents = vals.filter(([, v]) => /&(?:[a-zA-Z]+|#\d+);/.test(v));
check('no value carries an undecoded HTML entity', ents.length === 0,
  ents.slice(0, 3).map(([k, v]) => `${k}=${v}`).join(' | '));

check('no empty value', vals.every(([, v]) => v.trim().length > 0));
check('no value is only whitespace-padded',
  vals.every(([, v]) => v === v.trim()));

/* Any locale added later must not invent keys English does not have — that is a
   typo, and it silently renders English forever. */
const localeDir = path.join(SRC_DIR, 'locales');
for (const f of fs.readdirSync(localeDir)) {
  if (f === 'en.json' || !f.endsWith('.json')) continue;
  const other = I.flatten(JSON.parse(fs.readFileSync(path.join(localeDir, f), 'utf8')));
  const extra = Object.keys(other).filter((k) => !(k in flat));
  check(`${f} introduces no key absent from en.json`, extra.length === 0,
    extra.slice(0, 5).join(', '));
}

/* ═══════════════════════════════════════════════════════════════════════════
   7 — COVERAGE BUDGET
   ═══════════════════════════════════════════════════════════════════════════
   A proxy, and labelled as one: a conservative regex over the markup region,
   not the full tokenizer the extractor used. Because `[^<>{}]` excludes braces,
   it counts only text nodes with NO adjacent interpolation — i.e. the ones that
   were mechanically extractable and were left behind anyway (almost all of them
   text sitting inside an inline `<strong>`/`<code>` element, which the extractor
   held back as fragment-prone). Sentences split around a `{expr}` are a separate,
   larger set that this number deliberately does not claim to measure.

   Its job is to stop that count from GROWING as new UI lands, so "how much is
   left" is a tracked number rather than a claim. Lower it when a batch is
   extracted; never raise it to make the test pass. */
const MARKUP = app.slice(app.indexOf('\n', app.indexOf('</script>')) + 1, app.indexOf('\n<style>'));

/* Text nodes. Budget 1: the single survivor is `esc` on the palette key-cap,
   which is a key name rather than a word. */
const REMAINING_BUDGET = 1;
const leftovers = [...MARKUP.matchAll(/>([^<>{}]*[A-Za-z]{2}[^<>{}]*)</g)]
  .map((m) => m[1].trim())
  .filter((s) => s.length > 1);
check(`hardcoded markup text nodes within budget (${leftovers.length} <= ${REMAINING_BUDGET})`,
  leftovers.length <= REMAINING_BUDGET,
  'first few: ' + JSON.stringify(leftovers.slice(0, 6)));

/* English sitting INSIDE an expression — `title={cond ? 'Some text' : ''}`. This
   class was invisible to the original extraction (the tokenizer treats `{...}` as
   opaque) and is where tooltips live, so it gets its own budget. Counted only for
   quoted literals of 2+ words that are not CSS, a URL, or a filename template.

   A flat regex over the markup is useless here — it matches CSS class names and
   halves of adjacent attributes. So the {...} groups are collected properly first
   (balanced, nesting-aware) and only their contents are scanned. */
function expressionGroups(src) {
  const out = [];
  for (let i = 0; i < src.length; i++) {
    if (src[i] !== '{') continue;
    let d = 1;
    let j = i + 1;
    while (j < src.length && d) {
      if (src[j] === '{') d++;
      else if (src[j] === '}') d--;
      j++;
    }
    out.push(src.slice(i, j));
    i = j - 1;
  }
  return out;
}

/* Budget 7, and every one of them is accounted for rather than merely tolerated:
     * "Liked Songs"  x2 — the `name:` a tracked playlist is STORED under in
       config. Translating it would stop it matching entries already on disk.
     * "Apple Music"  x2 — brand name.
     * "+ .png"           — a literal filename suffix shown in a preview.
     * "; searchQuery =" and ").localeCompare(b.name ??" — scanner artifacts:
       code containing an apostrophe that the literal regex mis-pairs.
   Lower this when something real is extracted; never raise it to pass. */
const EXPR_BUDGET = 7;
const exprLits = [];
for (const g of expressionGroups(MARKUP)) {
  // drop the $t('key') calls themselves before looking for leftovers
  const body = g.replace(/\$?t\(\s*'[a-z0-9_.]+'/g, '');
  for (const m of body.matchAll(/'([^'\n]{4,})'|"([^"\n]{4,})"|`([^`\n]{4,})`/g)) {
    const v = (m[1] || m[2] || m[3]).trim();
    if (!/[A-Za-z]{3}/.test(v)) continue;
    if (v.split(/\s+/).length < 2) continue;                 // single tokens: ids, classes
    if (/^[a-z-]+\s*:|linear-gradient|^var\(|rgba?\(|^#|^https?:/.test(v)) continue;  // css / urls
    if (/^\{[a-z_]+\}/.test(v)) continue;                    // filename-template syntax
    if (/\$\{/.test(v)) continue;                            // pure interpolation glue
    exprLits.push(v);
  }
}
check(`English literals inside markup expressions within budget (${exprLits.length} <= ${EXPR_BUDGET})`,
  exprLits.length <= EXPR_BUDGET,
  'first few: ' + JSON.stringify([...new Set(exprLits)].slice(0, 8)));

/* English left in the `<script>` block. Same purpose as the two budgets above,
   for the third class SF-3 had to chase: strings assigned to component state or
   built inside a helper, which neither markup scan can see.

   `addLog(...)` arguments are masked out first and are NOT counted: log lines
   end up in the FR-4 export and in bug reports, and stay English with the
   Python log lines. So does `console.*`, for the same reason. */
const SCRIPT = app.slice(0, app.indexOf('</script>'));

function maskCalls(src, name) {
  let out = src;
  const re = new RegExp('\\b' + name + '\\s*\\(', 'g');
  let m;
  while ((m = re.exec(out)) !== null) {
    let d = 0;
    let j = m.index + m[0].length - 1;
    for (; j < out.length; j++) {
      if (out[j] === '(') d++;
      else if (out[j] === ')') { d--; if (!d) break; }
    }
    out = out.slice(0, m.index) + ' '.repeat(j - m.index + 1) + out.slice(j + 1);
    re.lastIndex = m.index;
  }
  return out;
}

/* Comments first: a doc block is prose, and the literal regex cannot tell it
   from code. Only BLOCK comments and whole-line // comments are stripped, so
   a '//' inside a string cannot truncate a real line. */
let scriptBody = SCRIPT.replace(/\/\*[\s\S]*?\*\//g, ' ')
                       .replace(/^[ 	]*\/\/.*$/gm, ' ');
scriptBody = maskCalls(scriptBody, 'addLog');
scriptBody = maskCalls(scriptBody, 'console\\.[a-z]+');

/* Budget 26, and it is an INVENTORY rather than a tolerance — every survivor is
   named here:
     * brand names in the theme / source tables: `Apple Music` x3, `Amazon Music`;
     * CSS: one `(prefers-color-scheme: light)` media query, the three font
       stacks in FONTS, and the `input, select, textarea, button` focus selector;
     * TEMPLATE_DEMO's seven sample values (`Come Together`, `The Beatles` x4,
       `Abbey Road`, `Apple Corps`, `Road Trip`) — demo DATA for the filename
       preview, not UI copy;
     * the five column labels of the end-of-run rundown, which is written into
       the activity LOG and stays English with the Python log lines;
     * `HTTP Error`, matched against a backend payload rather than shown;
     * four palette KEYWORDS (`bug report`, `new releases`, `fake lossless`,
       `log out`). Labels and descriptions are translated; the keyword lists are
       extra English search aliases and cost a non-English user nothing.
   Lower this when something real is extracted; never raise it to pass. */
const SCRIPT_BUDGET = 26;

/* A single left-to-right walk, tracking quote state and escapes. A regex here
   pairs the CLOSING quote of one literal with the OPENING quote of the next
   whenever what sits between them is shorter than the minimum length, which is
   where `", label:"` and `", desc:"` came from on the first run of this budget. */
function stringLiterals(src) {
  const out = [];
  for (let i = 0; i < src.length; i++) {
    const q = src[i];
    if (q !== "'" && q !== '"' && q !== '`') continue;
    let j = i + 1;
    let buf = '';
    while (j < src.length) {
      if (src[j] === '\\') { buf += src[j + 1] || ''; j += 2; continue; }
      if (src[j] === q) break;
      if (q !== '`' && src[j] === '\n') { j = -1; break; }   // unterminated: not a literal
      buf += src[j];
      j++;
    }
    if (j < 0 || j >= src.length) continue;
    out.push(buf);
    i = j;
  }
  return out;
}

const scriptLits = [];
for (const raw of stringLiterals(scriptBody)) {
  const v = raw.trim();
  if (v.length < 4) continue;
  if (!/[A-Za-z]{3}/.test(v)) continue;
  if (v.split(/\s+/).length < 2) continue;
  if (/^[a-z-]+\s*:|linear-gradient|^var\(|rgba?\(|^#|^https?:/.test(v)) continue;
  if (/^\{[a-z_]+\}/.test(v)) continue;
  if (/^[a-z0-9_]+(\.[a-z0-9_]+)+$/.test(v)) continue;              // catalogue keys
  if (/\$\{/.test(v)) continue;                                    // interpolation glue
  scriptLits.push(v);
}
check(`English literals left in the script block within budget (${scriptLits.length} <= ${SCRIPT_BUDGET})`,
  scriptLits.length <= SCRIPT_BUDGET,
  'first few: ' + JSON.stringify([...new Set(scriptLits)].slice(0, 10)));

/* ═══════════════════════════════════════════════════════════════════════════ */
console.log('\n' + '─'.repeat(70));
if (failures.length) {
  console.log(`FAIL — ${pass} passed, ${failures.length} failed\n`);
  failures.forEach((f, i) => console.log(`  ${i + 1}. ${f}`));
  process.exit(1);
}
console.log(`ALL PASS — ${pass} assertions`);
console.log(`   catalogue: ${Object.keys(flat).length} keys · markup uses ${used.size}`);
console.log(`   hardcoded markup strings remaining (proxy count): ${leftovers.length}`);
