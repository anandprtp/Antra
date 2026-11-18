/*
 * issue #74 — a download failure was reported against the wrong song.
 *
 * The webview correlated progress events with rows by `track_index`, but the
 * backend reorders the track list between emitting the tracklist and
 * downloading it (enrichment ends in _stamp_disc_totals, which groups by
 * album). So index N of the download list and row N of the tracklist were two
 * different songs, and every status landed on the wrong row.
 *
 * This suite pulls the REAL makeRowKey() and resolveTrackEventKey() out of
 * App.svelte, evaluates them, and drives the whole correlation the way a
 * download does: prepopulate rows from a tracklist, permute the list, emit
 * events over the permuted order, and assert on WHICH ROW each status lands.
 * It never asserts on a comment or a string this change could itself contain.
 *
 * A built-in negative control re-runs every check against sabotaged copies of
 * the source and requires each sabotage to be caught.
 *
 * Run: node test_row_identity.cjs   (from antra-wails/frontend)
 */
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, 'src', 'App.svelte');
const REAL = fs.readFileSync(SRC, 'utf8').replace(/\r\n/g, '\n');

// Body of the function whose declaration starts with `header`, brace-balanced.
function bodyOf(src, header) {
  const start = src.indexOf(header);
  if (start < 0) return null;
  let i = src.indexOf('{', start);
  if (i < 0) return null;
  let depth = 0;
  const open = i;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) return src.slice(open + 1, i); }
  }
  return null;
}

function build(src) {
  const rowKeyBody = bodyOf(src, 'function makeRowKey(');
  const resolveBody = bodyOf(src, 'function resolveTrackEventKey(');
  if (!rowKeyBody || !resolveBody) return null;
  return {
    makeRowKey: new Function('track', 'index', rowKeyBody),
    resolveTrackEventKey: new Function(
      'data', 'currentPlaylistTrackKeysByIndex', 'fallbackTrackKey', resolveBody),
  };
}

// A release whose SOURCE order disagrees with its track numbers — a
// compilation. The backend sequences it by track number, which permutes it,
// and fix 2 deliberately does not stop that: an album's filename numbering is
// derived from the new sequence. Only the row id can hold the rows together.
const TRACKLIST = [
  { row_id: 'ab12-1', artist: 'Artist', title: 'Third' },
  { row_id: 'ab12-2', artist: 'Artist', title: 'First' },
  { row_id: 'ab12-3', artist: 'Artist', title: 'Second' },
];
const DOWNLOAD_ORDER = ['First', 'Second', 'Third'];

function runChecks(fns) {
  const fails = [];
  let n = 0;
  const check = (label, cond) => { n++; if (!cond) fails.push(label); };
  fails.count = () => n;

  const fallbackTrackKey = (artist, title) => `fallback::${artist}::${title}`;

  // ── prepopulate exactly as the playlist_loaded handler does ──────────────
  const labels = {};              // rowKey -> what the user reads on that row
  const byIndex = {};             // currentPlaylistTrackKeysByIndex
  TRACKLIST.forEach((t, idx) => {
    const rowKey = fns.makeRowKey(t, idx + 1);
    labels[rowKey] = t.title;
    byIndex[idx + 1] = rowKey;
  });
  check('every tracklist row gets a distinct key',
    new Set(Object.values(byIndex)).size === TRACKLIST.length);

  // ── emit one event per track, in the DOWNLOAD order ──────────────────────
  const events = DOWNLOAD_ORDER.map((title, i) => {
    const row = TRACKLIST.find(t => t.title === title);
    return { row_id: row.row_id, track: title, artist: 'Artist',
             track_index: i + 1, track_data: { row_id: row.row_id } };
  });

  let landedRight = 0;
  events.forEach(ev => {
    const key = fns.resolveTrackEventKey(ev, byIndex, fallbackTrackKey);
    if (labels[key] === ev.track) landedRight++;
  });
  check('every status lands on the row showing that song',
    landedRight === events.length);

  // CONTROL — the same events with the identity removed, i.e. the old code.
  // This MUST come out the other way, or the test above is passing because
  // nothing was permuted rather than because the fix works.
  let oldLandedRight = 0;
  events.forEach(ev => {
    const stripped = { track: ev.track, artist: ev.artist, track_index: ev.track_index,
                       track_data: {} };
    const key = fns.resolveTrackEventKey(stripped, byIndex, fallbackTrackKey);
    if (labels[key] === ev.track) oldLandedRight++;
  });
  check('CONTROL: index keying alone mislabels this release',
    oldLandedRight < events.length);

  // The specific report: one track fails, and the row lit up is the one
  // showing a song that downloaded perfectly.
  const failing = events.find(e => e.track === 'Second');
  const keyNow = fns.resolveTrackEventKey(failing, byIndex, fallbackTrackKey);
  const keyOld = fns.resolveTrackEventKey(
    { track: failing.track, track_index: failing.track_index, track_data: {} },
    byIndex, fallbackTrackKey);
  check('the failing song\'s own row is the one marked failed',
    labels[keyNow] === 'Second');
  check('CONTROL: the old route marked a different song failed',
    labels[keyOld] !== 'Second');

  // ── a RETRY, which is the trigger that needs no reordering at all ────────
  // A single-track retry is its own job, so the engine numbers it 1 of 1
  // (measured against the real CLI). Nothing clears the index map on a normal
  // finish, so index 1 still pointed at the first row of the finished job:
  // every retry reported its result against a song that downloaded perfectly,
  // while the track being retried sat on "Retrying…" for ever.
  const retried = TRACKLIST.find(t => t.title === 'Second');
  const retryEvent = { row_id: retried.row_id, track: 'Second',
                       track_index: 1, track_total: 1, track_data: { row_id: retried.row_id } };
  check('a retry lands on the row it was started from',
    labels[fns.resolveTrackEventKey(retryEvent, byIndex, fallbackTrackKey)] === 'Second');
  check('CONTROL: without the id a retry lands on row 1 of the finished job',
    labels[fns.resolveTrackEventKey(
      { track: 'Second', track_index: 1, track_data: {} }, byIndex, fallbackTrackKey)]
      === TRACKLIST[0].title);

  // ── the identity must win over the index, not merely be consulted ────────
  const stale = { 1: 'row:someone-else', 2: 'row:someone-else-2' };
  check('row id is preferred over the index map',
    fns.resolveTrackEventKey({ row_id: 'ab12-2', track_index: 1 }, stale, fallbackTrackKey)
      === 'row:ab12-2');
  check('row id is read from track_data too',
    fns.resolveTrackEventKey({ track_data: { row_id: 'ab12-3' }, track_index: 1 },
      stale, fallbackTrackKey) === 'row:ab12-3');

  // ── the fallbacks still work, because some paths mint no ids ─────────────
  check('no id falls back to the index map',
    fns.resolveTrackEventKey({ track_index: 2, track_data: {} }, byIndex, fallbackTrackKey)
      === byIndex[2]);
  check('an empty id is treated as absent, not as a key',
    fns.resolveTrackEventKey({ row_id: '', track_index: 2, track_data: {} },
      byIndex, fallbackTrackKey) === byIndex[2]);
  check('no id and no index falls through to the stable service id',
    fns.resolveTrackEventKey({ track_data: { spotify_id: 'xyz' } }, {}, fallbackTrackKey)
      === 'spotify:xyz');
  check('a single-track retry still resolves to something',
    typeof fns.resolveTrackEventKey({ artist: 'A', track: 'B', track_data: {} },
      {}, fallbackTrackKey) === 'string');

  // ── a job whose ids the backend cleared mid-flight (_verify_row_ids) ─────
  // Rows were created with `row:` keys; the index map has to still address
  // them or the fallback lands nowhere at all.
  check('the index map still addresses id-created rows',
    labels[byIndex[1]] !== undefined && String(byIndex[1]).startsWith('row:'));

  // ── makeRowKey ──────────────────────────────────────────────────────────
  check('makeRowKey uses the backend id when there is one',
    fns.makeRowKey({ row_id: 'zz-7' }, 3) === 'row:zz-7');
  const legacy = fns.makeRowKey({}, 3);
  check('makeRowKey falls back to a positional key without one',
    typeof legacy === 'string' && legacy.includes('3') && !legacy.startsWith('row:'));
  check('two rows of an id-less tracklist do not collide',
    fns.makeRowKey({}, 1) !== fns.makeRowKey({}, 2));

  return fails;
}

// ── negative control ───────────────────────────────────────────────────────
// Applied to a COPY of the source; App.svelte is never written to.
const SABOTAGES = [
  ['the reported bug: index is consulted before the row id',
    src => src.replace(
      "    const rowId = String(data?.row_id || data?.track_data?.row_id || '');\n    if (rowId) return `row:${rowId}`;\n",
      '')],
  ['the row id is read but never used as the key',
    src => src.replace('if (rowId) return `row:${rowId}`;',
                       'if (false) return `row:${rowId}`;')],
  ['rows are built positionally, ignoring the id the backend sent',
    src => src.replace("    return rowId ? `row:${rowId}` : `track:${Date.now()}:${index}`;",
                       '    return `track:${Date.now()}:${index}`;')],
  ['the event key and the row key use different id formats',
    src => src.replace('if (rowId) return `row:${rowId}`;',
                       'if (rowId) return `rowid:${rowId}`;')],
  ['track_data is no longer consulted for the id',
    src => src.replace("String(data?.row_id || data?.track_data?.row_id || '')",
                       "String(data?.row_id || '')")],
  ['the index fallback is dropped, breaking id-less paths',
    src => src.replace(
      '    if (idx > 0 && currentPlaylistTrackKeysByIndex[idx]) {\n      return currentPlaylistTrackKeysByIndex[idx];\n    }\n',
      '')],
];

function main() {
  const fns = build(REAL);
  if (!fns) { console.log('REFUSING: could not extract the functions from App.svelte'); process.exit(1); }
  const fails = runChecks(fns);
  const total = fails.count();
  console.log(`${total - fails.length}/${total} checks passed`);
  fails.forEach(f => console.log(`  FAIL: ${f}`));

  console.log('\nnegative control:');
  let caught = 0;
  for (const [label, mutate] of SABOTAGES) {
    const broken = mutate(REAL);
    if (broken === REAL) { console.log(`  ANCHOR MISSING: ${label}`); continue; }
    const brokenFns = build(broken);
    let failed = !brokenFns;
    if (brokenFns) {
      try { failed = runChecks(brokenFns).length > 0; }
      catch { failed = true; }
    }
    if (failed) { caught++; console.log(`  caught: ${label}`); }
    else console.log(`  MISSED: ${label}`);
  }
  console.log(`\n${caught}/${SABOTAGES.length} sabotages caught`);
  process.exit(fails.length === 0 && caught === SABOTAGES.length ? 0 : 1);
}

main();
