export type Identified = {
  id: string;
};

export type PlaybackSelection<T extends Identified> = {
  queue: T[];
  currentId: string | null;
};

export function trustedApiOrigin(currentOrigin: string): string {
  return new URL(currentOrigin).origin;
}

export function storedVolumeOrDefault(
  storedValue: string | null,
  fallback = 1,
): number {
  if (storedValue === null || storedValue.trim() === "") return fallback;
  const parsed = Number(storedValue);
  return Number.isFinite(parsed) && parsed >= 0 && parsed <= 1
    ? parsed
    : fallback;
}

export function mergeUniqueById<T extends Identified>(
  existing: readonly T[],
  incoming: readonly T[],
): T[] {
  const merged = new Map(existing.map((item) => [item.id, item]));
  for (const item of incoming) merged.set(item.id, item);
  return [...merged.values()];
}

export function resolvePlaybackSelection<T extends Identified>(
  catalog: readonly T[],
  remoteQueueIds: readonly string[],
  remoteCurrentId: string | null,
  deepTrackId: string,
): PlaybackSelection<T> {
  const byId = new Map(catalog.map((track) => [track.id, track]));
  const restoredQueue = remoteQueueIds
    .map((id) => byId.get(id))
    .filter((track): track is T => Boolean(track));
  const deepTrack = deepTrackId ? byId.get(deepTrackId) : undefined;

  let queue: T[];
  if (restoredQueue.length) {
    queue = deepTrack
      ? [
          deepTrack,
          ...restoredQueue.filter((track) => track.id !== deepTrack.id),
        ]
      : restoredQueue;
  } else if (deepTrack) {
    queue = [
      deepTrack,
      ...catalog.filter((track) => track.id !== deepTrack.id),
    ];
  } else {
    queue = [...catalog];
  }

  const queueIds = new Set(queue.map((track) => track.id));
  const currentId = deepTrack
    ? deepTrack.id
    : remoteCurrentId && queueIds.has(remoteCurrentId)
      ? remoteCurrentId
      : queue[0]?.id ?? null;
  return { queue, currentId };
}

export function refreshQueueItems<T extends Identified>(
  queue: readonly T[],
  catalog: readonly T[],
): T[] {
  const byId = new Map(catalog.map((track) => [track.id, track]));
  const refreshed = queue
    .map((track) => byId.get(track.id))
    .filter((track): track is T => Boolean(track));
  return refreshed.length ? refreshed : [...catalog];
}

export function shouldIgnoreGlobalShortcut(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  return Boolean(
    target.closest(
      "input, textarea, select, button, a, [contenteditable='true'], " +
        "[role='button'], [role='link'], [role='menuitem'], [role='option'], " +
        "[role='slider'], [role='switch'], [role='tab']",
    ),
  );
}
