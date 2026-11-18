/*
 * PORT-3 (v2.0.0) — playlist-file importer tests.
 *
 * Drives the SHIPPED src/lib/importer.ts (compiled with the esbuild Vite already
 * ships), never a re-implementation. Sections:
 *   1-8  the web suite's cases, ported unchanged in intent
 *   9    desktop deviations (Antra's own JSON export, .txt, payload builder)
 *   10   a REAL round-trip: FR-3's Python exporter writes CSV / M3U8 / JSON and
 *        this parser reads them back
 *   11   wiring across App.svelte / Go / Python, asserted on call sites
 *
 * XSPF needs a DOMParser, which Node does not have and this project does not
 * vendor; it is exercised in the running app instead (WebView2's own parser).
 *
 * Run: node test_importer.cjs      (from antra-wails/frontend)
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const vm = require('vm');
const { spawnSync } = require('child_process');
const esbuild = require('esbuild');

const FRONTEND = __dirname;
const ROOT = path.resolve(FRONTEND, '..', '..');
const SRC = path.join(FRONTEND, 'src', 'lib', 'importer.ts');

let pass = 0;
const failures = [];
function ok(cond, label) { if (cond) pass++; else failures.push(label); }
function eq(got, want, label) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) pass++; else failures.push(`${label}\n      got:  ${g}\n      want: ${w}`);
}

function loadImporter(file) {
  const js = esbuild.transformSync(fs.readFileSync(file, 'utf8'), { loader: 'ts', format: 'cjs' }).code;
  const mod = { exports: {} };
  const sandbox = { console, module: mod, exports: mod.exports };
  vm.createContext(sandbox);
  vm.runInContext(js, sandbox);
  return mod.exports;
}
const I = loadImporter(process.env.IMPORTER_SRC || SRC);
if (typeof I.parsePlaylistFile !== 'function') { console.error('FATAL: importer did not load'); process.exit(1); }

/* 1 ── Exportify CSV ───────────────────────────────────────────────────── */
const EXPORTIFY = [
  '"Track URI","Track Name","Artist Name(s)","Album Name","Album Artist Name(s)","Album Release Date","Disc Number","Track Number","Track Duration (ms)","Explicit","ISRC"',
  '"spotify:track:1","God\'s Plan","Drake","Scorpion","Drake","2018-06-29","1","7","198973","true","USCM51800004"',
  '"spotify:track:2","Ballad of a Thin Man - Live, 1966","Bob Dylan","The Real Royal Albert Hall","Bob Dylan","2016-11-25","1","3","472000","false","USSM11607777"',
  '"spotify:track:3","No ISRC Here","Some Artist, Another Artist","An Album","Some Artist","2020","1","1","180000","false",""',
].join('\r\n');
let r = I.parsePlaylistFile('my_playlist.csv', EXPORTIFY);
eq(r.tracks.length, 3, 'exportify: 3 tracks');
eq(r.tracks[0].title, "God's Plan", 'exportify: apostrophe title');
eq(r.tracks[0].isrc, 'USCM51800004', 'exportify: ISRC');
eq(r.tracks[0].duration_ms, 198973, 'exportify: ms header read as ms');
eq(r.tracks[0].track_number, 7, 'exportify: track number');
eq(r.tracks[0].release_year, 2018, 'exportify: year from date');
eq(r.tracks[1].title, 'Ballad of a Thin Man - Live, 1966', 'exportify: COMMA INSIDE QUOTES does not split the row');
eq(r.tracks[2].artists, ['Some Artist', 'Another Artist'], 'exportify: multi-artist split');
eq(r.tracks[2].isrc, null, 'exportify: empty ISRC -> null');
eq(r.tracks[0].artists, ['Drake'], 'exportify: track artist column wins over album artist');
eq(r.playlistName, 'my_playlist', 'exportify: name falls back to the file stem');

/* 2 ── TuneMyMusic mixed Type rows ─────────────────────────────────────── */
r = I.parsePlaylistFile('tmm.csv', [
  'Track name,Artist name,Album,Playlist name,Type,ISRC',
  'Alright,Kendrick Lamar,To Pimp a Butterfly,My Mix,Track,USUM71502296',
  'To Pimp a Butterfly,Kendrick Lamar,,My Mix,Album,',
  'Kendrick Lamar,,,My Mix,Artist,',
  'HUMBLE.,Kendrick Lamar,DAMN.,My Mix,Track,USUM71703086',
].join('\n'));
eq(r.tracks.length, 2, 'tmm: only Track rows kept');
eq(r.skipped.length, 2, 'tmm: album/artist rows REPORTED as skipped');
eq(r.playlistName, 'My Mix', 'tmm: playlist name column');
ok(r.skipped[0].reason.indexOf('not a track row') === 0, 'tmm: skip reason names the cause');

/* 3 ── Soundiiz + delimiters + encoding ────────────────────────────────── */
r = I.parsePlaylistFile('s.csv', 'title,artist,album,isrc,duration\nTeardrop,Massive Attack,Mezzanine,GBAAA9800011,5:29');
eq(r.tracks[0].duration_ms, 329000, 'soundiiz: M:SS -> ms');
eq(I.detectDelimiter('a;b;c\n1;2;3'), ';', 'delimiter: semicolon');
eq(I.detectDelimiter('a\tb\tc'), '\t', 'delimiter: tab');
eq(I.detectDelimiter('"a,b";c'), ';', 'delimiter: commas inside quotes ignored');
r = I.parsePlaylistFile('eu.csv', 'Titel;Artist;Album\nSonne;Rammstein;Mutter');
ok(r.tracks.length === 0 && !!r.error, 'unknown title header -> explicit error, no tracks');
r = I.parsePlaylistFile('bom.csv', '\uFEFFTrack Name,Artist\nOne,Two');
eq(r.tracks.length, 1, 'UTF-8 BOM stripped from header');
r = I.parsePlaylistFile('q.csv', 'Title,Artist\n"He said ""hi""",Someone');
eq(r.tracks[0].title, 'He said "hi"', '"" becomes a literal quote');
r = I.parsePlaylistFile('nl.csv', 'Title,Artist\n"Line1\nLine2",Someone');
eq(r.tracks.length, 1, 'NEWLINE inside quotes does not split the row');
r = I.parsePlaylistFile('trail.csv', 'Title,Artist\nA,B\n\n\n');
eq(r.tracks.length, 1, 'trailing blank lines ignored');

/* 4 ── ISRC + duration coercion ────────────────────────────────────────── */
eq(I.normalizeIsrc('us-cm5-18-00004'), 'USCM51800004', 'isrc: hyphens/case normalised');
eq(I.normalizeIsrc('NOTANISRC'), null, 'isrc: malformed rejected');
eq(I.normalizeIsrc('USCM5180000'), null, 'isrc: too short rejected');
eq(I.normalizeIsrc('QZES52142404'), 'QZES52142404', 'isrc: digit in registrant accepted');
eq(I.parseDurationMs('198973', true), 198973, 'dur: ms header as-is');
eq(I.parseDurationMs('210', false), 210000, 'dur: bare seconds');
eq(I.parseDurationMs('198973', false), 198973, 'dur: large bare number already ms');
eq(I.parseDurationMs('1:02:03', false), 3723000, 'dur: H:MM:SS');
eq(I.parseDurationMs('abc', false), null, 'dur: junk -> null');
eq(I.parseDurationMs('0', true), null, 'dur: zero ms -> null');
eq(I.parseDurationMs('0', false), null, 'dur: zero seconds -> null, never 0 ms');

/* 5 ── M3U ─────────────────────────────────────────────────────────────── */
r = I.parsePlaylistFile('rt.m3u', [
  '#EXTM3U', '#PLAYLIST:Road Trip',
  "#EXTINF:198,Drake - God's Plan", "Drake/Scorpion/07 - God's Plan.flac",
  '#EXTINF:225,Pink Floyd - Wish You Were Here - Live', '/music/pf.flac',
  'no-extinf-line.mp3',
].join('\n'));
eq(r.format, 'm3u', 'm3u: format from extension');
eq(r.tracks.length, 2, 'm3u: two EXTINF entries');
eq(r.playlistName, 'Road Trip', 'm3u: #PLAYLIST');
eq(r.tracks[1].title, 'Wish You Were Here - Live', "m3u: split on FIRST ' - ' only");
eq(r.tracks[0].duration_ms, 198000, 'm3u: EXTINF seconds -> ms');
eq(r.skipped.length, 1, 'm3u: bare media line reported, not guessed');
r = I.parsePlaylistFile('x.m3u', '#EXTM3U\n#EXTINF:-1,Some Stream\nhttp://x/s');
ok(r.tracks.length === 0 && r.skipped[0].reason.indexOf('no artist') !== -1, 'm3u: no artist -> skipped with reason');

/* 6 ── JSPF ────────────────────────────────────────────────────────────── */
r = I.parsePlaylistFile('p.jspf', JSON.stringify({ playlist: { title: 'JS Playlist', track: [
  { title: 'A', creator: 'B', album: 'C', duration: 123000, identifier: ['isrc:USCM51800004'] },
  { title: 'NoArtist' },
] } }));
eq(r.tracks.length, 1, 'jspf: one valid track');
eq(r.tracks[0].isrc, 'USCM51800004', 'jspf: ISRC from identifier array');
eq(r.playlistName, 'JS Playlist', 'jspf: title');
eq(r.skipped.length, 1, 'jspf: creator-less skipped');
r = I.parsePlaylistFile('bad.jspf', '{not json');
ok(!!r.error && r.tracks.length === 0, 'jspf: malformed JSON -> error, no throw');

/* 7 ── detection ───────────────────────────────────────────────────────── */
eq(I.detectFormat('x', '#EXTM3U\n#EXTINF:1,a - b'), 'm3u', 'detect: #EXTM3U');
eq(I.detectFormat('x', '{"playlist":{}}'), 'jspf', 'detect: JSON');
eq(I.detectFormat('x', '<playlist/>'), 'xspf', 'detect: XML');
eq(I.detectFormat('x', 'Title,Artist\na,b'), 'csv', 'detect: default csv');
eq(I.detectFormat('a.TSV', ''), 'csv', 'detect: extension case-insensitive');
eq(I.detectFormat('x', '\uFEFF#EXTM3U\n#EXTINF:1,a - b'), 'm3u', 'detect: sniffing survives a BOM');

/* 8 ── hardening ───────────────────────────────────────────────────────── */
r = I.parsePlaylistFile('h.csv', 'Title,Artist,Album\n../../../etc/passwd,../../evil,../x');
eq(r.tracks[0].title, '../../../etc/passwd', 'parser passes values on; the backend sanitises');
r = I.parsePlaylistFile('h2.csv', 'Title,Artist\n' + 'A'.repeat(5000) + ',B');
eq(r.tracks[0].title.length, 500, 'over-long field capped at 500');
const many = ['Title,Artist'].concat(Array.from({ length: 6000 }, (_, i) => `T${i},A`)).join('\n');
r = I.parsePlaylistFile('h3.csv', many);
ok(r.tracks.length === I.MAX_ROWS && r.truncated === true, 'row count capped at MAX_ROWS and truncation REPORTED');
eq(I.parsePlaylistFile('empty.csv', '').tracks.length, 0, 'empty file -> no tracks');
eq(I.splitArtists('Simon & Garfunkel'), ['Simon & Garfunkel'], "' & ' not split");
eq(I.splitArtists('A; B, C'), ['A', 'B', 'C'], "';' and ',' split");

/* 9 ── desktop deviations ──────────────────────────────────────────────── */
// Antra's own FR-3 JSON export: "playlist" is a STRING, tracks at the top level.
r = I.parsePlaylistFile('mine.json', JSON.stringify({
  generator: 'Antra', playlist: 'ICEMAN', source_url: 'https://tidal.com/album/1', track_count: 2,
  tracks: [
    { title: 'Ran To Atlanta', artists: ['Drake'], album: 'ICEMAN', duration_ms: 201000, isrc: 'USUG12604764', track_number: 3, disc_number: 1, release_year: 2026 },
    { title: 'Collab', artists: ['Future', 'Metro Boomin'], album: 'X', duration_ms: null, isrc: null },
  ],
}));
eq(r.tracks.length, 2, "antra json: Antra's own export re-imports (web parser returned 0)");
eq(r.playlistName, 'ICEMAN', 'antra json: title from the playlist STRING');
eq(r.tracks[0].isrc, 'USUG12604764', 'antra json: top-level isrc field');
eq(r.tracks[0].duration_ms, 201000, 'antra json: duration_ms');
eq([r.tracks[0].track_number, r.tracks[0].disc_number, r.tracks[0].release_year], [3, 1, 2026], 'antra json: numbers carried');
eq(r.tracks[1].artists, ['Future', 'Metro Boomin'], 'antra json: artists array kept as-is (no comma split)');
r = I.parsePlaylistFile('list.txt', '1. Drake - God\'s Plan  [Scorpion]  (3:18)');
ok(r.tracks.length === 0 && /cannot be re-imported/.test(r.error || ''), '.txt refused with a reason, not a misleading CSV error');

const res = I.parsePlaylistFile('C:\\Users\\x\\Downloads\\Road Trip.m3u', '#EXTM3U\n#PLAYLIST:Road Trip\n#EXTINF:1,A - B\nb.flac');
let p = I.buildImportPayload(res, '  My Own Name  ', 'C:\\Users\\x\\Downloads\\Road Trip.m3u');
eq(p.name, 'My Own Name', 'payload: typed name wins, trimmed');
eq(p.source_file, 'Road Trip.m3u', 'payload: source_file is a basename, no local path leaks into history');
eq(p.format, 'm3u', 'payload: format carried');
p = I.buildImportPayload(res, '   ', 'x.m3u');
eq(p.name, 'Road Trip', 'payload: a cleared name falls back to the parsed name, never empty');
p = I.buildImportPayload(res, 'N'.repeat(400), 'x.m3u');
eq(p.name.length, 200, 'payload: name capped at 200');
eq(Object.keys(p).sort(), ['format', 'name', 'source_file', 'tracks'], 'payload: exactly the keys the backend reads');

const src = fs.readFileSync(SRC, 'utf8');
ok(!/\bfetch\s*\(|XMLHttpRequest|WebSocket|window\.go|wailsjs/.test(src), 'importer makes no network or Wails calls');
ok(!/\bdocument\.|localStorage|sessionStorage/.test(src), 'importer touches no DOM or storage');

/* 10 ── round-trip against the REAL FR-3 exporter ──────────────────────── */
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'antra-port3-'));
const PY = `
import sys, json
sys.path.insert(0, sys.argv[1])
from antra.core.models import TrackMetadata
from antra.utils.playlist_export import export_playlist
import antra.utils.playlist_export as pe
assert pe.__file__.startswith(sys.argv[1]), pe.__file__
tracks = [
  TrackMetadata(title="Ballad of a Thin Man - Live, 1966", artists=["Bob Dylan"], album="Royal Albert Hall",
                duration_ms=472000, isrc="USSM11607777", track_number=3, disc_number=1, release_year=2016),
  TrackMetadata(title='He said "hi"', artists=["Future", "Metro Boomin"], album="Café Été",
                duration_ms=198000, isrc=None, track_number=1),
  TrackMetadata(title="Wish You Were Here - Live", artists=["Pink Floyd"], album="Pulse",
                duration_ms=225000, isrc="GBN9Y1100081"),
]
out = sys.argv[2]
for fmt in ("csv", "m3u8", "json"):
    export_playlist(tracks, fmt, out + "/rt." + fmt, playlist_title="Round Trip", source_url="https://x/y")
print("OK")
`;
// This file is CRLF on disk; a \r reaching `python -c` is a SyntaxError.
const py = spawnSync('python', ['-c', PY.replace(/\r\n/g, '\n'), ROOT, tmp], { encoding: 'utf8' });
if (py.status !== 0 || !/OK/.test(py.stdout)) {
  failures.push('round-trip: python exporter failed\n      ' + (py.stderr || py.stdout).trim().split('\n').slice(-3).join('\n      '));
} else {
  const want = [
    ['Ballad of a Thin Man - Live, 1966', ['Bob Dylan']],
    ['He said "hi"', ['Future', 'Metro Boomin']],
    ['Wish You Were Here - Live', ['Pink Floyd']],
  ];
  for (const fmt of ['csv', 'm3u8', 'json']) {
    const file = path.join(tmp, 'rt.' + fmt);
    const rr = I.parsePlaylistFile(file, fs.readFileSync(file, 'utf8'));
    eq(rr.tracks.map((t) => [t.title, t.artists]), want, `round-trip ${fmt}: titles + artists survive in order`);
    eq(rr.tracks.map((t) => t.duration_ms), [472000, 198000, 225000], `round-trip ${fmt}: durations`);
    eq(rr.skipped.length, 0, `round-trip ${fmt}: nothing skipped`);
    if (fmt !== 'm3u8') {
      eq(rr.tracks.map((t) => t.isrc), ['USSM11607777', null, 'GBN9Y1100081'], `round-trip ${fmt}: ISRCs`);
      eq(rr.tracks[1].album, 'Café Été', `round-trip ${fmt}: accented album (CSV is utf-8-sig)`);
    }
    if (fmt !== 'csv') eq(rr.playlistName, 'Round Trip', `round-trip ${fmt}: playlist title`);
  }
}
fs.rmSync(tmp, { recursive: true, force: true });

/* 11 ── wiring ─────────────────────────────────────────────────────────── */
const app = fs.readFileSync(path.join(FRONTEND, 'src', 'App.svelte'), 'utf8').split('\r\n').join('\n');
function body(name) {
  const m = new RegExp(`(?:async )?function ${name}\\(`).exec(app);
  if (!m) return '';
  let i = app.indexOf('{', m.index), depth = 0;
  for (let j = i; j < app.length; j++) {
    if (app[j] === '{') depth++;
    else if (app[j] === '}' && --depth === 0) return app.slice(i, j + 1);
  }
  return '';
}
const confirm = body('confirmImport');
ok(/buildImportPayload\(importResult, importName, importFileName\)/.test(confirm), 'wiring: confirmImport builds the payload with the shared builder');
ok(/EnqueueImport\(JSON\.stringify\(payload\)/.test(confirm), 'wiring: confirmImport hands exactly that payload to Go (via the SF-1 queue)');
// SF-1 — both kinds start through queue_job_started, which owns the reset and
// the import flag; the entry points must not reset a view that may be running.
{
  const qs = app.slice(app.indexOf("if (payload.type === 'queue_job_started') {"), app.indexOf("if (payload.type === 'playlist_loaded') {"));
  ok(qs.length > 0 && /resetDownloadView\(\)/.test(qs) && !/resetDownloadView\(\)/.test(confirm) && !/resetDownloadView\(\)/.test(body('startDownload')), 'wiring: both entry points share resetDownloadView (through queue_job_started)');
  ok(/currentJobIsImport = it\.kind === 'import'/.test(qs), 'wiring: import flag set by import, cleared by a link download');
}
ok(/parsePlaylistFile\(importFileName, res\.text/.test(body('openImportFile')), 'wiring: picker text goes through the real parser');
ok(/id: 'act\.import'[\s\S]{0,400}run: \(\) => \{ activeTab = 'url'; openImportFile\(\); \}/.test(app), 'wiring: palette command runs the importer');
ok(/on:click=\{openImportFile\}/.test(app), 'wiring: Import file button is bound');
ok(/\{#if !currentJobIsImport\}\s*<div class="playlist-export">/.test(app), 'wiring: Export hidden for an import');
ok(/\{#if \/\^\(https\?:\|apple-music:\)\/i\.test\(item\.url \|\| ''\)\}/.test(app), 'wiring: history Re-queue gated to real links');

// Palette ranking, measured with the REAL matcher and the REAL settings catalog,
// using the label and keywords read out of App.svelte (not restated here).
{
  const P = loadImporter(path.join(FRONTEND, 'src', 'lib', 'palette.ts'));
  const CAT = loadImporter(path.join(FRONTEND, 'src', 'lib', 'settings-catalog.ts')).SETTINGS_CATALOG;
  /* SF-3: the label and hint are catalogue keys now, resolved here to the
     same English the user sees so the ranking assertions below are unchanged. */
  const EN = JSON.parse(fs.readFileSync(
    path.join(FRONTEND, 'src', 'locales', 'en.json'), 'utf8'));
  const enFlat = (o, p, out) => {
    out = out || {};
    for (const k of Object.keys(o)) {
      const kk = p ? p + '.' + k : k;
      if (o[k] && typeof o[k] === 'object') enFlat(o[k], kk, out); else out[kk] = o[k];
    }
    return out;
  };
  const EN_FLAT = enFlat(EN, '');
  const tr = (k) => (EN_FLAT[k] !== undefined ? EN_FLAT[k] : k);
  const m = /id: 'act\.import', group: tr\('[a-z0-9_.]+'\), label: tr\('([a-z0-9_.]+)'\),\s*hint: tr\('([a-z0-9_.]+)'\),\s*keywords: \[([^\]]*)\]/.exec(app);
  ok(!!m, 'palette: act.import command found in App.svelte');
  if (m) {
    const importCmd = { id: 'act.import', group: 'Actions', label: tr(m[1]), hint: tr(m[2]),
      keywords: [...m[3].matchAll(/'([^']*)'/g)].map((x) => x[1]), run() {} };
    const cmds = [importCmd, ...CAT.map((s) => ({ id: 'set.' + s.id, group: 'Settings', label: tr(s.label),
      hint: tr(s.group), keywords: ['setting', 'preference', 'option', tr(s.desc), tr(s.group)], run() {} }))];
    for (const q of ['import', 'import csv', 'import m3u', 'import xspf', 'import json', 'import playlist', 'csv', 'exportify', 'playlist file']) {
      eq(P.searchCommands(q, cmds)[0]?.cmd.id, 'act.import', `palette: "${q}" ranks the import command first`);
    }
  }
}

const goSrc = fs.readFileSync(path.join(ROOT, 'antra-wails', 'playlist_import.go'), 'utf8');
const cli = fs.readFileSync(path.join(ROOT, 'antra', 'json_cli.py'), 'utf8');
const binds = fs.readFileSync(path.join(FRONTEND, 'wailsjs', 'go', 'main', 'App.js'), 'utf8');
ok(/"--import-json", path\)/.test(goSrc) && /parser\.add_argument\("--import-json"/.test(cli), 'wiring: Go and Python agree on --import-json');
ok(/if args\.import_json:\s*\n\s*_run_playlist_import\(args\.import_json, cfg, service, options\)/.test(cli), 'wiring: json_cli dispatches the flag');
ok(/\['StartImportDownload'\]\(arg1\)/.test(binds) && /\['PickPlaylistFile'\]\(\)/.test(binds), 'wiring: bindings exported');
const goKeys = ['name', 'source_file', 'format', 'tracks'];
ok(goKeys.every((k) => new RegExp(`data\\.get\\("${k}"\\)`).test(fs.readFileSync(path.join(ROOT, 'antra', 'utils', 'playlist_import.py'), 'utf8'))), 'wiring: Python reads every key the payload builder writes');

console.log(`\n${pass} passed, ${failures.length} failed`);
for (const f of failures) console.log('  FAIL ' + f);
process.exit(failures.length ? 1 : 0);
