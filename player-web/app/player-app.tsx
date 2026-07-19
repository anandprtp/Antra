"use client";

import {
  Check,
  ChevronDown,
  Disc3,
  Ellipsis,
  House,
  Library,
  ListMusic,
  LoaderCircle,
  Menu,
  Music2,
  Pause,
  Play,
  Plus,
  Radio,
  RefreshCw,
  Repeat,
  Repeat1,
  Search,
  Send,
  Shuffle,
  SkipBack,
  SkipForward,
  Volume2,
  WifiOff,
  X,
} from "lucide-react";
import {
  type CSSProperties,
  type ChangeEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  mergeUniqueById,
  refreshQueueItems,
  resolvePlaybackSelection,
  shouldIgnoreGlobalShortcut,
  storedVolumeOrDefault,
  trustedApiOrigin,
} from "./player-core";

type Tab = "home" | "search" | "library";
type RepeatMode = "off" | "all" | "one";
type LoadState = "booting" | "ready" | "unauthorized" | "offline" | "error";

type Track = {
  id: string;
  title: string;
  artist: string;
  album: string;
  durationSeconds: number;
  sizeBytes: number;
  mimeType: string;
  streamUrl: string;
  artworkUrl: string;
  availability: string;
  addedAt: string;
  streamExpiresAt: number;
};

type RemotePlayerState = {
  revision: number;
  queue_ids: string[];
  current_id: string | null;
  position_ms: number;
  paused: boolean;
  shuffle: boolean;
  repeat_mode: RepeatMode;
};

type Credentials = {
  token: string;
  apiOrigin: string;
  deepTrackId: string;
};

type SaveSnapshot = {
  revision: number;
  queueIds: string[];
  currentId: string | null;
  positionMs: number;
  paused: boolean;
  shuffle: boolean;
  repeatMode: RepeatMode;
};

const STORAGE = {
  token: "antra.player.token",
  volume: "antra.player.volume",
  catalog: "antra.player.catalog",
};

const TELEGRAM_URL = "https://t.me/fnnlinkbot";
const DEMO_ENABLED = process.env.NEXT_PUBLIC_PLAYER_DEMO === "true";
const CATALOG_PAGE_SIZE = 100;
const CATALOG_REFRESH_MS = 5 * 60 * 1000;

const DEMO_TRACKS: Track[] = [
  {
    id: "demo-nina",
    title: "909 Festival 2025",
    artist: "Nina Kraviz",
    album: "Live archive",
    durationSeconds: 7223,
    sizeBytes: 123_000_000,
    mimeType: "audio/webm",
    streamUrl: "",
    artworkUrl: "",
    availability: "ready",
    addedAt: "2026-07-19T10:32:00Z",
    streamExpiresAt: 0,
  },
  {
    id: "demo-teardrop",
    title: "Teardrop",
    artist: "Massive Attack",
    album: "Mezzanine",
    durationSeconds: 330,
    sizeBytes: 12_000_000,
    mimeType: "audio/mp4",
    streamUrl: "",
    artworkUrl: "",
    availability: "ready",
    addedAt: "2026-07-18T20:12:00Z",
    streamExpiresAt: 0,
  },
  {
    id: "demo-avril",
    title: "Avril 14th",
    artist: "Aphex Twin",
    album: "Drukqs",
    durationSeconds: 125,
    sizeBytes: 5_000_000,
    mimeType: "audio/mp4",
    streamUrl: "",
    artworkUrl: "",
    availability: "ready",
    addedAt: "2026-07-17T13:45:00Z",
    streamExpiresAt: 0,
  },
  {
    id: "demo-hoppipolla",
    title: "Hoppípolla",
    artist: "Sigur Rós",
    album: "Takk…",
    durationSeconds: 269,
    sizeBytes: 9_000_000,
    mimeType: "audio/mp4",
    streamUrl: "",
    artworkUrl: "",
    availability: "ready",
    addedAt: "2026-07-16T18:10:00Z",
    streamExpiresAt: 0,
  },
  {
    id: "demo-windowlicker",
    title: "Windowlicker",
    artist: "Aphex Twin",
    album: "Windowlicker",
    durationSeconds: 366,
    sizeBytes: 14_000_000,
    mimeType: "audio/mp4",
    streamUrl: "",
    artworkUrl: "",
    availability: "ready",
    addedAt: "2026-07-15T09:18:00Z",
    streamExpiresAt: 0,
  },
];

class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function readCredentials(): Credentials {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const tokenFromLink = params.get("token")?.trim() ?? "";
  const deepTrackId = params.get("track")?.trim() ?? "";

  if (tokenFromLink) localStorage.setItem(STORAGE.token, tokenFromLink);
  // Older builds persisted a configurable API origin. The player and API now
  // intentionally share one origin so a crafted hash cannot exfiltrate the
  // stored bearer token to another server.
  localStorage.removeItem("antra.player.api");

  if (window.location.hash) {
    window.history.replaceState(
      null,
      document.title,
      `${window.location.pathname}${window.location.search}`,
    );
  }

  return {
    token: tokenFromLink || localStorage.getItem(STORAGE.token) || "",
    apiOrigin: trustedApiOrigin(window.location.origin),
    deepTrackId,
  };
}

function absoluteUrl(value: unknown, apiOrigin: string): string {
  if (typeof value !== "string" || !value.trim()) return "";
  try {
    return new URL(value, apiOrigin).toString();
  } catch {
    return "";
  }
}

function normalizeTrack(raw: Record<string, unknown>, index: number, apiOrigin: string): Track {
  const id = String(raw.id ?? raw.media_id ?? `track-${index}`);
  return {
    id,
    title: String(raw.title ?? raw.name ?? "Без названия"),
    artist: String(raw.artist ?? raw.artists ?? "Неизвестный исполнитель"),
    album: String(raw.album ?? raw.collection ?? "Без альбома"),
    durationSeconds: Number(raw.duration_seconds ?? raw.duration ?? 0) || 0,
    sizeBytes: Number(raw.size_bytes ?? 0) || 0,
    mimeType: String(raw.mime_type ?? "audio/mpeg"),
    streamUrl: absoluteUrl(raw.stream_url ?? raw.play_url ?? raw.audio_url, apiOrigin),
    artworkUrl: absoluteUrl(raw.artwork_url ?? raw.cover_url ?? raw.image_url, apiOrigin),
    availability: String(raw.availability ?? raw.status ?? "ready").toLowerCase(),
    addedAt: String(raw.added_at ?? raw.created_at ?? ""),
    streamExpiresAt: Math.max(0, Number(raw.stream_expires_at ?? 0) || 0),
  };
}

function normalizeTracks(payload: unknown, apiOrigin: string): Track[] {
  const source = Array.isArray(payload)
    ? payload
    : payload && typeof payload === "object"
      ? Array.isArray((payload as { items?: unknown[] }).items)
        ? (payload as { items: unknown[] }).items
        : Array.isArray((payload as { tracks?: unknown[] }).tracks)
          ? (payload as { tracks: unknown[] }).tracks
          : []
      : [];
  return source
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
    .map((item, index) => normalizeTrack(item, index, apiOrigin));
}

function normalizeRemoteState(payload: unknown): RemotePlayerState {
  const value = payload && typeof payload === "object" ? (payload as Record<string, unknown>) : {};
  const repeat = String(value.repeat_mode ?? "off");
  return {
    revision: Number(value.revision ?? 0) || 0,
    queue_ids: Array.isArray(value.queue_ids)
      ? value.queue_ids.map((id) => String(id))
      : Array.isArray(value.queue)
        ? value.queue.map((id) => String(id))
        : [],
    current_id: value.current_id ? String(value.current_id) : null,
    position_ms: Math.max(0, Number(value.position_ms ?? value.position_seconds) || 0) *
      (value.position_ms == null && value.position_seconds != null ? 1000 : 1),
    paused: value.paused !== false,
    shuffle: Boolean(value.shuffle),
    repeat_mode: repeat === "one" || repeat === "all" ? repeat : "off",
  };
}

function hashHue(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) | 0;
  }
  return Math.abs(hash) % 360;
}

function coverStyle(track: Track): CSSProperties {
  const hue = hashHue(`${track.artist}-${track.album}-${track.id}`);
  return {
    "--cover-a": `hsl(${hue} 68% 43%)`,
    "--cover-b": `hsl(${(hue + 58) % 360} 72% 23%)`,
  } as CSSProperties;
}

function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${minutes}:${String(rest).padStart(2, "0")}`;
}

function formatSize(bytes: number): string {
  if (!bytes) return "";
  if (bytes < 1_000_000) return `${Math.round(bytes / 1000)} КБ`;
  return `${(bytes / 1_000_000).toFixed(bytes > 100_000_000 ? 0 : 1)} МБ`;
}

function isReady(track: Track): boolean {
  return [
    "ready",
    "archived",
    "available",
    "complete",
    "completed",
    "",
  ].includes(track.availability);
}

function mergeClass(...values: Array<string | false | undefined>): string {
  return values.filter(Boolean).join(" ");
}

function Cover({
  track,
  size = "medium",
  decorative = false,
}: {
  track: Track;
  size?: "small" | "medium" | "large";
  decorative?: boolean;
}) {
  const [imageFailed, setImageFailed] = useState(false);
  const label = `${track.album || track.title} — ${track.artist}`;
  return (
    <div
      className={mergeClass("cover", `cover-${size}`)}
      style={coverStyle(track)}
      role={decorative ? undefined : "img"}
      aria-label={decorative ? undefined : label}
      aria-hidden={decorative ? true : undefined}
    >
      {track.artworkUrl && !imageFailed ? (
        // Catalog artwork can be hosted by the private API or an upstream CDN.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={track.artworkUrl}
          alt=""
          loading={size === "large" ? "eager" : "lazy"}
          onError={() => setImageFailed(true)}
        />
      ) : (
        <span>{(track.artist || track.title).trim().charAt(0).toUpperCase() || "A"}</span>
      )}
    </div>
  );
}

function IconButton({
  label,
  onClick,
  children,
  disabled = false,
  active = false,
  className = "",
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
  disabled?: boolean;
  active?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      className={mergeClass("icon-button", active && "is-active", className)}
      aria-label={label}
      aria-pressed={active || undefined}
      title={label}
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  );
}

function EmptyState({
  icon,
  title,
  body,
  action,
}: {
  icon: React.ReactNode;
  title: string;
  body: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-icon" aria-hidden="true">
        {icon}
      </div>
      <h2>{title}</h2>
      <p>{body}</p>
      {action}
    </div>
  );
}

export function PlayerApp() {
  const audioRef = useRef<HTMLAudioElement>(null);
  const pendingSeekRef = useRef(0);
  const saveInFlightRef = useRef(false);
  const catalogRefreshRef = useRef<Promise<Track[]> | null>(null);
  const streamRetryRef = useRef<string | null>(null);
  const snapshotRef = useRef<SaveSnapshot>({
    revision: 0,
    queueIds: [],
    currentId: null,
    positionMs: 0,
    paused: true,
    shuffle: false,
    repeatMode: "off",
  });

  const [credentials, setCredentials] = useState<Credentials | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("booting");
  const [statusMessage, setStatusMessage] = useState("");
  const [tracks, setTracks] = useState<Track[]>([]);
  const [userId, setUserId] = useState("");
  const [tab, setTab] = useState<Tab>("home");
  const [query, setQuery] = useState("");
  const [queue, setQueue] = useState<Track[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [position, setPosition] = useState(0);
  const [duration, setDuration] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [volume, setVolume] = useState(1);
  const [shuffle, setShuffle] = useState(false);
  const [repeatMode, setRepeatMode] = useState<RepeatMode>("off");
  const [revision, setRevision] = useState(0);
  const [nowPlayingOpen, setNowPlayingOpen] = useState(false);
  const [queueOpen, setQueueOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [playbackError, setPlaybackError] = useState("");
  const [offline, setOffline] = useState(false);
  const [demoMode, setDemoMode] = useState(false);
  const [announce, setAnnounce] = useState("");

  const currentTrack = useMemo(
    () => tracks.find((track) => track.id === currentId) ?? queue.find((track) => track.id === currentId) ?? null,
    [currentId, queue, tracks],
  );

  const currentQueueIndex = useMemo(
    () => queue.findIndex((track) => track.id === currentId),
    [currentId, queue],
  );

  const sortedTracks = useMemo(
    () =>
      [...tracks].sort((first, second) => {
        const a = Date.parse(first.addedAt) || 0;
        const b = Date.parse(second.addedAt) || 0;
        return b - a || first.title.localeCompare(second.title, "ru");
      }),
    [tracks],
  );

  const filteredTracks = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase("ru");
    if (!needle) return sortedTracks;
    return sortedTracks.filter((track) =>
      `${track.title} ${track.artist} ${track.album}`.toLocaleLowerCase("ru").includes(needle),
    );
  }, [query, sortedTracks]);

  const albumGroups = useMemo(() => {
    const groups = new Map<string, Track[]>();
    for (const track of sortedTracks) {
      const key = track.album || "Без альбома";
      const existing = groups.get(key) ?? [];
      existing.push(track);
      groups.set(key, existing);
    }
    return [...groups.entries()].slice(0, 8);
  }, [sortedTracks]);

  useEffect(() => {
    const parsed = readCredentials();
    const storedVolume = storedVolumeOrDefault(
      localStorage.getItem(STORAGE.volume),
    );
    const initialize = window.setTimeout(() => {
      setVolume(storedVolume);
      setCredentials(parsed);
      setOffline(!navigator.onLine);
    }, 0);

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js?v=3").catch(() => undefined);
    }

    const markOffline = () => setOffline(true);
    window.addEventListener("offline", markOffline);
    return () => {
      window.clearTimeout(initialize);
      window.removeEventListener("offline", markOffline);
    };
  }, []);

  const apiFetch = useCallback(
    async (path: string, init: RequestInit = {}) => {
      if (!credentials?.token) throw new ApiError(401, "Missing token");
      const response = await fetch(`${credentials.apiOrigin}${path}`, {
        ...init,
        headers: {
          Authorization: `Bearer ${credentials.token}`,
          "Content-Type": "application/json",
          ...init.headers,
        },
      });
      if (!response.ok) {
        throw new ApiError(response.status, `API request failed: ${response.status}`);
      }
      return response;
    },
    [credentials],
  );

  const fetchCatalog = useCallback(async (): Promise<Track[]> => {
    if (!credentials) return [];
    let offset = 0;
    let total = Number.POSITIVE_INFINITY;
    let catalog: Track[] = [];

    while (offset < total) {
      const response = await apiFetch(
        `/api/v1/tracks?limit=${CATALOG_PAGE_SIZE}&offset=${offset}`,
      );
      const payload = await response.json();
      const page = normalizeTracks(payload, credentials.apiOrigin);
      catalog = mergeUniqueById(catalog, page);

      const rawItems =
        payload &&
        typeof payload === "object" &&
        Array.isArray((payload as { items?: unknown[] }).items)
          ? (payload as { items: unknown[] }).items
          : page;
      const advertisedTotal = Number(
        payload && typeof payload === "object"
          ? (payload as { total?: unknown }).total
          : rawItems.length,
      );
      total =
        Number.isFinite(advertisedTotal) && advertisedTotal >= 0
          ? advertisedTotal
          : offset + rawItems.length;
      if (!rawItems.length) break;
      offset += rawItems.length;
      if (rawItems.length < CATALOG_PAGE_SIZE && offset >= total) break;
    }
    return catalog;
  }, [apiFetch, credentials]);

  const refreshCatalog = useCallback(async (): Promise<Track[]> => {
    if (!credentials?.token || demoMode) return tracks;
    if (catalogRefreshRef.current) return catalogRefreshRef.current;

    const refresh = fetchCatalog()
      .then((catalog) => {
        setTracks(catalog);
        setQueue((current) => refreshQueueItems(current, catalog));
        localStorage.setItem(STORAGE.catalog, JSON.stringify(catalog));
        setOffline(false);
        setStatusMessage("");
        return catalog;
      })
      .catch((error) => {
        if (error instanceof ApiError && [401, 403].includes(error.status)) {
          setLoadState("unauthorized");
        } else {
          setOffline(!navigator.onLine);
        }
        throw error;
      })
      .finally(() => {
        catalogRefreshRef.current = null;
      });
    catalogRefreshRef.current = refresh;
    return refresh;
  }, [credentials, demoMode, fetchCatalog, tracks]);

  const loadLibrary = useCallback(async () => {
    if (!credentials) return;
    setLoadState("booting");
    setStatusMessage("");

    if (!credentials.token) {
      if (DEMO_ENABLED) {
        setDemoMode(true);
        setTracks(DEMO_TRACKS);
        setQueue(DEMO_TRACKS);
        setCurrentId(DEMO_TRACKS[0].id);
        setLoadState("ready");
      } else {
        setLoadState("unauthorized");
      }
      return;
    }

    try {
      const catalog = await fetchCatalog();
      const [meResult, stateResult] = await Promise.allSettled([
        apiFetch("/api/v1/me"),
        apiFetch("/api/v1/player-state"),
      ]);
      const mePayload =
        meResult.status === "fulfilled" ? await meResult.value.json() : {};
      const statePayload =
        stateResult.status === "fulfilled" ? await stateResult.value.json() : {};
      const remoteState = normalizeRemoteState(statePayload);
      const selection = resolvePlaybackSelection(
        catalog,
        remoteState.queue_ids,
        remoteState.current_id,
        credentials.deepTrackId,
      );

      setTracks(catalog);
      setQueue(selection.queue);
      setCurrentId(selection.currentId);
      setShuffle(remoteState.shuffle);
      setRepeatMode(remoteState.repeat_mode);
      setRevision(remoteState.revision);
      pendingSeekRef.current = credentials.deepTrackId
        ? 0
        : remoteState.position_ms / 1000;
      setPosition(pendingSeekRef.current);
      const me = mePayload && typeof mePayload === "object" ? (mePayload as Record<string, unknown>) : {};
      setUserId(String(me.user_id ?? ""));
      localStorage.setItem(STORAGE.catalog, JSON.stringify(catalog));
      setLoadState("ready");
      setDemoMode(false);
    } catch (error) {
      if (error instanceof ApiError && [401, 403].includes(error.status)) {
        setLoadState("unauthorized");
        setStatusMessage("Ссылка доступа недействительна или истекла.");
        return;
      }

      const cached = localStorage.getItem(STORAGE.catalog);
      if (cached) {
        try {
          const catalog = normalizeTracks(JSON.parse(cached), credentials.apiOrigin);
          setTracks(catalog);
          setQueue(catalog);
          setCurrentId(catalog[0]?.id ?? null);
          setOffline(true);
          setLoadState("ready");
          setStatusMessage("Показываем сохранённую библиотеку. Для воспроизведения нужно подключение.");
          return;
        } catch {
          localStorage.removeItem(STORAGE.catalog);
        }
      }
      setLoadState(navigator.onLine ? "error" : "offline");
      setStatusMessage("Не удалось связаться с музыкальной библиотекой.");
    }
  }, [apiFetch, credentials, fetchCatalog]);

  useEffect(() => {
    const start = window.setTimeout(() => void loadLibrary(), 0);
    return () => window.clearTimeout(start);
  }, [loadLibrary]);

  useEffect(() => {
    if (loadState !== "ready" || demoMode || !credentials?.token) return;
    const refresh = () => {
      if (document.visibilityState === "visible" && navigator.onLine) {
        void refreshCatalog().catch(() => undefined);
      }
    };
    const markOnline = () => {
      setOffline(false);
      refresh();
    };
    const timer = window.setInterval(refresh, CATALOG_REFRESH_MS);
    window.addEventListener("online", markOnline);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("online", markOnline);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [credentials, demoMode, loadState, refreshCatalog]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.volume = volume;
    localStorage.setItem(STORAGE.volume, String(volume));
  }, [volume]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio || !currentTrack?.streamUrl) return;
    if (audio.src !== currentTrack.streamUrl) {
      audio.src = currentTrack.streamUrl;
      audio.load();
    }
  }, [currentTrack]);

  const playTrack = useCallback(
    async (track: Track, contextQueue?: Track[], startAt = 0) => {
      if (!isReady(track)) {
        setPlaybackError("Этот трек ещё подготавливается. Он появится в плеере автоматически.");
        return;
      }
      if (!track.streamUrl) {
        setPlaybackError(
          demoMode
            ? "Это демонстрационный каталог. Подключите бота, чтобы воспроизводить настоящие треки."
            : "Для этого трека пока нет ссылки воспроизведения.",
        );
        return;
      }

      let playableTrack = track;
      let latestCatalog = tracks;
      if (
        !demoMode &&
        track.streamExpiresAt > 0 &&
        track.streamExpiresAt <= Math.floor(Date.now() / 1000) + 30
      ) {
        try {
          const refreshed = await refreshCatalog();
          latestCatalog = refreshed;
          playableTrack =
            refreshed.find((candidate) => candidate.id === track.id) ?? track;
        } catch {
          setPlaybackError("Не удалось обновить ссылку на трек. Проверьте подключение.");
          return;
        }
      }

      const audio = audioRef.current;
      if (!audio) return;
      setPlaybackError("");
      if (contextQueue?.length) {
        setQueue(refreshQueueItems(contextQueue, latestCatalog));
      }
      setCurrentId(playableTrack.id);
      pendingSeekRef.current = Math.max(0, startAt);
      if (audio.src !== playableTrack.streamUrl) {
        audio.src = playableTrack.streamUrl;
        audio.load();
      } else {
        audio.currentTime = pendingSeekRef.current;
      }
      try {
        await audio.play();
        setAnnounce(`Сейчас играет: ${track.title}, ${track.artist}`);
      } catch {
        setPlaybackError("Safari не смог запустить трек. Нажмите Play ещё раз.");
      }
    },
    [demoMode, refreshCatalog, tracks],
  );

  const handlePlaybackError = useCallback(async () => {
    setIsPlaying(false);
    if (
      currentTrack &&
      credentials?.token &&
      !demoMode &&
      streamRetryRef.current !== currentTrack.id
    ) {
      streamRetryRef.current = currentTrack.id;
      try {
        const catalog = await refreshCatalog();
        const refreshed = catalog.find((track) => track.id === currentTrack.id);
        const audio = audioRef.current;
        if (refreshed?.streamUrl && audio) {
          audio.src = refreshed.streamUrl;
          audio.load();
          await audio.play();
          setPlaybackError("");
          return;
        }
      } catch {
        // Fall through to the actionable playback error below.
      }
    }
    setPlaybackError("Не удалось воспроизвести трек. Обновите библиотеку и попробуйте ещё раз.");
  }, [credentials, currentTrack, demoMode, refreshCatalog]);

  const togglePlayback = useCallback(async () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (!currentTrack) {
      const first = tracks.find(isReady);
      if (first) await playTrack(first, tracks);
      return;
    }
    if (audio.paused) {
      if (!currentTrack.streamUrl) {
        await playTrack(currentTrack, queue);
        return;
      }
      try {
        await audio.play();
        setPlaybackError("");
      } catch {
        setPlaybackError("Не удалось продолжить воспроизведение.");
      }
    } else {
      audio.pause();
    }
  }, [currentTrack, playTrack, queue, tracks]);

  const moveToNext = useCallback(
    async (fromEnded = false) => {
      if (!queue.length || !currentTrack) return;
      if (fromEnded && repeatMode === "one") {
        await playTrack(currentTrack, queue);
        return;
      }
      let nextIndex = currentQueueIndex + 1;
      if (shuffle && queue.length > 1) {
        do {
          nextIndex = Math.floor(Math.random() * queue.length);
        } while (nextIndex === currentQueueIndex);
      } else if (nextIndex >= queue.length) {
        if (repeatMode === "all") nextIndex = 0;
        else {
          audioRef.current?.pause();
          return;
        }
      }
      await playTrack(queue[nextIndex], queue);
    },
    [currentQueueIndex, currentTrack, playTrack, queue, repeatMode, shuffle],
  );

  const moveToPrevious = useCallback(async () => {
    const audio = audioRef.current;
    if (!audio || !currentTrack) return;
    if (audio.currentTime > 5 || currentQueueIndex <= 0) {
      audio.currentTime = 0;
      setPosition(0);
      return;
    }
    await playTrack(queue[currentQueueIndex - 1], queue);
  }, [currentQueueIndex, currentTrack, playTrack, queue]);

  const seekBy = useCallback((delta: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    const next = Math.min(Math.max(0, audio.currentTime + delta), audio.duration || Infinity);
    audio.currentTime = next;
    setPosition(next);
  }, []);

  const handleLoadedMetadata = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    setDuration(Number.isFinite(audio.duration) ? audio.duration : currentTrack?.durationSeconds ?? 0);
    if (pendingSeekRef.current > 0) {
      audio.currentTime = Math.min(pendingSeekRef.current, audio.duration || pendingSeekRef.current);
      setPosition(audio.currentTime);
      pendingSeekRef.current = 0;
    }
  }, [currentTrack]);

  const handleSeek = (event: ChangeEvent<HTMLInputElement>) => {
    const value = Number(event.currentTarget.value);
    if (!audioRef.current) return;
    audioRef.current.currentTime = value;
    setPosition(value);
  };

  const cycleRepeat = () => {
    setRepeatMode((current) => (current === "off" ? "all" : current === "all" ? "one" : "off"));
  };

  const removeFromQueue = (trackId: string) => {
    if (trackId === currentId) return;
    setQueue((current) => current.filter((track) => track.id !== trackId));
  };

  const applyRemotePlayerState = useCallback(
    (remoteState: RemotePlayerState) => {
      const selection = resolvePlaybackSelection(
        tracks,
        remoteState.queue_ids,
        remoteState.current_id,
        "",
      );
      setRevision(remoteState.revision);
      setQueue(selection.queue);
      setCurrentId(selection.currentId);
      setShuffle(remoteState.shuffle);
      setRepeatMode(remoteState.repeat_mode);
      pendingSeekRef.current = remoteState.position_ms / 1000;
      setPosition(pendingSeekRef.current);
      if (remoteState.paused) audioRef.current?.pause();
    },
    [tracks],
  );

  const saveState = useCallback(
    async (keepalive = false) => {
      if (!credentials?.token || demoMode || saveInFlightRef.current) return;
      saveInFlightRef.current = true;
      const snapshot = snapshotRef.current;
      try {
        const response = await fetch(`${credentials.apiOrigin}/api/v1/player-state`, {
          method: "PUT",
          keepalive,
          headers: {
            Authorization: `Bearer ${credentials.token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            revision: snapshot.revision,
            queue_ids: snapshot.queueIds,
            current_id: snapshot.currentId,
            position_ms: snapshot.positionMs,
            paused: snapshot.paused,
            shuffle: snapshot.shuffle,
            repeat_mode: snapshot.repeatMode,
          }),
        });
        if (response.ok) {
          const saved = normalizeRemoteState(await response.json());
          setRevision(saved.revision);
        } else if (response.status === 401 || response.status === 403) {
          setLoadState("unauthorized");
        } else if (response.status === 409) {
          const conflictPayload = await response.json();
          const latestState = normalizeRemoteState(
            conflictPayload &&
              typeof conflictPayload === "object" &&
              (conflictPayload as { current?: unknown }).current
              ? (conflictPayload as { current: unknown }).current
              : (await (await apiFetch("/api/v1/player-state")).json()),
          );
          applyRemotePlayerState(latestState);
          setStatusMessage(
            "Состояние обновлено из другой вкладки, чтобы не потерять изменения.",
          );
        } else {
          setStatusMessage(
            `Не удалось сохранить состояние плеера (HTTP ${response.status}).`,
          );
        }
      } catch {
        // State saving is best-effort; playback should continue during a brief outage.
      } finally {
        saveInFlightRef.current = false;
      }
    },
    [apiFetch, applyRemotePlayerState, credentials, demoMode],
  );

  useEffect(() => {
    snapshotRef.current = {
      revision,
      queueIds: queue.map((track) => track.id),
      currentId,
      positionMs: Math.round(position * 1000),
      paused: !isPlaying,
      shuffle,
      repeatMode,
    };
  }, [currentId, isPlaying, position, queue, repeatMode, revision, shuffle]);

  useEffect(() => {
    if (loadState !== "ready") return;
    const timer = window.setInterval(() => void saveState(), 5000);
    const saveBeforeLeave = () => void saveState(true);
    window.addEventListener("pagehide", saveBeforeLeave);
    document.addEventListener("visibilitychange", saveBeforeLeave);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("pagehide", saveBeforeLeave);
      document.removeEventListener("visibilitychange", saveBeforeLeave);
    };
  }, [loadState, saveState]);

  useEffect(() => {
    if (!("mediaSession" in navigator) || !currentTrack) return;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: currentTrack.title,
      artist: currentTrack.artist,
      album: currentTrack.album,
      artwork: currentTrack.artworkUrl
        ? [
            {
              src: currentTrack.artworkUrl,
              sizes: "512x512",
            },
          ]
        : [],
    });

    const handlers: Array<[MediaSessionAction, MediaSessionActionHandler | null]> = [
      ["play", () => void togglePlayback()],
      ["pause", () => void togglePlayback()],
      ["previoustrack", () => void moveToPrevious()],
      ["nexttrack", () => void moveToNext()],
      ["seekbackward", (details) => seekBy(-(details.seekOffset ?? 15))],
      ["seekforward", (details) => seekBy(details.seekOffset ?? 15)],
      [
        "seekto",
        (details) => {
          if (details.seekTime == null || !audioRef.current) return;
          audioRef.current.currentTime = details.seekTime;
          setPosition(details.seekTime);
        },
      ],
    ];
    for (const [action, handler] of handlers) {
      try {
        navigator.mediaSession.setActionHandler(action, handler);
      } catch {
        // Safari exposes a subset of Media Session actions.
      }
    }
    return () => {
      for (const [action] of handlers) {
        try {
          navigator.mediaSession.setActionHandler(action, null);
        } catch {
          // Ignore unsupported handlers.
        }
      }
    };
  }, [currentTrack, moveToNext, moveToPrevious, seekBy, togglePlayback]);

  useEffect(() => {
    if (!("mediaSession" in navigator) || !duration || duration === Infinity) return;
    try {
      navigator.mediaSession.playbackState = isPlaying ? "playing" : "paused";
      navigator.mediaSession.setPositionState({
        duration,
        playbackRate: audioRef.current?.playbackRate ?? 1,
        position: Math.min(Math.max(position, 0), duration),
      });
    } catch {
      // Position state is not available in every Safari release.
    }
  }, [duration, isPlaying, position]);

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setQueueOpen(false);
        setNowPlayingOpen(false);
        setSidebarOpen(false);
        return;
      }
      if (shouldIgnoreGlobalShortcut(event.target)) return;
      if (event.code === "Space") {
        event.preventDefault();
        void togglePlayback();
      } else if (event.key === "ArrowRight") {
        seekBy(5);
      } else if (event.key === "ArrowLeft") {
        seekBy(-5);
      } else if (event.key.toLowerCase() === "n") {
        void moveToNext();
      } else if (event.key.toLowerCase() === "p") {
        void moveToPrevious();
      } else if (event.key === "/") {
        event.preventDefault();
        setTab("search");
        window.setTimeout(() => document.getElementById("library-search")?.focus(), 0);
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [moveToNext, moveToPrevious, seekBy, togglePlayback]);

  const selectTab = (next: Tab) => {
    setTab(next);
    setSidebarOpen(false);
    if (next === "search") {
      window.setTimeout(() => document.getElementById("library-search")?.focus(), 0);
    }
  };

  const repeatLabel =
    repeatMode === "one"
      ? "Повторять текущий трек"
      : repeatMode === "all"
        ? "Повторять очередь"
        : "Повтор выключен";

  if (loadState === "booting") {
    return (
      <main className="centered-shell" aria-busy="true">
        <Brand />
        <LoaderCircle className="spin" size={28} aria-hidden="true" />
        <p>Открываем вашу медиатеку…</p>
      </main>
    );
  }

  if (loadState === "unauthorized") {
    return (
      <main className="centered-shell">
        <Brand />
        <EmptyState
          icon={<Radio size={28} />}
          title="Откройте плеер из Telegram"
          body={
            statusMessage ||
            "Нажмите кнопку «Слушать в Antra» у бота — она безопасно подключит вашу библиотеку."
          }
          action={
            <a className="primary-action" href={TELEGRAM_URL}>
              <Send size={18} aria-hidden="true" />
              Открыть @fnnlinkbot
            </a>
          }
        />
      </main>
    );
  }

  if (loadState === "offline" || loadState === "error") {
    return (
      <main className="centered-shell">
        <Brand />
        <EmptyState
          icon={loadState === "offline" ? <WifiOff size={28} /> : <Disc3 size={28} />}
          title={loadState === "offline" ? "Сейчас нет сети" : "Библиотека не отвечает"}
          body={statusMessage || "Проверьте подключение и попробуйте ещё раз."}
          action={
            <button className="primary-action" type="button" onClick={() => void loadLibrary()}>
              <RefreshCw size={18} aria-hidden="true" />
              Повторить
            </button>
          }
        />
      </main>
    );
  }

  return (
    <div className="app-shell">
      <audio
        ref={audioRef}
        preload="metadata"
        onTimeUpdate={(event) => setPosition(event.currentTarget.currentTime || 0)}
        onDurationChange={(event) =>
          setDuration(
            Number.isFinite(event.currentTarget.duration)
              ? event.currentTarget.duration
              : currentTrack?.durationSeconds ?? 0,
          )
        }
        onLoadedMetadata={handleLoadedMetadata}
        onPlay={() => {
          streamRetryRef.current = null;
          setIsPlaying(true);
        }}
        onPause={() => setIsPlaying(false)}
        onEnded={() => void moveToNext(true)}
        onError={() => void handlePlaybackError()}
      />

      <div className="sr-only" aria-live="polite">
        {announce}
      </div>

      <aside className={mergeClass("sidebar", sidebarOpen && "is-open")} aria-label="Основная навигация">
        <div className="sidebar-top">
          <Brand />
          <IconButton label="Закрыть меню" onClick={() => setSidebarOpen(false)} className="mobile-only">
            <X size={22} />
          </IconButton>
        </div>
        <nav className="side-nav">
          <NavButton active={tab === "home"} onClick={() => selectTab("home")} icon={<House size={21} />}>
            Главная
          </NavButton>
          <NavButton active={tab === "search"} onClick={() => selectTab("search")} icon={<Search size={21} />}>
            Поиск
          </NavButton>
          <NavButton
            active={tab === "library"}
            onClick={() => selectTab("library")}
            icon={<Library size={21} />}
          >
            Медиатека
          </NavButton>
        </nav>
        <div className="sidebar-library">
          <p className="eyebrow">Общая библиотека</p>
          <div className="library-stat">
            <Disc3 size={18} aria-hidden="true" />
            <span>{tracks.length} треков</span>
          </div>
          <div className="library-stat">
            <ListMusic size={18} aria-hidden="true" />
            <span>{albumGroups.length} коллекций</span>
          </div>
        </div>
        <a className="telegram-action" href={TELEGRAM_URL}>
          <Plus size={19} aria-hidden="true" />
          Добавить через Telegram
        </a>
      </aside>

      {sidebarOpen && (
        <button
          className="sidebar-backdrop"
          aria-label="Закрыть меню"
          type="button"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <div className="main-column">
        <header className="topbar">
          <IconButton label="Открыть меню" onClick={() => setSidebarOpen(true)} className="mobile-only">
            <Menu size={22} />
          </IconButton>
          <div className="topbar-copy">
            <span className="topbar-title">
              {tab === "home" ? "Главная" : tab === "search" ? "Поиск" : "Медиатека"}
            </span>
            {demoMode && <span className="demo-badge">Демо</span>}
          </div>
          <a className="profile-chip" href={TELEGRAM_URL} aria-label="Открыть Telegram-бота">
            <span>{userId ? userId.slice(-2) : "TG"}</span>
            <Send size={15} aria-hidden="true" />
          </a>
        </header>

        {(offline || statusMessage) && (
          <div className="connection-banner" role="status">
            <WifiOff size={17} aria-hidden="true" />
            <span>{statusMessage || "Нет сети. Доступна сохранённая оболочка плеера."}</span>
          </div>
        )}

        <main className="content">
          {tracks.length === 0 ? (
            <EmptyState
              icon={<Music2 size={30} />}
              title="Медиатека пока пуста"
              body="Отправьте название песни или ссылку на плейлист боту. Трек появится здесь автоматически."
              action={
                <a className="primary-action" href={TELEGRAM_URL}>
                  <Send size={18} aria-hidden="true" />
                  Добавить первую песню
                </a>
              }
            />
          ) : tab === "home" ? (
            <HomeView
              tracks={sortedTracks}
              albums={albumGroups}
              currentTrack={currentTrack}
              position={position}
              onPlay={(track, context) => void playTrack(track, context)}
              onOpenLibrary={() => selectTab("library")}
            />
          ) : tab === "search" ? (
            <SearchView
              query={query}
              onQuery={setQuery}
              tracks={filteredTracks}
              allTracks={sortedTracks}
              onPlay={(track, context) => void playTrack(track, context)}
            />
          ) : (
            <LibraryView tracks={sortedTracks} onPlay={(track, context) => void playTrack(track, context)} />
          )}
        </main>
      </div>

      {currentTrack && (
        <MiniPlayer
          track={currentTrack}
          isPlaying={isPlaying}
          position={position}
          duration={duration || currentTrack.durationSeconds}
          onOpen={() => setNowPlayingOpen(true)}
          onToggle={() => void togglePlayback()}
          onNext={() => void moveToNext()}
        />
      )}

      <nav className="bottom-nav" aria-label="Мобильная навигация">
        <NavButton active={tab === "home"} onClick={() => selectTab("home")} icon={<House size={21} />}>
          Главная
        </NavButton>
        <NavButton active={tab === "search"} onClick={() => selectTab("search")} icon={<Search size={21} />}>
          Поиск
        </NavButton>
        <NavButton
          active={tab === "library"}
          onClick={() => selectTab("library")}
          icon={<Library size={21} />}
        >
          Медиатека
        </NavButton>
      </nav>

      {nowPlayingOpen && currentTrack && (
        <section
          className="now-playing"
          role="dialog"
          aria-modal="true"
          aria-label="Сейчас играет"
        >
          <div className="now-playing-backdrop" style={coverStyle(currentTrack)} />
          <header className="now-header">
            <IconButton label="Свернуть плеер" onClick={() => setNowPlayingOpen(false)}>
              <ChevronDown size={26} />
            </IconButton>
            <div>
              <span className="eyebrow">Сейчас играет</span>
              <strong>{currentTrack.album}</strong>
            </div>
            <IconButton label="Открыть очередь" onClick={() => setQueueOpen(true)}>
              <ListMusic size={23} />
            </IconButton>
          </header>

          <div className="now-body">
            <Cover track={currentTrack} size="large" decorative />
            <div className="now-track-copy">
              <div>
                <h1>{currentTrack.title}</h1>
                <p>{currentTrack.artist}</p>
              </div>
              <IconButton label="Другие действия" onClick={() => setQueueOpen(true)}>
                <Ellipsis size={23} />
              </IconButton>
            </div>

            <div className="timeline">
              <input
                aria-label="Позиция воспроизведения"
                type="range"
                min="0"
                max={Math.max(duration || currentTrack.durationSeconds, 1)}
                value={Math.min(position, duration || currentTrack.durationSeconds || 0)}
                step="0.1"
                onChange={handleSeek}
              />
              <div className="timeline-labels">
                <span>{formatTime(position)}</span>
                <span>-{formatTime(Math.max((duration || currentTrack.durationSeconds) - position, 0))}</span>
              </div>
            </div>

            <div className="now-controls">
              <IconButton label={shuffle ? "Выключить перемешивание" : "Перемешать"} active={shuffle} onClick={() => setShuffle(!shuffle)}>
                <Shuffle size={23} />
              </IconButton>
              <IconButton label="Предыдущий трек" onClick={() => void moveToPrevious()}>
                <SkipBack size={30} fill="currentColor" />
              </IconButton>
              <IconButton label={isPlaying ? "Пауза" : "Воспроизвести"} onClick={() => void togglePlayback()} className="primary-play">
                {isPlaying ? <Pause size={32} fill="currentColor" /> : <Play size={32} fill="currentColor" />}
              </IconButton>
              <IconButton label="Следующий трек" onClick={() => void moveToNext()}>
                <SkipForward size={30} fill="currentColor" />
              </IconButton>
              <IconButton label={repeatLabel} active={repeatMode !== "off"} onClick={cycleRepeat}>
                {repeatMode === "one" ? <Repeat1 size={23} /> : <Repeat size={23} />}
              </IconButton>
            </div>

            {currentTrack.durationSeconds > 1200 && (
              <div className="long-form-controls">
                <button type="button" onClick={() => seekBy(-15)}>
                  −15 сек
                </button>
                <button type="button" onClick={() => seekBy(15)}>
                  +15 сек
                </button>
              </div>
            )}

            <div className="volume-row">
              <Volume2 size={19} aria-hidden="true" />
              <input
                aria-label="Громкость"
                type="range"
                min="0"
                max="1"
                step="0.01"
                value={volume}
                onChange={(event) => setVolume(Number(event.currentTarget.value))}
              />
              <button type="button" className="queue-link" onClick={() => setQueueOpen(true)}>
                <ListMusic size={18} aria-hidden="true" />
                Очередь · {queue.length}
              </button>
            </div>

            {playbackError && <p className="playback-error" role="alert">{playbackError}</p>}
          </div>
        </section>
      )}

      {queueOpen && (
        <QueueSheet
          queue={queue}
          currentId={currentId}
          onClose={() => setQueueOpen(false)}
          onPlay={(track) => void playTrack(track, queue)}
          onRemove={removeFromQueue}
        />
      )}

      {playbackError && !nowPlayingOpen && (
        <div className="toast" role="alert">
          <span>{playbackError}</span>
          <button type="button" aria-label="Закрыть сообщение" onClick={() => setPlaybackError("")}>
            <X size={18} />
          </button>
        </div>
      )}
    </div>
  );
}

function Brand() {
  return (
    <div className="brand" aria-label="Antra Player">
      <span className="brand-mark" aria-hidden="true">
        A
      </span>
      <span>
        <strong>ANTRA</strong>
        <small>PLAYER</small>
      </span>
    </div>
  );
}

function NavButton({
  active,
  onClick,
  icon,
  children,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      className={mergeClass("nav-button", active && "is-active")}
      aria-current={active ? "page" : undefined}
      onClick={onClick}
    >
      {icon}
      <span>{children}</span>
    </button>
  );
}

function HomeView({
  tracks,
  albums,
  currentTrack,
  position,
  onPlay,
  onOpenLibrary,
}: {
  tracks: Track[];
  albums: Array<[string, Track[]]>;
  currentTrack: Track | null;
  position: number;
  onPlay: (track: Track, context: Track[]) => void;
  onOpenLibrary: () => void;
}) {
  const resumable =
    currentTrack &&
    currentTrack.durationSeconds > 600 &&
    position > 60 &&
    position < currentTrack.durationSeconds - 30
      ? currentTrack
      : null;
  return (
    <>
      <section className="hero-copy">
        <p className="eyebrow">Приватная медиатека</p>
        <h1>Ваша музыка.<br />Без лишнего шума.</h1>
        <p>Добавляйте треки в Telegram и слушайте их здесь как в обычном плеере.</p>
      </section>

      {resumable && (
        <section className="resume-card" style={coverStyle(resumable)}>
          <div>
            <p className="eyebrow">Продолжить</p>
            <h2>{resumable.title}</h2>
            <p>{resumable.artist} · остановились на {formatTime(position)}</p>
          </div>
          <button type="button" aria-label={`Продолжить ${resumable.title}`} onClick={() => onPlay(resumable, tracks)}>
            <Play size={24} fill="currentColor" />
          </button>
        </section>
      )}

      <section className="content-section">
        <SectionHeading title="Недавно добавлено" action="Все треки" onAction={onOpenLibrary} />
        <div className="track-grid">
          {tracks.slice(0, 8).map((track) => (
            <TrackCard key={track.id} track={track} onPlay={() => onPlay(track, tracks)} />
          ))}
        </div>
      </section>

      <section className="content-section">
        <SectionHeading title="Коллекции" />
        <div className="album-row">
          {albums.slice(0, 6).map(([album, albumTracks]) => (
            <button
              type="button"
              className="album-card"
              key={album}
              onClick={() => onPlay(albumTracks[0], albumTracks)}
              aria-label={`Воспроизвести коллекцию ${album}`}
            >
              <Cover track={albumTracks[0]} size="medium" decorative />
              <strong>{album}</strong>
              <span>{albumTracks[0].artist} · {albumTracks.length}</span>
            </button>
          ))}
        </div>
      </section>

      <section className="content-section">
        <SectionHeading title="Последние треки" action="Медиатека" onAction={onOpenLibrary} />
        <TrackList tracks={tracks.slice(0, 7)} context={tracks} onPlay={onPlay} />
      </section>
    </>
  );
}

function SearchView({
  query,
  onQuery,
  tracks,
  allTracks,
  onPlay,
}: {
  query: string;
  onQuery: (value: string) => void;
  tracks: Track[];
  allTracks: Track[];
  onPlay: (track: Track, context: Track[]) => void;
}) {
  return (
    <section className="search-view">
      <div className="page-heading">
        <p className="eyebrow">Вся библиотека</p>
        <h1>Что послушаем?</h1>
      </div>
      <label className="search-box" htmlFor="library-search">
        <Search size={22} aria-hidden="true" />
        <input
          id="library-search"
          type="search"
          value={query}
          onChange={(event) => onQuery(event.currentTarget.value)}
          placeholder="Трек, исполнитель или альбом"
          autoComplete="off"
        />
        {query && (
          <button type="button" onClick={() => onQuery("")} aria-label="Очистить поиск">
            <X size={19} />
          </button>
        )}
      </label>

      {query && tracks.length === 0 ? (
        <EmptyState
          icon={<Search size={28} />}
          title="Ничего не нашли"
          body="Попробуйте другой запрос или отправьте эту песню боту — он добавит её в библиотеку."
          action={
            <a className="primary-action" href={TELEGRAM_URL}>
              <Send size={18} aria-hidden="true" />
              Найти через Telegram
            </a>
          }
        />
      ) : (
        <div className="search-results">
          <SectionHeading title={query ? `Результаты · ${tracks.length}` : "Все треки"} />
          <TrackList tracks={tracks} context={query ? tracks : allTracks} onPlay={onPlay} />
        </div>
      )}
    </section>
  );
}

function LibraryView({
  tracks,
  onPlay,
}: {
  tracks: Track[];
  onPlay: (track: Track, context: Track[]) => void;
}) {
  return (
    <section>
      <div className="page-heading library-heading">
        <div>
          <p className="eyebrow">Общая библиотека</p>
          <h1>Медиатека</h1>
          <p>{tracks.length} треков · добавляются через @fnnlinkbot</p>
        </div>
        <a className="secondary-action" href={TELEGRAM_URL}>
          <Plus size={18} aria-hidden="true" />
          Добавить
        </a>
      </div>
      <div className="library-table-head" aria-hidden="true">
        <span>#</span>
        <span>Название</span>
        <span>Альбом</span>
        <span>Формат</span>
        <span>Время</span>
      </div>
      <TrackList tracks={tracks} context={tracks} onPlay={onPlay} detailed />
    </section>
  );
}

function SectionHeading({
  title,
  action,
  onAction,
}: {
  title: string;
  action?: string;
  onAction?: () => void;
}) {
  return (
    <div className="section-heading">
      <h2>{title}</h2>
      {action && onAction && (
        <button type="button" onClick={onAction}>
          {action}
        </button>
      )}
    </div>
  );
}

function TrackCard({ track, onPlay }: { track: Track; onPlay: () => void }) {
  const ready = isReady(track);
  return (
    <button
      type="button"
      className="track-card"
      onClick={onPlay}
      disabled={!ready}
      aria-label={ready ? `Воспроизвести ${track.title}, ${track.artist}` : `${track.title} подготавливается`}
    >
      <div className="track-card-art">
        <Cover track={track} size="medium" decorative />
        <span className="card-play" aria-hidden="true">
          {ready ? <Play size={20} fill="currentColor" /> : <LoaderCircle className="spin" size={20} />}
        </span>
      </div>
      <strong>{track.title}</strong>
      <span>{ready ? track.artist : "Подготавливается…"}</span>
    </button>
  );
}

function TrackList({
  tracks,
  context,
  onPlay,
  detailed = false,
}: {
  tracks: Track[];
  context: Track[];
  onPlay: (track: Track, context: Track[]) => void;
  detailed?: boolean;
}) {
  return (
    <div className={mergeClass("track-list", detailed && "is-detailed")}>
      {tracks.map((track, index) => {
        const ready = isReady(track);
        return (
          <button
            type="button"
            className="track-row"
            key={track.id}
            onClick={() => onPlay(track, context)}
            disabled={!ready}
          >
            <span className="track-index">{ready ? index + 1 : <LoaderCircle className="spin" size={16} />}</span>
            <Cover track={track} size="small" decorative />
            <span className="track-copy">
              <strong>{track.title}</strong>
              <small>{ready ? track.artist : "Подготавливается для плеера…"}</small>
            </span>
            {detailed && <span className="track-album">{track.album}</span>}
            {detailed && (
              <span className="track-format">
                {track.mimeType.replace(/^audio\//, "").toUpperCase()}
                {formatSize(track.sizeBytes) && ` · ${formatSize(track.sizeBytes)}`}
              </span>
            )}
            <span className="track-duration">{formatTime(track.durationSeconds)}</span>
            <span className="row-play" aria-hidden="true">
              {ready ? <Play size={17} fill="currentColor" /> : <LoaderCircle className="spin" size={17} />}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function MiniPlayer({
  track,
  isPlaying,
  position,
  duration,
  onOpen,
  onToggle,
  onNext,
}: {
  track: Track;
  isPlaying: boolean;
  position: number;
  duration: number;
  onOpen: () => void;
  onToggle: () => void;
  onNext: () => void;
}) {
  const progress = duration > 0 ? Math.min((position / duration) * 100, 100) : 0;
  return (
    <div className="mini-player" style={{ "--progress": `${progress}%` } as CSSProperties}>
      <button type="button" className="mini-track" onClick={onOpen} aria-label="Открыть полный плеер">
        <Cover track={track} size="small" decorative />
        <span>
          <strong>{track.title}</strong>
          <small>{track.artist}</small>
        </span>
      </button>
      <div className="mini-controls">
        <IconButton label={isPlaying ? "Пауза" : "Воспроизвести"} onClick={onToggle}>
          {isPlaying ? <Pause size={21} fill="currentColor" /> : <Play size={21} fill="currentColor" />}
        </IconButton>
        <IconButton label="Следующий трек" onClick={onNext}>
          <SkipForward size={21} fill="currentColor" />
        </IconButton>
      </div>
    </div>
  );
}

function QueueSheet({
  queue,
  currentId,
  onClose,
  onPlay,
  onRemove,
}: {
  queue: Track[];
  currentId: string | null;
  onClose: () => void;
  onPlay: (track: Track) => void;
  onRemove: (trackId: string) => void;
}) {
  return (
    <div className="sheet-layer">
      <button className="sheet-backdrop" type="button" aria-label="Закрыть очередь" onClick={onClose} />
      <section
        className="queue-sheet"
        role="dialog"
        aria-modal="true"
        aria-label="Очередь воспроизведения"
      >
        <div className="sheet-handle" aria-hidden="true" />
        <header>
          <div>
            <p className="eyebrow">Далее</p>
            <h2>Очередь · {queue.length}</h2>
          </div>
          <IconButton label="Закрыть очередь" onClick={onClose}>
            <X size={22} />
          </IconButton>
        </header>
        <div className="queue-list">
          {queue.map((track) => (
            <div className={mergeClass("queue-row", track.id === currentId && "is-current")} key={track.id}>
              <button type="button" className="queue-track" onClick={() => onPlay(track)}>
                <Cover track={track} size="small" decorative />
                <span>
                  <strong>{track.title}</strong>
                  <small>{track.artist}</small>
                </span>
                {track.id === currentId && <Check size={18} aria-label="Сейчас играет" />}
              </button>
              {track.id !== currentId && (
                <IconButton label={`Убрать ${track.title} из очереди`} onClick={() => onRemove(track.id)}>
                  <X size={18} />
                </IconButton>
              )}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
