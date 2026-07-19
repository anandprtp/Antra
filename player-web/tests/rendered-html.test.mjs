import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

async function render(path = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request(`http://localhost${path}`, {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the branded player shell", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<html[^>]+lang="ru"/i);
  assert.match(html, /<title>Antra Player<\/title>/i);
  assert.match(html, /Открываем вашу медиатеку/i);
  assert.match(html, /ANTRA/);
  assert.match(html, /manifest\.webmanifest/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape|react-loading-skeleton/i);
});

test("server-renders the external-browser launcher separately", async () => {
  const response = await render("/open?launch=test-launch");
  assert.equal(response.status, 200);

  const html = await response.text();
  assert.match(html, /Открыть внешний браузер/i);
  assert.match(html, /Готовим безопасную ссылку/i);
  assert.doesNotMatch(html, /Открываем вашу медиатеку/i);
});

test("ships PWA and private-player integration surfaces", async () => {
  const [player, launcher, layout, manifest, serviceWorker, packageJson] = await Promise.all([
    readFile(new URL("../app/player-app.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/open/external-browser-launcher.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../public/manifest.webmanifest", import.meta.url), "utf8"),
    readFile(new URL("../public/sw.js", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  const parsedManifest = JSON.parse(manifest);
  assert.equal(parsedManifest.name, "Antra Player");
  assert.equal(parsedManifest.display, "standalone");
  assert.equal(parsedManifest.theme_color, "#081412");

  assert.match(player, /new URLSearchParams\(window\.location\.hash/);
  assert.match(player, /history\.replaceState/);
  assert.match(player, /Authorization: `Bearer \$\{credentials\.token\}`/);
  assert.match(player, /\/api\/v1\/tracks/);
  assert.match(player, /\/api\/v1\/player-state/);
  assert.match(player, /payload as \{ items: unknown\[\] \}/);
  assert.match(player, /navigator\.mediaSession/);
  assert.match(player, /NEXT_PUBLIC_PLAYER_DEMO/);
  assert.match(launcher, /telegram\.openLink/);
  assert.match(launcher, /\/api\/v1\/player-launch/);
  assert.match(launcher, /navigator\.clipboard\.writeText/);
  assert.doesNotMatch(launcher, /\/api\/v1\/tracks|new Audio|mediaSession/);
  assert.doesNotMatch(launcher, /window\.open/);
  assert.match(serviceWorker, /request\.destination === "audio"/);
  assert.match(serviceWorker, /request\.headers\.has\("range"\)/);
  assert.match(serviceWorker, /url\.pathname\.startsWith\("\/open"\)/);
  assert.match(layout, /appleWebApp/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);

  await assert.rejects(access(new URL("../app/_sites-preview/", import.meta.url)));
  await access(new URL("../public/antra-icon.png", import.meta.url));
  await access(new URL("../.openai/hosting.json", import.meta.url));
});
