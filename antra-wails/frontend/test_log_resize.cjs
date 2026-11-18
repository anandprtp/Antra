/*
 * PORT-5 (v2.0.0) — resizable, persisted activity-log panel.
 *
 * Everything under test is EXTRACTED FROM App.svelte and EXECUTED. Nothing is
 * re-implemented: a copy drifts from the shipped code and then passes while the
 * app is broken, which this repo has shipped before. The extraction is anchored
 * on real identifiers, so renaming a function fails the suite loudly rather
 * than quietly testing nothing.
 *
 * Timers are FAKE and driven explicitly, so the debounce is measured rather
 * than slept through — a real sleep cannot prove that a second call cancels the
 * first, only that something eventually fired.
 *
 * Run: node test_log_resize.cjs      (from antra-wails/frontend)
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const esbuild = require('esbuild');

const APP = path.join(__dirname, 'src', 'App.svelte');
// LF for scanning only — App.svelte is pure CRLF and multi-line anchors would
// otherwise match zero times.
const app = fs.readFileSync(APP, 'utf8').split('\r\n').join('\n');

let pass = 0;
const failures = [];
function check(label, cond, detail) {
  if (cond) { pass++; return; }
  failures.push(detail ? `${label}\n      ${detail}` : label);
}
function eq(label, got, want) {
  check(label, got === want, `got:  ${JSON.stringify(got)}\n      want: ${JSON.stringify(want)}`);
}
function fatal(msg) { console.error('FATAL: ' + msg); process.exit(1); }

/* ── Extract the shipped PORT-5 block ──────────────────────────────────────── */
function extract(startMarker, endMarker, what) {
  const from = app.indexOf(startMarker);
  if (from < 0) fatal(`could not find ${what} start marker in App.svelte`);
  const to = app.indexOf(endMarker, from);
  if (to < 0) fatal(`could not find ${what} end marker in App.svelte`);
  return app.slice(from, to + endMarker.length);
}

const block = extract(
  'const LOG_PANEL_DEFAULT = 380;',
  'function resetLogPanelWidth() { setLogPanelWidth(LOG_PANEL_DEFAULT); }',
  'PORT-5 resize block',
);
for (const fn of ['clampLogPanelWidth', 'setLogPanelWidth', 'logResizeStart',
                  'logResizeMove', 'logResizeEnd', 'logResizeKey']) {
  if (!block.includes('function ' + fn)) fatal(`extracted block has no ${fn} — wrong region`);
}
// The window-resize clamp lives inside onMount, so it is lifted separately.
const resizeHandler = extract('const handleWindowResize = () => {', '};', 'handleWindowResize');
if (!resizeHandler.includes('clampLogPanelWidth')) {
  fatal('handleWindowResize does not clamp — wrong region, or the clamp was dropped');
}
// The restore lives in the config-load block; lift the real lines rather than
// paraphrasing them, so a change to the guard is caught here.
const restore = extract(
  'if (config.log_panel_width != null) {',
  '}',
  'width restore',
);

/* Build a sandbox supplying exactly the outer bindings the real code closes
   over, then hand back the real functions plus the observable state. */
function makeBox({ innerWidth = 1280, storedWidth = null, autoScroll = false } = {}) {
  const saves = [];
  const captures = [];
  const releases = [];
  const panelStyle = {};
  const container = { scrollTop: 0, scrollHeight: 5000 };
  const rootStyle = { cursor: '', userSelect: '' };
  const win = { innerWidth };

  // Deterministic timers.
  let seq = 0;
  const pending = new Map();
  const fakeSetTimeout = (fn) => { pending.set(++seq, fn); return seq; };
  const fakeClearTimeout = (id) => { pending.delete(id); };
  const flush = () => {
    const fns = [...pending.values()];
    pending.clear();
    fns.forEach((fn) => fn());
    return fns.length;
  };

  const factory = `
    (function (env) {
      let config = env.config;
      let shouldAutoScroll = env.shouldAutoScroll;
      let terminalContainer = env.terminalContainer;
      const SaveConfig = env.SaveConfig;
      const window = env.window;
      const document = env.document;
      ${block}
      ${restore}
      logHandleEl = env.logHandleEl;
      ${resizeHandler.replace('const handleWindowResize', 'const onWindowResize')}
      return {
        clampLogPanelWidth, setLogPanelWidth, logResizeStart, logResizeMove,
        logResizeEnd, logResizeKey, resetLogPanelWidth, onWindowResize,
        LOG_PANEL_DEFAULT, LOG_PANEL_MIN, LOG_PANEL_MAX_VW,
        width: () => logPanelWidth,
        resizing: () => logResizing,
      };
    })
  `;
  const js = esbuild.transformSync(factory, { loader: 'ts' }).code;
  const sandbox = { console, setTimeout: fakeSetTimeout, clearTimeout: fakeClearTimeout };
  vm.createContext(sandbox);
  const make = vm.runInContext(js, sandbox);

  const api = make({
    config: storedWidth === null ? {} : { log_panel_width: storedWidth },
    shouldAutoScroll: autoScroll,
    terminalContainer: container,
    SaveConfig: (c) => saves.push({ ...c }),
    window: win,
    document: { documentElement: { style: rootStyle } },
    // The real code writes the CSS variable through logHandleEl.parentElement.
    logHandleEl: { parentElement: { style: {
      setProperty: (k, v) => { panelStyle[k] = v; },
    } } },
  });
  return { api, saves, captures, releases, panelStyle, container, rootStyle, win, flush,
           pendingCount: () => pending.size };
}

/* A pointer event just real enough for the handlers. */
function ptr(box, clientX, extra = {}) {
  return {
    button: 0, pointerType: 'mouse', pointerId: 7, clientX,
    preventDefault() { this._pd = true; },
    currentTarget: {
      setPointerCapture: (id) => box.captures.push(id),
      releasePointerCapture: (id) => box.releases.push(id),
    },
    ...extra,
  };
}
function key(k, shift = false) {
  return { key: k, shiftKey: shift, preventDefault() { this._pd = true; } };
}

/* ═════════════════════════════════════════════════════════════════════════
   1. CLAMP — including the degenerate case the source never handles.
   ═══════════════════════════════════════════════════════════════════════ */
{
  const { api } = makeBox({ innerWidth: 1280 });
  eq('the source constants are carried over unchanged (min)', api.LOG_PANEL_MIN, 300);
  eq('the source constants are carried over unchanged (max vw)', api.LOG_PANEL_MAX_VW, 0.9);
  eq('a width inside the range is untouched', api.clampLogPanelWidth(500), 500);
  eq('below the minimum clamps up', api.clampLogPanelWidth(120), 300);
  eq('above 90vw clamps down', api.clampLogPanelWidth(5000), 1152);
  eq('exactly 90vw is allowed', api.clampLogPanelWidth(1152), 1152);
  eq('a fractional width is rounded', api.clampLogPanelWidth(500.7), 501);
  eq('a negative width clamps to the minimum', api.clampLogPanelWidth(-40), 300);
}
{
  // A window narrower than the 300px minimum has no valid range. The viewport
  // must win, or the panel is wider than the screen and hides the whole app.
  const { api } = makeBox({ innerWidth: 260 });
  eq('on a window narrower than the minimum, the viewport wins',
     api.clampLogPanelWidth(900), 260);
  eq('...and the minimum is not enforced past the viewport either',
     api.clampLogPanelWidth(100), 260);
}

/* ═════════════════════════════════════════════════════════════════════════
   2. DRAG — and the bug the source has: a release outside the window.
   ═══════════════════════════════════════════════════════════════════════ */
{
  const box = makeBox({ innerWidth: 1000 });
  const { api } = box;
  eq('not resizing before pointerdown', api.resizing(), false);

  api.logResizeMove(ptr(box, 100));
  eq('a stray move before pointerdown is ignored', api.width(), 380);

  const down = ptr(box, 620);
  api.logResizeStart(down);
  eq('pointerdown starts the drag', api.resizing(), true);
  eq('pointerdown preventDefaults (no text selection, no drag ghost)', down._pd, true);
  eq('POINTER CAPTURE is requested — the source binds document instead',
     box.captures.join(','), '7');
  eq('the root cursor becomes ew-resize for the whole drag', box.rootStyle.cursor, 'ew-resize');
  eq('text selection is suppressed during the drag', box.rootStyle.userSelect, 'none');

  // width = viewport - clientX, because the panel is pinned to the right edge.
  api.logResizeMove(ptr(box, 600));
  eq('a move sets width from the viewport-relative pointer x', api.width(), 400);
  eq('the move writes the CSS variable straight onto the panel',
     box.panelStyle['--log-panel-width'], '400px');
  api.logResizeMove(ptr(box, 100));
  eq('dragging past 90vw clamps rather than overflowing', api.width(), 900);
  api.logResizeMove(ptr(box, 990));
  eq('dragging past the minimum clamps rather than collapsing', api.width(), 300);

  eq('no disk write is even SCHEDULED mid-drag', box.pendingCount(), 0);
  eq('no disk write happens mid-drag', box.saves.length, 0);

  api.logResizeEnd(ptr(box, 990));
  eq('pointerup ends the drag', api.resizing(), false);
  eq('pointer capture is released', box.releases.join(','), '7');
  eq('the root cursor is restored', box.rootStyle.cursor, '');
  eq('text selection is restored', box.rootStyle.userSelect, '');
}
{
  // THE BUG THIS AVOIDS: binding mouseup on `document` means a release that
  // happens outside the window never arrives and the panel sticks to the cursor.
  // Pointer capture guarantees lostpointercapture, which ends the drag.
  const box = makeBox();
  box.api.logResizeStart(ptr(box, 900));
  eq('drag is live', box.api.resizing(), true);
  box.api.logResizeEnd(ptr(box, -500));   // arrives as lostpointercapture
  eq('a release OUTSIDE the window still ends the drag', box.api.resizing(), false);
  eq('...and the cursor is not left stuck as ew-resize', box.rootStyle.cursor, '');
  const before = box.api.width();
  box.api.logResizeMove(ptr(box, 10));
  eq('...and a later stray move does not resize', box.api.width(), before);
}
{
  // logResizeEnd is wired to pointerup, pointercancel AND lostpointercapture,
  // so it can legitimately fire more than once for a single drag.
  const box = makeBox();
  box.api.logResizeStart(ptr(box, 800));
  box.api.logResizeEnd(ptr(box, 800));
  const after = box.releases.length;
  box.api.logResizeEnd(ptr(box, 800));
  eq('a second end for the same drag is a no-op', box.releases.length, after);
}
{
  const box = makeBox();
  box.api.logResizeStart(ptr(box, 700, { button: 2 }));
  eq('a non-primary mouse button does not start a drag', box.api.resizing(), false);
  // Touch and pen report button inconsistently, so they must not be gated out.
  box.api.logResizeStart(ptr(box, 700, { button: -1, pointerType: 'touch' }));
  eq('a touch drag still starts', box.api.resizing(), true);
}

/* ═════════════════════════════════════════════════════════════════════════
   3. PERSISTENCE — debounced, and only on commit.
   ═══════════════════════════════════════════════════════════════════════ */
{
  const box = makeBox({ innerWidth: 1000 });
  box.api.logResizeStart(ptr(box, 600));
  for (let x = 600; x > 500; x -= 10) box.api.logResizeMove(ptr(box, x));
  box.api.logResizeEnd(ptr(box, 500));
  eq('exactly one write is pending after a whole drag, not one per move',
     box.pendingCount(), 1);
  eq('nothing is written before the debounce elapses', box.saves.length, 0);
  box.flush();
  eq('exactly ONE write lands', box.saves.length, 1);
  // The commit is the width the last MOVE produced (clientX 510 -> 490), not the
  // up-event's own position. Deliberate: pointerup can report a position with no
  // preceding move, and honouring it would make the panel jump on release.
  eq('the written value is the last MOVED width', box.saves[0].log_panel_width, 490);
  eq('...and the up-event position is ignored, so release never jumps',
     box.api.width(), 490);
}
{
  // Two commits inside the debounce window must collapse into one write.
  const box = makeBox();
  box.api.logResizeKey(key('ArrowLeft'));
  box.api.logResizeKey(key('ArrowLeft'));
  box.api.logResizeKey(key('ArrowLeft'));
  eq('three quick nudges schedule one write, not three', box.pendingCount(), 1);
  box.flush();
  eq('...and only one lands', box.saves.length, 1);
  eq('...carrying the LAST value', box.saves[0].log_panel_width, 428);
}

/* ═════════════════════════════════════════════════════════════════════════
   4. KEYBOARD — the ARIA Window Splitter pattern.
   ═══════════════════════════════════════════════════════════════════════ */
{
  const { api } = makeBox({ innerWidth: 1280 });
  api.logResizeKey(key('ArrowLeft'));
  eq('ArrowLeft widens the panel by one step', api.width(), 396);
  api.logResizeKey(key('ArrowRight'));
  eq('ArrowRight narrows it back', api.width(), 380);
  api.logResizeKey(key('ArrowLeft', true));
  eq('Shift takes a bigger step', api.width(), 428);
  api.logResizeKey(key('Home'));
  eq('Home snaps to the maximum', api.width(), 1152);
  api.logResizeKey(key('End'));
  eq('End snaps to the minimum', api.width(), 300);
  api.logResizeKey(key('Enter'));
  eq('Enter restores the default', api.width(), 380);
  const unrelated = key('a');
  api.logResizeKey(unrelated);
  eq('an unrelated key leaves the width alone', api.width(), 380);
  eq('...and does NOT preventDefault, so typing elsewhere is unaffected',
     unrelated._pd, undefined);
  const arrow = key('ArrowLeft');
  api.logResizeKey(arrow);
  eq('a handled key DOES preventDefault (no page scroll)', arrow._pd, true);
}
{
  const box = makeBox({ innerWidth: 1280, storedWidth: 900 });
  box.api.resetLogPanelWidth();
  eq('reset returns to the default width', box.api.width(), 380);
  box.flush();
  eq('reset persists', box.saves[0].log_panel_width, 380);
}

/* ═════════════════════════════════════════════════════════════════════════
   5. RESTORE — re-clamped, because the window may be narrower now.
   ═══════════════════════════════════════════════════════════════════════ */
{
  eq('a stored width inside range is restored verbatim',
     makeBox({ innerWidth: 1280, storedWidth: 900 }).api.width(), 900);
  eq('a stored width wider than the window is clamped on restore',
     makeBox({ innerWidth: 800, storedWidth: 1400 }).api.width(), 720);
  eq('no stored width leaves the default',
     makeBox({ innerWidth: 1280 }).api.width(), 380);
  // 0 is not a meaningful width, but it IS what a plain Go `int` would read from
  // a pre-PORT-5 config.json. The clamp is the second line of defence behind
  // the *int, so even that cannot render as a collapsed panel.
  eq('a stored 0 clamps to the minimum rather than collapsing the panel',
     makeBox({ innerWidth: 1280, storedWidth: 0 }).api.width(), 300);
  eq('restoring schedules no write', makeBox({ innerWidth: 800, storedWidth: 1400 })
     .pendingCount(), 0);
}

/* ═════════════════════════════════════════════════════════════════════════
   6. WINDOW RESIZE — the source never re-clamps, so its panel can end up
      covering the whole app after the window shrinks.
   ═══════════════════════════════════════════════════════════════════════ */
{
  const box = makeBox({ innerWidth: 1280, storedWidth: 1100 });
  eq('starts at the stored width', box.api.width(), 1100);
  box.api.onWindowResize();
  eq('a resize that changes nothing leaves the width alone', box.api.width(), 1100);
  eq('...and schedules no write', box.pendingCount(), 0);

  box.win.innerWidth = 600;             // the user drags the window narrow
  box.api.onWindowResize();
  eq('shrinking the window re-clamps the panel to 90vw', box.api.width(), 540);
  eq('the re-clamp repaints the panel', box.panelStyle['--log-panel-width'], '540px');
  eq('a temporary shrink must NOT overwrite the stored preference',
     box.pendingCount(), 0);
  eq('...and nothing is written', box.saves.length, 0);

  box.win.innerWidth = 1920;            // and back wide again
  box.api.onWindowResize();
  eq('widening the window does not restore the old width (clamps are one-way)',
     box.api.width(), 540);
}

/* ═════════════════════════════════════════════════════════════════════════
   7. BOTTOM PIN — re-wrapping changes scrollHeight mid-drag.
   ═══════════════════════════════════════════════════════════════════════ */
{
  const box = makeBox({ innerWidth: 1000, autoScroll: true });
  box.api.logResizeStart(ptr(box, 600));
  box.container.scrollTop = 0;
  box.api.logResizeMove(ptr(box, 550));
  eq('a bottom-pinned log stays pinned while resizing',
     box.container.scrollTop, box.container.scrollHeight);
}
{
  const box = makeBox({ innerWidth: 1000, autoScroll: false });
  box.api.logResizeStart(ptr(box, 600));
  box.container.scrollTop = 123;
  box.api.logResizeMove(ptr(box, 550));
  eq('a log the user has scrolled up in is NOT yanked to the bottom',
     box.container.scrollTop, 123);
}

/* ═════════════════════════════════════════════════════════════════════════
   8. MARKUP + CSS — the wiring the executed code cannot see.
   ═══════════════════════════════════════════════════════════════════════ */
{
  const handle = app.slice(app.indexOf('class="log-resize-handle"'));
  const tag = handle.slice(0, handle.indexOf('></div>'));
  for (const [label, attr] of [
    ['pointerdown', 'on:pointerdown={logResizeStart}'],
    ['pointermove', 'on:pointermove={logResizeMove}'],
    ['pointerup', 'on:pointerup={logResizeEnd}'],
    ['pointercancel', 'on:pointercancel={logResizeEnd}'],
    ['lostpointercapture', 'on:lostpointercapture={logResizeEnd}'],
    ['dblclick reset', 'on:dblclick={resetLogPanelWidth}'],
    ['keydown', 'on:keydown={logResizeKey}'],
  ]) check(`the handle is wired for ${label}`, tag.includes(attr), `missing: ${attr}`);

  check('ARIA: the handle is a separator carrying a value range',
        tag.includes('role="separator"') && tag.includes('aria-valuenow={logPanelWidth}')
        && tag.includes('aria-valuemin') && tag.includes('aria-valuemax'));
  check('ARIA: the separator is focusable, or the keyboard path is unreachable',
        tag.includes('tabindex="0"'));
  check('the handle sets touch-action, or a touch drag scrolls instead of resizing',
        /\.log-resize-handle\s*\{[^}]*touch-action:\s*none/.test(app));

  check('the panel width comes from the CSS variable',
        app.includes('width: var(--log-panel-width, 380px);'));
  check('the variable FALLBACK is the pre-PORT-5 width, so a render before the '
        + 'inline value lands is unchanged',
        /var\(--log-panel-width,\s*380px\)/.test(app));
  check('the reactive value is bound into the panel style',
        app.includes('style="--log-panel-width: {logPanelWidth}px"'));
  check('no stale fixed width survives on .log-panel',
        !/\.log-panel \{[^}]*\n\s*width: 380px;/.test(app));
  check('mid-drag transitions are suppressed on the panel',
        /\.log-panel\[data-resizing\]\s*\{\s*transition:\s*none/.test(app));

  /* The focus indicator is held to a different bar from the hover tint, because
     MEASURED over 11 themes x 2 modes the accent tops out at 1.51-2.56:1 against
     this panel's near-black ground and does not improve with alpha. Focus is
     13.74-14.33:1 instead. Two properties have to hold for that to stay true. */
  const focusRule = app.slice(app.indexOf('.log-resize-handle:focus-visible {'),
                              app.indexOf('.log-resize-handle:focus-visible {') + 400);
  check('focus is NOT lumped in with the hover/drag accent tint',
        !/\.log-resize-handle:focus-visible,/.test(app));
  check('focus draws a real outline — an earlier cut set `outline: none` and left '
        + 'a ~2.3:1 tint as the only cue',
        /outline:\s*2px solid/.test(focusRule) && !/outline:\s*none/.test(focusRule));
  check('the focus colour is mode-STABLE, not --text-tint-l: .log-panel is '
        + 'hardcoded near-black in both modes, so a mode-flipped token would paint '
        + 'near-black on near-black in light mode (the FR-4 trap)',
        /rgba\(255, 255, 255/.test(focusRule) && !/text-tint-l/.test(focusRule));
  check('the panel carries data-resizing during a drag',
        app.includes("data-resizing={logResizing ? '1' : null}"));
  check('the palette can reset the width (the escape hatch if it is ever unusable)',
        app.includes("id: 'act.reset-log-width'"));
}

/* ── Report ────────────────────────────────────────────────────────────────── */
console.log(`\nPORT-5 log-panel resize: ${pass} passed, ${failures.length} failed`);
if (failures.length) {
  failures.forEach((f, i) => console.log(`  ${i + 1}. ${f}`));
  process.exit(1);
}
