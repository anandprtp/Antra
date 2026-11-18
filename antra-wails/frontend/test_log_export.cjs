/*
 * FR-4 (v2.0.0) — tests for the activity-log text builder.
 *
 * The functions under test are EXTRACTED FROM App.svelte and executed, not
 * re-implemented here: a re-implementation would drift from the shipped code
 * and pass while the app was broken. TypeScript annotations are stripped with
 * the esbuild that Vite already provides.
 *
 * Run: node test_log_export.cjs   (from antra-wails/frontend)
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const esbuild = require('esbuild');

const SRC = path.join(__dirname, 'src', 'App.svelte');
const src = fs.readFileSync(SRC, 'utf8');

let pass = 0;
const failures = [];
function check(label, cond, detail) {
  if (cond) { pass++; return; }
  failures.push(detail ? `${label}\n      ${detail}` : label);
}
function eq(label, got, want) {
  check(label, got === want, `got:  ${JSON.stringify(got)}\n      want: ${JSON.stringify(want)}`);
}

// ── Extract the real region from App.svelte ─────────────────────────────────
const START = 'const LOG_LEVEL_LABELS';
const END = 'async function saveActivityLog';
const from = src.indexOf(START);
const to = src.indexOf(END);
if (from < 0 || to < 0 || to <= from) {
  console.error('FATAL: could not locate the FR-4 export helpers in App.svelte. ' +
    'If they were renamed, update this test rather than deleting it.');
  process.exit(1);
}
const region = src.slice(from, to);

// Guard against a silently-empty extraction: if the anchors ever match the
// wrong place, the tests below would pass on a stub and prove nothing.
for (const fn of ['stripLogHtml', 'buildLogExportText', 'LOG_STAMP_WIDTH']) {
  if (!region.includes(fn)) {
    console.error(`FATAL: extracted region does not contain ${fn}`);
    process.exit(1);
  }
}

const js = esbuild.transformSync(region, { loader: 'ts' }).code;
const sandbox = { logs: [], console };
vm.createContext(sandbox);
vm.runInContext(js + '\n;this.stripLogHtml = stripLogHtml; this.buildLogExportText = buildLogExportText;', sandbox);
const { stripLogHtml, buildLogExportText } = sandbox;

// ── The real rundown block, byte-for-byte from formatAsciiRundown ───────────
const sep = '='.repeat(56);
const rundown =
  `<div style="font-family:var(--font-mono);font-size:13px;line-height:1.7;margin-top:8px">` +
  [
    `<span style="color:var(--accent-color)">${sep}</span>`,
    `  Tracks added      : <span style="color:#4ade80">17 / 18</span>`,
    `  Already in library: <span style="color:#facc15">0</span>`,
    `  Could not source  : <span style="color:var(--error-color)">1</span>`,
    `  Total size        : <span style="color:#94a3b8">412.6 MB</span>`,
    `<span style="color:var(--accent-color)">${sep}</span>`,
  ].join('<br>') +
  `</div>`;

const stripped = stripLogHtml(rundown);
check('rundown: no markup survives', !/[<>]/.test(stripped), JSON.stringify(stripped));
check('rundown: numbers survive', stripped.includes('17 / 18') && stripped.includes('412.6 MB'));
check('rundown: labels survive', stripped.includes('Tracks added') && stripped.includes('Could not source'));
check('rundown: <br> became newlines', stripped.split('\n').length === 6,
  `${stripped.split('\n').length} lines`);
check('rundown: separator rule survives', stripped.split('\n')[0] === sep);

// ── Entity decoding ─────────────────────────────────────────────────────────
eq('entities: basic', stripLogHtml('a &lt;b&gt; &amp; c &quot;d&quot;'), 'a <b> & c "d"');
// &amp; must decode LAST or "&amp;lt;" would wrongly become "<".
eq('entities: double-encoded stays encoded once', stripLogHtml('&amp;lt;script&amp;gt;'), '&lt;script&gt;');
eq('entities: nbsp becomes a space', stripLogHtml('a&nbsp;b'), 'a b');
eq('entities: apostrophe', stripLogHtml('it&#39;s'), "it's");
eq('br variants', stripLogHtml('a<br>b<br/>c<br />d'), 'a\nb\nc\nd');

// ── buildLogExportText ──────────────────────────────────────────────────────
const t = (h, m, s) => new Date(2026, 8, 10, h, m, s).getTime();

sandbox.logs = [
  { id: 0, type: 'info', text: 'Building your music library', ts: t(14, 3, 5) },
  { id: 1, type: 'success', text: 'Added to library: Drake - Ran To Atlanta', ts: t(14, 3, 9) },
  { id: 2, type: 'warn', text: 'Spotify Discover unavailable', ts: t(14, 4, 0) },
  { id: 3, type: 'error', text: 'FAIL: no source could find this track', ts: t(14, 4, 12) },
  { id: 4, type: 'terminal-rundown', text: rundown, isRawHtml: true, ts: t(14, 5, 30) },
];
const out = buildLogExportText();
const lines = out.split('\n');

check('every entry appears', lines.length >= 5 + 5, `${lines.length} lines`);
eq('first line', lines[0], '[14:03:05] INFO      Building your music library');
eq('success line', lines[1], '[14:03:09] SUCCESS   Added to library: Drake - Ran To Atlanta');
eq('warn folds onto WARNING', lines[2], '[14:04:00] WARNING   Spotify Discover unavailable');
eq('error line', lines[3], '[14:04:12] ERROR     FAIL: no source could find this track');
check('rundown level is SUMMARY', lines[4].startsWith('[14:05:30] SUMMARY   '), lines[4]);
check('rundown continuation lines are indented to the message column',
  lines[5].startsWith(' '.repeat(21)) && lines[5].trim().startsWith('Tracks added'), JSON.stringify(lines[5]));
check('no markup anywhere in the export', !/<[a-z/]/i.test(out));

// The timestamp column must be a fixed width so the file stays scannable.
const widths = new Set(lines.filter(l => l.startsWith('[')).map(l => l.indexOf(' ', 11 + 9)));
check('timestamp+level column is fixed width', widths.size === 1, [...widths].join(','));

// ── Degradation ─────────────────────────────────────────────────────────────
sandbox.logs = [];
eq('empty log yields empty text', buildLogExportText(), '');

sandbox.logs = [{ id: 0, type: '', text: 'no level given', ts: t(1, 2, 3) }];
check('missing level falls back to INFO', buildLogExportText().includes('INFO'), buildLogExportText());

// A log entry written before this change has no ts — it must not crash or
// print "Invalid Date"; it falls back to now.
sandbox.logs = [{ id: 0, type: 'info', text: 'legacy entry with no ts' }];
const legacy = buildLogExportText();
check('missing ts does not produce NaN/Invalid', !/NaN|Invalid/.test(legacy), legacy);
check('missing ts still emits the message', legacy.includes('legacy entry with no ts'));

// Multi-line plain text (not HTML) must also be indented.
sandbox.logs = [{ id: 0, type: 'error', text: 'line one\nline two', ts: t(9, 0, 0) }];
const ml = buildLogExportText().split('\n');
eq('multi-line plain: first', ml[0], '[09:00:00] ERROR     line one');
eq('multi-line plain: continuation', ml[1], ' '.repeat(21) + 'line two');

// ── Report ──────────────────────────────────────────────────────────────────
console.log(`\n${pass} passed, ${failures.length} failed`);
if (failures.length) {
  failures.forEach(f => console.log('  FAIL: ' + f));
  process.exit(1);
}
