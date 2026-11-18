/*
 * AUDIT-DESKTOP-15 - server-supplied URLs must not reach the OS opener unchecked.
 *
 * BrowserOpenURL hands its argument to the operating system, which resolves
 * file:, smb: and registered custom protocol handlers as well as web links. Two
 * values come from servers: the sign-in reply's verification_url and the remote
 * support-status link (a public gist). openExternal() is the second lock after
 * the Go-side validation.
 *
 * Pulls the REAL openExternal() out of App.svelte and drives it with a fake
 * BrowserOpenURL, then checks statically that no server-supplied value is still
 * passed to BrowserOpenURL directly. Re-runs everything against sabotaged
 * copies and requires each to be caught.
 *
 * Run: node test_external_links.cjs   (from antra-wails/frontend)
 */
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, 'src', 'App.svelte');
const REAL = fs.readFileSync(SRC, 'utf8').replace(/\r\n/g, '\n');

function bodyOf(src, header) {
  const start = src.indexOf(header);
  if (start < 0) return null;
  let i = src.indexOf('{', start);
  const open = i;
  let depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) return src.slice(open + 1, i); }
  }
  return null;
}

function opener(src) {
  const body = bodyOf(src, 'function openExternal(');
  if (!body) return null;
  const opened = [];
  // The component body is TypeScript; the extracted function body is plain JS apart from annotations on its
  // parameters (none are referenced in the body), so it evaluates as-is.
  const fn = new Function('url', 'BrowserOpenURL', 'URL', body);
  return { call: (u) => fn(u, (x) => opened.push(x), URL), opened };
}

function run(src) {
  const failures = [];
  const check = (label, ok, detail) => { if (!ok) failures.push(label + (detail ? ' :: ' + detail : '')); };

  const o = opener(src);
  if (!o) return ['openExternal() not found in App.svelte'];

  const allowed = ['https://patreon.com/AntraVerse', 'https://antra.hoshi.cfd/device?code=ABCD-EFGH', 'HTTPS://Patreon.com/x'];
  for (const u of allowed) {
    const before = o.opened.length;
    const r = o.call(u);
    check('control: ' + u + ' is opened', r === true && o.opened.length === before + 1 && o.opened[o.opened.length - 1] === u,
      JSON.stringify({ r, opened: o.opened }));
  }

  const refused = {
    'file URL': 'file:///C:/Windows/System32/calc.exe',
    'smb URL': 'smb://evil/share',
    'javascript URL': 'javascript:alert(1)',
    'custom protocol handler': 'ms-msdt:/id PCWDiagnostic',
    'http (not https)': 'http://patreon.com/x',
    'data URL': 'data:text/html,<script>1</script>',
    'credentials in the URL': 'https://patreon.com@evil.example/x',
    'user:password in the URL': 'https://u:p@patreon.com/x',
    // ('https:///path' is not here: the WHATWG URL parser collapses the extra slashes and reads host "path",
    // a plain https URL, whereas Go's parser reports an empty host and the Go validator refuses it.)
    'relative path': '/patreon',
    'embedded newline': 'https://patreon.com/x\r\nHost: evil',
    'embedded space': 'https://pat reon.com/x',
    'empty string': '',
    'undefined': undefined,
    'null': null,
    'a number': 12345,
    'an object': { toString() { return 'https://patreon.com'; } },
  };
  for (const [name, u] of Object.entries(refused)) {
    const before = o.opened.length;
    let r;
    try { r = o.call(u); } catch (e) { r = 'threw: ' + e.message; }
    check(name + ' is refused', r === false && o.opened.length === before, JSON.stringify({ r, opened: o.opened.slice(before) }));
  }

  // No server-supplied value may still reach the OS opener directly.
  const direct = [...src.matchAll(/BrowserOpenURL\(([^)]*)\)/g)].map((m) => m[1].trim())
    .filter((arg) => !/^['"]https:\/\//.test(arg) && arg !== 'url' && arg !== 'STATUS_PAGE_URL');
  check('no server-supplied URL is passed to BrowserOpenURL directly', direct.length === 0, JSON.stringify(direct));
  for (const needle of ['openExternal(deviceVerifyUrl)', 'openExternal(supportStatus.link)']) {
    check('call site uses the wrapper: ' + needle, src.includes(needle));
  }
  check('control: the status page URL is still a compile-time https constant',
    /const STATUS_PAGE_URL = 'https:\/\//.test(src));
  return failures;
}

const base = run(REAL);
if (base.length) { console.log('FAIL (unsabotaged)'); base.forEach((f) => console.log('  ' + f)); process.exit(1); }
console.log('ok: ' + 'all checks pass on the real source');

const sabotages = {
  'wrapper opens everything': (s) => s.replace("if (typeof url !== 'string' || url.length === 0) return false;", ''),
  'scheme check removed': (s) => s.replace("u.protocol !== 'https:' || ", ''),
  'credentials allowed': (s) => s.replace(' || u.username || u.password', ''),
  'whitespace/control check removed': (s) => s.replace('if (c <= 32 || c === 127) return false;', ''),
  'parse failure opens anyway': (s) => s.replace('} catch (_e) {\n      return false;\n    }', '} catch (_e) {\n    }'),
  'sign-in URL bypasses the wrapper': (s) => s.replace('openExternal(deviceVerifyUrl)', 'BrowserOpenURL(deviceVerifyUrl)'),
  'support link bypasses the wrapper': (s) => s.replace('on:click={() => openExternal(supportStatus.link)}', 'on:click={() => BrowserOpenURL(supportStatus.link)}'),
  'sponsor toast bypasses the wrapper': (s) => s.replace('{ openExternal(supportStatus.link); dismissSponsorToast(); }', '{ BrowserOpenURL(supportStatus.link); dismissSponsorToast(); }'),
};
let missed = 0;
for (const [name, mutate] of Object.entries(sabotages)) {
  const mutated = mutate(REAL);
  if (mutated === REAL) { console.log('NO-OP sabotage (anchor gone): ' + name); missed++; continue; }
  const f = run(mutated);
  console.log((f.length ? 'caught  ' : 'MISSED  ') + name);
  if (!f.length) missed++;
}
if (missed) { console.log(missed + ' sabotage(s) not caught'); process.exit(1); }
console.log('all sabotages caught');
