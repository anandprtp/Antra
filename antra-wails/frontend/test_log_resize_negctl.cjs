/*
 * PORT-5 negative control.
 *
 * A green suite proves nothing until it has been shown to fail against broken
 * code. This sabotages the SHIPPED App.svelte one way at a time, re-runs
 * test_log_resize.cjs, and asserts the suite goes red — then restores the file
 * and verifies it is byte-identical.
 *
 * Run: node test_log_resize_negctl.cjs      (from antra-wails/frontend)
 *
 * NOTE: this rewrites App.svelte. Stop `wails dev` first, or its hot reload will
 * reload the native window (and make it re-save config.json) once per sabotage.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');

const APP = path.join(__dirname, 'src', 'App.svelte');
const original = fs.readFileSync(APP);
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');
const originalSha = sha(original);
const text = original.toString('utf8');

function runSuite() {
  try {
    execFileSync(process.execPath, [path.join(__dirname, 'test_log_resize.cjs')],
                 { cwd: __dirname, stdio: 'pipe' });
    return { red: false };
  } catch (e) {
    const out = String(e.stdout || '') + String(e.stderr || '');
    return { red: true, out };
  }
}

/* Each sabotage is [label, from, to]. `from` must match exactly once, or the
   control itself is broken and says so rather than silently skipping. */
const SABOTAGES = [
  ['pointer capture dropped (the source\'s document-level binding)',
   '(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);',
   'void e.pointerId;'],

  ['clamp reverted to the source: no viewport-vs-minimum handling',
   'const max = Math.max(Math.min(LOG_PANEL_MIN, vw), Math.floor(vw * LOG_PANEL_MAX_VW));\r\n'
   + '    return Math.round(Math.min(max, Math.max(Math.min(LOG_PANEL_MIN, vw), px)));',
   'return Math.round(Math.min(Math.floor(vw * LOG_PANEL_MAX_VW), Math.max(LOG_PANEL_MIN, px)));'],

  ['window-resize clamp persists, so a temporary shrink overwrites the preference',
   'if (clamped !== logPanelWidth) setLogPanelWidth(clamped, false);',
   'if (clamped !== logPanelWidth) setLogPanelWidth(clamped);'],

  ['window-resize clamp removed entirely',
   'const clamped = clampLogPanelWidth(logPanelWidth);',
   'const clamped = logPanelWidth;'],

  ['the in-progress guard dropped from the move handler',
   'function logResizeMove(e: PointerEvent) {\r\n    if (!logResizing) return;',
   'function logResizeMove(e: PointerEvent) {'],

  ['lostpointercapture unwired, so an out-of-window release sticks',
   '      on:lostpointercapture={logResizeEnd}\r\n',
   ''],

  ['the restore clamp dropped, so a stored width can exceed the window',
   'logPanelWidth = clampLogPanelWidth(config.log_panel_width);',
   'logPanelWidth = config.log_panel_width;'],

  ['the debounce no longer cancels, so every commit schedules its own write',
   'clearTimeout(logWidthSaveTimer);\r\n    logWidthSaveTimer = setTimeout(() => {\r\n'
   + '      config.log_panel_width = logPanelWidth;',
   'logWidthSaveTimer = setTimeout(() => {\r\n'
   + '      config.log_panel_width = logPanelWidth;'],

  ['a move persists on every pointermove instead of on commit',
   'setLogPanelWidth(window.innerWidth - e.clientX, false);',
   'setLogPanelWidth(window.innerWidth - e.clientX);'],

  ['the CSS variable reverted to a fixed width',
   'width: var(--log-panel-width, 380px);',
   'width: 380px;'],

  ['the bottom pin removed, so resizing unpins a log that was at the bottom',
   'if (shouldAutoScroll && terminalContainer) {\r\n'
   + '      terminalContainer.scrollTop = terminalContainer.scrollHeight;\r\n'
   + '    }',
   '/* pin removed */'],

  ['touch-action dropped, so a touch drag scrolls instead of resizing',
   '    touch-action: none;\r\n',
   ''],

  ['the focus outline removed again, leaving a ~2.3:1 tint as the only focus cue',
   '    outline: 2px solid rgba(255, 255, 255, 0.95);',
   '    outline: none;'],

  ['focus folded back into the hover/drag accent tint (1.51-2.56:1)',
   '  .log-resize-handle:hover,\r\n',
   '  .log-resize-handle:hover,\r\n  .log-resize-handle:focus-visible,\r\n'],

  ['the focus colour switched to the mode-flipping --text-tint-l, which paints '
   + 'near-black on near-black in light mode',
   '    background: rgba(255, 255, 255, 0.85);',
   '    background: oklch(var(--text-tint-l) 0 0 / 0.85);'],

  // The bare tabindex line appears twice in App.svelte (this handle and a
  // Discover card), so the anchor carries the preceding aria-valuemax line to
  // stay unique to the handle.
  ['the handle is no longer focusable, which kills the keyboard path',
   'LOG_PANEL_MAX_VW)}\r\n      tabindex="0"\r\n',
   'LOG_PANEL_MAX_VW)}\r\n'],
];

/* Sanity: the suite must be GREEN before any sabotage, or every "detected"
   below would be meaningless. */
const before = runSuite();
if (before.red) {
  console.error('FATAL: the suite is already failing on the unmodified file:\n' + before.out);
  process.exit(1);
}
console.log('control: suite green on the unmodified file');

let detected = 0;
const missed = [];
for (const [label, from, to] of SABOTAGES) {
  const n = text.split(from).length - 1;
  if (n !== 1) {
    console.error(`FATAL: sabotage anchor matched ${n} times (expected 1): ${label}`);
    fs.writeFileSync(APP, original);
    process.exit(1);
  }
  fs.writeFileSync(APP, Buffer.from(text.replace(from, to), 'utf8'));
  const r = runSuite();
  fs.writeFileSync(APP, original);
  if (sha(fs.readFileSync(APP)) !== originalSha) {
    console.error('FATAL: App.svelte was not restored byte-identically');
    process.exit(1);
  }
  if (r.red) { detected++; console.log(`  CAUGHT   ${label}`); }
  else { missed.push(label); console.log(`  MISSED   ${label}`); }
}

/* Restored, and still green. */
const after = runSuite();
if (after.red) {
  console.error('FATAL: the suite is failing after restore — the file is not clean');
  process.exit(1);
}

console.log(`\nnegative control: ${detected}/${SABOTAGES.length} sabotages detected`);
console.log(`App.svelte restored byte-identically (sha256 ${originalSha.slice(0, 16)}…), suite green`);
if (missed.length) {
  console.log('\nMISSED — these break the feature and the suite does not notice:');
  missed.forEach((m, i) => console.log(`  ${i + 1}. ${m}`));
  process.exit(1);
}
