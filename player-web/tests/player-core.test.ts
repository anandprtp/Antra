import assert from "node:assert/strict";
import test from "node:test";

import {
  mergeUniqueById,
  refreshQueueItems,
  resolvePlaybackSelection,
  storedVolumeOrDefault,
  trustedApiOrigin,
} from "../app/player-core.ts";

test("always keeps bearer API requests on the current origin", () => {
  assert.equal(
    trustedApiOrigin("https://music.example/player?x=1"),
    "https://music.example",
  );
});

test("uses full volume on a clean browser and preserves an explicit mute", () => {
  assert.equal(storedVolumeOrDefault(null), 1);
  assert.equal(storedVolumeOrDefault(""), 1);
  assert.equal(storedVolumeOrDefault("invalid"), 1);
  assert.equal(storedVolumeOrDefault("0"), 0);
  assert.equal(storedVolumeOrDefault("0.35"), 0.35);
});

test("deep-linked track is always part of the restored queue", () => {
  const catalog = [{ id: "a" }, { id: "b" }, { id: "c" }];
  assert.deepEqual(
    resolvePlaybackSelection(catalog, ["a"], "a", "b"),
    {
      queue: [{ id: "b" }, { id: "a" }],
      currentId: "b",
    },
  );
});

test("unknown restored current track falls back to a queue member", () => {
  const catalog = [{ id: "a" }, { id: "b" }];
  assert.deepEqual(
    resolvePlaybackSelection(catalog, ["b"], "missing", ""),
    {
      queue: [{ id: "b" }],
      currentId: "b",
    },
  );
});

test("paginated catalog merge de-duplicates overlapping pages", () => {
  const first = Array.from({ length: 100 }, (_, index) => ({ id: String(index) }));
  const second = Array.from({ length: 41 }, (_, index) => ({ id: String(index + 99) }));
  assert.equal(mergeUniqueById(first, second).length, 140);
});

test("queue keeps its order while receiving refreshed stream metadata", () => {
  const queue = [{ id: "b", url: "old" }, { id: "a", url: "old" }];
  const catalog = [{ id: "a", url: "new-a" }, { id: "b", url: "new-b" }];
  assert.deepEqual(refreshQueueItems(queue, catalog), [
    { id: "b", url: "new-b" },
    { id: "a", url: "new-a" },
  ]);
});
