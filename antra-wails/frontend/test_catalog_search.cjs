/*
 * issue #58 — "I don't see a way to search by music title".
 *
 * The app could only search for an ARTIST. Song and album search were added by
 * routing a result's URL into the PV-1 preview, which means the frontend has
 * exactly three invariants worth pinning, and all three are easy to break by
 * accident later:
 *
 *   1. the artist kind still goes to the ARTIST modal, not the new one — the
 *      old flow must not be collateral damage;
 *   2. song/album go to SearchCatalog with the kind and source the user chose;
 *   3. clicking a result hands its URL to loadReleasePreview and does NOT
 *      switch tabs — the preview carries its own Download now, and switching
 *      would throw the user out of the search they are in the middle of.
 *
 * The real startSearch() and openSearchResult() are pulled out of App.svelte
 * and executed against stubs, so the assertions are about behaviour rather
 * than about any string this change could itself contain. A built-in negative
 * control re-runs everything against sabotaged copies.
 *
 * Run: node test_catalog_search.cjs   (from antra-wails/frontend)
 */
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, 'src', 'App.svelte');
const REAL = fs.readFileSync(SRC, 'utf8').replace(/\r\n/g, '\n');

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

/* The two functions close over component state, so each is rebuilt as a
   function of an explicit `s` (state) object and the collaborators it calls.
   Every `bareName` that is really component state is rewritten to `s.bareName`
   — mechanical, and it keeps the function BODY verbatim. */
const STATE = [
  'searchQuery', 'searchKind', 'searchSource', 'artistSearchReqId',
  'catalogLoading', 'showCatalogResults', 'showArtistSearch', 'catalogResults',
  'activeTab',
];

function build(src) {
  let startBody = bodyOf(src, 'async function startSearch(');
  let openBody = bodyOf(src, 'async function openSearchResult(');
  if (!startBody || !openBody) return null;

  const rewrite = (body) => {
    let out = body;
    for (const name of STATE) {
      // Assignments and reads, but never a property access (`x.searchKind`)
      // and never inside a longer identifier.
      out = out.replace(new RegExp(`(?<![.\\w$])${name}(?![\\w$])`, 'g'), `s.${name}`);
    }
    return out;
  };

  const deps = ['s', 'SearchCatalog', 'startArtistSearch', 'loadReleasePreview', 'addLog'];
  return {
    startSearch: new Function(...deps, `return (async () => {${rewrite(startBody)}})();`),
    openSearchResult: new Function(...deps, `return (async (row) => {${rewrite(openBody)}})(arguments[5]);`),
  };
}

const ROWS = [
  { kind: 'song', id: '1', title: 'Bohemian Rhapsody', artists: 'Queen',
    url: 'https://music.apple.com/us/album/bohemian-rhapsody/6781024026?i=6781024437',
    source: 'apple', match_score: 1.0 },
];

let pass = 0;
const fails = [];
function check(label, got, want) {
  if (JSON.stringify(got) === JSON.stringify(want)) pass++;
  else fails.push(`${label}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}
function checkTrue(label, got) { check(label, !!got, true); }

function freshState(over) {
  return Object.assign({
    searchQuery: 'bohemian rhapsody', searchKind: 'song', searchSource: 'apple',
    artistSearchReqId: 0, catalogLoading: false, showCatalogResults: false,
    showArtistSearch: false, catalogResults: [], activeTab: 'artist',
  }, over || {});
}

async function run(src, record) {
  const api = build(src);
  if (!api) { record('the two functions could not be extracted from App.svelte', false, true); return; }

  /* 1 — artist still goes to the artist flow, and never calls SearchCatalog. */
  {
    const s = freshState({ searchKind: 'artist' });
    let catalogCalls = 0, artistCalls = 0;
    await api.startSearch(s, async () => { catalogCalls++; return '[]'; },
      () => { artistCalls++; }, async () => {}, () => {});
    record('artist kind calls the artist search', artistCalls, 1);
    record('artist kind does NOT call SearchCatalog', catalogCalls, 0);
    record('artist kind does not open the new results modal', s.showCatalogResults, false);
  }

  /* 2 — song and album reach SearchCatalog with what the user chose. */
  for (const kind of ['song', 'album']) {
    const s = freshState({ searchKind: kind, searchSource: 'spotify' });
    let seen = null;
    await api.startSearch(s, async (q, k, src2) => { seen = [q, k, src2]; return JSON.stringify(ROWS); },
      () => { record(`${kind} must not call the artist search`, true, false); },
      async () => {}, () => {});
    record(`${kind}: query is trimmed and passed`, seen[0], 'bohemian rhapsody');
    record(`${kind}: the chosen kind is passed`, seen[1], kind);
    record(`${kind}: the chosen source is passed`, seen[2], 'spotify');
    record(`${kind}: results are stored`, s.catalogResults.length, 1);
    record(`${kind}: the results modal opens`, s.showCatalogResults, true);
    record(`${kind}: loading is cleared when done`, s.catalogLoading, false);
  }

  /* 3 — an empty query does nothing at all. */
  {
    const s = freshState({ searchQuery: '   ' });
    let calls = 0;
    await api.startSearch(s, async () => { calls++; return '[]'; }, () => {}, async () => {}, () => {});
    record('a blank query does not call the backend', calls, 0);
    record('a blank query does not open the modal', s.showCatalogResults, false);
  }

  /* 4 — a backend error closes the modal and logs, rather than showing an
         empty list that looks like "no results". */
  {
    const s = freshState();
    const logs = [];
    await api.startSearch(s, async () => '{"error":"search timed out"}', () => {},
      async () => {}, (lvl, msg) => logs.push([lvl, msg]));
    record('an error closes the modal', s.showCatalogResults, false);
    record('an error is logged', logs.length && logs[0][0], 'error');
    record('loading is cleared after an error', s.catalogLoading, false);
  }

  /* 5 — a stale response is discarded. The counter is shared with the artist
         search on purpose: switching kind mid-flight must invalidate whichever
         request is in the air. */
  {
    const s = freshState();
    const p = api.startSearch(s, async () => {
      s.artistSearchReqId++;              // the user searched again meanwhile
      return JSON.stringify(ROWS);
    }, () => {}, async () => {}, () => {});
    await p;
    record('a superseded response is not rendered', s.catalogResults.length, 0);
    // CONTROL: without the bump, the same response IS rendered — so the check
    // above is about staleness and not about the stub returning nothing.
    const s2 = freshState();
    await api.startSearch(s2, async () => JSON.stringify(ROWS), () => {}, async () => {}, () => {});
    record('CONTROL: a current response IS rendered', s2.catalogResults.length, 1);
  }

  /* 6 — the Apple-fallback notice. The backend falls back when Spotify is
         unreachable; presenting Apple rows under a "Spotify" label is the
         thing being prevented. */
  {
    const s = freshState({ searchSource: 'spotify' });
    const logs = [];
    await api.startSearch(s, async () => JSON.stringify(ROWS), () => {}, async () => {},
      (lvl, msg) => logs.push([lvl, msg]));
    checkTrueRecord(record, 'asking for Spotify and getting Apple is reported',
      logs.some(l => l[0] === 'warning'));
    // CONTROL: asking for Apple and getting Apple must NOT warn.
    const s2 = freshState({ searchSource: 'apple' });
    const logs2 = [];
    await api.startSearch(s2, async () => JSON.stringify(ROWS), () => {}, async () => {},
      (lvl, msg) => logs2.push([lvl, msg]));
    record('CONTROL: asking for Apple and getting Apple does not warn',
      logs2.filter(l => l[0] === 'warning').length, 0);
  }

  /* 7 — clicking a result. The three properties the feature rests on. */
  {
    const s = freshState();
    const previews = [];
    await api.openSearchResult(s, async () => '[]', () => {},
      async (url) => { previews.push(url); }, () => {}, ROWS[0]);
    record('the clicked row is resolved by its own URL', previews, [ROWS[0].url]);
    record('the results modal closes', s.showCatalogResults, false);
    // The preview renders outside the tab switch and carries its own Download
    // now, so the search box must stay where it is.
    record('the tab is NOT switched away from the search', s.activeTab, 'artist');
  }
  {
    const s = freshState();
    const previews = [];
    await api.openSearchResult(s, async () => '[]', () => {},
      async (url) => { previews.push(url); }, () => {}, { kind: 'song', id: '9' });
    record('a row with no URL resolves nothing', previews.length, 0);
  }
}

function checkTrueRecord(record, label, got) { record(label, !!got, true); }

(async () => {
  await run(REAL, check);

  if (fails.length) {
    console.log(`FAIL — ${fails.length} of ${pass + fails.length} checks failed:`);
    for (const f of fails) console.log('  -', f);
    process.exit(1);
  }
  const total = pass;
  console.log(`main suite: ${total} checks pass`);

  /* ── negative control ──────────────────────────────────────────────────── */
  const SABOTAGES = [
    ['artist kind falls into the catalogue path',
      "if (searchKind === 'artist') { startArtistSearch(); return; }",
      "if (false) { startArtistSearch(); return; }"],
    ['the chosen kind is ignored and every search is a song search',
      'await SearchCatalog(searchQuery.trim(), searchKind, searchSource)',
      "await SearchCatalog(searchQuery.trim(), 'song', searchSource)"],
    ['the chosen source is ignored',
      'await SearchCatalog(searchQuery.trim(), searchKind, searchSource)',
      "await SearchCatalog(searchQuery.trim(), searchKind, 'apple')"],
    // The anchor carries the SearchCatalog call on purpose: startArtistSearch
    // contains the identical two lines and comes FIRST in the file, so a
    // shorter anchor sabotaged the artist function instead and this control
    // was correctly reported MISSED.
    // The anchor carries the SearchCatalog call on purpose: startArtistSearch
    // contains the identical two lines and comes FIRST in the file, so a
    // shorter anchor sabotaged the artist function instead and this control
    // was correctly reported MISSED.
    ['a stale response is rendered anyway',
      'searchKind, searchSource);\n      if (reqId !== artistSearchReqId) return;',
      'searchKind, searchSource);'],
    ['a backend error leaves an empty modal open, looking like "no results"',
      "addLog('error', `Search error: ${parsed.error}`);\n        showCatalogResults = false;",
      "addLog('error', `Search error: ${parsed.error}`);"],
    ['the Apple-fallback notice is dropped',
      "addLog('warning', 'Spotify search was unavailable",
      "addLog('debug', 'Spotify search was unavailable"],
    ['clicking a result switches tab, throwing the user out of their search',
      'showCatalogResults = false;\n    await loadReleasePreview(row.url);',
      'showCatalogResults = false;\n    activeTab = \'url\';\n    await loadReleasePreview(row.url);'],
    ['a row with no URL is resolved anyway, previewing "undefined"',
      'if (!row?.url) return;',
      'if (!row) return;'],
    ['a blank query still hits the backend',
      'if (!searchQuery.trim()) return;\n    if (searchKind',
      'if (false) return;\n    if (searchKind'],
  ];

  let caught = 0;
  for (const [label, from, to] of SABOTAGES) {
    if (!REAL.includes(from)) { console.log(`  ??  ${label}  <-- anchor not found`); continue; }
    const broken = REAL.replace(from, to);
    const localFails = [];
    let localPass = 0;
    const rec = (l, got, want) => {
      if (JSON.stringify(got) === JSON.stringify(want)) localPass++;
      else localFails.push(l);
    };
    try { await run(broken, rec); } catch (e) { localFails.push('threw: ' + e.message); }
    if (localFails.length) { caught++; console.log(`  OK   ${label}\n       first miss: ${localFails[0]}`); }
    else console.log(`  MISS ${label}  <-- NOT caught`);
  }

  console.log(`\nnegative control: ${caught} of ${SABOTAGES.length} sabotages caught`);
  if (caught !== SABOTAGES.length) process.exit(1);
  console.log(`\nALL PASS — ${total} checks, ${caught}/${SABOTAGES.length} sabotages caught`);
})();
