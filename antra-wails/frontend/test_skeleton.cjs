/*
 * PORT-4 (v2.0.0) — skeleton loaders while a release resolves.
 *
 * Geometry itself is verified in the running app (see the internal v2.0.0 notes);
 * this suite pins the MECHANISMS that make that geometry correct and that a
 * later edit could silently break:
 *   - the visibility condition, EVALUATED over every combination of inputs;
 *   - which code paths raise and drop the flag (and that retry never does);
 *   - that the skeleton is built from the real header/row classes, and that
 *     the real markup still uses those classes (rename one and this fails);
 *   - 1lh, not 1em; text tint; reduced motion.
 * A built-in negative control re-runs every check against sabotaged copies of
 * App.svelte and requires each sabotage to be caught.
 *
 * Run: node test_skeleton.cjs   (from antra-wails/frontend)
 */
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, 'src', 'App.svelte');
const REAL = fs.readFileSync(SRC, 'utf8').replace(/\r\n/g, '\n');

function between(src, start, end, from = 0) {
  const a = src.indexOf(start, from);
  if (a < 0) return null;
  const b = src.indexOf(end, a + start.length);
  if (b < 0) return null;
  return src.slice(a, b);
}

function runChecks(src) {
  const fails = [];
  let n = 0;
  const check = (label, cond) => { n++; if (!cond) fails.push(label); };
  fails.count = () => n;

  // ── 1. The visibility condition, evaluated ────────────────────────────────
  // PV-1 added a fourth input (a preview being resolved) and made the expression
  // span two lines, hence [\s\S] rather than [^\n].
  const m = src.match(/\$: showSkeleton = ([\s\S]*?);\n/);
  check('showSkeleton reactive declaration exists', !!m);
  if (m) {
    let fn = null;
    try {
      fn = new Function('awaitingTracklist', 'isDownloading', 'trackOrder',
        'previewReleaseLoading', 'return (' + m[1] + ');');
    } catch { check('showSkeleton expression parses', false); }
    if (fn) {
      const empty = [], one = ['track:1'];
      check('visible only while awaiting + downloading + no rows',
        fn(true, true, empty, false) === true);
      check('hidden once a row exists', fn(true, true, one, false) === false);
      check('hidden when not downloading (error / cancel / failed start)', fn(true, false, empty, false) === false);
      check('hidden when not awaiting (retry of a finished job)', fn(false, true, empty, false) === false);
      check('hidden in the idle state', fn(false, false, empty, false) === false);
      // PV-1 — resolving a link shows the placeholders on its own, and must do so
      // EVEN when the last job's rows are still on screen; otherwise the previous
      // release's header and tracklist sit there looking like the answer.
      check('visible while a preview is being resolved', fn(false, false, empty, true) === true);
      check('preview placeholders win over leftover rows', fn(false, false, one, true) === true);
    }
  }
  check('previewReleaseLoading declared', /let previewReleaseLoading = false;/.test(src));
  check('awaitingTracklist declared', /let awaitingTracklist = false;/.test(src));

  // ── 2. Who raises and drops the flag ──────────────────────────────────────
  const reset = between(src, 'function resetDownloadView() {', '\n  }\n');
  check('resetDownloadView raises awaitingTracklist', !!reset && /awaitingTracklist = true;/.test(reset));

  const pl = between(src, "if (payload.type === 'playlist_loaded') {", "if (payload.type === 'tracks_appended')");
  check('playlist_loaded drops awaitingTracklist', !!pl && /awaitingTracklist = false;/.test(pl));

  const pe = between(src, "if (payload.type === 'process_ended') {", "} else if (payload.status === 'failed')");
  check('process_ended drops awaitingTracklist', !!pe && /awaitingTracklist = false;/.test(pe));

  // SF-1 — every job now starts through the queue, so the reset lives in ONE
  // place: the queue_job_started handler. Entry points must NOT reset the view
  // themselves, or queueing a second link would wipe the running job's rows.
  const qs = between(src, "if (payload.type === 'queue_job_started') {", "if (payload.type === 'playlist_loaded') {");
  check('queue_job_started goes through resetDownloadView', !!qs && /resetDownloadView\(\);/.test(qs));
  check('queue_job_started sets currentJobIsImport from the job kind', !!qs && /currentJobIsImport = it\.kind === 'import';/.test(qs));

  // PV-1 — a pasted link now RESOLVES first; enqueueing moved to Download now.
  // The invariant is the same one: an entry point must not reset the view.
  const sd = between(src, 'async function startDownload() {', 'async function startArtistSearch()');
  check('startDownload previews and does not reset the running view',
    !!sd && /loadReleasePreview\(/.test(sd) && !/resetDownloadView\(\)/.test(sd));
  check('startDownload does not download anything itself', !!sd && !/enqueueUrls\(/.test(sd));

  // Download now queues the selection. It must NOT reset either: while a job is
  // running this only joins the queue, so resetting would wipe the running rows.
  const dp = between(src, 'async function downloadPreviewSelection() {', '\n  }\n');
  check('downloadPreviewSelection queues the selection',
    !!dp && /EnqueuePreviewSelection\(/.test(dp));
  check('downloadPreviewSelection does not reset the running view',
    !!dp && !/resetDownloadView\(\)/.test(dp));
  // Resolving a link must not reset the view either - the user can still cancel
  // the preview and go back to watching what is downloading.
  const lp = between(src, 'async function loadReleasePreview(', '\n  // Download now');
  check('loadReleasePreview does not reset the running view',
    !!lp && !/resetDownloadView\(\)/.test(lp));

  const ci = between(src, 'async function confirmImport() {', '\n  }\n');
  check('confirmImport enqueues and does not reset the running view', !!ci && /EnqueueImport\(/.test(ci) && !/resetDownloadView\(\)/.test(ci));

  const rt = between(src, 'async function retryFailedTrack(', '\n  }\n');
  check('retryFailedTrack exists', !!rt);
  check('retryFailedTrack never raises the skeleton', !!rt && !/resetDownloadView\(\)|awaitingTracklist = true/.test(rt));

  const disc = between(src, 'const albumUrls = [...discographySelected];', 'await enqueueUrls(albumUrls');
  check('discography download goes through the queue', !!disc);
  check('discography download does not hand-copy or call the reset', !!disc && !/playlistTitle = ''|resetDownloadView\(\)/.test(disc));

  // the only writes of `awaitingTracklist = true` are in resetDownloadView
  const raises = (src.match(/awaitingTracklist = true;/g) || []).length;
  check('awaitingTracklist raised in exactly one place', raises === 1);

  // ── 3. Built from the real classes ────────────────────────────────────────
  const skHead = between(src, '{:else if showSkeleton}', '{/if}');
  check('skeleton header is the else-branch of the real header block',
    !!skHead && src.indexOf('{#if playlistTitle || playlistArtwork}') < src.indexOf('{:else if showSkeleton}'));
  for (const cls of ['playlist-header', 'playlist-cover', 'playlist-meta', 'playlist-type',
    'playlist-title', 'playlist-artists', 'playlist-info-line', 'playlist-quality-badge']) {
    check(`skeleton header uses .${cls}`, !!skHead && new RegExp('class="[^"]*\\b' + cls + '\\b').test(skHead));
  }
  check('skeleton header is aria-hidden', !!skHead && /skel-header" aria-hidden="true"/.test(skHead));

  // PV-1 — the live header is reached only when no preview is being resolved, so
  // the branch opens with that guard now.
  const realHead = between(src, '{:else if !previewReleaseLoading && (playlistTitle || playlistArtwork)}', '{:else if showSkeleton}');
  for (const cls of ['playlist-header', 'playlist-cover', 'playlist-meta', 'playlist-type',
    'playlist-title', 'playlist-artists', 'playlist-info-line', 'playlist-quality-badge']) {
    check(`real header still uses .${cls}`, !!realHead && new RegExp('class="' + cls + '"').test(realHead));
  }

  // PV-1 — the preview's own branches come first in the tracklist chain, so the
  // skeleton branch is an {:else if}. Its condition no longer repeats the row
  // count: showSkeleton already carries it for a job, and a preview being
  // resolved must show placeholders whatever is left on screen.
  const skRows = between(src, '{:else if showSkeleton}', '{:else if trackOrder.length === 0}',
    src.indexOf('<div class="tracklist" '));
  check('skeleton rows block exists', !!skRows);
  check('skeleton rows iterate SKEL_WIDTHS', !!skRows && /\{#each SKEL_WIDTHS as w/.test(skRows));
  for (const cls of ['track-row', 'track-row-main', 'track-row-head', 'prev-btn',
    'track-row-name', 'track-row-side', 'track-row-status']) {
    check(`skeleton row uses .${cls}`, !!skRows && new RegExp('class="[^"]*\\b' + cls + '\\b').test(skRows));
  }
  check('skeleton rows are aria-hidden', !!skRows && /skel-row" aria-hidden="true"/.test(skRows));
  const w = src.match(/const SKEL_WIDTHS[^=]*= \[([\s\S]*?)\];/);
  check('SKEL_WIDTHS has 8 rows', !!w && (w[1].match(/\[\s*\d+\s*,\s*\d+\s*\]/g) || []).length === 8);

  const realRow = between(src, '{@const state = activeTracks[trackKey]}', '{#if state.mode === \'progress\'}');
  for (const [cls, re] of [
    ['track-row', /<div class="track-row"/], ['track-row-main', /class="track-row-main"/],
    ['track-row-head', /class="track-row-head"/], ['prev-btn', /class="prev-btn"/],
    ['track-row-name', /class="track-row-name"/], ['track-row-side', /class="track-row-side"/],
    ['track-row-status', /class="track-row-status"/]]) {
    check(`real row still uses .${cls}`, !!realRow && re.test(realRow));
  }

  // ── 3a. PV-1 — what the one button actually does ──────────────────────────
  // The trap this guards: with a preview open the button says "Download now", so
  // typing a DIFFERENT link and pressing it would download the old release. Both
  // the condition and the label/handler must read the same derived flag, or they
  // drift apart and the label lies about what happens.
  const stale = src.match(/\$: previewStale = ([\s\S]*?);\n/);
  const downloads = src.match(/\$: urlBarDownloads = ([\s\S]*?);\n/);
  check('previewStale is derived', !!stale);
  check('urlBarDownloads is derived', !!downloads);
  if (stale && downloads) {
    let fn = null;
    try {
      fn = new Function('previewRelease', 'inputUrl', 'previewReleaseUrl',
        'const previewStale = (' + stale[1] + ');' +
        'return (' + downloads[1] + ');');
    } catch { check('the derived flags parse', false); }
    if (fn) {
      const rel = { title: 'x' };
      check('no preview means the button previews', fn(null, 'https://a', '') === false);
      check('a pasted preview (box emptied) downloads', fn(rel, '', 'https://a') === true);
      check('a card-click preview (box holds that link) downloads', fn(rel, 'https://a', 'https://a') === true);
      check('a DIFFERENT link in the box previews instead of downloading',
        fn(rel, 'https://b', 'https://a') === false);
      check('whitespace around the same link still downloads',
        fn(rel, '  https://a  ', 'https://a') === true);
    }
  }
  // The handler must branch on the same flag the label does.
  const sub = between(src, 'function submitUrlBar() {', '\n  }\n');
  check('submitUrlBar branches on urlBarDownloads',
    !!sub && /if \(urlBarDownloads\)/.test(sub) && /downloadPreviewSelection\(\)/.test(sub));
  check('the button label branches on the same flag',
    /\{:else if urlBarDownloads\}/.test(src));

  // ── 3b. PV-1 preview rows ─────────────────────────────────────────────────
  // The requirement that created this class: on the live tracklist the 30-second
  // preview button is revealed by :hover, which is wrong on a screen whose whole
  // purpose is listening before committing. Both halves are checked - the class on
  // the row, and the rule that makes it visible - because either alone is a
  // no-op, which is exactly how a hover-only button would come back.
  const pvRows = between(src, '{#each previewRelease.tracks as row, i (i)}', '{:else if showSkeleton}');
  check('preview rows block exists', !!pvRows);
  check('preview row button is always visible, not hover-revealed',
    !!pvRows && /class="prev-btn prev-btn-always"/.test(pvRows));
  check('preview row has a selection checkbox',
    !!pvRows && /class="preview-check"/.test(pvRows) && /type="checkbox"/.test(pvRows));
  check('.prev-btn-always overrides the hover reveal',
    /\.prev-btn\.prev-btn-always \{ opacity: 1; \}/.test(src));
  // ...and the live tracklist must KEEP its hover behaviour: making every row's
  // button permanent there was never the ask.
  check('live tracklist keeps the hover reveal',
    /\.track-row:hover \.prev-btn,/.test(src));

  // ── 4. CSS ────────────────────────────────────────────────────────────────
  const css = between(src, '/* ── PORT-4 skeleton loaders', '@media (prefers-reduced-motion: reduce) {\n    .skel-block');
  check('PORT-4 CSS block exists', !!css);
  check('text bars are one line box tall (1lh)', !!css && /\.skel-line \{[^}]*height: 1lh;/.test(css));
  check('no text bar sized in em', !!css && !/\.skel-line \{[^}]*height:\s*1em/.test(css));
  check('bars use the mode-flipping TEXT tint', !!css && /\.skel-block \{[^}]*--text-tint-l/.test(css));
  check('hidden prev-btn reserves its box (visibility, not display)',
    !!css && /\.skel-prev \{[^}]*visibility: hidden;/.test(css) && !/\.skel-prev \{[^}]*display:\s*none/.test(css));
  check('badge keeps real text, painted transparent', !!css && /\.playlist-quality-badge\.skel-block \{[^}]*color: transparent;/.test(css));
  check('reduced motion stops the shimmer',
    /@media \(prefers-reduced-motion: reduce\) \{\s*\.skel-block \{ animation: none;/.test(src));

  return fails;
}

// ── Real source ────────────────────────────────────────────────────────────
const realFails = runChecks(REAL);
const total = realFails.count();
if (realFails.length) {
  console.log(`FAIL on shipped App.svelte (${realFails.length}):`);
  realFails.forEach(f => console.log('  - ' + f));
} else {
  console.log('shipped App.svelte: all checks pass');
}

// ── Negative control ───────────────────────────────────────────────────────
const sabotages = [
  ['condition ignores isDownloading', s => s.replace('awaitingTracklist && isDownloading && trackOrder.length === 0', 'awaitingTracklist && trackOrder.length === 0')],
  ['condition ignores rows', s => s.replace('awaitingTracklist && isDownloading && trackOrder.length === 0', 'awaitingTracklist && isDownloading')],
  ['playlist_loaded forgets to drop flag', s => s.replace('awaitingTracklist = false;   // PORT-4 — real header', '// dropped')],
  ['process_ended forgets to drop flag', s => s.replace('awaitingTracklist = false;   // PORT-4 — ended before', '// dropped')],
  ['reset forgets to raise flag', s => s.replace('function resetDownloadView() {\n    awaitingTracklist = true;', 'function resetDownloadView() {')],
  ['retry raises the skeleton', s => s.replace('    clearTrackInterval(trackName);\n    isDownloading = true;', '    clearTrackInterval(trackName);\n    awaitingTracklist = true;\n    isDownloading = true;')],
  ['bars sized in em', s => s.replace('.skel-line { height: 14px; height: 1lh; }', '.skel-line { height: 14px; height: 1em; }')],
  ['prev-btn placeholder dropped', s => s.replace('<span class="prev-btn skel-prev"></span>', '')],
  ['real row renames its name class', s => s.replace('<span class="track-row-name">{trackLabel}</span>', '<span class="track-name">{trackLabel}</span>')],
  ['skeleton header drops .playlist-meta', s => s.replace('<div class="playlist-cover skel-block"></div>\n        <div class="playlist-meta">', '<div class="playlist-cover skel-block"></div>\n        <div class="skel-meta">')],
  ['prev placeholder display:none', s => s.replace('.skel-row .prev-btn.skel-prev { visibility: hidden;', '.skel-row .prev-btn.skel-prev { display: none;')],
  ['reduced-motion guard removed', s => s.replace('@media (prefers-reduced-motion: reduce) {\n    .skel-block { animation: none; background-image: none; }\n  }', '')],
  ['discography hand-copies reset again', s => s.replace("          const artistName = discographyArtist?.artist_name || '';", "          playlistTitle = '';\n          const artistName = discographyArtist?.artist_name || '';")],
  ['link entry point wipes the running view', s => s.replace("      inputUrl = '';\n      // PV-1", "      inputUrl = '';\n      resetDownloadView();\n      // PV-1")],
  // PV-1 sabotages: the two new paths that must not reset, and the flag itself.
  ['Download now wipes the running view', s => s.replace('    clearReleasePreview();\n    try {\n      const res = await EnqueuePreviewSelection(', '    clearReleasePreview();\n    resetDownloadView();\n    try {\n      const res = await EnqueuePreviewSelection(')],
  ['preview resolving does not show placeholders', s => s.replace('    || previewReleaseLoading;', '    || false;')],
  ['preview row drops its always-visible modifier', s => s.replace('class="prev-btn prev-btn-always"', 'class="prev-btn"')],
  ['stale preview still claims to download', s => s.replace('$: urlBarDownloads = !!previewRelease && !previewStale;', '$: urlBarDownloads = !!previewRelease;')],
  ['handler and label disagree', s => s.replace('    if (urlBarDownloads) { downloadPreviewSelection(); return; }', '    if (previewRelease) { downloadPreviewSelection(); return; }')],
  ['queue_job_started skips the reset', s => s.replace("      const it = payload.item || {};\n      resetDownloadView();", "      const it = payload.item || {};")],
];
let caught = 0;
const missed = [];
for (const [label, fn] of sabotages) {
  const sab = fn(REAL);
  if (sab === REAL) { missed.push(label + ' (sabotage did not apply)'); continue; }
  if (runChecks(sab).length > 0) caught++; else missed.push(label);
}
console.log(`negative control: ${caught}/${sabotages.length} sabotages caught`);
missed.forEach(m => console.log('  MISSED: ' + m));

console.log(`${total - realFails.length}/${total} checks`);
process.exit(realFails.length || missed.length ? 1 : 0);
