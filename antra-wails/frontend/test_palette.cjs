/*
 * PORT-1 (v2.0.0) — command palette tests.
 *
 * Everything under test is either IMPORTED or EXTRACTED FROM the shipped files
 * and executed; nothing is re-implemented here. A re-implementation drifts from
 * the real code and passes while the app is broken — this repo has shipped that
 * exact failure before (a "perfect method that is never called correctly").
 *
 * Run: node test_palette.cjs      (from antra-wails/frontend)
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const esbuild = require('esbuild');

const SRC_DIR = path.join(__dirname, 'src');
const APP = path.join(SRC_DIR, 'App.svelte');
// Normalised to LF for scanning only: App.svelte is pure CRLF and every
// multi-line anchor below would otherwise match zero times.
const app = fs.readFileSync(APP, 'utf8').split('\r\n').join('\n');

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

/* ── Load the two real modules ─────────────────────────────────────────────
   Compiled with the esbuild Vite already ships, so the SHIPPED source runs. */
function loadModule(file) {
  const ts = fs.readFileSync(path.join(SRC_DIR, 'lib', file), 'utf8');
  const js = esbuild.transformSync(ts, { loader: 'ts', format: 'cjs' }).code;
  const mod = { exports: {} };
  const sandbox = { console, module: mod, exports: mod.exports, require };
  vm.createContext(sandbox);
  vm.runInContext(js, sandbox);
  return mod.exports;
}

const P = loadModule('palette.ts');
const CATALOG = loadModule('settings-catalog.ts').SETTINGS_CATALOG;

if (!Array.isArray(CATALOG) || CATALOG.length < 20) fatal('settings catalog did not load');

/* ═════════════════════════════════════════════════════════════════════════
   1. MATCHER — the two rules that were paid for by measurement on the web
      build and would be silently re-broken by a naive rewrite.
   ═══════════════════════════════════════════════════════════════════════ */
eq('norm folds separators to spaces', P.norm('Auto-Sync/Scheduled'), 'auto sync scheduled');
eq('norm collapses runs', P.norm('  a   b  '), 'a b');

// Separator folding: without it the hyphen forces the query onto the
// subsequence path, where a long description beats the short label.
eq('hyphenated label matches as a PREFIX once folded',
   P.scoreField('auto sync', 'Auto-sync scheduled downloads'), 1000);
check('a non-subsequence scores exactly 0', P.scoreField('zzqx', 'Cover file name') === 0);
check('substring beats subsequence',
      P.scoreField('quality', 'JPEG quality') > P.scoreField('quality', 'Queue a list yet'));

// Length penalty: a long DESCRIPTION must not out-score a short LABEL.
//
// Asserted at scoreField level ON PURPOSE. A first version of this compared two
// COMMANDS and passed for the wrong reason — both took the substring path
// (score 1000) and never reached the penalty at all, so the negative control
// correctly reported it as MISSED. Both strings below take the SUBSEQUENCE
// path, and the long one carries the query letters early and nearly adjacent so
// gap distance cannot be what separates them: measured, removing the penalty
// flips the winner (short 450 / long 278 → short 456 / long 458).
{
  const q = 'cover size';
  const shortLabel = 'Cover-max-size';
  const longDesc =
    'Cover-a-size of image written next to every track so that any music player ' +
    'you happen to use can find the artwork without opening the file itself';
  check('a long description does not out-score a short label on a subsequence match',
        P.scoreField(q, shortLabel) > P.scoreField(q, longDesc),
        `short=${P.scoreField(q, shortLabel)} long=${P.scoreField(q, longDesc)}`);
  check('both sides really are on the subsequence path (not substring hits)',
        P.scoreField(q, shortLabel) < 800 && P.scoreField(q, longDesc) < 800,
        'if either scored >=800 this test would prove nothing about the penalty');
}

/* when() gating, and a throwing predicate must hide the row rather than take
   the whole palette down. */
{
  const run = () => {};
  const cmds = [
    { id: 'y', label: 'Yes', run },
    { id: 'n', label: 'No', run, when: () => false },
    { id: 'boom', label: 'Boom', run, when: () => { throw new Error('x'); } },
    { id: 'norun', label: 'No run function' },
  ];
  const ok = P.usable(cmds).map(c => c.id);
  eq('when()=false is dropped', ok.includes('n'), false);
  eq('a throwing when() is dropped, not fatal', ok.includes('boom'), false);
  eq('a command with no run() is dropped', ok.includes('norun'), false);
  eq('a plain command survives', ok.join(','), 'y');
}

/* Providers: one that throws must not lose the others. */
{
  const good = () => [{ id: 'g', label: 'Good', run: () => {} }];
  const bad = () => { throw new Error('provider blew up'); };
  const got = P.collect([bad, good, bad]).map(c => c.id);
  eq('a throwing provider does not lose the others', got.join(','), 'g');
}

/* No query → registration order, capped at 80 (not 40: settings register last,
   and a 40 cap hid every one of them until you typed). */
{
  const many = Array.from({ length: 200 }, (_, i) => ({ id: 'c' + i, label: 'Cmd ' + i, run: () => {} }));
  const r = P.searchCommands('', many);
  eq('no query returns at most 80 rows', r.length, 80);
  eq('no query preserves registration order', r.map(x => x.cmd.id).slice(0, 3).join(','), 'c0,c1,c2');
  eq('no query keeps rows past the 40th', r[60].cmd.id, 'c60');
}

/* ═════════════════════════════════════════════════════════════════════════
   2. THE SETTINGS PROVIDER — extracted from App.svelte and EXECUTED, so the
      real mapping is what is scored, not a copy of it.
   ═══════════════════════════════════════════════════════════════════════ */
function extract(startMarker, endMarker, what) {
  const from = app.indexOf(startMarker);
  if (from < 0) fatal(`could not find ${what} start marker in App.svelte`);
  const to = app.indexOf(endMarker, from);
  if (to < 0) fatal(`could not find ${what} end marker in App.svelte`);
  return app.slice(from, to + endMarker.length);
}

const surfaceMap = extract('const SETTING_SURFACE_LABEL', '};', 'SETTING_SURFACE_LABEL');
const settingsProvider = extract(
  'SETTINGS_CATALOG.forEach(s => {',
  'run: () => focusSetting(s.id),\n      });\n    });',
  'settings provider',
);
if (!settingsProvider.includes('focusSetting(s.id)')) {
  fatal('extracted settings provider does not call focusSetting — wrong region');
}

const provJs = esbuild.transformSync(
  `${surfaceMap}\nfunction settingsCommands(SETTINGS_CATALOG, focusSetting, tr) {\n` +
  `  const c = [];\n  ${settingsProvider}\n  return c;\n}`,
  { loader: 'ts' },
).code;
const provBox = { console };

/* The catalog holds catalogue KEYS (SF-3), so the provider is handed a `tr`.
   It resolves to the REAL English from en.json rather than echoing the key,
   so the query assertions further down still score what a user reads. */
const EN = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'src', 'locales', 'en.json'), 'utf8'));
function enFlat(o, p, out) {
  out = out || {};
  for (const k of Object.keys(o)) {
    const kk = p ? p + '.' + k : k;
    if (o[k] && typeof o[k] === 'object') enFlat(o[k], kk, out);
    else out[kk] = o[k];
  }
  return out;
}
const EN_FLAT = enFlat(EN, '');
const tr = (k) => EN_FLAT[k] !== undefined ? EN_FLAT[k] : k;
check('every catalog key resolves to English',
      CATALOG.every(s => EN_FLAT[s.label] && EN_FLAT[s.desc] && EN_FLAT[s.group]),
      CATALOG.filter(s => !(EN_FLAT[s.label] && EN_FLAT[s.desc] && EN_FLAT[s.group]))
             .map(s => s.id).slice(0, 5).join(', '));
vm.createContext(provBox);
vm.runInContext(provJs + '\n;this.settingsCommands = settingsCommands;', provBox);

const focusCalls = [];
const settingCmds = provBox.settingsCommands(CATALOG, (id) => focusCalls.push(id), tr);

eq('the provider emits one command per catalog entry', settingCmds.length, CATALOG.length);
check('every settings command carries its section and surface in the hint',
      settingCmds.every(c => typeof c.hint === 'string' && c.hint.includes('·')));
check('every settings command feeds the translated description into keywords',
      CATALOG.every(s => settingCmds.find(c => c.id === 'set.' + s.id)
                                    .keywords.includes(tr(s.desc))));
{
  const cmd = settingCmds.find(c => c.id === 'set.jpeg_image_quality');
  cmd.run();
  eq('running a settings command calls focusSetting with the catalog id',
     focusCalls.join(','), 'jpeg_image_quality');
}

/* Real queries against the REAL catalog through the REAL matcher. */
function topFor(q) {
  const r = P.searchCommands(q, settingCmds);
  return r.length ? r[0].cmd.id.replace(/^set\./, '') : null;
}
eq('"jpeg" finds the JPEG quality setting', topFor('jpeg'), 'jpeg_image_quality');
eq('"cover file" finds the cover filename template', topFor('cover file'), 'cover_image_template');
eq('"folder structure" finds the folder template', topFor('folder structure'), 'folder_structure_template');
eq('"soulseek password" finds the password field', topFor('soulseek password'), 'soulseek_password');
eq('"whitespace" finds whitespace handling', topFor('whitespace'), 'whitespace_handling');
eq('"typeface" finds the font picker', topFor('typeface'), 'font');
eq('"library folder" finds the download path', topFor('library folder'), 'download_path');
eq('a nonsense query returns nothing rather than a wrong row',
   P.searchCommands('qqzzxwv', settingCmds).length, 0);

/* ═════════════════════════════════════════════════════════════════════════
   3. CATALOG ↔ MARKUP — the staleness guard.

   Antra-Web gets this by construction (its drawer is rendered FROM the
   schema). The desktop's settings are hand-written across three surfaces, so
   the guarantee is enforced HERE instead — and only holds while this runs.
   ═══════════════════════════════════════════════════════════════════════ */
const stamped = [...app.matchAll(/data-setting="([a-z_0-9]+)"/g)].map(m => m[1]);
const stampedSet = new Set(stamped);
const catalogIds = CATALOG.map(s => s.id);

eq('every data-setting anchor is unique', stamped.length, stampedSet.size);
{
  const missing = catalogIds.filter(id => !stampedSet.has(id));
  eq('every catalog entry has a data-setting anchor in App.svelte',
     missing.join(', ') || '(none)', '(none)');
  const orphan = stamped.filter(id => !catalogIds.includes(id));
  eq('every data-setting anchor has a catalog entry',
     orphan.join(', ') || '(none)', '(none)');
}
check('catalog ids are unique', new Set(catalogIds).size === catalogIds.length);
check('every entry declares a known surface',
      CATALOG.every(s => ['main', 'settings', 'folder', 'themes'].includes(s.surface)));
check('every entry has a non-empty label, desc and group',
      CATALOG.every(s => s.label && s.desc && s.group));

/* A setting the user can change but that is NOT indexed is invisible to the
   palette. Catch new bindings that were never catalogued. `library_mode` is
   two radios sharing one row, and hour/minute are one visible "Time" row, so
   they are mapped rather than counted. */
{
  const ALIAS = { auto_sync_hour: 'auto_sync_time', auto_sync_minute: 'auto_sync_time' };
  const bound = new Set(
    [...app.matchAll(/bind:(?:checked|value|group)=\{config\.([a-z_0-9]+)\}/g)]
      .map(m => ALIAS[m[1]] || m[1]),
  );
  const uncatalogued = [...bound].filter(k => !catalogIds.includes(k));
  eq('every config field bound in the markup is in the catalog',
     uncatalogued.sort().join(', ') || '(none)', '(none)');
}

/* ═════════════════════════════════════════════════════════════════════════
   3b. CONDITIONALLY-RENDERED ROWS.

   Ten anchors sit inside a `{#if config.<parent>}` block, so they are not in
   the DOM while that parent toggle is off. The palette still indexes them, and
   `requires` is what lets the jump fall back to the parent instead of silently
   highlighting nothing. That was found by RUNNING the app, not by reading it.

   The guard is derived from the markup here rather than hard-coded, so adding
   a new `{#if}` around an existing row fails this test instead of shipping a
   dead jump.
   ═══════════════════════════════════════════════════════════════════════ */
{
  const guardOf = {};                       // settingId -> config key gating it
  const stack = [];
  const TOKENS = /\{#if ([^}]*)\}|\{:else if ([^}]*)\}|(\{:else\})|(\{\/if\})/g;
  for (const line of app.split('\n')) {
    let m;
    TOKENS.lastIndex = 0;
    while ((m = TOKENS.exec(line)) !== null) {
      if (m[1] !== undefined) stack.push(m[1].trim());
      else if (m[2] !== undefined) { if (stack.length) stack[stack.length - 1] = 'elseif'; }
      else if (m[3]) { if (stack.length) stack[stack.length - 1] = 'not'; }
      else if (m[4]) stack.pop();
    }
    const a = /data-setting="([a-z_0-9]+)"/.exec(line);
    if (a) {
      const cfg = stack
        .map(g => (/config\.([a-z_0-9]+)/.exec(g) || [])[1])
        .filter(Boolean);
      if (cfg.length) guardOf[a[1]] = cfg[cfg.length - 1];
    }
  }

  const declared = Object.fromEntries(
    CATALOG.filter(s => s.requires).map(s => [s.id, s.requires]));

  eq('every conditionally-rendered row declares `requires`',
     Object.keys(guardOf).filter(k => !declared[k]).sort().join(', ') || '(none)', '(none)');
  eq('nothing declares `requires` without actually being conditional',
     Object.keys(declared).filter(k => !guardOf[k]).sort().join(', ') || '(none)', '(none)');
  eq('the declared parent matches the guard measured from the markup',
     Object.keys(declared).filter(k => declared[k] !== guardOf[k]).join(', ') || '(none)', '(none)');
  check('every `requires` names a real catalog entry',
        CATALOG.every(s => !s.requires || CATALOG.some(p => p.id === s.requires)));
  check('a parent is itself always rendered (else the fallback has nowhere to go)',
        CATALOG.filter(s => s.requires).every(s => !guardOf[s.requires]));
  check('there is at least one conditional row, so this section is not vacuous',
        Object.keys(guardOf).length >= 5, `found ${Object.keys(guardOf).length}`);
}

/* ═════════════════════════════════════════════════════════════════════════
   4. focusSetting — EXECUTED, not read. The property that matters is that at
      most one settings surface is open afterwards: two stacked overlays both
      want the same Escape key.
   ═══════════════════════════════════════════════════════════════════════ */
{
  const fsSrc = extract('async function focusSetting(id: string) {', '\n  }\n', 'focusSetting');
  const fsJs = esbuild.transformSync(fsSrc, { loader: 'ts' }).code;
  const flashed = [];
  const box = {
    console,
    SETTINGS_CATALOG: CATALOG,
    tick: () => Promise.resolve(),
    setTimeout: (fn) => fn(),           // run the deferred scroll immediately
    showThemes: false, showFolderSettings: false, showSettings: false,
    openSettings: async () => { box.showSettings = true; },
    openFolderSettings: async () => { box.showFolderSettings = true; },
    document: {
      querySelector: (sel) => {
        flashed.push(sel);
        return { scrollIntoView() {}, classList: { add() {}, remove() {} }, querySelector: () => null };
      },
    },
  };
  vm.createContext(box);
  vm.runInContext(fsJs + '\n;this.focusSetting = focusSetting;', box);

  const openSurfaces = () => ['showSettings', 'showFolderSettings', 'showThemes'].filter(k => box[k]);

  (async () => {
    await box.focusSetting('jpeg_image_quality');
    eq('a settings-modal entry opens exactly the settings modal', openSurfaces().join(','), 'showSettings');
    eq('it targets the right anchor', flashed[flashed.length - 1], '[data-setting="jpeg_image_quality"]');

    await box.focusSetting('folder_structure_template');
    eq('switching to a folder entry leaves ONLY the folder modal open',
       openSurfaces().join(','), 'showFolderSettings');

    await box.focusSetting('theme');
    eq('switching to a themes entry leaves ONLY the themes panel open',
       openSurfaces().join(','), 'showThemes');

    await box.focusSetting('output_format');
    eq('a main-screen entry closes every settings surface', openSurfaces().join(','), '');

    const before = flashed.length;
    await box.focusSetting('no_such_setting');
    eq('an unknown id is a no-op, not a crash', flashed.length, before);

    // The row is behind {#if config.save_cover_art_sidecar}: with the toggle
    // off it is not in the DOM at all, and the jump must land on the parent.
    box.__present = new Set(['save_cover_art_sidecar']);
    box.document.querySelector = (sel) => {
      flashed.push(sel);
      const id = /data-setting="([^"]+)"/.exec(sel)[1];
      if (!box.__present.has(id)) return null;
      return { scrollIntoView() {}, classList: { add() {}, remove() {} }, querySelector: () => null };
    };
    flashed.length = 0;
    await box.focusSetting('jpeg_image_quality');
    eq('a hidden row falls back to the parent it depends on',
       flashed.join(' -> '),
       '[data-setting="jpeg_image_quality"] -> [data-setting="save_cover_art_sidecar"]');

    box.__present = new Set();
    flashed.length = 0;
    await box.focusSetting('jpeg_image_quality');
    eq('with the parent gone too it is still a no-op, not a crash', flashed.length, 2);

    report();
  })().catch(e => { console.error(e); process.exit(1); });
}

/* ═════════════════════════════════════════════════════════════════════════
   5. WIRING — the palette is useless if it is never reachable. These are the
      few properties that can only be seen in App.svelte itself.
   ═══════════════════════════════════════════════════════════════════════ */
check('the key handler is registered in the CAPTURE phase',
      /addEventListener\('keydown', paletteKeyHandler, true\)/.test(app),
      'capture is what lets the palette consume Escape before the modals underneath it do');
check('the handler is removed on destroy',
      /removeEventListener\('keydown', paletteKeyHandler, true\)/.test(app));
check('Ctrl/Cmd+K is the only global binding',
      /const mod = e\.ctrlKey \|\| e\.metaKey;[\s\S]{0,200}e\.key === 'k'/.test(app));
check('commands are snapshotted on OPEN, not rebuilt per keystroke',
      /function openPalette\(\)[\s\S]{0,400}paletteAll = buildPaletteCommands\(\)/.test(app)
      && !/function refreshPalette\(\)[\s\S]{0,200}buildPaletteCommands\(\)/.test(app));
check('running a row closes the palette before the command runs',
      /function runPaletteRow[\s\S]{0,300}closePalette\(\);[\s\S]{0,200}setTimeout/.test(app));
eq('the overlay is rendered once', (app.match(/class="pal-overlay"/g) || []).length, 1);
check('the flash class is :global so Svelte does not scope it away',
      app.includes(':global(.setting-flash)'));

function report() {
  console.log(`\n${failures.length ? 'FAIL' : 'PASS'} — ${pass} passed, ${failures.length} failed`);
  failures.forEach(f => console.log('  ✗ ' + f));
  process.exit(failures.length ? 1 : 0);
}
