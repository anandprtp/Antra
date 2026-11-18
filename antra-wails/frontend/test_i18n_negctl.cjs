/*
 * SF-3 — negative control for test_i18n.cjs.
 *
 * A green suite proves nothing until it has been shown to go red against broken
 * code. Each sabotage below reverts one guarantee of the i18n design in place,
 * runs the real suite, and requires it to FAIL; the file is then restored and
 * verified byte-identical.
 *
 * Run: node test_i18n_negctl.cjs      (from antra-wails/frontend)
 */
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const SRC = path.join(__dirname, 'src');
const I18N = path.join(SRC, 'lib', 'i18n.ts');
const EN = path.join(SRC, 'locales', 'en.json');
const APP = path.join(SRC, 'App.svelte');
const CATALOG = path.join(SRC, 'lib', 'settings-catalog.ts');

function suitePasses() {
  try {
    execFileSync('node', [path.join(__dirname, 'test_i18n.cjs')], { stdio: 'pipe' });
    return true;
  } catch {
    return false;
  }
}

const SABOTAGES = [
  {
    name: 'a key App.svelte asks for is deleted from en.json',
    file: EN,
    why: 'that key would render as a raw dotted string to the user',
    mutate(s) {
      const d = JSON.parse(s);
      delete d.common.close;
      return JSON.stringify(d, null, 2) + '\n';
    },
  },
  {
    name: 'an orphan key is added to en.json',
    file: EN,
    why: 'dead catalogue entries accumulate and get sent to translators',
    mutate(s) {
      const d = JSON.parse(s);
      d.common.__orphan__ = 'never used';
      return JSON.stringify(d, null, 2) + '\n';
    },
  },
  {
    name: 'a catalogue value regains a raw newline',
    file: EN,
    why: 'multi-line indentation leaks into the string a translator edits',
    mutate(s) {
      const d = JSON.parse(s);
      d.common.close = 'Close\n          again';
      return JSON.stringify(d, null, 2) + '\n';
    },
  },
  {
    name: 'a catalogue value regains an undecoded HTML entity',
    file: EN,
    why: '{$t(...)} renders text, so &amp; would be shown literally',
    mutate(s) {
      const d = JSON.parse(s);
      d.common.close = 'Close &amp; done';
      return JSON.stringify(d, null, 2) + '\n';
    },
  },
  {
    name: 'missing-translation fallback to English is removed',
    file: I18N,
    why: 'a partial translation would show raw keys instead of English',
    mutate: (s) => s.replace(
      '  const fallback = lookup(EN, FALLBACK_LOCALE, key, vars);',
      '  const fallback = null as string | null;'),
  },
  {
    name: 'plurals use n === 1 instead of Intl.PluralRules',
    file: I18N,
    why: 'looks right in English and is wrong in every Slavic language',
    mutate: (s) => s.replace(
      '    const cat = new Intl.PluralRules(localeId).select(count);',
      "    const cat = count === 1 ? 'one' : 'other';"),
  },
  {
    name: 'interpolation evaluates instead of substituting',
    file: I18N,
    why: 'catalogue content is written by translators and must never execute',
    mutate: (s) => s.replace(
      '    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole,',
      '    Object.prototype.hasOwnProperty.call(vars, name)\n' +
      "      ? eval('`' + String(vars[name]) + '`') : whole,"),
  },
  {
    name: 'negotiate stops falling back from a region tag to its base language',
    file: I18N,
    why: 'en-GB would silently get the fallback locale instead of English',
    mutate: (s) => s.replace(
      '    const baseHit = supported.find((s) => s.toLowerCase().split(\'-\')[0] === base);',
      '    const baseHit = undefined as string | undefined;'),
  },
  {
    name: 'a translated string is reverted to hardcoded English in the markup',
    file: APP,
    why: 'the coverage budget must notice untranslated UI creeping back in',
    mutate(s) {
      // 60 distinct reversions: enough to clear the budget headroom, so the
      // check is shown to bite rather than merely to exist.
      let n = 0;
      return s.replace(/\{\$t\('([a-z0-9_.]+)'\)\}/g, (whole, key) =>
        n++ < 60 ? 'HARDCODED' + n : whole);
    },
  },
  {
    name: 'a data-table key is renamed in App.svelte but not in en.json',
    file: APP,
    why: 'the theme card would render the raw key, and the old value would rot',
    mutate: (s) => s.replace("'themes.cards.antra'", "'themes.cards.antraa'"),
  },
  {
    name: 'a settings-catalog key is renamed but not in en.json',
    file: CATALOG,
    why: 'the palette row would show a dotted key instead of the setting name',
    mutate: (s) => s.replace(
      "label: 'settings.catalog.jpeg_image_quality.label'",
      "label: 'settings.catalog.jpeg_image_quality.lable'"),
  },
  {
    name: 'hardcoded English comes back into the script block',
    file: APP,
    why: 'the script-side coverage budget is what keeps that count from growing',
    mutate: (s) => s.replace(
      "  let showThemes = false;",
      "  let showThemes = false;\n"
      + "  const reintroduced = ['Download all of the things', 'Another English label',"
      + " 'A third hardcoded sentence'];\n"
      + "  void reintroduced;"),
  },
];

console.log('SF-3 i18n negative control\n' + '='.repeat(70));

if (!suitePasses()) {
  console.error('FATAL: the suite is already failing before any sabotage.');
  process.exit(1);
}
console.log('baseline: test_i18n.cjs passes\n');

let caught = 0;
const missed = [];

for (const s of SABOTAGES) {
  const original = fs.readFileSync(s.file);
  const mutated = s.mutate(original.toString('utf8'));
  if (mutated === original.toString('utf8')) {
    console.error(`  !! sabotage produced no change: ${s.name}`);
    fs.writeFileSync(s.file, original);
    missed.push(s.name + ' (anchor did not match)');
    continue;
  }
  fs.writeFileSync(s.file, mutated, 'utf8');
  const stillPasses = suitePasses();
  fs.writeFileSync(s.file, original);

  const restored = fs.readFileSync(s.file);
  if (!restored.equals(original)) {
    console.error(`  !! FILE NOT RESTORED after: ${s.name}`);
    process.exit(1);
  }

  if (stillPasses) {
    missed.push(s.name);
    console.log(`  MISSED   ${s.name}`);
  } else {
    caught++;
    console.log(`  caught   ${s.name}`);
    console.log(`           (${s.why})`);
  }
}

console.log('\n' + '='.repeat(70));
console.log(`${caught}/${SABOTAGES.length} sabotages caught`);

if (!suitePasses()) {
  console.error('FATAL: suite does not pass after restore — a file was left modified.');
  process.exit(1);
}
console.log('suite passes again after restore; all files byte-identical.');
if (missed.length) {
  console.log('\nMISSED:');
  missed.forEach((m) => console.log('  - ' + m));
  process.exit(1);
}
