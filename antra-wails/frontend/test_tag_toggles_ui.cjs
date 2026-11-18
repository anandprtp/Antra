/* v2.0.0 DMX-1 — the Svelte side of the per-tag toggles.
 *
 * The helpers are EXTRACTED from App.svelte and executed, never
 * re-implemented: renaming one fails this suite loudly instead of leaving it
 * quietly testing a copy that no longer ships.
 *
 * Run:  node test_tag_toggles_ui.cjs
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, 'src', 'App.svelte');
const src = fs.readFileSync(APP, 'utf8');

let checks = 0;
const fails = [];
function check(label, ok, detail) {
  checks += 1;
  if (!ok) {
    fails.push(label + (detail ? ' — ' + detail : ''));
    console.log('  FAIL  ' + label + (detail ? ' — ' + detail : ''));
  }
}

/* ── Extract the shipped block ───────────────────────────────────────────── */
function slice(startMarker, endMarker) {
  const i = src.indexOf(startMarker);
  if (i < 0) throw new Error('start marker not found: ' + startMarker);
  const j = src.indexOf(endMarker, i);
  if (j < 0) throw new Error('end marker not found: ' + endMarker);
  return src.slice(i, j);
}

const tables = slice('const TAG_GROUPS', '  let showAdvancedTags');
const helpers = slice('  function tagOn(', '  let config: main.Config');

// Strip the TypeScript annotations Svelte's preprocessor would remove.
const stripped = (tables + '\n' + helpers)
  .replace(/:\s*\{\s*group:[\s\S]*?\}\[\]\s*=/, ' =')
  .replace(/disabled:\s*string\[\]\s*\|\s*undefined,\s*id:\s*string/, 'disabled, id')
  .replace(/\(id:\s*string,\s*on:\s*boolean\)/, '(id, on)')
  .replace(/:\s*boolean\s*\{/, ' {')
  .replace(/const current:\s*string\[\]\s*=/, 'const current =');

const ctx = { config: { disabled_tags: [] }, module: {}, exports: {} };
vm.createContext(ctx);
vm.runInContext(stripped + '\nthis.TAG_GROUPS = TAG_GROUPS; this.DATE_FORMATS = DATE_FORMATS;'
  + ' this.tagOn = tagOn; this.setTag = setTag; this.allTagsOn = allTagsOn;', ctx);

const { TAG_GROUPS, DATE_FORMATS, tagOn, setTag, allTagsOn } = ctx;

console.log('\n[1] tables');
const ids = TAG_GROUPS.flatMap((g) => g.fields.map((f) => f.id));
check('every group has a name', TAG_GROUPS.every((g) => !!g.group));
check('every field has a label', TAG_GROUPS.every((g) => g.fields.every((f) => !!f.label)));
check('no duplicate ids', new Set(ids).size === ids.length, ids.join(','));
// Bumped 20 -> 21 by ADV-1 (issue #43). This count is a blunt sentinel: it
// catches an added or removed switch but not a RENAMED one, so the real
// id-for-id comparison against tagger.TAG_FIELDS lives in
// antra/utils/test_tag_toggles.py, which is where App.svelte's comment
// has always said it was.
check('21 switches', ids.length === 21, String(ids.length));
check('4 date formats', DATE_FORMATS.length === 4, String(DATE_FORMATS.length));

console.log('\n[2] tagOn — absent/empty means ON');
check('undefined -> on', tagOn(undefined, 'title') === true);
check('empty -> on', tagOn([], 'title') === true);
check('listed -> off', tagOn(['title'], 'title') === false);
check('other listed -> still on', tagOn(['album'], 'title') === true);

console.log('\n[3] setTag');
ctx.config.disabled_tags = [];
setTag('lyrics', false);
check('switching off adds the id', ctx.config.disabled_tags.join() === 'lyrics',
  JSON.stringify(ctx.config.disabled_tags));
setTag('cover', false);
check('a second id is appended', ctx.config.disabled_tags.slice().sort().join() === 'cover,lyrics',
  JSON.stringify(ctx.config.disabled_tags));
setTag('lyrics', false);
check('switching off twice does not duplicate',
  ctx.config.disabled_tags.filter((x) => x === 'lyrics').length === 1,
  JSON.stringify(ctx.config.disabled_tags));
setTag('lyrics', true);
check('switching back on removes only that id', ctx.config.disabled_tags.join() === 'cover',
  JSON.stringify(ctx.config.disabled_tags));
setTag('never_disabled', true);
check('turning on an already-on id is a no-op', ctx.config.disabled_tags.join() === 'cover');

console.log('\n[4] a new array each time (so the "N off" count re-renders)');
ctx.config.disabled_tags = ['a'];
const before = ctx.config.disabled_tags;
setTag('b', false);
check('setTag does not mutate in place', ctx.config.disabled_tags !== before);
check('the old array is untouched', before.join() === 'a', before.join());

console.log('\n[5] allTagsOn');
ctx.config.disabled_tags = ['a', 'b', 'c'];
allTagsOn();
check('resets to empty', Array.isArray(ctx.config.disabled_tags)
  && ctx.config.disabled_tags.length === 0, JSON.stringify(ctx.config.disabled_tags));

console.log('\n[6] markup');
check('the select carries appearance:none (WebView2 renders native selects '
  + 'in OS light mode otherwise — v1.1.6 BUG-C)',
  /id="dateFormatSel"[\s\S]{0,200}appearance: none/.test(src));
check('the block starts collapsed', /let showAdvancedTags = false;/.test(src));
check('the count reads from disabled_tags',
  /\(config\.disabled_tags \|\| \[\]\)\.length === 0/.test(src));
check('"turn everything back on" is disabled when nothing is off',
  /disabled=\{\(config\.disabled_tags \|\| \[\]\)\.length === 0\}/.test(src));
check('checkbox is bound through setTag, not bind:checked',
  /checked=\{tagOn\(config\.disabled_tags, f\.id\)\}/.test(src));

console.log('\n' + checks + ' checks, ' + fails.length + ' failed');
fails.forEach((f) => console.log('  - ' + f));
process.exit(fails.length ? 1 : 0);
