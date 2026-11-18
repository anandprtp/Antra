/*
 * PORT-3 negative control. Each sabotage breaks ONE property of the shipped
 * code, runs the suite that owns it, and requires that suite to FAIL. Every file
 * is restored byte-identically (sha256 checked) whatever happens.
 *
 * Run: node test_importer_negctl.cjs      (from antra-wails/frontend)
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { spawnSync } = require('child_process');

const FRONTEND = __dirname;
const ROOT = path.resolve(FRONTEND, '..', '..');
const F = {
  ts: path.join(FRONTEND, 'src', 'lib', 'importer.ts'),
  py: path.join(ROOT, 'antra', 'utils', 'playlist_import.py'),
  cli: path.join(ROOT, 'antra', 'json_cli.py'),
  app: path.join(FRONTEND, 'src', 'App.svelte'),
};
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');

function runNode() {
  return spawnSync('node', ['test_importer.cjs'], { cwd: FRONTEND, encoding: 'utf8' }).status;
}
function runPy() {
  return spawnSync('python', [path.join(ROOT, 'antra', 'utils', 'test_playlist_import.py'), ROOT],
    { cwd: ROOT, encoding: 'utf8' }).status;
}

const SABOTAGES = [
  ['ts', "Antra JSON: descend into a string 'playlist' like the web parser",
    "(data && typeof data.playlist === 'object' && data.playlist !== null)\n    ? data.playlist",
    '(data && data.playlist)\n    ? data.playlist', runNode],
  ['ts', 'header mapping: last header wins (album artist beats track artist)',
    'if (mapped[key] === undefined && HEADER_MAPPINGS[key]', 'if (HEADER_MAPPINGS[key]', runNode],
  ['ts', "M3U: split on the LAST ' - '",
    "const idx = label.indexOf(' - ');", "const idx = label.lastIndexOf(' - ');", runNode],
  ['ts', 'truncation no longer reported',
    'truncated: tracks.length >= MAX_ROWS,', 'truncated: false,', runNode],
  ['ts', 'payload: a cleared name is sent as empty',
    "clean(typedName).slice(0, MAX_NAME) || clean(result.playlistName).slice(0, MAX_NAME) || 'Imported playlist'",
    'clean(typedName).slice(0, MAX_NAME)', runNode],
  ['py', 'playlist_position dropped (every file becomes 01)',
    'playlist_position=i + 1,', 'playlist_position=None,', runPy],
  ['py', 'bool accepted as an int',
    'if isinstance(value, bool) or not isinstance(value, int):', 'if not isinstance(value, int):', runPy],
  ['cli', 'temp payload never deleted',
    '                os.remove(path)\n', '                pass\n', runPy],
  ['cli', 'LibraryOrganizer named in the signature (start-time NameError)',
    'def _make_organizer(cfg):', 'def _make_organizer(cfg) -> LibraryOrganizer:', runPy],
  ['app', 'palette label without formats ("import csv" scores 0)',
    "label: tr('palette.cmd.import'),", "label: tr('palette.cmd.artistsearch'),", runNode],
  ['app', 'Export no longer hidden for an import',
    '{#if !currentJobIsImport}\n        <div class="playlist-export">', '{#if true}\n        <div class="playlist-export">', runNode],
];

const originals = {};
for (const k of Object.keys(F)) originals[k] = fs.readFileSync(F[k]);

let detected = 0;
const missed = [];
try {
  if (runNode() !== 0 || runPy() !== 0) { console.error('FATAL: suites are not green before sabotage'); process.exit(2); }
  for (const [key, label, from, to, run] of SABOTAGES) {
    const raw = originals[key];
    const crlf = raw.includes(Buffer.from('\r\n'));
    const text = raw.toString('utf8').split('\r\n').join('\n');
    const n = text.split(from).length - 1;
    if (n !== 1) { missed.push(`${label} — anchor matched ${n} times, sabotage not applied`); continue; }
    let out = text.replace(from, to);
    if (crlf) out = out.split('\n').join('\r\n');
    fs.writeFileSync(F[key], out);
    const status = run();
    fs.writeFileSync(F[key], raw);
    if (status !== 0) detected++; else missed.push(label);
  }
} finally {
  for (const k of Object.keys(F)) fs.writeFileSync(F[k], originals[k]);
}
for (const k of Object.keys(F)) {
  if (sha(fs.readFileSync(F[k])) !== sha(originals[k])) { console.error('FATAL: ' + F[k] + ' not restored'); process.exit(3); }
}
const greenAfter = runNode() === 0 && runPy() === 0;
console.log(`${detected}/${SABOTAGES.length} sabotages detected; files restored byte-identically; suites green after: ${greenAfter}`);
for (const m of missed) console.log('  MISSED ' + m);
process.exit(missed.length || !greenAfter ? 1 : 0);
