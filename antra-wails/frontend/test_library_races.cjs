/*
 * AUDIT-DESKTOP-15 - out-of-order responses in the My Library screen.
 *
 * openDownloadedRelease() and refreshDownloadedMusicLibrary() awaited a backend
 * call and then wrote the result into shared state with no check that it was
 * still the latest request. Click release A, then B: if A's reply lands last,
 * B is highlighted but A's tracks are shown (and Play plays them), and whichever
 * reply lands FIRST switches the spinner off while the other is still loading.
 * The rest of the file (search, discography, release preview) already guards
 * with a request counter; these two did not.
 *
 * Pulls the REAL function bodies out of App.svelte and runs them against a state
 * object with deferred fake backend calls, so the arrival order is controlled.
 * Re-runs against sabotaged copies of the source and requires each to be caught.
 *
 * Run: node test_library_races.cjs   (from antra-wails/frontend)
 */
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, 'src', 'App.svelte');
const REAL = fs.readFileSync(SRC, 'utf8').replace(/\r\n/g, '\n');

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

const stripTs = (s) => s.replace(/catch \((\w+): any\)/g, 'catch ($1)')
  .replace(/\(item: LibraryReleaseSummary\)/g, '(item)');

function deferred() {
  let resolve; const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

function load(src, name, params) {
  const body = bodyOf(src, 'async function ' + name + '(');
  if (!body) return null;
  return new Function(...params, 'state', 'with (state) { return (async () => {' + stripTs(body) + '})(); }');
}

function freshState(extra = {}) {
  return Object.assign({
    downloadedSelectedPath: '', downloadedSelectedRelease: null, downloadedSelectedReleaseLoading: false,
    downloadedLibraryError: '', downloadedView: 'albums',
    downloadedLibrary: { albums: [], playlists: [] }, downloadedLibraryLoading: false,
    downloadedReleaseReqId: 0, downloadedLibraryReqId: 0,
  }, extra);
}

const tick = () => new Promise((r) => setTimeout(r, 0));

async function run(src) {
  const failures = [];
  const check = (label, ok, detail) => { if (!ok) failures.push(label + (detail ? ' :: ' + detail : '')); };

  const openFn = load(src, 'openDownloadedRelease', ['release', 'GetDownloadedRelease', 'adoptArtwork']);
  const refreshFn = load(src, 'refreshDownloadedMusicLibrary', ['GetDownloadedMusicLibrary', 'openDownloadedRelease']);
  if (!openFn || !refreshFn) return ['could not extract the functions from App.svelte'];

  const A = { relative_path: 'A', kind: 'album' };
  const B = { relative_path: 'B', kind: 'album' };
  const payload = (name) => JSON.stringify({ title: name, tracks: [{ title: name + '-1' }], artwork_url: '' });

  // -- 1. A then B, replies arrive B first, A last (the stale one) ------------
  {
    const st = freshState();
    const calls = {};
    const GET = (p) => { calls[p] = deferred(); return calls[p].promise; };
    const a = openFn(A, GET, () => {}, st);
    const b = openFn(B, GET, () => {}, st);
    calls.B.resolve(payload('B')); await b;
    check('B reply applied', st.downloadedSelectedRelease && st.downloadedSelectedRelease.title === 'B', JSON.stringify(st.downloadedSelectedRelease));
    calls.A.resolve(payload('A')); await a; await tick();
    check('a late reply for the OLD selection does not replace the shown release',
      st.downloadedSelectedRelease && st.downloadedSelectedRelease.title === 'B' && st.downloadedSelectedPath === 'B',
      JSON.stringify({ shown: st.downloadedSelectedRelease && st.downloadedSelectedRelease.title, path: st.downloadedSelectedPath }));
  }

  // -- 2. spinner: the stale reply landing first must not switch it off -------
  {
    const st = freshState();
    const calls = {};
    const GET = (p) => { calls[p] = deferred(); return calls[p].promise; };
    const a = openFn(A, GET, () => {}, st);
    const b = openFn(B, GET, () => {}, st);
    calls.A.resolve(payload('A')); await a; await tick();
    check('spinner stays on while the latest request is still loading', st.downloadedSelectedReleaseLoading === true,
      'loading=' + st.downloadedSelectedReleaseLoading);
    check('the stale reply is not shown', st.downloadedSelectedRelease === null, JSON.stringify(st.downloadedSelectedRelease));
    calls.B.resolve(payload('B')); await b; await tick();
    check('spinner off and B shown once the latest reply arrives',
      st.downloadedSelectedReleaseLoading === false && st.downloadedSelectedRelease.title === 'B');
  }

  // -- 3. a stale ERROR must not overwrite a good result -----------------------
  {
    const st = freshState();
    const calls = {};
    const GET = (p) => { calls[p] = deferred(); return calls[p].promise; };
    const a = openFn(A, GET, () => {}, st);
    const b = openFn(B, GET, () => {}, st);
    calls.B.resolve(payload('B')); await b;
    calls.A.resolve(JSON.stringify({ error: 'A is gone' })); await a; await tick();
    check('a stale error does not surface over a good newer result', st.downloadedLibraryError === '', st.downloadedLibraryError);
  }

  // -- control: in-order replies and a single request behave as before ---------
  {
    const st = freshState();
    const GET = () => Promise.resolve(payload('A'));
    await openFn(A, GET, () => {}, st);
    check('control: a single request is applied', st.downloadedSelectedRelease.title === 'A' && st.downloadedSelectedReleaseLoading === false
      && st.downloadedView === 'albums');
    const st2 = freshState();
    const calls = {};
    const GET2 = (p) => { calls[p] = deferred(); return calls[p].promise; };
    const a = openFn(A, GET2, () => {}, st2);
    calls.A.resolve(payload('A')); await a;
    const b = openFn(B, GET2, () => {}, st2);
    calls.B.resolve(payload('B')); await b;
    check('control: A then B in order ends on B', st2.downloadedSelectedRelease.title === 'B');
    const st3 = freshState();
    await openFn(A, () => Promise.resolve(JSON.stringify({ error: 'boom' })), () => {}, st3);
    check('control: a current error is still reported', st3.downloadedLibraryError === 'boom', st3.downloadedLibraryError);
  }

  // -- 4. library refresh: an older reply must not overwrite a newer one -------
  {
    const st = freshState();
    const calls = [];
    const GETLIB = () => { const d = deferred(); calls.push(d); return d.promise; };
    const r1 = refreshFn(GETLIB, async () => {}, st);
    const r2 = refreshFn(GETLIB, async () => {}, st);
    calls[1].resolve(JSON.stringify({ albums: [{ relative_path: 'NEW', kind: 'album' }], playlists: [] })); await r2;
    calls[0].resolve(JSON.stringify({ albums: [{ relative_path: 'OLD', kind: 'album' }], playlists: [] })); await r1; await tick();
    check('an older library listing does not overwrite the newer one',
      st.downloadedLibrary.albums.length === 1 && st.downloadedLibrary.albums[0].relative_path === 'NEW',
      JSON.stringify(st.downloadedLibrary.albums));
    check('library spinner is off after the latest reply', st.downloadedLibraryLoading === false);
  }
  {
    const st = freshState();
    const calls = [];
    const GETLIB = () => { const d = deferred(); calls.push(d); return d.promise; };
    const r1 = refreshFn(GETLIB, async () => {}, st);
    const r2 = refreshFn(GETLIB, async () => {}, st);
    calls[0].resolve(JSON.stringify({ albums: [], playlists: [] })); await r1; await tick();
    check('library spinner stays on while the latest refresh is still loading', st.downloadedLibraryLoading === true);
    calls[1].resolve(JSON.stringify({ albums: [], playlists: [] })); await r2;
  }
  {
    const st = freshState();
    await refreshFn(() => Promise.resolve(JSON.stringify({ albums: [{ relative_path: 'X', kind: 'album' }], playlists: [] })),
      async () => {}, st);
    check('control: a single refresh is applied', st.downloadedLibrary.albums.length === 1 && st.downloadedLibraryLoading === false);
  }
  return failures;
}

(async () => {
  const base = await run(REAL);
  if (base.length) { console.log('FAIL (unsabotaged)'); base.forEach((f) => console.log('  ' + f)); process.exit(1); }
  console.log('ok: all checks pass on the real source');

  const sabotages = {
    'release reply applied without a request check': (s) => s.replace('if (mine !== downloadedReleaseReqId) return;', ''),
    'release spinner cleared by any request': (s) => s.replace('if (mine === downloadedReleaseReqId) downloadedSelectedReleaseLoading = false;', 'downloadedSelectedReleaseLoading = false;'),
    'release request counter never advanced': (s) => s.replace('const mine = ++downloadedReleaseReqId;', 'const mine = downloadedReleaseReqId;'),
    'library reply applied without a request check': (s) => s.replace('if (libMine !== downloadedLibraryReqId) return;', ''),
    'library spinner cleared by any request': (s) => s.replace('if (libMine === downloadedLibraryReqId) downloadedLibraryLoading = false;', 'downloadedLibraryLoading = false;'),
  };
  let missed = 0;
  for (const [name, mutate] of Object.entries(sabotages)) {
    const mutated = mutate(REAL);
    if (mutated === REAL) { console.log('NO-OP sabotage (anchor gone): ' + name); missed++; continue; }
    const f = await run(mutated);
    console.log((f.length ? 'caught  ' : 'MISSED  ') + name);
    if (!f.length) missed++;
  }
  if (missed) { console.log(missed + ' sabotage(s) not caught'); process.exit(1); }
  console.log('all sabotages caught');
})();
