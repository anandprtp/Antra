// Pins the community invite links and their icons in App.svelte.
//
// Both invites expired once already and the failure is silent: the button still
// renders, still opens a browser, and only the user finds out the link is dead.
// So assert on the live source, not on a copy.
const fs = require('fs');
const path = require('path');

const root = __dirname;
const app = fs.readFileSync(path.join(root, 'src', 'App.svelte'), 'utf8');

let pass = 0;
let fail = 0;
function check(name, cond) {
  if (cond) { pass++; }
  else { fail++; console.log('  FAIL: ' + name); }
}

const TELEGRAM = 'https://t.me/antraaverse';
const DISCORD = 'https://discord.gg/gAJ3QymUta';

check('telegram invite is the current one', app.includes(`BrowserOpenURL('${TELEGRAM}')`));
check('discord invite is the current one', app.includes(`BrowserOpenURL('${DISCORD}')`));

// The controls: the retired invites must be gone. Without these, the two checks
// above would still pass if a second, stale button were left behind.
check('no retired discord.com/invite link', !app.includes('discord.com/invite/'));
check('exactly one discord invite', (app.match(/discord\.gg\//g) || []).length === 1);
check('exactly one telegram invite', (app.match(/t\.me\//g) || []).length === 1);

// Icons: each button must reference its PNG, and the PNG must actually ship.
for (const [name, file] of [['telegram', 'telegram.png'], ['discord', 'discord.png']]) {
  const ref = `/icons/${file}`;
  check(`${name} button uses ${file}`, app.includes(`<img src="${ref}"`));
  const p = path.join(root, 'public', 'icons', file);
  check(`${file} exists in public/icons`, fs.existsSync(p));
  // A zero-byte or HTML-placeholder file would satisfy existsSync alone.
  if (fs.existsSync(p)) {
    const buf = fs.readFileSync(p);
    check(`${file} is a real PNG`, buf.length > 1000 && buf.slice(1, 4).toString('latin1') === 'PNG');
  } else {
    fail++;
  }
}

// The old inline SVGs carried the brand fills; if either is back, the PNG swap
// was reverted somewhere.
check('telegram inline svg removed', !app.includes('#26A5E4'));
check('discord inline svg removed', !app.includes('#5865F2'));

console.log(`test_social_links: ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
