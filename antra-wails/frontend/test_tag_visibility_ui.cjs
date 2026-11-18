/*
 * AUDIT-DESKTOP-16 - a track saved without tags must not read as a clean success.
 *
 * DECISION: the audio is kept and the track stays "done", but the row, the log and
 * the end-of-run rundown say so. Pulls the REAL completedText() and
 * formatAsciiRundown() out of App.svelte (strings resolved through the real
 * en.json) and drives them; the event handler wiring is checked statically,
 * because that code lives inside a closure. Re-runs against sabotaged copies of
 * the source and requires each to be caught.
 *
 * Run: node test_tag_visibility_ui.cjs   (from antra-wails/frontend)
 */
const fs = require('fs');
const path = require('path');

const REAL = fs.readFileSync(path.join(__dirname, 'src', 'App.svelte'), 'utf8').replace(/\r\n/g, '\n');
const EN = JSON.parse(fs.readFileSync(path.join(__dirname, 'src', 'locales', 'en.json'), 'utf8'));

function bodyOf(src, header) {
  const start = src.indexOf(header);
  if (start < 0) return null;
  let i = src.indexOf('{', src.indexOf(')', start));
  const open = i;
  let depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) return src.slice(open + 1, i); }
  }
  return null;
}

const tr = (key) => key.split('.').reduce((o, k) => (o == null ? o : o[k]), EN) ?? ('<<' + key + '>>');

function run(src) {
  const failures = [];
  const check = (label, ok, detail) => { if (!ok) failures.push(label + (detail ? ' :: ' + detail : '')); };

  const ctBody = bodyOf(src, 'function completedText(');
  const rdBody = bodyOf(src, 'function formatAsciiRundown(');
  if (!ctBody || !rdBody) return ['could not extract completedText / formatAsciiRundown'];
  // The component body is TypeScript: drop `const x: T =` annotations so the extracted body is plain JS.
  const stripTs = (s) => s.replace(/\b(const|let) (\w+): [^=\n]+ =/g, '$1 $2 =')
    .replace(/(\w+): (string|number|boolean|any)\b/g, '$1');
  const completedText = new Function('tagsProblem', 'tr', stripTs(ctBody));
  const rundown = new Function('summary', 'tr', stripTs(rdBody));

  const ok = tr('status.added_to_library');
  const failed = tr('status.added_untagged_failed');
  const unsupported = tr('status.added_untagged_unsupported');
  check('catalogue strings exist and differ',
    ok && failed && unsupported && !String(failed).startsWith('<<') && !String(unsupported).startsWith('<<')
    && new Set([ok, failed, unsupported]).size === 3, JSON.stringify([ok, failed, unsupported]));

  check("'failed' gets its own row text", completedText('failed', tr) === failed, completedText('failed', tr));
  check("'unsupported' gets its own row text", completedText('unsupported', tr) === unsupported, completedText('unsupported', tr));
  for (const v of [null, undefined, '', 'bogus']) {
    check('control: ' + JSON.stringify(v) + ' reads as a normal success', completedText(v, tr) === ok, completedText(v, tr));
  }

  const base = { downloaded: 5, skipped: 1, failed: 0, total: 6 };
  const label = tr('status.saved_untagged');
  const with3 = rundown({ ...base, tags_incomplete: 3 }, tr);
  check('rundown shows the untagged count when it is above zero', with3.includes(label) && />3<\/span>/.test(with3.split(label)[1] || ''),
    with3.slice(0, 200));
  for (const v of [0, undefined, null, '0']) {
    check('control: no untagged line when the count is ' + JSON.stringify(v), !rundown({ ...base, tags_incomplete: v }, tr).includes(label));
  }
  check('control: the usual lines are unchanged', /Tracks added/.test(rundown(base, tr)) && /Could not source/.test(rundown(base, tr)));
  const hostile = rundown({ ...base, tags_incomplete: '<img src=x onerror=alert(1)>' }, tr);
  check('a hostile tags_incomplete is coerced to a number, never interpolated', !hostile.includes('<img') && !hostile.includes(label), hostile.slice(0, 160));
  check("a numeric string is accepted ('5')", rundown({ ...base, tags_incomplete: '5' }, tr).includes('>5<'));

  // Wiring inside the event handler (a closure, so checked on the source).
  check('track_completed row text comes from completedText(data.tags_problem)', /text: completedText\(data\.tags_problem\)/.test(src));
  check('track_completed logs a WARNING (not a success) for an untagged track',
    /if \(data\.tags_problem\) addLog\('warning'/.test(src));
  check('control: the normal success log is still there', /addLog\('success', `\[✓\] Added to library: \$\{trackLabel\}`\)/.test(src));
  return failures;
}

const base = run(REAL);
if (base.length) { console.log('FAIL (unsabotaged)'); base.forEach((f) => console.log('  ' + f)); process.exit(1); }
console.log('ok: all checks pass on the real source');

const sabotages = {
  'row text ignores the problem': (s) => s.replace('text: completedText(data.tags_problem),', "text: tr('status.added_to_library'),"),
  'unsupported shares the failed text': (s) => s.replace("tr('status.added_untagged_unsupported')", "tr('status.added_untagged_failed')"),
  "'failed' treated as success": (s) => s.replace("if (tagsProblem === 'failed') return tr('status.added_untagged_failed');", ''),
  'untagged logged as a success': (s) => s.replace("if (data.tags_problem) addLog('warning'", "if (false) addLog('warning'"),
  'rundown never shows the count': (s) => s.replace('...(untagged > 0 ?', '...(false ?'),
  'rundown shows a zero count': (s) => s.replace('...(untagged > 0 ?', '...(true ?'),
  // (Removing the Number() coercion is deliberately NOT a sabotage here: the `untagged > 0` gate already
  // rejects every string JSON can carry, so the mutation is equivalent. The coercion stays as defence in depth.)
};
let missed = 0;
for (const [name, mutate] of Object.entries(sabotages)) {
  const mutated = mutate(REAL);
  if (mutated === REAL) { console.log('NO-OP sabotage (anchor gone): ' + name); missed++; continue; }
  let f;
  try { f = run(mutated); } catch (e) { f = ['threw: ' + e.message]; }
  console.log((f.length ? 'caught  ' : 'MISSED  ') + name);
  if (!f.length) missed++;
}
if (missed) { console.log(missed + ' sabotage(s) not caught'); process.exit(1); }
console.log('all sabotages caught');
