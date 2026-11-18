/*
 * PORT-1 (v2.0.0) — negative control for test_palette.cjs.
 *
 * A green suite proves nothing until it has been shown to go red against code
 * that is actually broken. Each sabotage below breaks ONE real property; the
 * suite must fail for each, and every file is restored byte-for-byte after.
 *
 * Run: node test_palette_negctl.cjs      (from antra-wails/frontend)
 */
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const SRC = path.join(__dirname, 'src');
const FILES = {
  app: path.join(SRC, 'App.svelte'),
  pal: path.join(SRC, 'lib', 'palette.ts'),
  cat: path.join(SRC, 'lib', 'settings-catalog.ts'),
};

function suitePasses() {
  try {
    execFileSync(process.execPath, [path.join(__dirname, 'test_palette.cjs')], { stdio: 'pipe' });
    return true;
  } catch {
    return false;
  }
}

const SABOTAGES = [
  {
    name: 'norm no longer folds separators (hyphen breaks the prefix match)',
    file: 'pal',
    from: ".replace(/[-_/.,:]+/g, ' ')",
    to: '',
  },
  {
    name: 'subsequence length penalty removed (long descriptions win again)',
    file: 'pal',
    from: 'const lenPenalty = Math.min(180, Math.max(0, t.length - q.length) * 1.5);',
    to: 'const lenPenalty = 0;',
  },
  {
    name: 'no-query cap dropped back to 40 (settings become invisible until you type)',
    file: 'pal',
    from: 'cmds: Command[], limit = 80',
    to: 'cmds: Command[], limit = 40',
  },
  {
    name: 'when() predicate ignored (disabled rows are shown and can be run)',
    file: 'pal',
    from: "if (typeof c.when === 'function') {",
    to: 'if (false) {',
  },
  {
    name: 'a catalogued setting loses its markup anchor',
    file: 'app',
    from: 'data-setting="jpeg_image_quality" ',
    to: '',
  },
  {
    name: 'a markup anchor exists with no catalog entry',
    file: 'app',
    from: '<div data-setting="download_path" class="field">',
    to: '<div data-setting="download_path" class="field"><span data-setting="ghost_setting"></span>',
  },
  {
    name: 'focusSetting stops closing the other surfaces (two stacked overlays)',
    file: 'app',
    from: '    showThemes = false;\r\n    showFolderSettings = false;\r\n' +
          "    if (entry.surface !== 'settings') showSettings = false;",
    to: '    /* sabotage: surfaces left open */',
  },
  {
    name: 'the key listener moves from capture to bubble phase',
    file: 'app',
    from: "document.addEventListener('keydown', paletteKeyHandler, true);",
    to: "document.addEventListener('keydown', paletteKeyHandler, false);",
  },
  {
    name: 'commands are rebuilt per keystroke instead of snapshotted on open',
    file: 'app',
    from: '  function refreshPalette() {\r\n    paletteResults = searchCommands(paletteQuery, paletteAll);',
    to: '  function refreshPalette() {\r\n    paletteAll = buildPaletteCommands();\r\n' +
        '    paletteResults = searchCommands(paletteQuery, paletteAll);',
  },
  {
    name: 'the settings provider stops feeding descriptions into keywords',
    file: 'app',
    from: "keywords: ['setting', 'preference', 'option', tr(s.desc), tr(s.group)],",
    to: "keywords: ['setting', 'preference', 'option', tr(s.group)],",
  },
  {
    name: 'a catalog entry loses its description',
    file: 'cat',
    from: "desc: 'settings.catalog.jpeg_image_quality.desc'",
    to: "desc: ''",
  },
  {
    name: 'focusSetting drops the hidden-row fallback (jump silently highlights nothing)',
    file: 'app',
    from: '      if (!el && entry.requires) {',
    to: '      if (false) {',
  },
  {
    name: 'a conditionally-rendered row loses its `requires`',
    file: 'cat',
    from: ", requires: 'save_artist_image' }",
    to: ' }',
  },
  {
    name: 'a `requires` points at the wrong parent',
    file: 'cat',
    from: "desc: 'settings.catalog.soulseek_username.desc', requires: 'soulseek_enabled' }",
    to: "desc: 'settings.catalog.soulseek_username.desc', requires: 'save_artist_image' }",
  },
];

console.log('Baseline (unmodified):', suitePasses() ? 'PASS' : 'FAIL');
if (!suitePasses()) {
  console.error('FATAL: the suite is already failing — fix that before running the control.');
  process.exit(1);
}

let caught = 0;
const missed = [];

for (const s of SABOTAGES) {
  const file = FILES[s.file];
  const original = fs.readFileSync(file, 'utf8');
  const n = original.split(s.from).length - 1;
  if (n !== 1) {
    console.error(`FATAL: sabotage "${s.name}" matched ${n} times — it would test nothing.`);
    process.exit(1);
  }
  fs.writeFileSync(file, original.replace(s.from, s.to));
  const stillPasses = suitePasses();
  fs.writeFileSync(file, original);
  if (fs.readFileSync(file, 'utf8') !== original) {
    console.error('FATAL: failed to restore ' + file);
    process.exit(1);
  }
  if (stillPasses) { missed.push(s.name); console.log('  MISSED  ' + s.name); }
  else { caught++; console.log('  caught  ' + s.name); }
}

console.log(`\nControl: ${caught}/${SABOTAGES.length} detected`);
if (missed.length) {
  console.log('Not detected — the suite has a hole here:');
  missed.forEach(m => console.log('  ✗ ' + m));
}
console.log('Suite green again after restore:', suitePasses() ? 'yes' : 'NO');
process.exit(missed.length ? 1 : 0);
