/*
 * The preview screen numbered every row by `track_number` — where the track
 * sits on ITS OWN album. On a playlist that restarts at 1 each time the list
 * moves to another release (reported as 3, 4, 5, then 1 again), which reads as
 * scrambled and does not match what the file is named on disk: in the default
 * playlist_folder mode LibraryOrganizer.playlist_stem_for numbers by
 * `playlist_position`.
 *
 * This suite pulls the REAL reactive statements and the REAL row-number markup
 * out of App.svelte, evaluates them against fixture rows, and asserts on the
 * number that would be rendered. It never asserts on a comment or on a string
 * this change could itself contain.
 *
 * A built-in negative control re-runs every check against sabotaged copies of
 * the source and requires each sabotage to be caught.
 *
 * Run: node test_preview_numbering.cjs   (from antra-wails/frontend)
 */
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, 'src', 'App.svelte');
const REAL = fs.readFileSync(SRC, 'utf8').replace(/\r\n/g, '\n');

// The expression assigned by `$: name = ...;` — bracket aware, because both of
// these are written as ternaries spanning several lines.
function reactiveExpr(src, name) {
  const m = new RegExp('\\$:\\s*' + name + '\\s*=\\s*').exec(src);
  if (!m) return null;
  const start = m.index + m[0].length;
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    const c = src[i];
    if ('([{'.includes(c)) depth++;
    else if (')]}'.includes(c)) depth--;
    else if (c === ';' && depth === 0) return src.slice(start, i);
  }
  return null;
}

// `{#if C}A{:else}B{/if}` with `{expr}` interpolations -> a JS expression.
// One non-nested if/else, which is what the row number is.
function markupToExpr(markup) {
  const lit = (s) => '`' + s.replace(/\{([^{}]*)\}/g, (_, e) => '${' + e + '}') + '`';
  const m = /^\{#if ([^}]*)\}([\s\S]*)\{:else\}([\s\S]*)\{\/if\}$/.exec(markup.trim());
  if (!m) return lit(markup);
  return '((' + m[1] + ') ? ' + lit(m[2]) + ' : ' + lit(m[3]) + ')';
}

// The script block is TypeScript; `new Function` is not. Only parameter type
// annotations appear in these two expressions, so they are dropped — nothing
// else about the expression is rewritten.
function stripTypes(expr) {
  return expr.replace(/(\w+)\s*:\s*(any|number|string|boolean)\b/g, '$1');
}

function build(src) {
  const usesExpr = stripTypes(reactiveExpr(src, 'previewUsesPlaylistPos') || '');
  const discsExpr = stripTypes(reactiveExpr(src, 'previewHasDiscs') || '');
  const guard = /\{#if ([^\n]*?)\}\n\s*<span class="preview-row-num">/.exec(src);
  const span = /<span class="preview-row-num">([\s\S]*?)<\/span>/.exec(src);
  if (!usesExpr || !discsExpr || !guard || !span) return null;
  const numExpr = markupToExpr(span[1]);
  return {
    // Both reactive statements read `previewRelease`; the second also reads
    // previewUsesPlaylistPos, so it is passed in exactly as Svelte feeds it.
    usesPlaylistPos: new Function('previewRelease',
      'return (' + usesExpr + ');'),
    hasDiscs: new Function('previewRelease', 'previewUsesPlaylistPos',
      'return (' + discsExpr + ');'),
    shows: new Function('row', 'previewUsesPlaylistPos',
      'return !!(' + guard[1] + ');'),
    number: new Function('row', 'previewUsesPlaylistPos', 'previewHasDiscs',
      'return String(' + numExpr + ');'),
  };
}

// Three consecutive entries from the reported playlist: two tracks from the
// middle of one album, then track 1 of another — the exact shape that made the
// old numbering read as 4, 5, 1.
const PLAYLIST = [
  { title: 'interlude',      track_number: 4, disc_number: 1, playlist_position: 1 },
  { title: 'the.climb.back', track_number: 5, disc_number: 1, playlist_position: 2 },
  { title: 'Under The Sun',  track_number: 1, disc_number: 1, playlist_position: 3 },
];
// An album must keep album numbering. Its rows carry playlist_position too —
// measured on a live album preview, the backend fills it for ordinary album
// tracks — which is exactly why the switch cannot key off that field alone.
const ALBUM = [
  { title: 'Survival',     track_number: 1, disc_number: 1, playlist_position: 1 },
  { title: 'Nonstop',      track_number: 2, disc_number: 1, playlist_position: 2 },
  { title: 'Summer Games', track_number: 2, disc_number: 2, playlist_position: 14 },
];

function run(src, report) {
  const api = build(src);
  if (!api) { report('source could not be parsed', false); return; }
  const eq = (name, got, want) =>
    report(name, JSON.stringify(got) === JSON.stringify(want), got, want);

  const plRelease = { content_type: 'PLAYLIST', tracks: PLAYLIST };
  const alRelease = { content_type: 'ALBUM', tracks: ALBUM };

  const plUses = api.usesPlaylistPos(plRelease);
  const alUses = api.usesPlaylistPos(alRelease);
  eq('a playlist switches to playlist numbering', !!plUses, true);
  eq('an album does NOT, though its rows carry positions too', !!alUses, false);
  eq('no preview at all is not playlist numbering', !!api.usesPlaylistPos(null), false);

  const plDiscs = api.hasDiscs(plRelease, plUses);
  const alDiscs = api.hasDiscs(alRelease, alUses);
  eq('a multi-disc album still gets disc prefixes', !!alDiscs, true);

  eq('playlist rows are numbered 1,2,3 in playlist order',
     PLAYLIST.map((r) => api.number(r, plUses, plDiscs)), ['1', '2', '3']);
  eq('album rows keep their album numbers, disc-prefixed',
     ALBUM.map((r) => api.number(r, alUses, alDiscs)), ['1-1', '1-2', '2-2']);

  // A playlist whose tracks come from disc 2 of their albums must not have
  // that disc number glued onto the playlist position.
  const disc2 = [{ track_number: 3, disc_number: 2, playlist_position: 1 },
                 { track_number: 4, disc_number: 2, playlist_position: 2 }];
  const d2Rel = { content_type: 'PLAYLIST', tracks: disc2 };
  const d2Uses = api.usesPlaylistPos(d2Rel);
  eq('a playlist never shows an album disc prefix',
     disc2.map((r) => api.number(r, d2Uses, api.hasDiscs(d2Rel, d2Uses))), ['1', '2']);

  // A playlist whose rows carry no positions at all (an older preview cache,
  // or a fetcher that does not fill them) falls back to album numbering rather
  // than rendering a column of blanks.
  const noPos = [{ track_number: 7, disc_number: 1 }, { track_number: 2, disc_number: 1 }];
  const noPosRel = { content_type: 'PLAYLIST', tracks: noPos };
  const npUses = api.usesPlaylistPos(noPosRel);
  eq('a playlist with no positions falls back to album numbers',
     noPos.map((r) => api.number(r, npUses, api.hasDiscs(noPosRel, npUses))), ['7', '2']);

  // The guard decides whether a number is shown at all.
  eq('a playlist row with a position shows one', api.shows(PLAYLIST[0], true), true);
  eq('an album row with a track number shows one', api.shows(ALBUM[0], false), true);
  eq('a row with neither shows nothing', api.shows({ title: 'x' }, false), false);
  eq('a playlist row missing its position shows nothing',
     api.shows({ track_number: 9 }, true), false);
}

// ── main ────────────────────────────────────────────────────────────────────
let failed = 0, total = 0;
console.log('preview row numbering');
run(REAL, (name, ok, got, want) => {
  total++;
  if (ok) { console.log('  PASS  ' + name); }
  else {
    failed++;
    console.log('  FAIL  ' + name + '  got ' + JSON.stringify(got) +
                ' want ' + JSON.stringify(want));
  }
});

// ── negative control ────────────────────────────────────────────────────────
const SABOTAGES = [
  ['the reported bug: number by track_number always',
   (s) => s.replace('{#if previewUsesPlaylistPos}{row.playlist_position}{:else}',
                    '{#if false}{row.playlist_position}{:else}')],
  ['detection keys off the field instead of the release kind',
   (s) => s.replace("previewRelease.content_type === 'PLAYLIST'" + String.fromCharCode(10) + "    && ", '')],
  ['playlist detection always false',
   (s) => s.replace('some((x: any) => x.playlist_position)',
                    'some((x: any) => false)')],
  ['playlist detection always true',
   (s) => s.replace('some((x: any) => x.playlist_position)',
                    'some((x: any) => true)')],
  ['playlist branch falls through to the album branch',
   (s) => s.replace('{#if previewUsesPlaylistPos}{row.playlist_position}{:else}',
                    '{#if previewUsesPlaylistPos && false}{row.playlist_position}{:else}')],
  ['guard still tests track_number only',
   (s) => s.replace('{#if previewUsesPlaylistPos ? row.playlist_position : row.track_number}',
                    '{#if row.track_number}')],
  ['album numbering dropped entirely',
   (s) => s.replace("{previewHasDiscs ? (row.disc_number || 1) + '-' : ''}{row.track_number}",
                    '{row.playlist_position}')],
];
console.log('');
console.log('negative control');
let caught = 0;
for (const [name, mutate] of SABOTAGES) {
  const bad = mutate(REAL);
  if (bad === REAL) { console.log('  MISSED  ' + name + '  (sabotage did not apply)'); continue; }
  let sawFailure = false;
  try { run(bad, (_n, ok) => { if (!ok) sawFailure = true; }); }
  catch (e) { sawFailure = true; }
  if (sawFailure) { caught++; console.log('  caught  ' + name); }
  else { console.log('  MISSED  ' + name); }
}

console.log('');
if (failed === 0 && caught === SABOTAGES.length) {
  console.log('ALL PASS — ' + total + ' checks, ' + caught + '/' + SABOTAGES.length + ' sabotages caught');
} else {
  console.log('FAILURES — ' + failed + ' checks failed, ' +
              caught + '/' + SABOTAGES.length + ' sabotages caught');
  process.exit(1);
}
