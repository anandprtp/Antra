<script lang="ts">
  import { onMount } from 'svelte';
  import { GetConfig, SaveConfig, PickDirectory, StartDownload, RetryTrackDownload, CancelDownload, GetHistory, AddHistory, ClearHistory, ValidateTidalAuth, StartTidalOAuthLogin, StartAppleBrowserLogin, StartAmazonBrowserLogin, ConfirmAmazonLogin } from '../wailsjs/go/main/App.js';
  import { ScanFolder, AnalyzeAudio, PickAnalyzerFiles, WriteFile, GetArtistDiscography, SearchArtists, CheckSourceHealth, GetSlskdWebUIInfo, GetDownloadedMusicLibrary, GetDownloadedRelease, GetSupportStatus } from '../wailsjs/go/main/App.js';
  import { EventsOn, BrowserOpenURL } from '../wailsjs/runtime/runtime.js';
  import type { main } from '../wailsjs/go/models';

  let config: main.Config = {
    download_path: '',
    soulseek_enabled: false,
    soulseek_username: '',
    soulseek_password: '',
    soulseek_seed_after_download: false,
    sources_enabled: [],
    first_run_complete: false,
    apple_enabled: true,
    apple_authorization_token: '',
    apple_music_user_token: '',
    apple_storefront: 'us',
    apple_wvd_path: '',
    amazon_enabled: false,
    amazon_direct_creds_json: '',
    amazon_wvd_path: '',
    qobuz_enabled: false,
    qobuz_email: '',
    qobuz_password: '',
    qobuz_app_id: '285473059',
    qobuz_app_secret: '',
    qobuz_user_auth_token: '',
    output_format: 'lossless',
    max_retries: 3,
    library_mode: 'smart_dedup',
    prefer_explicit: true,
    folder_structure: 'standard',
    filename_format: 'default',
    spotify_sp_dc: '',
    tidal_enabled: false,
    tidal_auth_mode: 'session_json',
    tidal_session_json: '',
    tidal_access_token: '',
    tidal_refresh_token: '',
    tidal_session_id: '',
    tidal_token_type: 'Bearer',
    tidal_country_code: '',
  };
  let tidalValidationStatus: { ok: boolean; message: string; display_name?: string; country_code?: string } | null = null;
  let tidalValidationLoading = false;

  // ── TIDAL OAuth login state ─────────────────────────────────────────────────
  interface TidalOAuthState {
    phase: 'idle' | 'starting' | 'waiting_browser' | 'success' | 'error';
    url?: string;
    code?: string;
    message?: string;
    displayName?: string;
    countryCode?: string;
    sessionJson?: string;
  }
  let tidalOAuth: TidalOAuthState = { phase: 'idle' };

  interface BrowserLoginState {
    phase: 'idle' | 'starting' | 'waiting_for_user' | 'capturing' | 'success' | 'error';
    message?: string;
    detail?: string;
  }
  let appleLogin: BrowserLoginState = { phase: 'idle' };
  let amazonLogin: BrowserLoginState = { phase: 'idle' };

  interface FailedTrackPayload {
    title: string;
    artists: string[];
    album: string;
    playlist_name?: string;
    playlist_owner?: string;
    playlist_description?: string;
    playlist_position?: number;
    release_year?: number;
    release_date?: string;
    track_number?: number;
    disc_number?: number;
    total_tracks?: number;
    total_discs?: number;
    duration_ms?: number;
    isrc?: string;
    spotify_id?: string;
    album_id?: string;
    spotify_url?: string;
    amazon_asin?: string;
    upc?: string;
    iswc?: string;
    audio_traits?: string[];
    genres?: string[];
    album_artists?: string[];
    artwork_url?: string;
    playlist_artwork_url?: string;
    is_explicit?: boolean;
    lyrics?: string;
    synced_lyrics?: string;
  }

  let isLoading = true;
  let setupMode = false;
  let showHistory = false;
  let showSettings = false;
  let settingsScrollTarget: string | null = null; // id of settings section to scroll to on open
  let showDownloadedMusic = false;
  let slskdWebUIInfo: {url: string, username: string, password: string} | null = null;
  let historyItems: any[] = [];
  let inputUrl = '';
  let isDownloading = false;

  interface LibraryReleaseSummary {
    kind: string;
    relative_path: string;
    title: string;
    artist?: string;
    year?: string;
    track_count: number;
    artwork_url?: string;
  }

  interface LibraryReleaseTrack {
    title: string;
    artist?: string;
    album?: string;
    file_name: string;
    file_path: string;
    disc_number?: number;
    track_number?: number;
    duration_seconds?: number;
    codec?: string;
    audio_url: string;
  }

  interface LibraryReleaseDetail extends LibraryReleaseSummary {
    tracks: LibraryReleaseTrack[];
  }

  let downloadedLibrary: { albums: LibraryReleaseSummary[]; playlists: LibraryReleaseSummary[]; error?: string } = {
    albums: [],
    playlists: []
  };
  let downloadedLibraryLoading = false;
  let downloadedLibraryError = '';
  let downloadedSelectedRelease: LibraryReleaseDetail | null = null;
  let downloadedSelectedReleaseLoading = false;
  let downloadedSelectedPath = '';
  let downloadedView: 'albums' | 'playlists' = 'albums';
  let audioEl: HTMLAudioElement;
  let playerQueue: LibraryReleaseTrack[] = [];
  let playerTrackIndex = -1;
  let playerCurrentTime = 0;
  let playerDuration = 0;
  let playerSeeking = false;
  let playerVolume = 1;
  let playerError = '';
  let playerReleaseTitle = '';
  $: currentPlayerTrack = playerTrackIndex >= 0 ? playerQueue[playerTrackIndex] : null;

  // ── Source health check ─────────────────────────────────────────────────────
  interface EndpointStatus { url: string; alive: boolean; latency_ms: number; }
  interface SourceHealth { source: string; total: number; live: number; endpoints: EndpointStatus[]; }
  let healthCache: Record<string, SourceHealth> = {};
  let healthPopoverSource = '';
  let healthLoading = false;
  let showHealthPopover = false;

  const healthSources = [
    { key: 'hifi',   label: 'Tidal',   abbr: 'T', bg: '#1a1a2e', bgEnabled: 'rgba(29,185,222,0.14)',  border: '#1DB9DE', text: '#1DB9DE' },
    { key: 'apple',  label: 'Apple',   abbr: '',  bg: '#230a10', bgEnabled: 'rgba(252,60,68,0.14)',   border: '#fc3c44', text: '#fc3c44' },
    { key: 'amazon', label: 'Amazon',  abbr: 'a', bg: '#1a1200', bgEnabled: 'rgba(255,153,0,0.14)',   border: '#FF9900', text: '#FF9900' },
    { key: 'dab',    label: 'Qobuz',   abbr: 'Q', bg: '#0d0d1f', bgEnabled: 'rgba(123,94,167,0.18)',  border: '#7B5EA7', text: '#7B5EA7' },
  ];
  const formatOptions = [
    { value: 'auto',     name: 'Auto',     label: 'Best available — lossless preferred, MP3 fallback' },
    { value: 'lossless', name: 'Lossless', label: 'FLAC only — skip if unavailable' },
    { value: 'alac',     name: 'ALAC',     label: 'Apple Lossless .m4a — iPhone / Apple Music compatible' },
    { value: 'aac',      name: 'AAC',      label: '~320kbps AAC — uses JioSaavn directly' },
    { value: 'mp3',      name: 'MP3',      label: '~320kbps MP3 — uses JioSaavn / NetEase directly' },
  ];

  async function checkHealth(src: string) {
    healthPopoverSource = src;
    healthLoading = true;
    showHealthPopover = true;
    try {
      const raw = await CheckSourceHealth(src);
      healthCache[src] = JSON.parse(raw);
      healthCache = { ...healthCache };
    } catch (e) { console.error(e); }
    finally { healthLoading = false; }
  }

  // Map health chip key → settings section id and config enabled key
  const chipSettingsMap: Record<string, { sectionId: string; enableKey: string }> = {
    hifi:   { sectionId: 'settings-tidal',  enableKey: 'tidal_enabled'  },
    apple:  { sectionId: 'settings-apple',  enableKey: 'apple_enabled'  },
    amazon: { sectionId: 'settings-amazon', enableKey: 'amazon_enabled' },
    dab:    { sectionId: 'settings-qobuz',  enableKey: 'qobuz_enabled'  },
  };

  // Explicit reactive enabled states — Svelte tracks these directly.
  // Using $: ensures they update whenever config changes (e.g. after GetConfig() in onMount).
  $: chipEnabled = {
    hifi:   !!config.tidal_enabled,
    apple:  !!config.apple_enabled,
    amazon: !!config.amazon_enabled,
    dab:    !!config.qobuz_enabled,
  };

  function isChipEnabled(key: string): boolean {
    return !!chipEnabled[key];
  }

  async function openSettingsAt(sectionId: string) {
    settingsScrollTarget = sectionId;
    showSettings = true;
    try { const raw = await GetSlskdWebUIInfo(); const info = JSON.parse(raw); slskdWebUIInfo = (info && info.url) ? info : null; } catch { slskdWebUIInfo = null; }
    // Wait for the modal DOM to render, then scroll the target section into view
    setTimeout(() => {
      const el = document.getElementById(sectionId);
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      settingsScrollTarget = null;
    }, 80);
  }

  function handleChipClick(src: string) {
    // Always open settings at the adapter's section — whether enabled or not.
    // Disabled: enables the adapter, saves config, then opens settings.
    // Enabled: opens settings directly (so user can manage credentials).
    const m = chipSettingsMap[src];
    if (!m) return;
    if (!isChipEnabled(src)) {
      (config as any)[m.enableKey] = true;
      SaveConfig(config).catch(() => {});
    }
    openSettingsAt(m.sectionId);
  }

  // ── Tracklist scroll state ─────────────────────────────────────────────────
  let tracklistEl: HTMLDivElement;
  let tracklistAtBottom = true;
  let tracklistHasScrolled = false;

  function updateTracklistScroll() {
    if (!tracklistEl) return;
    const d = tracklistEl.scrollHeight - tracklistEl.scrollTop - tracklistEl.clientHeight;
    tracklistAtBottom = d <= 40;
    tracklistHasScrolled = true;
  }

  function scrollTracklistToBottom() {
    if (tracklistEl) { tracklistEl.scrollTop = tracklistEl.scrollHeight; tracklistAtBottom = true; }
  }

  // ── Multi-URL separators ────────────────────────────────────────────────────
  let separatorMeta: Record<string, { title: string; artwork: string }> = {};

  // ── Sponsor toast ────────────────────────────────────────────────────────────
  let showSponsorToast = false;
  let sponsorToastLeaving = false;
  let kofiTooltipVisible = false;
  let sponsorToastTimer: ReturnType<typeof setTimeout>;

  interface SupportStatus {
    enabled: boolean;
    title: string;
    message: string;
    current: number;
    goal: number;
    currency: string;
    link: string;
  }

  let supportStatus: SupportStatus = {
    enabled: true,
    title: 'Support Antra',
    message: 'Solo-maintained by one developer. Help fund bug fixes, updates, and endpoint costs.',
    current: 0,
    goal: 200,
    currency: 'USD',
    link: 'https://ko-fi.com/antraverse'
  };
  let supportStatusLoading = false;
  $: supportProgress = supportStatus.goal > 0 ? Math.min(100, Math.max(0, (supportStatus.current / supportStatus.goal) * 100)) : 0;

  function dismissSponsorToast() {
    sponsorToastLeaving = true;
    clearTimeout(sponsorToastTimer);
    setTimeout(() => { showSponsorToast = false; sponsorToastLeaving = false; }, 450);
  }

  function formatSupportAmount(value: number): string {
    const currency = supportStatus.currency || 'USD';
    if (currency === 'USD') return `$${Math.round(value)}`;
    return `${Math.round(value)} ${currency}`;
  }

  async function loadSupportStatus() {
    supportStatusLoading = true;
    try {
      const raw = await GetSupportStatus();
      const parsed = JSON.parse(raw || '{}');
      supportStatus = {
        enabled: parsed.enabled !== false,
        title: parsed.title || supportStatus.title,
        message: parsed.message || supportStatus.message,
        current: typeof parsed.current === 'number' ? parsed.current : supportStatus.current,
        goal: typeof parsed.goal === 'number' && parsed.goal > 0 ? parsed.goal : supportStatus.goal,
        currency: parsed.currency || supportStatus.currency,
        link: parsed.link || supportStatus.link
      };
    } catch (e) {
      console.error('Failed to load support status', e);
    } finally {
      supportStatusLoading = false;
    }
  }

  // Logs terminal
  let logs: {id: number, type: string, text: string, isRawHtml?: boolean}[] = [];
  let logId = 0;
  let terminalContainer: HTMLDivElement;
  let terminalEnd: HTMLElement;
  let shouldAutoScroll = true;
  let logAtBottom = true;
  let showLog = false;
  let trackOrder: string[] = [];
  let playlistTitle = '';
  let playlistArtwork = '';
  let playlistArtists = '';
  let playlistReleaseDate = '';
  let playlistContentType = '';
  let playlistQualityBadge = '';
  let playlistTotalDurationMs = 0;
  let playlistTotalTracks = 0;

  // Track progress mapping
  let activeTracks: Record<string, {
    progress?: number,
    text: string,
    error?: string,
    mode: 'status' | 'progress',
    status: 'resolving' | 'downloading' | 'done' | 'failed' | 'skipped',
    retrying?: boolean,
    trackData?: FailedTrackPayload,
  }> = {};

  function updateActiveTrack(trackName: string, patch: Partial<typeof activeTracks[string]>) {
    const existing = activeTracks[trackName] || { mode: 'status' as const, text: 'Resolving source...', status: 'resolving' as const };
    activeTracks[trackName] = { ...existing, ...patch };
    activeTracks = { ...activeTracks };
  }

  function clearTrackInterval(trackName: string) {
    const intervalId = (activeTracks[trackName] as any)?._intervalId;
    if (intervalId) {
      clearInterval(intervalId);
      delete (activeTracks[trackName] as any)._intervalId;
    }
  }

  onMount(async () => {
    try {
      config = await GetConfig();
      if (!config.first_run_complete) {
        setupMode = true;
      }
      if (!config.output_format) {
        config.output_format = 'lossless';
      }
      if (!config.max_retries || config.max_retries < 1) {
        config.max_retries = 3;
      }
      if (!config.sources_enabled) {
        config.sources_enabled = [];
      }
      // Retire the old source-group checkbox model in favor of a single
      // Soulseek toggle plus the separate TIDAL Premium section.
      if (config.sources_enabled.includes('soulseek')) {
        config.soulseek_enabled = true;
      }
      config.sources_enabled = [];
      if (typeof config.soulseek_seed_after_download !== 'boolean') {
        config.soulseek_seed_after_download = false;
      }
      if (!config.qobuz_app_id) {
        config.qobuz_app_id = '285473059';
      }
      if (!config.library_mode) {
        config.library_mode = 'smart_dedup';
      }
      if (config.prefer_explicit === undefined || config.prefer_explicit === null) {
        config.prefer_explicit = true;
      }
      if (!config.folder_structure) {
        config.folder_structure = 'standard';
      }
      if (!config.filename_format) {
        config.filename_format = 'default';
      }
      if (config.spotify_sp_dc === undefined || config.spotify_sp_dc === null) {
        config.spotify_sp_dc = '';
      }
      if (config.apple_storefront === undefined || config.apple_storefront === null || !config.apple_storefront) {
        config.apple_storefront = 'us';
      }
      if (config.amazon_wvd_path === undefined || config.amazon_wvd_path === null) {
        config.amazon_wvd_path = '';
      }

    } catch (e) {
      console.error('Failed to load config', e);
      setupMode = true;
    }

    await loadSupportStatus();
    isLoading = false;

    // Listen to backend events
    EventsOn("backend-event", handleEvent);

    // Listen to TIDAL OAuth events
    EventsOn("tidal-oauth-event", (payload: any) => {
      if (!payload || !payload.type) return;
      switch (payload.type) {
        case 'tidal_oauth_status':
          tidalOAuth = { ...tidalOAuth, phase: 'starting', message: payload.message };
          break;
        case 'tidal_oauth_url':
          tidalOAuth = {
            phase: 'waiting_browser',
            url: payload.url,
            code: payload.code || '',
            message: 'Open the link below in your browser and log in to TIDAL:',
          };
          break;
        case 'tidal_oauth_success':
          tidalOAuth = {
            phase: 'success',
            displayName: payload.display_name || '',
            countryCode: payload.country_code || '',
            sessionJson: payload.session_json || '',
            message: payload.message || 'Login successful!',
          };
          // Auto-populate config fields and re-load config to pick up saved values
          if (payload.session_json) {
            config.tidal_enabled = true;
            config.tidal_auth_mode = 'session_json';
            config.tidal_session_json = payload.session_json;
          }
          // Trigger validation automatically so user sees the green tick
          tidalValidationStatus = {
            ok: true,
            message: payload.message || 'TIDAL session is valid.',
            display_name: payload.display_name,
            country_code: payload.country_code,
          };
          break;
        case 'tidal_oauth_error':
          tidalOAuth = { phase: 'error', message: payload.message || 'OAuth login failed.' };
          break;
        case 'tidal_oauth_done':
          // Process ended — if still in starting/waiting state, mark as error
          if (tidalOAuth.phase === 'starting' || tidalOAuth.phase === 'waiting_browser') {
            tidalOAuth = { ...tidalOAuth, phase: 'error', message: tidalOAuth.message || 'OAuth process ended unexpectedly.' };
          }
          break;
      }
      tidalOAuth = { ...tidalOAuth }; // trigger reactivity
    });

    EventsOn("apple-login-event", (payload: any) => {
      if (!payload || !payload.type) return;
      switch (payload.type) {
        case 'apple_login_status':
          appleLogin = { phase: 'starting', message: payload.message || 'Opening Apple Music login...' };
          break;
        case 'apple_login_success':
          appleLogin = { phase: 'success', message: payload.message || 'Apple Music connected.' };
          config.apple_enabled = true;
          if (payload.authorization_token) config.apple_authorization_token = payload.authorization_token;
          if (payload.music_user_token) config.apple_music_user_token = payload.music_user_token;
          if (payload.storefront) config.apple_storefront = payload.storefront;
          break;
        case 'apple_login_error':
          appleLogin = { phase: 'error', message: payload.message || 'Apple Music login failed.' };
          break;
        case 'apple_login_done':
          if (appleLogin.phase === 'starting') {
            appleLogin = { phase: 'error', message: appleLogin.message || 'Apple Music login ended unexpectedly.' };
          }
          break;
      }
    });

    EventsOn("amazon-login-event", (payload: any) => {
      if (!payload || !payload.type) return;
      switch (payload.type) {
        case 'amazon_login_status':
          if (payload.phase === 'waiting_for_user') {
            amazonLogin = { phase: 'waiting_for_user', message: payload.message || 'Sign in to Amazon Music in your browser, then click \'I\'m Signed In\' below.' };
          } else if (payload.phase === 'capturing') {
            amazonLogin = { phase: 'capturing', message: payload.message || 'Reading your browser session…' };
          } else {
            amazonLogin = { phase: 'starting', message: payload.message || 'Opening Amazon Music login…' };
          }
          break;
        case 'amazon_login_success':
          amazonLogin = {
            phase: 'success',
            message: payload.message || 'Amazon Music connected.',
            detail: payload.has_wvd_path ? '' : 'A Widevine device path is still required for downloads.',
          };
          config.amazon_enabled = true;
          if (payload.direct_creds_json) config.amazon_direct_creds_json = payload.direct_creds_json;
          break;
        case 'amazon_login_error':
          amazonLogin = { phase: 'error', message: payload.message || 'Amazon Music login failed.' };
          break;
        case 'amazon_login_done':
          if (amazonLogin.phase === 'starting' || amazonLogin.phase === 'waiting_for_user' || amazonLogin.phase === 'capturing') {
            amazonLogin = { phase: 'error', message: amazonLogin.message || 'Amazon Music login ended unexpectedly.' };
          }
          break;
      }
    });

    // Show sponsor toast after UI settles (skip during first-run setup)
    if (!setupMode) {
      setTimeout(() => {
        showSponsorToast = true;
        sponsorToastTimer = setTimeout(() => dismissSponsorToast(), 9000);
      }, 1200);
    }
  });

  function updateAutoScrollState() {
    if (!terminalContainer) return;
    const distanceFromBottom =
      terminalContainer.scrollHeight - terminalContainer.scrollTop - terminalContainer.clientHeight;
    shouldAutoScroll = distanceFromBottom <= 80;
    logAtBottom = distanceFromBottom <= 40;
  }

  function scrollToBottom(force: boolean = false) {
    if (terminalContainer && terminalEnd && (force || shouldAutoScroll)) {
      setTimeout(() => {
        terminalContainer.scrollTo({
          top: terminalContainer.scrollHeight,
          behavior: force ? 'auto' : 'smooth'
        });
      }, 50);
    }
  }

  function addLog(type: string, text: string, isRawHtml: boolean = false) {
    logs = [...logs, { id: logId++, type, text, isRawHtml }];
    scrollToBottom();
  }

  function formatDuration(ms: number): string {
    const totalSec = Math.floor(ms / 1000);
    const hours = Math.floor(totalSec / 3600);
    const mins = Math.floor((totalSec % 3600) / 60);
    if (hours > 0) return `${hours} hr ${mins} min`;
    if (mins > 0) return `${mins} min`;
    return `${totalSec} sec`;
  }

  function formatAsciiRundown(summary: any): string {
    const downloaded = summary.downloaded || 0;
    const skipped = summary.skipped || 0;
    const failed = summary.failed || 0;
    const total = summary.total || 0;
    const totalMb: number | null = typeof summary.total_mb === 'number' ? summary.total_mb : null;
    const elapsed: number | null = typeof summary.elapsed_seconds === 'number' ? summary.elapsed_seconds : null;

    const sep = '═'.repeat(56);
    const pad = (label: string, value: string) => {
      const full = `  ${label}${value}`;
      return full;
    };

    const lines = [
      `<span style="color:var(--accent-color)">${sep}</span>`,
      pad('Tracks added      : ', `<span style="color:#4ade80">${downloaded} / ${total}</span>`),
      pad('Already in library: ', `<span style="color:#facc15">${skipped}</span>`),
      pad('Could not source  : ', `<span style="color:${failed > 0 ? 'var(--error-color)' : '#94a3b8'}">${failed}</span>`),
      ...(totalMb !== null ? [pad('Total size        : ', `<span style="color:#94a3b8">${totalMb} MB</span>`)] : []),
      ...(elapsed !== null ? [pad('Time taken        : ', `<span style="color:#94a3b8">${elapsed}s</span>`)] : []),
      `<span style="color:var(--accent-color)">${sep}</span>`,
    ];

    return `<div style="font-family:var(--font-mono);font-size:13px;line-height:1.7;margin-top:8px">${lines.join('<br>')}</div>`;
  }

  function handleEvent(payload: any) {
    if (payload.type === 'playlist_loaded') {
      playlistTitle = payload.title || '';
      playlistArtwork = payload.artwork_url || '';
      playlistArtists = payload.artists_string || '';
      playlistReleaseDate = payload.release_date || '';
      playlistContentType = payload.content_type || '';
      playlistQualityBadge = payload.quality_badge || '';
      const trkList: any[] = payload.tracks || [];
      playlistTotalTracks = trkList.length;
      playlistTotalDurationMs = trkList.reduce((sum: number, t: any) => sum + (t.duration_ms || 0), 0);
      // Insert a visual separator when a second+ URL's tracks arrive
      if (trackOrder.length > 0) {
        const sepKey = `__SEP__${Date.now()}`;
        separatorMeta[sepKey] = { title: payload.title || '', artwork: payload.artwork_url || '' };
        separatorMeta = { ...separatorMeta };
        trackOrder = [...trackOrder, sepKey];
      }

      // Pre-populate the full tracklist in waiting state before downloads begin
      const incoming: string[] = trkList.map((t: any) => `${t.artist} - ${t.title}`);
      for (const name of incoming) {
        if (!trackOrder.includes(name)) {
          trackOrder = [...trackOrder, name];
        }
        if (!activeTracks[name]) {
          activeTracks[name] = { mode: 'status', text: 'Waiting...', status: 'resolving' };
        }
      }
      activeTracks = { ...activeTracks };
      return;
    }

    if (payload.type === 'process_ended') {
      isDownloading = false;
      Object.keys(activeTracks).forEach(clearTrackInterval);
      if (payload.status === 'cancelled') {
        trackOrder = [];
        activeTracks = {};
        addLog('warning', '■ Library sync stopped');
      } else if (payload.status === 'failed') {
        addLog('error', '✖ Library sync stopped with errors');
      } else {
        addLog('success', '✔ Library updated successfully');
      }
      return;
    }

    if (payload.type === 'log') {
      if (typeof payload.message === 'string' && payload.message.includes('HTTP Error') && payload.message.includes('403')) {
        return; // hide spotify irrelevant 403 error
      }
      if (typeof payload.message === 'string' && payload.message.includes('0xc000013a')) {
        return; // standard cancel status on windows
      }
      addLog(payload.level, payload.message);
    } else if (payload.type === 'progress') {
      addLog('info', `[Bulk Progress] ${payload.message}`);
    } else if (payload.type === 'event') {
      const name = payload.name;
      const data = payload.payload;

      const trackName = data.track ? `${data.artist} - ${data.track}` : 'Unknown Track';

      if (name === 'track_started') {
        if (!trackOrder.includes(trackName)) {
          trackOrder = [...trackOrder, trackName];
        }
        updateActiveTrack(trackName, {
          mode: 'status',
          progress: undefined,
          text: 'Resolving best source...',
          status: 'resolving',
          retrying: false,
          trackData: data.track_data || activeTracks[trackName]?.trackData,
        });

      } else if (name === 'track_resolved') {
        clearTrackInterval(trackName);
        let displaySource = data.source || 'auto';
        if (displaySource === 'hifi') displaySource = 'Tidal';
        else if (displaySource === 'apple') displaySource = 'Apple';
        else if (displaySource === 'amazon') displaySource = 'Amazon';
        else if (displaySource === 'dab') displaySource = 'Qobuz';
        else displaySource = displaySource.charAt(0).toUpperCase() + displaySource.slice(1);

        updateActiveTrack(trackName, {
          mode: 'status',
          progress: undefined,
          text: `Accepted via ${displaySource}${data.quality_label ? ` • ${data.quality_label}` : ''}`,
          status: 'resolving',
          retrying: false,
          trackData: data.track_data || activeTracks[trackName]?.trackData,
        });

      } else if (name === 'track_download_attempt') {
        const source = String(data.source || 'auto');
        const attempt = data.attempt ?? 1;
        clearTrackInterval(trackName);

        if (source.startsWith('soulseek')) {
          updateActiveTrack(trackName, {
            mode: 'status',
            progress: undefined,
            text: 'Waiting for Soulseek transfer...',
            status: 'downloading',
            retrying: false,
            trackData: data.track_data || activeTracks[trackName]?.trackData,
          });
          return;
        }

        const attemptSuffix = attempt > 1 ? ` • Retry ${attempt}` : '';
        let displaySource = source;
        if (displaySource === 'hifi') displaySource = 'Tidal';
        else if (displaySource === 'apple') displaySource = 'Apple';
        else if (displaySource === 'amazon') displaySource = 'Amazon';
        else if (displaySource === 'dab') displaySource = 'Qobuz';
        else displaySource = displaySource.charAt(0).toUpperCase() + displaySource.slice(1);

        updateActiveTrack(trackName, {
          mode: 'progress',
          progress: 8,
          text: `Downloading from ${displaySource}${data.quality_label ? ` • ${data.quality_label}` : ''}${attemptSuffix}`,
          status: 'downloading',
          retrying: false,
          trackData: data.track_data || activeTracks[trackName]?.trackData,
        });

        const intervalId = setInterval(() => {
          if (activeTracks[trackName] && activeTracks[trackName].mode === 'progress' && (activeTracks[trackName].progress ?? 0) < 85) {
            updateActiveTrack(trackName, {
              progress: Math.min(85, (activeTracks[trackName].progress ?? 0) + Math.random() * 5)
            });
          } else {
            clearInterval(intervalId);
          }
        }, 800);

        (activeTracks[trackName] as any)._intervalId = intervalId;
        activeTracks = { ...activeTracks };

      } else if (name === 'track_completed') {
        addLog('success', `[✓] Added to library: ${trackName}`);
        clearTrackInterval(trackName);
        updateActiveTrack(trackName, {
          mode: 'progress',
          progress: 100,
          text: '✓ Added to library',
          error: undefined,
          status: 'done',
          retrying: false,
          trackData: data.track_data || activeTracks[trackName]?.trackData,
        });
      } else if (name === 'track_failed') {
        addLog('error', `[FAIL] ${trackName} - ${data.error}`);
        clearTrackInterval(trackName);
        updateActiveTrack(trackName, {
          mode: 'status',
          progress: undefined,
          text: 'Download failed',
          error: data.error || 'Failed',
          status: 'failed',
          retrying: false,
          trackData: data.track_data || activeTracks[trackName]?.trackData,
        });
      } else if (name === 'track_skipped') {
        addLog('warning', `[—] Already in library: ${trackName}`);
        updateActiveTrack(trackName, {
          mode: 'status',
          text: 'Already in library',
          status: 'skipped',
          retrying: false,
          trackData: data.track_data || activeTracks[trackName]?.trackData,
        });
      } else if (name === 'playlist_started') {
        addLog('info', `Creating playlist structure and syncing tracks: ${data.message}`);
      }
    } else if (payload.type === 'playlist_summary') {
      const htmlRundown = formatAsciiRundown(payload);
      addLog('terminal-rundown', htmlRundown, true);
      AddHistory(payload).catch(err => console.error("Failed to add history:", err));

    } else if (payload.type === 'done') {
      // JSON CLI said done
    }
  }

  // ── Audio Quality Analyzer ────────────────────────────────────────────────

  type TrackStatus = 'pending' | 'analyzing' | 'done' | 'error';
  type ViewMode = 'gallery' | 'single';

  interface TrackAnalysis {
    filePath: string;
    fileName: string;
    status: TrackStatus;
    probe?: any;
    spectrogram?: string;
    error?: string;
  }

  // Tab mode
  let activeTab: 'url' | 'artist' | 'discover' = 'url';
  let searchQuery = '';

  // Discovery variables
  let discoveryRegion = 'in';
  let discoveryGenre = '';
  let discoveryData: any = null;
  let discoveryLoading = false;
  let discoveryGenres: any[] = [];
  let discoveryGenresLoading = false;

  async function loadDiscoveryGenres() {
    discoveryGenresLoading = true;
    try {
      // @ts-ignore
      const raw = await window.go.main.App.GetDiscoveryGenres(discoveryRegion);
      const parsed = JSON.parse(raw);
      if (parsed.type === 'discovery_genres') {
        discoveryGenres = parsed.data || [];
      } else {
        addLog('error', `Failed to load genres: ${parsed.error || parsed.message}`);
      }
    } catch (e) {
      console.error(e);
    }
    discoveryGenresLoading = false;
  }

  async function loadDiscoveryData() {
    discoveryLoading = true;
    try {
      // @ts-ignore
      const raw = await window.go.main.App.GetDiscoveryData(discoveryRegion, discoveryGenre, discoveryGenres.find(g => g.id === discoveryGenre)?.name || '');
      const parsed = JSON.parse(raw);
      if (parsed.type === 'discovery') {
        discoveryData = parsed.data;
      } else {
        addLog('error', `Failed to load discovery: ${parsed.error || parsed.message}`);
      }
    } catch (e) {
      console.error(e);
    }
    discoveryLoading = false;
  }

  function handleDiscoveryClick(url: string) {
    activeTab = 'url';
    inputUrl = url;
    // Auto-focus text area or user can click download
  }
  let searchSource: 'spotify' | 'apple' = 'apple';
  let showArtistSearch = false;
  let artistSearchResults: any[] = [];
  let artistSearchLoading = false;
  let artistSearchReqId = 0;

  // Discography modal
  let showDiscography = false
  let discographyLoading = false
  let discographyArtist: any = null
  let discographySelected: Set<string> = new Set()
  let discographyReqId = 0  // incremented on each new request; stale responses are ignored

  function discographyReleaseKey(album: any) {
    const name = String(album?.name ?? '').toLowerCase().replace(/\s+/g, ' ').trim();
    const type = String(album?.type ?? 'album');
    const year = Number(album?.year ?? 0);
    const trackCount = Number(album?.track_count ?? 0);
    return `${type}::${year}::${trackCount}::${name}`;
  }

  function discographyReleaseScore(album: any) {
    const name = String(album?.name ?? '');
    const isCleanNamed = /\b(clean|edited|radio edit|censored)\b/i.test(name);
    const explicitScore =
      album?.is_explicit === true ? 2 :
      album?.is_explicit === false ? 0 :
      isCleanNamed ? 0 : 1;
    return [
      explicitScore,
      album?.artwork_url ? 1 : 0,
      Number(album?.track_count ?? 0),
      String(album?.id ?? ''),
    ];
  }

  function isBetterDiscographyRelease(candidate: any, current: any) {
    const candidateScore = discographyReleaseScore(candidate);
    const currentScore = discographyReleaseScore(current);
    for (let i = 0; i < candidateScore.length; i++) {
      if (candidateScore[i] === currentScore[i]) continue;
      return candidateScore[i] > currentScore[i];
    }
    return false;
  }

  function dedupeDiscographyAlbums(albums: any[]) {
    const grouped = new Map<string, any[]>();
    for (const album of albums || []) {
      const key = discographyReleaseKey(album);
      const group = grouped.get(key) ?? [];
      group.push(album);
      grouped.set(key, group);
    }

    const deduped: any[] = [];
    for (const group of grouped.values()) {
      let best = group[0];
      for (const candidate of group.slice(1)) {
        if (isBetterDiscographyRelease(candidate, best)) {
          best = candidate;
        }
      }
      deduped.push(best);
    }
    return deduped;
  }

  let showAnalyzer = false;
  let analyzerTracks: TrackAnalysis[] = [];
  let analyzerCurrentIndex = 0;
  let analyzerViewMode: ViewMode = 'gallery';
  let analyzerDragOver = false;
  let analyzerProcessing = false;
  let analyzerExportStatus = '';

  $: analyzerDoneCount = analyzerTracks.filter(t => t.status === 'done').length;
  $: analyzerShowSidebar = analyzerTracks.length > 1;
  $: analyzerShowExportAll = analyzerTracks.length >= 2;
  $: {
    if (analyzerTracks.length >= 3 && analyzerViewMode !== 'gallery') {
      // default to gallery for 3+ tracks — only auto-set once on first load
    }
  }

  function analyzerReset() {
    analyzerTracks = [];
    analyzerCurrentIndex = 0;
    analyzerViewMode = 'gallery';
    analyzerExportStatus = '';
  }

  function analyzerFileName(path: string): string {
    return path.replace(/\\/g, '/').split('/').pop() || path;
  }

  async function analyzerLoadFiles(paths: string[]) {
    const newTracks: TrackAnalysis[] = paths.map(p => ({
      filePath: p,
      fileName: analyzerFileName(p),
      status: 'pending' as TrackStatus,
    }));

    // Merge — skip already-loaded paths
    const existing = new Set(analyzerTracks.map(t => t.filePath));
    const toAdd = newTracks.filter(t => !existing.has(t.filePath));
    if (toAdd.length === 0) return;

    analyzerTracks = [...analyzerTracks, ...toAdd].sort((a, b) =>
      a.fileName.localeCompare(b.fileName)
    );

    if (analyzerTracks.length >= 3) analyzerViewMode = 'gallery';

    await analyzerProcessQueue();
  }

  async function analyzerProcessQueue() {
    if (analyzerProcessing) return;
    analyzerProcessing = true;

    for (let i = 0; i < analyzerTracks.length; i++) {
      if (analyzerTracks[i].status !== 'pending') continue;

      analyzerTracks[i] = { ...analyzerTracks[i], status: 'analyzing' };
      analyzerTracks = [...analyzerTracks];

      try {
        const result = await AnalyzeAudio(analyzerTracks[i].filePath);
        analyzerTracks[i] = {
          ...analyzerTracks[i],
          status: result.spectrogramError && result.probeError ? 'error' : 'done',
          probe: result.probe,
          spectrogram: result.spectrogram,
          error: result.spectrogramError || result.probeError || undefined,
        };
      } catch (e: any) {
        analyzerTracks[i] = { ...analyzerTracks[i], status: 'error', error: String(e) };
      }
      analyzerTracks = [...analyzerTracks];
    }

    analyzerProcessing = false;
  }

  async function analyzerOnDrop(e: DragEvent) {
    e.preventDefault();
    analyzerDragOver = false;
    if (!e.dataTransfer) return;

    const paths: string[] = [];

    // WebkitGetAsEntry gives us folder support in WebView2
    const items = Array.from(e.dataTransfer.items || []);
    for (const item of items) {
      const entry = (item as any).webkitGetAsEntry?.();
      if (entry?.isDirectory) {
        // Ask Go to enumerate audio files inside this folder
        const folderPath = (item.getAsFile() as any)?.path as string;
        if (folderPath) {
          const scanned: string[] = await ScanFolder(folderPath);
          paths.push(...scanned);
        }
      } else {
        const file = item.getAsFile();
        if (file) {
          const ext = file.name.split('.').pop()?.toLowerCase() || '';
          if (['flac','mp3','m4a','aac','alac','wav','aiff','aif','ogg'].includes(ext)) {
            paths.push((file as any).path as string);
          }
        }
      }
    }

    if (paths.length > 0) await analyzerLoadFiles(paths);
  }

  async function analyzerPickFiles() {
    const paths = await PickAnalyzerFiles();
    if (paths.length > 0) await analyzerLoadFiles(paths);
  }

  function analyzerFormatProbe(track: TrackAnalysis): { label: string; value: string }[] {
    if (!track.probe) return [];
    const fmt = track.probe.format || {};
    const streams = track.probe.streams || [];
    const stream = streams[0] || {};
    const rows: { label: string; value: string }[] = [];

    const codec = stream.codec_name?.toUpperCase() || '—';
    rows.push({ label: 'Codec', value: codec });

    const sr = stream.sample_rate ? `${(+stream.sample_rate / 1000).toFixed(1)} kHz` : '—';
    rows.push({ label: 'Sample Rate', value: sr });

    const bits = stream.bits_per_raw_sample || stream.bits_per_sample;
    rows.push({ label: 'Bit Depth', value: bits ? `${bits}-bit` : '—' });

    const channels = stream.channels;
    const layout = stream.channel_layout || (channels === 2 ? 'stereo' : channels === 1 ? 'mono' : '—');
    rows.push({ label: 'Channels', value: layout });

    const br = fmt.bit_rate ? `${Math.round(+fmt.bit_rate / 1000)} kbps` : '—';
    rows.push({ label: 'Bit Rate', value: br });

    const dur = fmt.duration ? `${(+fmt.duration / 60).toFixed(0)}:${String(Math.round(+fmt.duration % 60)).padStart(2,'0')}` : '—';
    rows.push({ label: 'Duration', value: dur });

    const size = fmt.size ? `${(+fmt.size / 1048576).toFixed(2)} MB` : '—';
    rows.push({ label: 'File Size', value: size });

    // Tags
    const tags = fmt.tags || stream.tags || {};
    if (tags.title) rows.push({ label: 'Title', value: tags.title });
    if (tags.artist || tags.ARTIST) rows.push({ label: 'Artist', value: tags.artist || tags.ARTIST });
    if (tags.album || tags.ALBUM) rows.push({ label: 'Album', value: tags.album || tags.ALBUM });

    return rows;
  }

  function analyzerQualityBadge(track: TrackAnalysis): { label: string; color: string } {
    if (!track.probe) return { label: '—', color: '#555' };
    const streams = track.probe.streams || [];
    const stream = streams[0] || {};
    const fmt = track.probe.format || {};
    const codec = (stream.codec_name || '').toLowerCase();
    const bits = +(stream.bits_per_raw_sample || stream.bits_per_sample || 0);
    const sr = +(stream.sample_rate || 0);
    const br = +(fmt.bit_rate || 0);

    if (codec === 'flac' || codec === 'alac') {
      if (bits >= 24 && sr >= 88200) {
        // Extra sanity: hi-res lossless should be at least 800kbps
        if (br > 0 && br < 400000) return { label: 'Suspect (Low Bitrate)', color: '#f87171' };
        return { label: 'Hi-Res Lossless', color: '#a78bfa' };
      }
      // Standard lossless (16-bit CD quality) should be at least ~400kbps;
      // below ~250kbps is almost always a lossy-to-lossless transcode (fake FLAC).
      if (br > 0 && br < 250000) return { label: 'Fake Lossless', color: '#f87171' };
      if (br > 0 && br < 400000) return { label: 'Lossless (Low BR)', color: '#facc15' };
      return { label: 'Lossless', color: '#00ffcc' };
    }
    if (codec === 'mp3' || codec === 'aac' || codec === 'vorbis' || codec === 'opus') {
      if (br >= 256000) return { label: 'High Quality', color: '#4ade80' };
      if (br >= 192000) return { label: 'Standard', color: '#facc15' };
      return { label: 'Low Quality', color: '#f87171' };
    }
    if (codec === 'pcm_s16le' || codec === 'pcm_s24le' || codec === 'pcm_f32le') {
      return { label: 'Lossless (PCM)', color: '#00ffcc' };
    }
    return { label: codec.toUpperCase() || 'Unknown', color: '#94a3b8' };
  }

  async function analyzerExportAll() {
    const dir = await PickDirectory();
    if (!dir) return;

    const done = analyzerTracks.filter(t => t.status === 'done' && t.spectrogram);
    if (done.length === 0) return;

    for (let i = 0; i < done.length; i++) {
      const track = done[i];
      analyzerExportStatus = `Exporting ${i + 1}/${done.length}...`;
      await analyzerExportSingleTo(track, dir, i + 1);
    }
    analyzerExportStatus = `Exported ${done.length} PNG${done.length !== 1 ? 's' : ''} to ${dir}`;
    setTimeout(() => { analyzerExportStatus = ''; }, 5000);
  }

  async function analyzerExportSingleTo(track: TrackAnalysis, dir: string, index: number) {
    if (!track.spectrogram) return;
    const tags = track.probe?.format?.tags || track.probe?.streams?.[0]?.tags || {};
    const artist = tags.artist || tags.ARTIST || '';
    const title = tags.title || tags.TITLE || '';
    const padded = String(index).padStart(2, '0');
    const safeName = (artist && title)
      ? `${padded} - ${artist} - ${title}`.replace(/[\\/:*?"<>|]/g, '_')
      : track.fileName.replace(/\.[^.]+$/, '');

    // Draw to off-screen canvas and export
    const img = new Image();
    await new Promise<void>(res => { img.onload = () => res(); img.src = track.spectrogram!; });

    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d')!;
    ctx.drawImage(img, 0, 0);

    const blob: Blob = await new Promise(res => canvas.toBlob(b => res(b!), 'image/png'));
    const arrayBuffer = await blob.arrayBuffer();
    const b64 = btoa(String.fromCharCode(...new Uint8Array(arrayBuffer)));
    const outPath = `${dir}/${safeName}.png`.replace(/\\/g, '/');
    await WriteFile(outPath, b64);
  }

  function analyzerExportCurrent() {
    const track = analyzerTracks[analyzerCurrentIndex];
    if (!track?.spectrogram) return;
    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement('canvas');
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      canvas.getContext('2d')!.drawImage(img, 0, 0);
      const a = document.createElement('a');
      a.href = canvas.toDataURL('image/png');
      a.download = track.fileName.replace(/\.[^.]+$/, '') + '.png';
      a.click();
    };
    img.src = track.spectrogram;
  }

  function analyzerHandleKey(e: KeyboardEvent) {
    if (!showAnalyzer) return;
    if (e.key === 'Escape') { analyzerReset(); showAnalyzer = false; }
    if (e.key === 'ArrowLeft' && analyzerViewMode === 'single')
      analyzerCurrentIndex = Math.max(0, analyzerCurrentIndex - 1);
    if (e.key === 'ArrowRight' && analyzerViewMode === 'single')
      analyzerCurrentIndex = Math.min(analyzerTracks.length - 1, analyzerCurrentIndex + 1);
    if ((e.ctrlKey || e.metaKey) && e.key === 'e') { e.preventDefault(); analyzerExportAll(); }
  }

  function formatPlaybackTime(seconds: number): string {
    if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
    const whole = Math.floor(seconds);
    const mins = Math.floor(whole / 60);
    const secs = whole % 60;
    return `${mins}:${String(secs).padStart(2, '0')}`;
  }

  function releaseMetaLine(release: LibraryReleaseSummary | LibraryReleaseDetail): string {
    const parts = [];
    if (release.artist && release.artist !== 'Playlist') parts.push(release.artist);
    if (release.year) parts.push(release.year);
    parts.push(`${release.track_count} track${release.track_count === 1 ? '' : 's'}`);
    return parts.join(' · ');
  }

  async function openDownloadedMusic() {
    showDownloadedMusic = true;
    await refreshDownloadedMusicLibrary();
  }

  async function refreshDownloadedMusicLibrary() {
    downloadedLibraryLoading = true;
    downloadedLibraryError = '';
    try {
      const raw = await GetDownloadedMusicLibrary();
      const parsed = JSON.parse(raw || '{}');
      downloadedLibrary = {
        albums: Array.isArray(parsed.albums) ? parsed.albums : [],
        playlists: Array.isArray(parsed.playlists) ? parsed.playlists : [],
        error: parsed.error
      };
      downloadedLibraryError = parsed.error || '';

      const selectedStillExists = [...downloadedLibrary.albums, ...downloadedLibrary.playlists]
        .some((item: LibraryReleaseSummary) => item.relative_path === downloadedSelectedPath);
      if (!selectedStillExists) {
        downloadedSelectedRelease = null;
        downloadedSelectedPath = '';
      }

      if (!downloadedSelectedPath) {
        const first = downloadedView === 'playlists'
          ? (downloadedLibrary.playlists[0] || downloadedLibrary.albums[0])
          : (downloadedLibrary.albums[0] || downloadedLibrary.playlists[0]);
        if (first) {
          downloadedView = first.kind === 'playlist' ? 'playlists' : 'albums';
          await openDownloadedRelease(first);
        }
      }
    } catch (e: any) {
      downloadedLibraryError = String(e);
      downloadedLibrary = { albums: [], playlists: [] };
    } finally {
      downloadedLibraryLoading = false;
    }
  }

  async function openDownloadedRelease(release: LibraryReleaseSummary) {
    downloadedSelectedPath = release.relative_path;
    downloadedSelectedReleaseLoading = true;
    try {
      const raw = await GetDownloadedRelease(release.relative_path);
      const parsed = JSON.parse(raw || '{}');
      if (parsed?.error) {
        downloadedLibraryError = parsed.error;
        return;
      }
      downloadedSelectedRelease = parsed;
      downloadedView = release.kind === 'playlist' ? 'playlists' : 'albums';
    } catch (e: any) {
      downloadedLibraryError = String(e);
    } finally {
      downloadedSelectedReleaseLoading = false;
    }
  }

  async function playDownloadedTrack(index: number) {
    if (!downloadedSelectedRelease?.tracks?.length || !audioEl) return;
    playerQueue = downloadedSelectedRelease.tracks;
    playerTrackIndex = index;
    playerReleaseTitle = downloadedSelectedRelease.title;
    playerError = '';
    playerCurrentTime = 0;
    playerDuration = downloadedSelectedRelease.tracks[index]?.duration_seconds || 0;
    audioEl.src = downloadedSelectedRelease.tracks[index].audio_url;
    audioEl.load();
    try {
      await audioEl.play();
    } catch (e: any) {
      playerError = String(e);
    }
  }

  async function togglePlayback() {
    if (!audioEl) return;
    if (!currentPlayerTrack && downloadedSelectedRelease?.tracks?.length) {
      await playDownloadedTrack(0);
      return;
    }
    if (audioEl.paused) {
      try {
        await audioEl.play();
        playerError = '';
      } catch (e: any) {
        playerError = String(e);
      }
    } else {
      audioEl.pause();
    }
  }

  async function playNextTrack() {
    if (playerTrackIndex < 0 || playerTrackIndex >= playerQueue.length - 1) return;
    await playQueuedTrack(playerTrackIndex + 1);
  }

  async function playPreviousTrack() {
    if (playerTrackIndex <= 0) {
      if (audioEl) audioEl.currentTime = 0;
      return;
    }
    await playQueuedTrack(playerTrackIndex - 1);
  }

  async function playQueuedTrack(index: number) {
    if (!playerQueue.length || index < 0 || index >= playerQueue.length || !audioEl) return;
    playerTrackIndex = index;
    playerError = '';
    playerCurrentTime = 0;
    playerDuration = playerQueue[index]?.duration_seconds || 0;
    audioEl.src = playerQueue[index].audio_url;
    audioEl.load();
    try {
      await audioEl.play();
    } catch (e: any) {
      playerError = String(e);
    }
  }

  function handleAudioTimeUpdate() {
    if (!audioEl || playerSeeking) return;
    playerCurrentTime = audioEl.currentTime || 0;
  }

  function handleAudioLoadedMetadata() {
    if (!audioEl) return;
    playerDuration = audioEl.duration || playerDuration;
  }

  async function handleAudioEnded() {
    if (playerTrackIndex >= 0 && playerTrackIndex < playerQueue.length - 1) {
      await playQueuedTrack(playerTrackIndex + 1);
    }
  }

  function handleSeekInput(event: Event) {
    const target = event.currentTarget as HTMLInputElement;
    playerCurrentTime = Number(target.value);
  }

  function handleSeekCommit(event: Event) {
    const target = event.currentTarget as HTMLInputElement;
    const nextTime = Number(target.value);
    playerSeeking = false;
    playerCurrentTime = nextTime;
    if (audioEl) audioEl.currentTime = nextTime;
  }

  async function pickDir() {
    const dir = await PickDirectory();
    if (dir) config.download_path = dir;
  }

  async function saveSetup() {
    if (!config.download_path) {
      alert("Please select your Music Library folder.");
      return;
    }
    if (config.soulseek_enabled && (!config.soulseek_username || !config.soulseek_password)) {
      alert("Please enter Soulseek credentials to enable P2P lossless sourcing. You can register a free account at slsknet.org.");
      return;
    }
    await SaveConfig(config);
    setupMode = false;
  }

  async function startDownload() {
    if (!inputUrl) return;

    // Accept one URL per line, or comma-separated, or both
    let urls = inputUrl
      .split(/[\n,]+/)
      .map(s => s.trim())
      .filter(s => s.startsWith('http'));
    if (urls.length === 0) return;

    const isArtistUrl = (u: string) => {
      const norm = u.replace(/spotify\.com\/intl-[a-z]+\//, 'spotify.com/');
      return norm.includes('spotify.com/artist/') ||
        (u.includes('music.apple.com') && u.includes('/artist/')) ||
        (u.includes('music.amazon.com') && u.includes('/artists/'));
    };
    const artistUrls = urls.filter(isArtistUrl);
    const otherUrls = urls.filter(u => !isArtistUrl(u));

    if (otherUrls.length > 0) {
      isDownloading = true;
      logs = [];
      trackOrder = [];
      playlistTitle = '';
      playlistArtwork = '';
      playlistArtists = '';
      playlistReleaseDate = '';
      playlistContentType = '';
      playlistQualityBadge = '';
      playlistTotalDurationMs = 0;
      playlistTotalTracks = 0;
      Object.keys(activeTracks).forEach(clearTrackInterval);
      activeTracks = {};
      separatorMeta = {};
      shouldAutoScroll = true;
      addLog('info', `━━━ Building your music library ━━━`);
      addLog('info', `Searching best available source (lossless prioritized)...`);
      inputUrl = '';
      try {
        await StartDownload(otherUrls);
      } catch (err) {
        addLog('error', `Library engine error: ${err}`);
        isDownloading = false;
      }
    }

    for (const artistUrl of artistUrls) {
      const reqId = ++discographyReqId;
      discographyLoading = true;
      showDiscography = true;
      discographyArtist = null;
      discographySelected = new Set();
      inputUrl = '';
      try {
        const raw = await GetArtistDiscography(artistUrl);
        // If user closed the modal while loading, reqId won't match — ignore result
        if (reqId !== discographyReqId) break;
        const parsed = JSON.parse(raw);
        if (parsed?.error) {
          addLog('error', `Discography error: ${parsed.error}`);
          showDiscography = false;
        } else {
          discographyArtist = {
            ...parsed,
            albums: dedupeDiscographyAlbums(Array.isArray(parsed?.albums) ? parsed.albums : []),
          };
          if (discographyArtist?.albums) {
            discographySelected = new Set(discographyArtist.albums.map(a => a.url));
          }
        }
      } catch (e) {
        if (reqId === discographyReqId) {
          addLog('error', `Failed to fetch discography: ${e}`);
          showDiscography = false;
        }
      } finally {
        if (reqId === discographyReqId) discographyLoading = false;
      }
    }
  }

  async function startArtistSearch() {
    if (!searchQuery.trim()) return;
    const reqId = ++artistSearchReqId;
    artistSearchLoading = true;
    showArtistSearch = true;
    artistSearchResults = [];
    try {
      const raw = await SearchArtists(searchQuery.trim(), searchSource);
      if (reqId !== artistSearchReqId) return;
      const parsed = JSON.parse(raw);
      if (parsed?.error) {
        addLog('error', `Artist search error: ${parsed.error}`);
        showArtistSearch = false;
      } else {
        artistSearchResults = Array.isArray(parsed) ? parsed : [];
      }
    } catch (e) {
      if (reqId === artistSearchReqId) {
        addLog('error', `Artist search failed: ${e}`);
        showArtistSearch = false;
      }
    } finally {
      if (reqId === artistSearchReqId) artistSearchLoading = false;
    }
  }

  async function openArtistFromSearch(artist: any) {
    showArtistSearch = false;
    activeTab = 'url';
    const reqId = ++discographyReqId;
    discographyLoading = true;
    showDiscography = true;
    discographyArtist = null;
    discographySelected = new Set();
    try {
      const raw = await GetArtistDiscography(artist.profile_url);
      if (reqId !== discographyReqId) return;
      const parsed = JSON.parse(raw);
      if (parsed?.error) {
        addLog('error', `Discography error: ${parsed.error}`);
        showDiscography = false;
      } else {
        discographyArtist = {
          ...parsed,
          albums: dedupeDiscographyAlbums(Array.isArray(parsed?.albums) ? parsed.albums : []),
        };
        if (discographyArtist?.albums) {
          discographySelected = new Set(discographyArtist.albums.map((a: any) => a.url));
        }
      }
    } catch (e) {
      if (reqId === discographyReqId) {
        addLog('error', `Failed to fetch discography: ${e}`);
        showDiscography = false;
      }
    } finally {
      if (reqId === discographyReqId) discographyLoading = false;
    }
  }

  async function cancelDownload() {
    if (!isDownloading) return;
    try {
      await CancelDownload();
      addLog('warning', 'Stopping library engine...');
    } catch (err) {
      console.error(err);
    }
  }

  async function openHistory() {
    try {
      historyItems = await GetHistory() || [];
    } catch (e) {
      console.error(e);
      historyItems = [];
    }
    showHistory = true;
  }

  async function clearHistory() {
    if(confirm("Are you sure you want to clear your library build history?")) {
      await ClearHistory();
      historyItems = [];
    }
  }

  async function saveSettings() {
    if (!config.max_retries || config.max_retries < 1) {
      config.max_retries = 3;
    }
    await SaveConfig(config);
    showSettings = false;
  }

  async function validateTidalSettings() {
    tidalValidationLoading = true;
    tidalValidationStatus = null;
    try {
      // Sanitize tidal_session_json: strip all control characters, normalize whitespace,
      // re-serialize to ensure clean JSON before saving to disk.
      if (config.tidal_session_json && config.tidal_session_json.trim()) {
        try {
          const cleaned = config.tidal_session_json
            .replace(/[\r\n\t]/g, ' ')          // replace CR/LF/tabs with space
            .replace(/[\u0000-\u001F\u007F]/g, '') // strip remaining control chars
            .replace(/\s+/g, ' ')               // collapse runs of spaces
            .trim();
          const parsed = JSON.parse(cleaned);
          config.tidal_session_json = JSON.stringify(parsed); // re-serialize to compact, clean JSON
        } catch (parseErr) {
          tidalValidationStatus = { ok: false, message: `Invalid session JSON: ${String(parseErr)}` };
          tidalValidationLoading = false;
          return;
        }
      }
      await SaveConfig(config);
      const raw = await ValidateTidalAuth();
      tidalValidationStatus = JSON.parse(raw);
    } catch (e) {
      tidalValidationStatus = { ok: false, message: String(e) };
    } finally {
      tidalValidationLoading = false;
    }
  }

  async function startTidalOAuth() {
    tidalOAuth = { phase: 'starting', message: 'Connecting to TIDAL...' };
    tidalValidationStatus = null;
    try {
      await StartTidalOAuthLogin();
    } catch (e) {
      tidalOAuth = { phase: 'error', message: `Failed to start OAuth: ${e}` };
    }
  }

  async function startAppleLogin() {
    appleLogin = { phase: 'starting', message: 'Opening Apple Music login...' };
    try {
      await SaveConfig(config);
      await StartAppleBrowserLogin();
    } catch (e) {
      appleLogin = { phase: 'error', message: `Failed to start Apple Music login: ${e}` };
    }
  }

  // Parse the Amazon session capture time from the credentials JSON.
  // csrf_ts is a Unix timestamp (seconds) of when the session was captured.
  // Amazon Atna tokens typically expire within ~24 hours of capture.
  function amazonSessionInfo(): { capturedAt: string; expiresNote: string } | null {
    if (!config.amazon_direct_creds_json) return null;
    try {
      const creds = JSON.parse(config.amazon_direct_creds_json);
      if (!creds.authorization || !creds.csrf_token) return null;
      const csrfTs = parseInt(creds.csrf_ts || '0', 10);
      if (!csrfTs) return null;
      const capturedDate = new Date(csrfTs * 1000);
      const expiresDate = new Date((csrfTs + 86400) * 1000); // +24h estimate
      const now = Date.now();
      const msLeft = expiresDate.getTime() - now;
      const capturedAt = capturedDate.toLocaleString();
      let expiresNote: string;
      if (msLeft <= 0) {
        expiresNote = 'Session likely expired — re-login recommended.';
      } else {
        const hLeft = Math.floor(msLeft / 3600000);
        const mLeft = Math.floor((msLeft % 3600000) / 60000);
        expiresNote = hLeft > 0
          ? `Expires in ~${hLeft}h ${mLeft}m (est.)`
          : `Expires in ~${mLeft}m (est.)`;
      }
      return { capturedAt, expiresNote };
    } catch {
      return null;
    }
  }

  async function startAmazonLogin() {
    amazonLogin = { phase: 'starting', message: 'Opening Amazon Music login...' };
    try {
      await SaveConfig(config);
      await StartAmazonBrowserLogin();
    } catch (e) {
      amazonLogin = { phase: 'error', message: `Failed to start Amazon Music login: ${e}` };
    }
  }

  async function retryFailedTrack(trackName: string) {
    const state = activeTracks[trackName];
    if (!state?.trackData || state.retrying || isDownloading) return;

    clearTrackInterval(trackName);
    isDownloading = true;
    addLog('info', `[↻] Retrying failed track: ${trackName}`);
    updateActiveTrack(trackName, {
      mode: 'status',
      progress: undefined,
      text: 'Retrying failed track...',
      error: undefined,
      status: 'resolving',
      retrying: true,
    });

    try {
      await RetryTrackDownload(JSON.stringify(state.trackData));
    } catch (err) {
      addLog('error', `Retry failed to start for ${trackName}: ${err}`);
      isDownloading = false;
      updateActiveTrack(trackName, {
        mode: 'status',
        text: state.text || 'Retry failed',
        error: state.error || 'Retry failed',
        status: 'failed',
        retrying: false,
      });
    }
  }

</script>

{#if isLoading}
  <main class="loading">
     <h2>Initializing library engine...</h2>
     <p style="color: #94a3b8; font-size: 13px; margin-top: 8px; opacity: 0.7;">Optimized for Navidrome & Jellyfin</p>
  </main>
{:else if setupMode}
  <main class="setup">
    <div class="logo">
      <pre>
    ___    _   __ ______  ____     ___
   /   |  / | / //_  __/ / __ \  /   |
  / /| | /  |/ /  / /   / /_/ / / /| |
 / ___ |/ /|  /  / /   / _, _/ / ___ |
/_/  |_/_/ |_/  /_/   /_/ |_/ /_/  |_|
      </pre>
      <p>Your music. Offline. Lossless.</p>
      <p style="font-size: 12px; opacity: 0.5; margin-top: -8px;">Paste a Spotify, Apple Music, or Amazon Music playlist and Antra builds your music library automatically.</p>
      <p style="font-size: 11px; opacity: 0.35; margin-top: -4px; letter-spacing: 0.04em;">OPTIMIZED FOR NAVIDROME &amp; JELLYFIN</p>
    </div>

    <div class="setup-box">
      <h3>Setup Your Music Library</h3>
      <div class="field">
        <label for="outDir">Music Library Folder</label>
        <p style="font-size: 11px; color: #555; margin: 0 0 8px;">Navidrome / Jellyfin compatible — point this at your media server's music directory.</p>
        <div style="display: flex; gap: 8px;">
          <input id="outDir" readonly type="text" value={config.download_path} placeholder="Select your music library location..." />
          <button on:click={pickDir}>Browse</button>
        </div>
      </div>
      <div class="field" style="margin-top: 20px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 8px;">Folder Structure</p>
        <div style="display: flex; flex-direction: column; gap: 10px;">
          <label style="display: flex; align-items: flex-start; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="standard" bind:group={config.folder_structure} style="margin-top: 2px;" />
            <div>
              Standard <span style="font-size: 11px; opacity: 0.6; margin-left: 4px;">Artist / Album / files</span>
              <p style="font-size: 11px; color: #555; margin: 3px 0 0;">Recommended for Navidrome, Jellyfin, and Plex.</p>
            </div>
          </label>
          <label style="display: flex; align-items: flex-start; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="flat" bind:group={config.folder_structure} style="margin-top: 2px;" />
            <div>
              Flat <span style="font-size: 11px; opacity: 0.6; margin-left: 4px;">Album / files</span>
              <p style="font-size: 11px; color: #555; margin: 3px 0 0;">No artist folder. Good for manual organisation.</p>
            </div>
          </label>
        </div>
      </div>

      <div class="field" style="margin-top: 20px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 8px;">Filename Format</p>
        <div style="display: flex; flex-direction: column; gap: 8px;">
          <label style="display: flex; align-items: center; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="default" bind:group={config.filename_format} />
            <span>Default <span style="font-size: 11px; opacity: 0.6;">01 - Title.flac</span></span>
          </label>
          <label style="display: flex; align-items: center; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="title_only" bind:group={config.filename_format} />
            <span>Title only <span style="font-size: 11px; opacity: 0.6;">Title.flac</span></span>
          </label>
          <label style="display: flex; align-items: center; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="artist_title" bind:group={config.filename_format} />
            <span>Artist – Title <span style="font-size: 11px; opacity: 0.6;">Artist - Title.flac</span></span>
          </label>
          <label style="display: flex; align-items: center; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="title_artist" bind:group={config.filename_format} />
            <span>Title – Artist <span style="font-size: 11px; opacity: 0.6;">Title - Artist.flac</span></span>
          </label>
        </div>
      </div>

      <div class="field" style="margin-top: 20px;">
        <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
          <input type="checkbox" bind:checked={config.soulseek_enabled} style="margin-top: 2px;" />
          <div>
            <span style="font-weight: 500;">Enable Soulseek (P2P) — optional</span>
            <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Find rare or hi-res versions not on streaming services. Requires a free account.</p>
          </div>
        </label>
      </div>

      {#if config.soulseek_enabled}
        <div class="field" style="margin-top: 16px; padding: 14px; background: rgba(0,255,204,0.03); border: 1px solid rgba(0,255,204,0.1); border-radius: 6px;">
          <label for="slskUsername" style="font-size: 13px;">Soulseek Username</label>
          <input id="slskUsername" type="text" bind:value={config.soulseek_username} placeholder="Your Soulseek username" style="width: 100%; box-sizing: border-box; margin-top: 6px;" />
          <label for="slskPassword" style="font-size: 13px; margin-top: 12px; display: block;">Soulseek Password</label>
          <input id="slskPassword" type="password" bind:value={config.soulseek_password} placeholder="Your Soulseek password" style="width: 100%; box-sizing: border-box; margin-top: 6px;" />
          <p style="font-size: 11px; color: #555; margin: 8px 0 0;">No account? Just pick a username &amp; password — <span style="color: var(--accent-color);">it's created automatically on first connect</span></p>
        </div>
      {/if}

      <button style="margin-top: 24px; width: 100%;" on:click={saveSetup}>Build My Library →</button>
    </div>
  </main>
{:else}
  <main class="app">
    <div class="header">
      <div style="display: flex; justify-content: space-between; align-items: center;">
        <div style="display: flex; align-items: center; gap: 10px;">
          <div>
            <h3 style="margin:0; color:var(--accent-color);">Antra</h3>
            <p style="margin:0; font-size: 11px; color: #555; letter-spacing: 0.05em;">MUSIC LIBRARY ENGINE</p>
          </div>
        </div>
        <div style="display: flex; gap: 8px; align-items: center;">
          <button on:click={openDownloadedMusic} title="Downloaded Music" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">🎵</button>
          <button on:click={openHistory} title="Library History" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">🕒</button>
          <button on:click={() => { showAnalyzer = true; }} title="Audio Analyzer" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">🔬</button>
          <button on:click={async () => { showSettings = true; try { const raw = await GetSlskdWebUIInfo(); const info = JSON.parse(raw); slskdWebUIInfo = (info && info.url) ? info : null; } catch { slskdWebUIInfo = null; } }} title="Settings" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">⚙️</button>
          <div style="width: 1px; height: 20px; background: rgba(255,255,255,0.1); margin: 0 2px;"></div>
          <div class="kofi-wrap">
            <button on:click={() => kofiTooltipVisible = !kofiTooltipVisible} style="background: transparent; border: none; padding: 4px 6px; cursor: pointer; display: flex; align-items: center; opacity: 0.7; transition: opacity 0.15s;" on:mouseenter={(e) => e.currentTarget.style.opacity='1'} on:mouseleave={(e) => e.currentTarget.style.opacity='0.7'}>
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M23.881 8.948c-.773-4.085-4.859-4.593-4.859-4.593H.723c-.604 0-.679.798-.679.798s-.082 7.324-.022 11.822c.164 2.424 2.586 2.672 2.586 2.672s8.267-.023 11.966-.049c2.438-.426 2.683-2.566 2.658-3.734 4.352.24 7.422-2.831 6.649-6.916zm-11.062 3.511c-1.246 1.453-4.011 3.976-4.011 3.976s-.121.119-.31.023c-.076-.057-.108-.09-.108-.09-.443-.441-3.368-3.049-4.034-3.954-.709-.965-1.041-2.7-.091-3.71.951-1.01 3.005-1.086 4.363.407 0 0 1.565-1.782 3.468-.963 1.904.82 1.832 3.011.723 4.311zm6.173.478c-.928.116-1.682.028-1.682.028V7.284h1.77s1.971.551 1.971 2.638c0 1.913-.985 2.910-2.059 3.015z" fill="#FF5E5B"/>
              </svg>
            </button>
            {#if kofiTooltipVisible && supportStatus.enabled}
              <div class="kofi-tooltip">
                <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px;">
                  <p class="kofi-tooltip-title">{supportStatus.title}</p>
                  <button class="support-close-btn" on:click={() => kofiTooltipVisible = false} title="Close">×</button>
                </div>
                <p class="kofi-tooltip-body">{supportStatus.message}</p>
                <div class="support-progress-wrap">
                  <div class="support-progress-head">
                    <span>{formatSupportAmount(supportStatus.current)} raised</span>
                    <span>Goal {formatSupportAmount(supportStatus.goal)}</span>
                  </div>
                  <div class="support-progress-bar">
                    <div class="support-progress-fill" style={`width:${supportProgress}%`}></div>
                  </div>
                  <div class="support-progress-foot">
                    <span>{Math.round(supportProgress)}%</span>
                    {#if supportStatusLoading}
                      <span>Refreshing…</span>
                    {/if}
                  </div>
                </div>
                <button class="kofi-tooltip-btn" on:click={() => BrowserOpenURL(supportStatus.link)}>
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><path d="M23.881 8.948c-.773-4.085-4.859-4.593-4.859-4.593H.723c-.604 0-.679.798-.679.798s-.082 7.324-.022 11.822c.164 2.424 2.586 2.672 2.586 2.672s8.267-.023 11.966-.049c2.438-.426 2.683-2.566 2.658-3.734 4.352.24 7.422-2.831 6.649-6.916zm-11.062 3.511c-1.246 1.453-4.011 3.976-4.011 3.976s-.121.119-.31.023c-.076-.057-.108-.09-.108-.09-.443-.441-3.368-3.049-4.034-3.954-.709-.965-1.041-2.7-.091-3.71.951-1.01 3.005-1.086 4.363.407 0 0 1.565-1.782 3.468-.963 1.904.82 1.832 3.011.723 4.311zm6.173.478c-.928.116-1.682.028-1.682.028V7.284h1.77s1.971.551 1.971 2.638c0 1.913-.985 2.910-2.059 3.015z" fill="#FF5E5B"/></svg>
                  Support on Ko-fi
                </button>
              </div>
            {/if}
          </div>
          <button title="Join our Telegram community" on:click={() => BrowserOpenURL('https://t.me/antraaverse')} style="background: transparent; border: none; padding: 4px 6px; cursor: pointer; display: flex; align-items: center; opacity: 0.7; transition: opacity 0.15s;" on:mouseenter={(e) => e.currentTarget.style.opacity='1'} on:mouseleave={(e) => e.currentTarget.style.opacity='0.7'}>
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <circle cx="12" cy="12" r="12" fill="#26A5E4"/>
              <path d="M17.93 6.56L5.54 11.17c-.83.33-.82.8-.15 1l3.17.99 7.34-4.63c.35-.21.66-.1.4.14L9.4 14.1l-.22 3.37c.32 0 .46-.15.63-.3l1.52-1.48 3.16 2.33c.58.32 1 .16 1.14-.54l2.07-9.73c.2-.8-.3-1.16-.77-.95z" fill="white"/>
            </svg>
          </button>
        </div>
      </div>
      <!-- Mode toggle -->
      <div style="margin-top: 16px; display: flex; gap: 6px; margin-bottom: 8px;">
        <button
          on:click={() => { activeTab = 'url'; searchQuery = ''; }}
          style="font-size: 12px; padding: 4px 12px; opacity: {activeTab === 'url' ? 1 : 0.45}; border-color: {activeTab === 'url' ? 'var(--accent-color)' : 'rgba(255,255,255,0.1)'};">
          🔗 URL
        </button>
        <button
          on:click={() => { activeTab = 'artist'; inputUrl = ''; }}
          style="font-size: 12px; padding: 4px 12px; opacity: {activeTab === 'artist' ? 1 : 0.45}; border-color: {activeTab === 'artist' ? 'var(--accent-color)' : 'rgba(255,255,255,0.1)'};">
          🔍 Search Artist
        </button>
        <button
          on:click={() => { activeTab = 'discover'; inputUrl = ''; if (!discoveryGenres.length) loadDiscoveryGenres(); if (!discoveryData) loadDiscoveryData(); }}
          style="font-size: 12px; padding: 4px 12px; opacity: {activeTab === 'discover' ? 1 : 0.45}; border-color: {activeTab === 'discover' ? 'var(--accent-color)' : 'rgba(255,255,255,0.1)'};">
          🌟 Discover
        </button>
      </div>

      {#if activeTab === 'artist'}
        <!-- Artist search input -->
        <div class="input-bar" style="display: flex; gap: 8px; align-items: center;">
          <input
            bind:value={searchQuery}
            placeholder="Artist name..."
            on:keydown={(e) => e.key === 'Enter' && startArtistSearch()}
            style="flex: 1; min-width: 0; font-family: inherit; font-size: 13px; height: 38px;"
          />
          <button on:click={startArtistSearch} disabled={!searchQuery.trim() || artistSearchLoading} style="height: 38px; white-space: nowrap;">
            {artistSearchLoading ? 'Searching...' : 'Search'}
          </button>
        </div>
      {:else if activeTab === 'discover'}
      {:else}
        <!-- URL input -->
        <div class="input-bar" style="display: flex; gap: 8px; align-items: flex-start;">
          <textarea
            bind:value={inputUrl}
            placeholder="Paste one or more Spotify / Apple Music / SoundCloud / Amazon Music URLs (one per line or comma-separated)..."
            disabled={isDownloading}
            rows="4"
            on:keydown={(e) => e.key === 'Enter' && e.ctrlKey && startDownload()}
            style="flex: 1; min-width: 0; resize: vertical; min-height: 64px; max-height: 200px; font-family: inherit; font-size: 13px;"
          ></textarea>
          {#if isDownloading}
            <button on:click={cancelDownload} style="background: var(--error-color); color: white; border-color: var(--error-color); align-self: stretch;">Stop</button>
          {:else}
            <button on:click={startDownload} disabled={!inputUrl} style="align-self: stretch;">
              Add to<br>Library
            </button>
          {/if}
        </div>
      {/if}

      <!-- Source health chips + format selector -->
      <div class="source-health-bar">
        {#each healthSources as src}
          {@const enabled = !!chipEnabled[src.key]}
          <button
            class="health-chip"
            class:health-chip-disabled={!enabled}
            class:health-chip-enabled={enabled}
            style={enabled
              ? `background:${src.bgEnabled};`
              : `background:rgba(0,0,0,0);`}
            on:click={() => handleChipClick(src.key)}
            title={!enabled
              ? `Tap to enable ${src.label} — opens settings`
              : `${src.label} — click to manage`}
          >
            {#if enabled}
              <span class="health-chip-on-badge">on</span>
            {:else}
              <span class="health-chip-off-badge">off</span>
            {/if}
            {#if src.key === 'hifi'}
              <img src="/icons/tidal.webp" alt="Tidal" class="health-chip-icon" style="opacity:{!enabled ? 0.25 : 1};" />
            {:else if src.key === 'apple'}
              <img src="/icons/apple-music.png" alt="Apple Music" class="health-chip-icon" style="opacity:{!enabled ? 0.25 : 1};" />
            {:else if src.key === 'amazon'}
              <img src="/icons/amazon-music.jpg" alt="Amazon Music" class="health-chip-icon" style="opacity:{!enabled ? 0.25 : 1}; border-radius: 4px;" />
            {:else if src.key === 'dab'}
              <img src="/icons/qobuz.png" alt="Qobuz" class="health-chip-icon" style="opacity:{!enabled ? 0.25 : 1};" />
            {/if}
          </button>
        {/each}
        <div class="format-selector">
          {#each formatOptions as fmt}
            <button
              class="format-pill"
              class:active={config.output_format === fmt.value}
              title={fmt.label}
              on:click={async () => { config.output_format = fmt.value; await SaveConfig(config); }}
            >{fmt.name}</button>
          {/each}
        </div>
      </div>
    </div>

    <!-- Playlist header: cover art + rich metadata, shown once playlist_loaded fires -->
    {#if playlistTitle || playlistArtwork}
      <div class="playlist-header">
        {#if playlistArtwork}
          <img src={playlistArtwork} alt="" class="playlist-cover" />
        {/if}
        <div class="playlist-meta">
          {#if playlistContentType}
            <span class="playlist-type">{playlistContentType}</span>
          {/if}
          <span class="playlist-title">{playlistTitle || '—'}</span>
          {#if playlistArtists}
            <span class="playlist-artists">{playlistArtists}</span>
          {/if}
          <div class="playlist-info-line">
            {#if playlistTotalTracks > 0}
              <span>{playlistTotalTracks} song{playlistTotalTracks !== 1 ? 's' : ''}</span>
            {/if}
            {#if playlistTotalDurationMs > 0}
              <span class="playlist-info-sep">·</span>
              <span>{formatDuration(playlistTotalDurationMs)}</span>
            {/if}
            {#if playlistReleaseDate}
              <span class="playlist-info-sep">·</span>
              <span>{playlistReleaseDate}</span>
            {/if}
          </div>
          {#if playlistQualityBadge}
            <span class="playlist-quality-badge">{playlistQualityBadge}</span>
          {/if}
        </div>
      </div>
    {/if}

    <!-- Tracklist: shows all tracks that have started, persists until next download -->
    <div class="tracklist-wrapper">
      <div class="tracklist" bind:this={tracklistEl} on:scroll={updateTracklistScroll}>
        {#if trackOrder.length === 0}
          <div class="tracklist-empty">
            <p>Paste a URL above and press <strong>Add to Library</strong></p>
          </div>
        {:else}
          {#each trackOrder as trackName (trackName)}
            {#if trackName.startsWith('__SEP__')}
              {@const sep = separatorMeta[trackName]}
              <div class="tracklist-album-sep">
                {#if sep?.artwork}
                  <img src={sep.artwork} alt="" class="sep-artwork" />
                {/if}
                <span class="sep-title">{sep?.title || 'Next album'}</span>
              </div>
            {:else}
              {@const state = activeTracks[trackName]}
              {#if state}
                <div class="track-row"
                  class:track-done={state.status === 'done'}
                  class:track-failed={state.status === 'failed'}
                  class:track-skipped={state.status === 'skipped'}>
                  <div class="track-row-main">
                    <div class="track-row-head">
                      <span class="track-row-name">{trackName}</span>
                      {#if state.status === 'failed'}
                        <button
                          class="track-retry-btn"
                          on:click={() => retryFailedTrack(trackName)}
                          disabled={isDownloading || !state.trackData || state.retrying}
                          title={isDownloading ? 'Wait for the current download to finish before retrying a single track.' : 'Retry this failed track only'}
                        >
                          {state.retrying ? 'Retrying…' : 'Retry'}
                        </button>
                      {/if}
                    </div>
                    <div class="track-row-side">
                      <span class="track-row-status">{state.error || state.text}</span>
                    </div>
                  </div>
                  {#if state.mode === 'progress'}
                    <div class="progress-bar-bg" style="margin-top:6px;">
                      <div class="progress-bar-fg"
                           class:error={!!state.error}
                           style="width: {state.progress ?? 0}%">
                      </div>
                    </div>
                  {/if}
                </div>
              {/if}
            {/if}
          {/each}
        {/if}
      </div>
      {#if !tracklistAtBottom && tracklistHasScrolled && trackOrder.length > 0}
        <button class="tracklist-jump-btn" on:click={scrollTracklistToBottom} title="Jump to bottom">↓</button>
      {/if}
    </div>

    <!-- Floating log toggle button -->
    <button class="log-toggle" on:click={() => showLog = !showLog} title="Activity Log">
      📋 {showLog ? 'Hide Log' : 'Log'}
    </button>
  </main>

  <!-- ── Discover full-screen overlay ──────────────────────────────────────── -->
  {#if activeTab === 'discover'}
  <div class="discover-overlay">
    <!-- Top bar: title + filters + close -->
    <div class="discover-topbar">
      <div class="discover-topbar-left">
        <span class="discover-topbar-title">🌟 Discover</span>
        <select bind:value={discoveryRegion} on:change={() => { loadDiscoveryGenres(); loadDiscoveryData(); }} class="discover-select">
          <option value="in">India (IN)</option>
          <option value="us">United States (US)</option>
          <option value="gb">United Kingdom (GB)</option>
          <option value="ca">Canada (CA)</option>
          <option value="au">Australia (AU)</option>
          <option value="jp">Japan (JP)</option>
          <option value="de">Germany (DE)</option>
          <option value="fr">France (FR)</option>
          <option value="br">Brazil (BR)</option>
          <option value="mx">Mexico (MX)</option>
          <option value="kr">South Korea (KR)</option>
          <option value="sg">Singapore (SG)</option>
          <option value="ae">UAE (AE)</option>
          <option value="sa">Saudi Arabia (SA)</option>
          <option value="ng">Nigeria (NG)</option>
          <option value="za">South Africa (ZA)</option>
          <option value="eg">Egypt (EG)</option>
          <option value="tr">Turkey (TR)</option>
          <option value="it">Italy (IT)</option>
          <option value="es">Spain (ES)</option>
          <option value="nl">Netherlands (NL)</option>
          <option value="se">Sweden (SE)</option>
          <option value="no">Norway (NO)</option>
          <option value="pl">Poland (PL)</option>
          <option value="ru">Russia (RU)</option>
          <option value="id">Indonesia (ID)</option>
          <option value="ph">Philippines (PH)</option>
          <option value="th">Thailand (TH)</option>
          <option value="my">Malaysia (MY)</option>
          <option value="pk">Pakistan (PK)</option>
          <option value="nz">New Zealand (NZ)</option>
          <option value="cn">China (CN)</option>
          <option value="hk">Hong Kong (HK)</option>
          <option value="tw">Taiwan (TW)</option>
        </select>
        <select bind:value={discoveryGenre} on:change={loadDiscoveryData} class="discover-select discover-select-genre">
          <option value="">Top Charts (All Genres)</option>
          {#each discoveryGenres as genre}
            <option value={genre.id}>{genre.name}</option>
          {/each}
        </select>
        <button on:click={loadDiscoveryData} disabled={discoveryLoading} class="discover-refresh-btn">
          {discoveryLoading ? '↻' : '↻ Refresh'}
        </button>
      </div>
      <button class="discover-close-btn" on:click={() => { activeTab = 'url'; }} title="Back to main">✕</button>
    </div>

    <!-- Scrollable content -->
    <div class="discover-body">
      {#if discoveryLoading}
        <div class="discover-loading">
          <div class="loading-spinner" style="font-size: 28px; margin-bottom: 14px;">↻</div>
          LOADING CHARTS...
        </div>
      {:else if discoveryData}
        <div class="discover-sections">
          {#if (discoveryData.top_albums && discoveryData.top_albums.length > 0) || (discoveryData.genre_albums && discoveryData.genre_albums.length > 0)}
            <div class="discover-section">
              <div class="discover-section-header">
                <h3 class="discover-section-title">Top Albums</h3>
                <div class="discover-section-rule"></div>
              </div>
              <div class="discover-grid">
                {#each (discoveryData.top_albums || discoveryData.genre_albums) as item}
                  <!-- svelte-ignore a11y-click-events-have-key-events -->
                  <!-- svelte-ignore a11y-no-static-element-interactions -->
                  <div class="discovery-card" on:click={() => handleDiscoveryClick(item.url)}>
                    <div class="discovery-artwork-wrapper">
                      <img src={item.artwork_url} alt={item.name} class="discovery-artwork" loading="lazy" />
                      <div class="discovery-play-overlay">
                        <svg width="32" height="32" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
                      </div>
                    </div>
                    <div class="discovery-info">
                      <div class="discovery-title" title={item.name}>{item.name}</div>
                      <div class="discovery-subtitle" title={item.artist_name}>{item.artist_name}</div>
                    </div>
                  </div>
                {/each}
              </div>
            </div>
          {/if}

          {#if (discoveryData.top_playlists && discoveryData.top_playlists.length > 0) || (discoveryData.genre_playlists && discoveryData.genre_playlists.length > 0)}
            <div class="discover-section">
              <div class="discover-section-header">
                <h3 class="discover-section-title">Recommended Playlists</h3>
                <div class="discover-section-rule"></div>
              </div>
              <div class="discover-grid">
                {#each (discoveryData.top_playlists || discoveryData.genre_playlists) as item}
                  <!-- svelte-ignore a11y-click-events-have-key-events -->
                  <!-- svelte-ignore a11y-no-static-element-interactions -->
                  <div class="discovery-card" on:click={() => handleDiscoveryClick(item.url)}>
                    <div class="discovery-artwork-wrapper">
                      <img src={item.artwork_url} alt={item.name} class="discovery-artwork" loading="lazy" />
                      <div class="discovery-play-overlay">
                        <svg width="32" height="32" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
                      </div>
                    </div>
                    <div class="discovery-info">
                      <div class="discovery-title" title={item.name}>{item.name}</div>
                      <div class="discovery-subtitle" title={item.curator_name || 'Apple Music'}>{item.curator_name || 'Apple Music'}</div>
                    </div>
                  </div>
                {/each}
              </div>
            </div>
          {/if}
        </div>
      {:else}
        <div class="discover-empty">
          <div style="font-size: 36px; margin-bottom: 12px; opacity: 0.3;">🎵</div>
          <p>No items found for this selection.</p>
        </div>
      {/if}
    </div>
  </div>
  {/if}

  <!-- Slide-in log panel (outside main so it overlays everything) -->
  {#if showLog}
  <div class="log-panel">
    <div class="log-panel-head">
      <span>Activity Log</span>
      <div style="display:flex; gap:6px; align-items:center;">
        {#if !logAtBottom}
          <button class="log-jump-btn" on:click={() => scrollToBottom(true)} title="Jump to bottom">↓</button>
        {/if}
        <button on:click={() => showLog = false} style="padding: 2px 8px; font-size: 12px;">✕</button>
      </div>
    </div>
    <div class="log-panel-body" bind:this={terminalContainer} on:scroll={updateAutoScrollState}>
      {#each logs as log (log.id)}
        <div class="log-line {log.type}">
          {#if log.isRawHtml}
            {@html log.text}
          {:else}
            <span class="prefix">❯</span> {log.text}
          {/if}
        </div>
      {/each}
      <div bind:this={terminalEnd}></div>
    </div>
  </div>
  {/if}
{/if}

<!-- ── Sponsor toast ────────────────────────────────────────────────────────── -->
{#if showSponsorToast}
  <div class="sponsor-toast" class:leaving={sponsorToastLeaving}
    on:mouseenter={() => clearTimeout(sponsorToastTimer)}
    on:mouseleave={() => { sponsorToastTimer = setTimeout(() => dismissSponsorToast(), 3000); }}
  >
    <div class="sponsor-toast-icon">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none"><path d="M23.881 8.948c-.773-4.085-4.859-4.593-4.859-4.593H.723c-.604 0-.679.798-.679.798s-.082 7.324-.022 11.822c.164 2.424 2.586 2.672 2.586 2.672s8.267-.023 11.966-.049c2.438-.426 2.683-2.566 2.658-3.734 4.352.24 7.422-2.831 6.649-6.916zm-11.062 3.511c-1.246 1.453-4.011 3.976-4.011 3.976s-.121.119-.31.023c-.076-.057-.108-.09-.108-.09-.443-.441-3.368-3.049-4.034-3.954-.709-.965-1.041-2.7-.091-3.71.951-1.01 3.005-1.086 4.363.407 0 0 1.565-1.782 3.468-.963 1.904.82 1.832 3.011.723 4.311zm6.173.478c-.928.116-1.682.028-1.682.028V7.284h1.77s1.971.551 1.971 2.638c0 1.913-.985 2.910-2.059 3.015z" fill="#FF5E5B"/></svg>
    </div>
    <div class="sponsor-toast-body">
      <p class="sponsor-toast-title">{supportStatus.title}</p>
      <p class="sponsor-toast-text">{supportStatus.message}</p>
      {#if supportStatus.enabled}
        <div class="support-progress-wrap support-progress-toast">
          <div class="support-progress-head">
            <span>{formatSupportAmount(supportStatus.current)} raised</span>
            <span>{formatSupportAmount(supportStatus.goal)} goal</span>
          </div>
          <div class="support-progress-bar">
            <div class="support-progress-fill" style={`width:${supportProgress}%`}></div>
          </div>
        </div>
      {/if}
      <button class="sponsor-toast-btn" on:click={() => { BrowserOpenURL(supportStatus.link); dismissSponsorToast(); }}>
        Support on Ko-fi
      </button>
    </div>
    <button class="sponsor-toast-close" on:click={dismissSponsorToast} title="Dismiss">×</button>
  </div>
{/if}

{#if showDownloadedMusic}
<div class="modal-overlay" on:click={() => showDownloadedMusic = false}>
  <div class="modal-content downloaded-modal" on:click|stopPropagation>
    <div class="downloaded-modal-head">
      <div>
        <h3 style="margin:0; color:var(--accent-color);">🎵 Downloaded Music</h3>
        <p style="margin:4px 0 0; font-size:12px; opacity:0.55;">Browse and play albums or playlists already saved to your library.</p>
      </div>
      <div style="display:flex; gap:8px; align-items:center;">
        <button on:click={refreshDownloadedMusicLibrary} style="padding: 6px 10px; font-size: 12px;">Refresh</button>
        <button on:click={() => showDownloadedMusic = false} style="padding: 6px 10px; font-size: 12px;">Close</button>
      </div>
    </div>

    <div class="downloaded-tabs">
      <button class:active-tab={downloadedView === 'albums'} on:click={() => downloadedView = 'albums'}>Albums</button>
      <button class:active-tab={downloadedView === 'playlists'} on:click={() => downloadedView = 'playlists'}>Playlists</button>
    </div>

    <div class="downloaded-layout">
      <div class="downloaded-library-pane">
        {#if downloadedLibraryLoading}
          <div class="downloaded-empty">Scanning your library...</div>
        {:else if downloadedLibraryError}
          <div class="downloaded-empty" style="color:#fca5a5;">{downloadedLibraryError}</div>
        {:else}
          {@const items = downloadedView === 'albums' ? downloadedLibrary.albums : downloadedLibrary.playlists}
          {#if items.length === 0}
            <div class="downloaded-empty">No {downloadedView} found in `{config.download_path}`.</div>
          {:else}
            <div class="downloaded-list">
              {#each items as release (release.relative_path)}
                <button
                  class="downloaded-card"
                  class:selected={downloadedSelectedPath === release.relative_path}
                  on:click={() => openDownloadedRelease(release)}
                >
                  {#if release.artwork_url}
                    <img src={release.artwork_url} alt="" class="downloaded-card-art" />
                  {:else}
                    <div class="downloaded-card-placeholder">♫</div>
                  {/if}
                  <div class="downloaded-card-copy">
                    <div class="downloaded-card-title">{release.title}</div>
                    <div class="downloaded-card-meta">{releaseMetaLine(release)}</div>
                  </div>
                </button>
              {/each}
            </div>
          {/if}
        {/if}
      </div>

      <div class="downloaded-detail-pane">
        {#if downloadedSelectedReleaseLoading}
          <div class="downloaded-empty">Loading release...</div>
        {:else if downloadedSelectedRelease}
          <div class="downloaded-release-hero">
            {#if downloadedSelectedRelease.artwork_url}
              <img src={downloadedSelectedRelease.artwork_url} alt="" class="downloaded-release-art" />
            {:else}
              <div class="downloaded-release-placeholder">♫</div>
            {/if}
            <div class="downloaded-release-copy">
              <div class="downloaded-release-type">{downloadedSelectedRelease.kind === 'playlist' ? 'Playlist' : 'Album'}</div>
              <h2>{downloadedSelectedRelease.title}</h2>
              <p>{releaseMetaLine(downloadedSelectedRelease)}</p>
              <button on:click={() => playDownloadedTrack(0)} disabled={!downloadedSelectedRelease.tracks?.length}>
                {currentPlayerTrack && playerReleaseTitle === downloadedSelectedRelease.title ? 'Play From Start' : 'Play Release'}
              </button>
            </div>
          </div>

          <div class="downloaded-tracks">
            {#each downloadedSelectedRelease.tracks as track, index}
              <button class="downloaded-track-row" class:is-active={currentPlayerTrack?.file_path === track.file_path} on:click={() => playDownloadedTrack(index)}>
                <div class="downloaded-track-index">
                  {#if currentPlayerTrack?.file_path === track.file_path}
                    ▶
                  {:else if track.disc_number && track.track_number}
                    {track.disc_number}{String(track.track_number).padStart(2, '0')}
                  {:else if track.track_number}
                    {String(track.track_number).padStart(2, '0')}
                  {:else}
                    {index + 1}
                  {/if}
                </div>
                <div class="downloaded-track-copy">
                  <div class="downloaded-track-title">{track.title || track.file_name}</div>
                  <div class="downloaded-track-meta">
                    {track.artist || downloadedSelectedRelease.artist || 'Unknown artist'}
                    {#if track.codec}
                      <span>· {track.codec}</span>
                    {/if}
                  </div>
                </div>
                <div class="downloaded-track-duration">{formatPlaybackTime(track.duration_seconds || 0)}</div>
              </button>
            {/each}
          </div>
        {:else}
          <div class="downloaded-empty">Pick an album or playlist to start browsing.</div>
        {/if}
      </div>
    </div>

    <div class="downloaded-player">
      <div class="downloaded-player-copy">
        <div class="downloaded-player-title">{currentPlayerTrack?.title || 'Nothing playing'}</div>
        <div class="downloaded-player-meta">
          {#if currentPlayerTrack}
            {(currentPlayerTrack.artist || downloadedSelectedRelease?.artist || 'Unknown artist')} · {playerReleaseTitle || downloadedSelectedRelease?.title || 'Release'}
          {:else}
            Select a downloaded track to play it here.
          {/if}
        </div>
      </div>
      <div class="downloaded-player-controls">
        <button on:click={playPreviousTrack} disabled={!currentPlayerTrack}>⏮</button>
        <button on:click={togglePlayback} disabled={!currentPlayerTrack && !(downloadedSelectedRelease?.tracks?.length)}>{audioEl && !audioEl.paused ? 'Pause' : 'Play'}</button>
        <button on:click={playNextTrack} disabled={!currentPlayerTrack || playerTrackIndex >= playerQueue.length - 1}>⏭</button>
      </div>
      <div class="downloaded-player-timeline">
        <span>{formatPlaybackTime(playerCurrentTime)}</span>
        <input
          type="range"
          min="0"
          max={playerDuration || 0}
          step="0.1"
          value={playerCurrentTime}
          disabled={!currentPlayerTrack}
          on:mousedown={() => playerSeeking = true}
          on:mouseup={handleSeekCommit}
          on:input={handleSeekInput}
          on:change={handleSeekCommit}
        />
        <span>{formatPlaybackTime(playerDuration)}</span>
      </div>
      <div class="downloaded-player-volume">
        <span>Vol</span>
        <input type="range" min="0" max="1" step="0.01" bind:value={playerVolume} on:input={() => { if (audioEl) audioEl.volume = playerVolume; }} />
      </div>
    </div>

    {#if playerError}
      <div style="margin-top: 10px; color: #fca5a5; font-size: 12px;">Playback error: {playerError}</div>
    {/if}

    <audio
      bind:this={audioEl}
      preload="metadata"
      on:timeupdate={handleAudioTimeUpdate}
      on:loadedmetadata={handleAudioLoadedMetadata}
      on:ended={handleAudioEnded}
      on:error={() => playerError = 'The file could not be played in the embedded player.'}
    ></audio>
  </div>
</div>
{/if}

{#if showHistory}
<div class="modal-overlay" on:click={() => showHistory = false}>
  <div class="modal-content" on:click|stopPropagation>
    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(0,255,204,0.2); padding-bottom: 16px; margin-bottom: 16px;">
      <h3 style="margin:0; color:var(--accent-color);">🕒 Library Build History</h3>
      <button on:click={() => showHistory = false} style="padding: 4px 8px; font-size: 12px;">Close</button>
    </div>
    <div style="overflow-y: auto; max-height: 400px; display: flex; flex-direction: column; gap: 12px; padding-right: 8px;">
      {#if historyItems.length === 0}
        <p style="color: #777; font-size: 13px; text-align: center;">No history found.</p>
      {:else}
        {#each historyItems as item}
          <div class="history-card" style={item.error ? 'border-color: rgba(248,113,113,0.4); background: rgba(248,113,113,0.04);' : ''}>
            <div style="display: flex; align-items: flex-start; gap: 10px;">
              {#if item.artwork_url}
                <img src={item.artwork_url} alt="" style="width:44px; height:44px; border-radius:5px; object-fit:cover; flex-shrink:0;" />
              {:else}
                <div style="width:44px; height:44px; border-radius:5px; background:rgba(255,255,255,0.06); flex-shrink:0; display:flex; align-items:center; justify-content:center; font-size:20px;">🎵</div>
              {/if}
              <div style="flex: 1; min-width: 0;">
                <div style="font-weight: 600; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 2px;">
                  {item.title || item.url}
                </div>
                {#if item.title}
                  <div style="font-size: 11px; opacity: 0.4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 3px;">{item.url}</div>
                {/if}
                <div style="font-size: 11px; opacity: 0.55; display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                  <span>{item.total || 0} tracks</span>
                  {#if item.failed > 0}<span style="color:#f87171;">· {item.failed} failed</span>{/if}
                  <span>· {new Date(item.date).toLocaleDateString(undefined, {month:'short', day:'numeric', year:'numeric'})}</span>
                </div>
              </div>
              <button
                title="Re-queue this URL"
                on:click={() => { inputUrl = (inputUrl ? inputUrl + '\n' : '') + item.url; showHistory = false; }}
                style="flex-shrink: 0; padding: 2px 8px; font-size: 11px; border-color: rgba(0,255,204,0.3); color: var(--accent-color); background: rgba(0,255,204,0.05);"
              >↩ Re-queue</button>
            </div>
            {#if item.error}
              <div style="margin-top: 8px; font-size: 11px; color: #f87171; background: rgba(248,113,113,0.08); border: 1px solid rgba(248,113,113,0.2); border-radius: 4px; padding: 4px 8px; word-break: break-word;">
                ✗ {item.error}
              </div>
            {/if}
            {#if item.sources && Object.keys(item.sources).length > 0}
              <div style="margin-top: 8px; font-size: 11px; color: #94a3b8; display: flex; gap: 4px; flex-wrap: wrap;">
                {#each Object.entries(item.sources) as [src, count]}
                  <span style="background: rgba(0,255,204,0.1); padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(0,255,204,0.2)">{src}: {count}</span>
                {/each}
              </div>
            {/if}
          </div>
        {/each}
      {/if}
    </div>
    {#if historyItems.length > 0}
      <button on:click={clearHistory} style="margin-top: 16px; width: 100%; border-color: var(--error-color); color: var(--error-color); background: rgba(255,0,0,0.05);">Clear History</button>
    {/if}
  </div>
</div>
{/if}

{#if showSettings}
<div class="modal-overlay" on:click={saveSettings}>
  <div class="modal-content" on:click|stopPropagation style="max-height: 88vh; display: flex; flex-direction: column;">
    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(0,255,204,0.2); padding-bottom: 16px; margin-bottom: 16px; flex-shrink: 0;">
      <h3 style="margin:0; color:var(--accent-color);">⚙️ Settings</h3>
      <button on:click={saveSettings} style="padding: 4px 8px; font-size: 12px;">Save & Close</button>
    </div>
    <div style="display: flex; flex-direction: column; gap: 16px; overflow-y: auto; flex: 1; padding-right: 4px;">

      <div class="field">
        <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
          <input type="checkbox" bind:checked={config.prefer_explicit} style="margin-top: 2px;" />
          <div>
            <span style="font-weight: 500; font-size: 13px;">Prefer explicit versions</span>
            <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Avoid radio edits and clean versions. When a track is marked explicit, Antra keeps searching if the first result looks censored.</p>
          </div>
        </label>

        <div style="margin-top: 14px;">
          <label for="maxRetriesModal">Failed track retries</label>
          <p style="font-size: 11px; color: #555; margin: 4px 0 8px;">Automatic retries for transient failures like truncated downloads before a track is marked failed.</p>
          <input
            id="maxRetriesModal"
            type="number"
            min="1"
            max="10"
            bind:value={config.max_retries}
            style="width: 96px;"
          />
        </div>
      </div>

      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 0;">Library Mode</p>
        <div style="display: flex; flex-direction: column; gap: 12px; margin-top: 8px;">
          <label style="display: flex; align-items: flex-start; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="smart_dedup" bind:group={config.library_mode} style="margin-top: 2px;" />
            <div>
              Smart Dedup <span style="font-size: 11px; opacity: 0.6; margin-left: 4px;">(Default — skip if already in library anywhere)</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Saves storage. If a track was downloaded as part of a Best Of, it won't be re-downloaded for the studio album.</p>
            </div>
          </label>
          <label style="display: flex; align-items: flex-start; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="full_albums" bind:group={config.library_mode} style="margin-top: 2px;" />
            <div>
              Full Albums <span style="font-size: 11px; opacity: 0.6; margin-left: 4px;">(Each album complete — allows cross-album duplicates)</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Every album folder is always complete. A track on both a studio album and a compilation will exist in both folders.</p>
            </div>
          </label>
        </div>
      </div>

      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 0;">Folder Structure</p>
        <div style="display: flex; flex-direction: column; gap: 12px; margin-top: 8px;">
          <label style="display: flex; align-items: flex-start; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="standard" bind:group={config.folder_structure} style="margin-top: 2px;" />
            <div>
              Standard <span style="font-size: 11px; opacity: 0.6; margin-left: 4px;">(Artist / Album / files)</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Default. Compatible with Navidrome, Jellyfin, and Plex.</p>
            </div>
          </label>
          <label style="display: flex; align-items: flex-start; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="flat" bind:group={config.folder_structure} style="margin-top: 2px;" />
            <div>
              Flat <span style="font-size: 11px; opacity: 0.6; margin-left: 4px;">(Album / files — no artist folder)</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Simpler layout for manual organisation or single-artist libraries.</p>
            </div>
          </label>
        </div>
      </div>

      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 0;">Filename Format</p>
        <div style="display: flex; flex-direction: column; gap: 10px; margin-top: 8px;">
          <label style="display: flex; align-items: center; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="default" bind:group={config.filename_format} />
            <div>Default <span style="font-size: 11px; opacity: 0.6; margin-left: 4px;">01 - Title.flac</span></div>
          </label>
          <label style="display: flex; align-items: center; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="title_only" bind:group={config.filename_format} />
            <div>Title only <span style="font-size: 11px; opacity: 0.6; margin-left: 4px;">Title.flac</span></div>
          </label>
          <label style="display: flex; align-items: center; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="artist_title" bind:group={config.filename_format} />
            <div>Artist – Title <span style="font-size: 11px; opacity: 0.6; margin-left: 4px;">Artist - Title.flac</span></div>
          </label>
          <label style="display: flex; align-items: center; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="title_artist" bind:group={config.filename_format} />
            <div>Title – Artist <span style="font-size: 11px; opacity: 0.6; margin-left: 4px;">Title - Artist.flac</span></div>
          </label>
        </div>
      </div>

      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <label for="outDirModal">Music Folder</label>
        <div style="display: flex; gap: 8px; margin-top: 8px;">
          <input id="outDirModal" readonly type="text" value={config.download_path} />
          <button on:click={pickDir}>Browse</button>
        </div>
      </div>

      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 12px;">Sources</p>

        <!-- Soulseek toggle -->
        <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
          <input type="checkbox" bind:checked={config.soulseek_enabled} style="margin-top: 2px;" />
          <div>
            <span style="font-weight: 500;">Soulseek (P2P)</span>
            <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Find rare or hi-res versions not on streaming. Requires account.</p>
          </div>
        </label>

        {#if config.soulseek_enabled}
          <div style="margin-top: 12px; padding: 12px; background: rgba(255,255,255,0.03); border-radius: 8px; border: 1px solid rgba(255,255,255,0.06);">
            <label for="slskUsernameSettings" style="font-size: 12px; opacity: 0.7;">Soulseek Username</label>
            <input id="slskUsernameSettings" type="text" bind:value={config.soulseek_username} placeholder="Your Soulseek username" style="width: 100%; box-sizing: border-box; margin-top: 6px;" />
            <label for="slskPasswordSettings" style="font-size: 12px; opacity: 0.7; margin-top: 10px; display: block;">Soulseek Password</label>
            <input id="slskPasswordSettings" type="password" bind:value={config.soulseek_password} placeholder="Your Soulseek password" style="width: 100%; box-sizing: border-box; margin-top: 6px;" />

            <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin-top: 14px;">
              <input type="checkbox" bind:checked={config.soulseek_seed_after_download} style="margin-top: 2px;" />
              <div>
                <span style="font-weight: 500; font-size: 13px;">Seed after download</span>
                <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Keep a hardlink in slskd's folder so files are seeded back to the Soulseek network. Zero extra disk space.</p>
              </div>
            </label>

            {#if slskdWebUIInfo}
            <div style="margin-top: 14px; padding: 10px 12px; background: rgba(0,0,0,0.2); border-radius: 6px; border: 1px solid rgba(255,255,255,0.07);">
              <p style="font-size: 11px; opacity: 0.5; margin: 0 0 6px; text-transform: uppercase; letter-spacing: 0.05em;">slskd Web UI</p>
              <p style="font-size: 12px; margin: 0 0 4px; font-family: monospace; color: var(--accent-color);">{slskdWebUIInfo.url}</p>
              <p style="font-size: 11px; margin: 0; opacity: 0.65; font-family: monospace;">user: {slskdWebUIInfo.username} &nbsp;·&nbsp; pass: {slskdWebUIInfo.password}</p>
            </div>
            {/if}
          </div>
        {/if}


      </div>

      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p id="settings-tidal" style="font-size: 13px; font-weight: 600; margin: 0 0 4px;">TIDAL Premium</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 12px;">Connect your TIDAL account via OAuth (recommended) or paste a session JSON blob directly.</p>

        <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin-bottom: 12px;">
          <input type="checkbox"
            checked={config.tidal_enabled}
            on:change={(e) => config.tidal_enabled = e.currentTarget.checked}
            style="margin-top: 2px;" />
          <div>
            <span style="font-weight: 500;">Enable TIDAL Premium</span>
            <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Turns on the TIDAL account configuration block and validation flow.</p>
          </div>
        </label>

        {#if config.tidal_enabled}
          <div style="margin-top: 12px; padding: 12px; background: rgba(255,255,255,0.03); border-radius: 8px; border: 1px solid rgba(255,255,255,0.06);">

            <!-- OAuth Login Section -->
            <div style="margin-bottom: 16px; padding: 12px; background: rgba(29,185,222,0.06); border: 1px solid rgba(29,185,222,0.2); border-radius: 6px;">
              <p style="font-size: 12px; font-weight: 600; margin: 0 0 6px; color: #1DB9DE;">🔐 OAuth Login (Recommended)</p>
              <p style="font-size: 11px; color: #888; margin: 0 0 10px;">Open TIDAL's login page in your browser. Antra will capture your session automatically — no manual copy-paste needed.</p>

              {#if tidalOAuth.phase === 'idle' || tidalOAuth.phase === 'error'}
                <button
                  on:click={startTidalOAuth}
                  style="padding: 6px 12px; font-size: 12px; background: rgba(29,185,222,0.15); border-color: rgba(29,185,222,0.4); color: #1DB9DE;"
                >
                  Login with TIDAL
                </button>
                {#if tidalOAuth.phase === 'error'}
                  <p style="font-size: 11px; color: #fca5a5; margin: 8px 0 0;">✖ {tidalOAuth.message}</p>
                  <button on:click={() => tidalOAuth = { phase: 'idle' }} style="font-size: 10px; padding: 3px 8px; margin-top: 6px; opacity: 0.6;">Reset</button>
                {/if}

              {:else if tidalOAuth.phase === 'starting'}
                <p style="font-size: 11px; color: #94a3b8; margin: 0;">⏳ {tidalOAuth.message || 'Starting...'}</p>

              {:else if tidalOAuth.phase === 'waiting_browser'}
                <div style="margin-top: 4px;">
                  <p style="font-size: 11px; color: #facc15; margin: 0 0 8px;">⏳ Waiting for you to approve in your browser...</p>
                  {#if tidalOAuth.url}
                    <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                      <button
                        on:click={() => BrowserOpenURL(tidalOAuth.url)}
                        style="padding: 5px 10px; font-size: 11px; background: rgba(29,185,222,0.2); border-color: rgba(29,185,222,0.4); color: #1DB9DE;"
                      >
                        Open TIDAL Login ↗
                      </button>
                      <code style="font-size: 11px; background: rgba(255,255,255,0.05); padding: 3px 8px; border-radius: 4px; color: #e2e8f0; word-break: break-all; flex: 1;">{tidalOAuth.url}</code>
                    </div>
                    {#if tidalOAuth.code}
                      <p style="font-size: 11px; color: #94a3b8; margin: 6px 0 0;">Device code: <code style="color: #facc15;">{tidalOAuth.code}</code></p>
                    {/if}
                  {/if}
                  <p style="font-size: 10px; color: #555; margin: 8px 0 0;">After you log in, Antra will automatically save your session. This window will update within seconds.</p>
                </div>

              {:else if tidalOAuth.phase === 'success'}
                <div style="display: flex; align-items: center; gap: 8px;">
                  <span style="font-size: 14px;">✅</span>
                  <div>
                    <p style="font-size: 12px; color: #86efac; margin: 0;">{tidalOAuth.message}</p>
                    {#if tidalOAuth.displayName}
                      <p style="font-size: 11px; color: #94a3b8; margin: 3px 0 0;">Account: {tidalOAuth.displayName}{tidalOAuth.countryCode ? ` (${tidalOAuth.countryCode})` : ''}</p>
                    {/if}
                  </div>
                  <button on:click={() => tidalOAuth = { phase: 'idle' }} style="font-size: 10px; padding: 3px 8px; margin-left: auto; opacity: 0.6;">Re-login</button>
                </div>
              {/if}
            </div>

            <!-- Divider -->
            <p style="font-size: 10px; color: #444; text-align: center; margin: 0 0 12px; letter-spacing: 0.08em;">— OR PASTE SESSION JSON MANUALLY —</p>

            <label for="tidalAuthMode" style="font-size: 12px; opacity: 0.7;">Auth method</label>
            <select id="tidalAuthMode" bind:value={config.tidal_auth_mode} style="width: 100%; box-sizing: border-box; margin-top: 6px;">
              <option value="session_json">Session JSON (Recommended)</option>
              <option value="manual_tokens">Manual tokens</option>
            </select>

            {#if config.tidal_auth_mode === 'session_json'}
              <label for="tidalSessionJson" style="font-size: 12px; opacity: 0.7; margin-top: 12px; display: block;">Session JSON</label>
              <textarea
                id="tidalSessionJson"
                bind:value={config.tidal_session_json}
                on:paste={(e) => {
                  e.preventDefault();
                  const raw = e.clipboardData?.getData('text') ?? '';
                  const cleaned = raw
                    .replace(/[\r\n\t]/g, ' ')
                    .replace(/[\u0000-\u001F\u007F]/g, '')
                    .replace(/\s+/g, ' ')
                    .trim();
                  try {
                    config.tidal_session_json = JSON.stringify(JSON.parse(cleaned));
                  } catch {
                    config.tidal_session_json = cleaned;
                  }
                }}
                placeholder='&#123;"token_type":&#123;"data":"Bearer"&#125;,"session_id":&#123;"data":"..."&#125;,"access_token":&#123;"data":"..."&#125;,"refresh_token":&#123;"data":"..."&#125;,"is_pkce":&#123;"data":true&#125;&#125;'
                style="width: 100%; min-height: 140px; box-sizing: border-box; margin-top: 6px; font-family: monospace; font-size: 11px;"
              />
            {:else}
              <label for="tidalAccessToken" style="font-size: 12px; opacity: 0.7; margin-top: 12px; display: block;">Access token</label>
              <input id="tidalAccessToken" type="password" bind:value={config.tidal_access_token} placeholder="Bearer access token" style="width: 100%; box-sizing: border-box; margin-top: 6px;" />

              <label for="tidalRefreshToken" style="font-size: 12px; opacity: 0.7; margin-top: 10px; display: block;">Refresh token</label>
              <input id="tidalRefreshToken" type="password" bind:value={config.tidal_refresh_token} placeholder="Refresh token" style="width: 100%; box-sizing: border-box; margin-top: 6px;" />

              <label for="tidalSessionId" style="font-size: 12px; opacity: 0.7; margin-top: 10px; display: block;">Session ID</label>
              <input id="tidalSessionId" type="text" bind:value={config.tidal_session_id} placeholder="Optional session id" style="width: 100%; box-sizing: border-box; margin-top: 6px;" />

              <div style="display: grid; grid-template-columns: 1fr 120px; gap: 8px; margin-top: 10px;">
                <div>
                  <label for="tidalTokenType" style="font-size: 12px; opacity: 0.7; display: block;">Token type</label>
                  <input id="tidalTokenType" type="text" bind:value={config.tidal_token_type} placeholder="Bearer" style="width: 100%; box-sizing: border-box; margin-top: 6px;" />
                </div>
                <div>
                  <label for="tidalCountryCode" style="font-size: 12px; opacity: 0.7; display: block;">Country</label>
                  <input id="tidalCountryCode" type="text" bind:value={config.tidal_country_code} placeholder="US" style="width: 100%; box-sizing: border-box; margin-top: 6px;" />
                </div>
              </div>
            {/if}

            <div style="display: flex; align-items: center; gap: 10px; margin-top: 14px;">
              <button on:click={validateTidalSettings} disabled={tidalValidationLoading} style="padding: 6px 10px; font-size: 12px;">
                {#if tidalValidationLoading}Validating...{:else}Test TIDAL Connection{/if}
              </button>
              <span style="font-size: 11px; opacity: 0.65;">This saves Settings first, then checks the imported TIDAL session.</span>
            </div>

            {#if tidalValidationStatus}
              <div style={`margin-top: 12px; padding: 10px 12px; border-radius: 6px; border: 1px solid ${tidalValidationStatus.ok ? 'rgba(16,185,129,0.3)' : 'rgba(239,68,68,0.3)'}; background: ${tidalValidationStatus.ok ? 'rgba(16,185,129,0.08)' : 'rgba(239,68,68,0.08)'};`}>
                <p style={`font-size: 12px; margin: 0; color: ${tidalValidationStatus.ok ? '#86efac' : '#fca5a5'};`}>
                  {tidalValidationStatus.ok ? 'TIDAL connection looks valid.' : 'TIDAL validation failed.'}
                </p>
                <p style="font-size: 11px; margin: 6px 0 0; opacity: 0.75;">{tidalValidationStatus.message}</p>
                {#if tidalValidationStatus.display_name}
                  <p style="font-size: 11px; margin: 6px 0 0; opacity: 0.75;">Account: {tidalValidationStatus.display_name}{tidalValidationStatus.country_code ? ` (${tidalValidationStatus.country_code})` : ''}</p>
                {/if}
              </div>
            {/if}
          </div>
        {/if}
      </div>

      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p id="settings-apple" style="font-size: 13px; font-weight: 600; margin: 0 0 4px;">Apple Music</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 12px;">Use your own Apple Music subscription for direct lossless downloads. Paste the web player tokens plus your Widevine device file path.</p>

        <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin-bottom: 12px;">
          <input
            type="checkbox"
            checked={config.apple_enabled}
            on:change={(e) => config.apple_enabled = e.currentTarget.checked}
            style="margin-top: 2px;" />
          <div>
            <span style="font-weight: 500;">Enable Apple Music</span>
            <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Turns on the direct Apple Music adapter. Songs download with your own subscription instead of shared mirrors.</p>
          </div>
        </label>

        {#if config.apple_enabled}
          <div style="margin-top: 12px; padding: 12px; background: rgba(255,255,255,0.03); border-radius: 8px; border: 1px solid rgba(255,255,255,0.06);">
            <div style="margin-bottom: 16px; padding: 12px; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 6px;">
              <p style="font-size: 12px; font-weight: 600; margin: 0 0 6px;">Browser Session Login (Recommended)</p>
              <p style="font-size: 11px; color: #888; margin: 0 0 10px;">Open a browser window, sign in to Apple Music, and let Antra capture the session automatically.</p>
              {#if appleLogin.phase === 'idle' || appleLogin.phase === 'error'}
                <button on:click={startAppleLogin} style="padding: 6px 12px; font-size: 12px;">Login with Apple Music</button>
                {#if appleLogin.phase === 'error'}
                  <p style="font-size: 11px; color: #fca5a5; margin: 8px 0 0;">✖ {appleLogin.message}</p>
                {/if}
              {:else if appleLogin.phase === 'starting'}
                <p style="font-size: 11px; color: #94a3b8; margin: 0;">⏳ {appleLogin.message}</p>
              {:else if appleLogin.phase === 'success'}
                <p style="font-size: 11px; color: #86efac; margin: 0;">✅ {appleLogin.message}</p>
              {/if}
            </div>

            <label for="appleAuthorizationToken" style="font-size: 12px; opacity: 0.7;">Authorization token</label>
            <input id="appleAuthorizationToken" type="password" bind:value={config.apple_authorization_token} placeholder="Bearer eyJ..." style="width: 100%; box-sizing: border-box; margin-top: 6px; font-family: monospace; font-size: 12px;" />

            <label for="appleMusicUserToken" style="font-size: 12px; opacity: 0.7; margin-top: 10px; display: block;">Music user token</label>
            <input id="appleMusicUserToken" type="password" bind:value={config.apple_music_user_token} placeholder="Music-User-Token" style="width: 100%; box-sizing: border-box; margin-top: 6px; font-family: monospace; font-size: 12px;" />

            <div style="display: grid; grid-template-columns: 120px 1fr; gap: 8px; margin-top: 10px;">
              <div>
                <label for="appleStorefront" style="font-size: 12px; opacity: 0.7; display: block;">Storefront</label>
                <input id="appleStorefront" type="text" bind:value={config.apple_storefront} placeholder="us" style="width: 100%; box-sizing: border-box; margin-top: 6px;" />
              </div>
              <div>
                <label for="appleWvdPath" style="font-size: 12px; opacity: 0.7; display: block;">Widevine device path</label>
                <input id="appleWvdPath" type="text" bind:value={config.apple_wvd_path} placeholder="C:\\path\\to\\android_l3.wvd" style="width: 100%; box-sizing: border-box; margin-top: 6px; font-family: monospace; font-size: 12px;" />
              </div>
            </div>

            <p style="font-size: 11px; color: #555; margin: 10px 0 0;">The browser login fills the tokens automatically. You still need a valid <code>.wvd</code> device file path for decryption.</p>
          </div>
        {/if}
      </div>

      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p id="settings-amazon" style="font-size: 13px; font-weight: 600; margin: 0 0 4px;">Amazon Music</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 12px;">Use your own Amazon Music subscription for direct FLAC downloads. Paste the extracted credentials JSON from your browser session.</p>

        <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin-bottom: 12px;">
          <input
            type="checkbox"
            checked={config.amazon_enabled}
            on:change={(e) => config.amazon_enabled = e.currentTarget.checked}
            style="margin-top: 2px;" />
          <div>
            <span style="font-weight: 500;">Enable Amazon Music</span>
            <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Turns on the direct Amazon adapter. The JSON must include your session tokens plus a <code>wvd_path</code>.</p>
          </div>
        </label>

        {#if config.amazon_enabled}
          <div style="margin-top: 12px; padding: 12px; background: rgba(255,255,255,0.03); border-radius: 8px; border: 1px solid rgba(255,255,255,0.06);">
            <div style="margin-bottom: 16px; padding: 12px; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 6px;">
              <p style="font-size: 12px; font-weight: 600; margin: 0 0 6px;">Browser Session Login (Recommended)</p>
              <p style="font-size: 11px; color: #888; margin: 0 0 10px;">Open a browser window, sign in to Amazon Music, and let Antra capture the session automatically.</p>

              {#if amazonLogin.phase === 'idle' || amazonLogin.phase === 'error'}
                {@const sessionInfo = amazonSessionInfo()}
                {#if sessionInfo && amazonLogin.phase === 'idle'}
                  <!-- Session already saved — show status instead of login button -->
                  <div style="display:flex; align-items:flex-start; gap:8px; padding:8px 10px; background:rgba(34,197,94,0.08); border:1px solid rgba(34,197,94,0.25); border-radius:6px; margin-bottom:10px;">
                    <span style="font-size:13px; flex-shrink:0;">✅</span>
                    <div>
                      <p style="font-size:11px; color:#86efac; margin:0 0 2px; font-weight:600;">Session active</p>
                      <p style="font-size:10px; color:#555; margin:0;">Captured {sessionInfo.capturedAt} · {sessionInfo.expiresNote}</p>
                    </div>
                  </div>
                  <button on:click={startAmazonLogin} style="padding: 5px 10px; font-size: 11px; opacity:0.7;">↻ Re-login</button>
                {:else}
                  {#if !config.amazon_wvd_path}
                    <div style="display:flex; align-items:flex-start; gap:8px; padding:8px 10px; background:rgba(250,204,21,0.08); border:1px solid rgba(250,204,21,0.25); border-radius:6px; margin-bottom:10px;">
                      <span style="font-size:13px; flex-shrink:0;">⚠️</span>
                      <p style="font-size:11px; color:#facc15; margin:0; line-height:1.5;">Set your <strong>Widevine device path</strong> below before starting the browser login — it's required for downloads.</p>
                    </div>
                  {/if}
                  <button on:click={startAmazonLogin} style="padding: 6px 12px; font-size: 12px;">Browser Session Login</button>
                  {#if amazonLogin.phase === 'error'}
                    <p style="font-size: 11px; color: #fca5a5; margin: 8px 0 0;">✖ {amazonLogin.message}</p>
                  {/if}
                {/if}
              {:else if amazonLogin.phase === 'starting'}
                <div style="display:flex; align-items:center; gap:10px;">
                  <p style="font-size: 11px; color: #94a3b8; margin: 0; flex:1;">⏳ {amazonLogin.message}</p>
                  <button on:click={() => { amazonLogin = { phase: 'idle' }; config.amazon_enabled = false; }} style="padding:3px 10px; font-size:11px; opacity:0.6;">Cancel</button>
                </div>
              {:else if amazonLogin.phase === 'waiting_for_user'}
                <p style="font-size: 11px; color: #94a3b8; margin: 0 0 10px;">{amazonLogin.message}</p>
                <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                  <button
                    on:click={async () => { amazonLogin = { phase: 'capturing', message: 'Reading your browser session…' }; try { await ConfirmAmazonLogin(); } catch(e) { amazonLogin = { phase: 'error', message: String(e) }; } }}
                    style="padding: 6px 14px; font-size: 12px; background: #16a34a; border-color: #15803d;"
                  >
                    ✓ I'm Signed In
                  </button>
                  <button on:click={() => { amazonLogin = { phase: 'idle' }; config.amazon_enabled = false; }} style="padding:5px 12px; font-size:11px; opacity:0.6;">Cancel</button>
                </div>
              {:else if amazonLogin.phase === 'capturing'}
                <div style="display:flex; align-items:center; gap:10px;">
                  <p style="font-size: 11px; color: #94a3b8; margin: 0; flex:1;">⏳ {amazonLogin.message}</p>
                  <button on:click={() => { amazonLogin = { phase: 'idle' }; config.amazon_enabled = false; }} style="padding:3px 10px; font-size:11px; opacity:0.6;">Cancel</button>
                </div>
              {:else if amazonLogin.phase === 'success'}
                <p style="font-size: 11px; color: #86efac; margin: 0;">✅ {amazonLogin.message}</p>
                {#if amazonLogin.detail}
                  <p style="font-size: 11px; color: #facc15; margin: 6px 0 0;">{amazonLogin.detail}</p>
                {/if}
              {/if}
            </div>

            <label for="amazonWvdPath" style="font-size: 12px; opacity: 0.7;">Widevine device path</label>
            <input
              id="amazonWvdPath"
              type="text"
              bind:value={config.amazon_wvd_path}
              placeholder="C:\\path\\to\\android_l3.wvd"
              style="width: 100%; box-sizing: border-box; margin-top: 6px; font-family: monospace; font-size: 12px;"
            />

            <p style="font-size: 10px; color: #444; text-align: center; margin: 12px 0; letter-spacing: 0.08em;">— ADVANCED / FALLBACK JSON —</p>

            <label for="amazonDirectCredsJson" style="font-size: 12px; opacity: 0.7;">Credentials JSON</label>
            <textarea
              id="amazonDirectCredsJson"
              bind:value={config.amazon_direct_creds_json}
              placeholder={`{"cookie":"...","authorization":"Bearer ...","csrf_token":"...","csrf_rnd":"...","csrf_ts":"...","customer_id":"...","device_id":"...","session_id":"...","wvd_path":"C:\\path\\to\\android_l3.wvd"}`}
              style="width: 100%; min-height: 140px; box-sizing: border-box; margin-top: 6px; font-family: monospace; font-size: 11px;"
            />
            <p style="font-size: 11px; color: #555; margin: 10px 0 0;">The browser login fills the session JSON automatically. Amazon sessions typically last <strong>~24 hours</strong> — re-login when downloads start failing.</p>
          </div>
        {/if}
      </div>

      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p id="settings-qobuz" style="font-size: 13px; font-weight: 600; margin: 0 0 4px;">Qobuz</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 12px;">Enable direct Qobuz FLAC downloads with your own account. Antra will try TIDAL first, then Qobuz, then Soulseek.</p>

        <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin-bottom: 12px;">
          <input
            type="checkbox"
            checked={config.qobuz_enabled}
            on:change={(e) => config.qobuz_enabled = e.currentTarget.checked}
            style="margin-top: 2px;" />
          <div>
            <span style="font-weight: 500;">Enable Qobuz</span>
            <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Uses your Qobuz account for direct FLAC downloads. Email/password is the simplest setup.</p>
          </div>
        </label>

        {#if config.qobuz_enabled}
          <div style="margin-top: 12px; padding: 12px; background: rgba(255,255,255,0.03); border-radius: 8px; border: 1px solid rgba(255,255,255,0.06);">
            <label for="qobuzEmail" style="font-size: 12px; opacity: 0.7;">Qobuz Email</label>
            <input id="qobuzEmail" type="text" bind:value={config.qobuz_email} placeholder="you@example.com" style="width: 100%; box-sizing: border-box; margin-top: 6px;" />

            <label for="qobuzPassword" style="font-size: 12px; opacity: 0.7; margin-top: 10px; display: block;">Qobuz Password</label>
            <input id="qobuzPassword" type="password" bind:value={config.qobuz_password} placeholder="Your Qobuz password" style="width: 100%; box-sizing: border-box; margin-top: 6px;" />

            <p style="font-size: 10px; color: #444; text-align: center; margin: 12px 0; letter-spacing: 0.08em;">— OPTIONAL ADVANCED FIELDS —</p>

            <label for="qobuzUserAuthToken" style="font-size: 12px; opacity: 0.7;">User auth token</label>
            <input id="qobuzUserAuthToken" type="password" bind:value={config.qobuz_user_auth_token} placeholder="Optional existing Qobuz user_auth_token" style="width: 100%; box-sizing: border-box; margin-top: 6px; font-family: monospace; font-size: 12px;" />

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px;">
              <div>
                <label for="qobuzAppId" style="font-size: 12px; opacity: 0.7; display: block;">App ID</label>
                <input id="qobuzAppId" type="text" bind:value={config.qobuz_app_id} placeholder="285473059" style="width: 100%; box-sizing: border-box; margin-top: 6px; font-family: monospace; font-size: 12px;" />
              </div>
              <div>
                <label for="qobuzAppSecret" style="font-size: 12px; opacity: 0.7; display: block;">App Secret</label>
                <input id="qobuzAppSecret" type="password" bind:value={config.qobuz_app_secret} placeholder="Optional" style="width: 100%; box-sizing: border-box; margin-top: 6px; font-family: monospace; font-size: 12px;" />
              </div>
            </div>

            <p style="font-size: 11px; color: #555; margin: 10px 0 0;">If App ID / App Secret are left alone, Antra will try to refresh Qobuz app credentials automatically when needed.</p>
          </div>
        {/if}
      </div>

      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 4px;">Spotify Podcasts</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 12px;">Paste your Spotify account cookie to enable podcast episode and show downloads. Episodes are saved to <code>Podcasts/Show Name/</code> inside your music folder.</p>

        <label for="spDcInput" style="font-size: 12px; opacity: 0.7;">sp_dc cookie</label>
        <input
          id="spDcInput"
          type="password"
          bind:value={config.spotify_sp_dc}
          placeholder="AQ..."
          style="width: 100%; box-sizing: border-box; margin-top: 6px; font-family: monospace; font-size: 12px;"
        />
        <p style="font-size: 11px; color: #555; margin: 6px 0 0;">
          Get it from your browser: open <strong>open.spotify.com</strong> while logged in →
          DevTools (F12) → Application → Cookies → <code>sp_dc</code>.
          Valid for ~1 year. Rate-limited to 50 episodes/hour with a 3–7s delay between downloads.
        </p>
        {#if config.spotify_sp_dc}
          <p style="font-size: 11px; color: #00ffcc; margin: 6px 0 0;">● Cookie configured — podcast downloads enabled</p>
        {:else}
          <p style="font-size: 11px; color: #555; margin: 6px 0 0;">○ No cookie set — podcast downloads disabled</p>
        {/if}
      </div>

    </div>

    <div style="flex-shrink: 0; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.05); margin-top: 8px;">
      <p style="text-align: center; font-size: 11px; color: rgba(255,255,255,0.2); margin: 0;">Antra v1.1.4</p>
    </div>
  </div>
</div>
{/if}

<!-- ── Artist Search Results Modal ───────────────────────────────────────── -->
{#if showArtistSearch}
<div class="modal-overlay" on:click={() => { artistSearchReqId++; showArtistSearch = false; }} on:keydown={(e) => e.key === 'Escape' && (showArtistSearch = false)} role="dialog" aria-modal="true" tabindex="-1">
  <div class="modal-content" on:click|stopPropagation on:keydown|stopPropagation style="max-width: 560px; width: 100%; max-height: 75vh; display: flex; flex-direction: column;">

    <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid rgba(0,255,204,0.2); padding-bottom:14px; margin-bottom:14px; flex-shrink:0;">
      <h3 style="margin:0; color:var(--accent-color); font-size:15px;">Artist Results — "{searchQuery}"</h3>
      <button on:click={() => { artistSearchReqId++; showArtistSearch = false; }} style="padding:4px 8px; font-size:12px;">✕</button>
    </div>

    {#if artistSearchLoading}
      <p style="text-align:center; color:#777; padding:32px 0;">Searching...</p>
    {:else if artistSearchResults.length === 0}
      <p style="text-align:center; color:#777; padding:32px 0;">No artists found.</p>
    {:else}
      <div style="overflow-y:auto; flex:1; display:flex; flex-direction:column; gap:4px; padding-right:4px;">
        {#each artistSearchResults as artist (artist.artist_id)}
          <div
            on:click={() => openArtistFromSearch(artist)}
            on:keydown={(e) => e.key === 'Enter' && openArtistFromSearch(artist)}
            role="button"
            tabindex="0"
            style="display:flex; align-items:center; gap:12px; padding:10px 10px; border-radius:8px; cursor:pointer; transition:background 0.12s;"
            on:mouseenter={(e) => e.currentTarget.style.background='rgba(255,255,255,0.05)'}
            on:mouseleave={(e) => e.currentTarget.style.background='transparent'}
          >
            {#if artist.artwork_url}
              <img src={artist.artwork_url} alt="" style="width:44px; height:44px; border-radius:50%; object-fit:cover; flex-shrink:0;"/>
            {:else}
              <div style="width:44px; height:44px; border-radius:50%; background:rgba(255,255,255,0.07); flex-shrink:0; display:flex; align-items:center; justify-content:center; font-size:20px;">🎤</div>
            {/if}
            <div style="flex:1; min-width:0;">
              <div style="font-size:14px; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{artist.name}</div>
              {#if artist.genres?.length}
                <div style="font-size:11px; color:#777; margin-top:2px;">{artist.genres.slice(0, 2).join(' · ')}</div>
              {/if}
            </div>
            <div style="flex-shrink:0; display:flex; flex-direction:column; align-items:flex-end; gap:4px;">
              <span style="font-size:10px; padding:2px 7px; border-radius:99px; background:rgba(0,255,204,0.12); color:var(--accent-color);">
                {Math.round(artist.match_score * 100)}% match
              </span>
              {#if artist.followers != null}
                <span style="font-size:10px; color:#555;">{artist.followers.toLocaleString()} followers</span>
              {/if}
            </div>
          </div>
        {/each}
      </div>
    {/if}

  </div>
</div>
{/if}

<!-- ── Artist Discography Modal ───────────────────────────────────────────── -->
{#if showDiscography}
<div class="modal-overlay" on:click={() => { discographyReqId++; showDiscography = false; }}>
  <div class="modal-content" on:click|stopPropagation style="max-width: 640px; width: 100%; max-height: 80vh; display: flex; flex-direction: column;">

    <!-- Header -->
    <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid rgba(0,255,204,0.2); padding-bottom:16px; margin-bottom:16px; flex-shrink:0;">
      <div style="display:flex; align-items:center; gap:12px;">
        {#if discographyArtist?.artwork_url}
          <img src={discographyArtist.artwork_url} alt="" style="width:48px; height:48px; border-radius:50%; object-fit:cover;"/>
        {/if}
        <div>
          <h3 style="margin:0; color:var(--accent-color);">{discographyArtist?.artist_name ?? 'Loading...'}</h3>
          <p style="margin:0; font-size:11px; color:#555;">Select releases to download</p>
        </div>
      </div>
      <button on:click={() => { discographyReqId++; showDiscography = false; }} style="padding:4px 8px; font-size:12px;">✕</button>
    </div>

    {#if discographyLoading}
      <p style="text-align:center; color:#777; padding:32px 0;">Fetching discography...</p>
    {:else if discographyArtist}
      <!-- Select all / deselect all -->
      <div style="display:flex; gap:8px; margin-bottom:12px; flex-shrink:0;">
        <button on:click={() => { discographySelected = new Set(discographyArtist.albums.map(a => a.url)); }} style="font-size:12px; padding:4px 10px;">Select All</button>
        <button on:click={() => { discographySelected = new Set(); }} style="font-size:12px; padding:4px 10px;">Deselect All</button>
        <span style="margin-left:auto; font-size:12px; color:#777; align-self:center;">{discographySelected.size} selected</span>
      </div>

      <!-- Album list grouped by type -->
      <div style="overflow-y:auto; flex:1; display:flex; flex-direction:column; gap:4px; padding-right:4px;">
        {#each [['album','Albums'], ['single','Singles'], ['compilation','EPs & Compilations']] as [type, label]}
          {#if discographyArtist.albums.filter(a => a.type === type).length > 0}
            <div style="display:flex; align-items:center; justify-content:space-between; margin-top:12px; margin-bottom:4px;">
              <span style="font-size:11px; color:#555; letter-spacing:0.08em;">{label.toUpperCase()}</span>
              <div style="display:flex; gap:4px;">
                <button
                  on:click={() => { discographyArtist.albums.filter(a => a.type === type).forEach(a => discographySelected.add(a.url)); discographySelected = discographySelected; }}
                  style="font-size:10px; padding:2px 7px; opacity:0.55; border-color:rgba(255,255,255,0.1);">All</button>
                <button
                  on:click={() => { discographyArtist.albums.filter(a => a.type === type).forEach(a => discographySelected.delete(a.url)); discographySelected = discographySelected; }}
                  style="font-size:10px; padding:2px 7px; opacity:0.55; border-color:rgba(255,255,255,0.1);">None</button>
              </div>
            </div>
            {#each discographyArtist.albums.filter(a => a.type === type).sort((a, b) => (b.year ?? 0) - (a.year ?? 0) || (a.name ?? '').localeCompare(b.name ?? '')) as album (album.id)}
              <label style="display:flex; align-items:center; gap:10px; padding:6px 8px; border-radius:6px; cursor:pointer;"
                     on:mouseenter={(e) => e.currentTarget.style.background='rgba(255,255,255,0.04)'}
                     on:mouseleave={(e) => e.currentTarget.style.background='transparent'}>
                <input type="checkbox"
                  checked={discographySelected.has(album.url)}
                  on:change={() => {
                    if (discographySelected.has(album.url)) discographySelected.delete(album.url);
                    else discographySelected.add(album.url);
                    discographySelected = discographySelected;
                  }}
                  style="accent-color:var(--accent-color); width:14px; height:14px; flex-shrink:0;"
                />
                {#if album.artwork_url}
                  <img src={album.artwork_url} alt="" style="width:36px; height:36px; border-radius:4px; object-fit:cover; flex-shrink:0;"/>
                {/if}
                <div style="flex:1; min-width:0;">
                  <div style="font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{album.name}</div>
                  <div style="font-size:11px; color:#666;">{album.year ?? '—'} · {album.track_count} track{album.track_count !== 1 ? 's' : ''}</div>
                </div>
              </label>
            {/each}
          {/if}
        {/each}
      </div>

      <!-- Download button -->
      <button
        disabled={discographySelected.size === 0}
        on:click={async () => {
          const albumUrls = [...discographySelected];
          showDiscography = false;
          isDownloading = true;
          logs = [];
          trackOrder = [];
          playlistTitle = '';
          playlistArtwork = '';
          playlistArtists = '';
          playlistReleaseDate = '';
          playlistContentType = '';
          playlistQualityBadge = '';
          playlistTotalDurationMs = 0;
          playlistTotalTracks = 0;
          Object.keys(activeTracks).forEach(clearTrackInterval);
          activeTracks = {};
          separatorMeta = {};
          shouldAutoScroll = true;
          addLog('info', `━━━ Building your music library ━━━`);
          addLog('info', `Downloading ${albumUrls.length} release${albumUrls.length !== 1 ? 's' : ''} (lossless prioritized)...`);
          try { await StartDownload(albumUrls); }
          catch (err) { addLog('error', `Library engine error: ${err}`); isDownloading = false; }
        }}
        style="margin-top:16px; width:100%; flex-shrink:0;">
        Download {discographySelected.size} Release{discographySelected.size !== 1 ? 's' : ''}
      </button>
    {/if}

  </div>
</div>
{/if}

<!-- ── Source Health Popover ────────────────────────────────────────────────── -->
{#if showHealthPopover}
  {@const activeSrc = healthSources.find(s => s.key === healthPopoverSource)}
<div class="modal-overlay" on:click={() => showHealthPopover = false}>
  <div class="modal-content" on:click|stopPropagation style="max-width: 320px;">
    <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid rgba(255,255,255,0.07); padding-bottom:14px; margin-bottom:16px;">
      <div style="display:flex; align-items:center; gap:10px;">
        {#if healthPopoverSource === 'hifi'}
          <img src="/icons/tidal.webp" alt="Tidal" style="width:26px; height:26px; object-fit:contain;" />
        {:else if healthPopoverSource === 'apple'}
          <img src="/icons/apple-music.png" alt="Apple Music" style="width:26px; height:26px; object-fit:contain;" />
        {:else if healthPopoverSource === 'amazon'}
          <img src="/icons/amazon-music.jpg" alt="Amazon Music" style="width:26px; height:26px; object-fit:contain; border-radius:4px;" />
        {:else if healthPopoverSource === 'dab'}
          <img src="/icons/qobuz.png" alt="Qobuz" style="width:26px; height:26px; object-fit:contain;" />
        {/if}
        <span style="font-size:14px; font-weight:600; color:{activeSrc?.text ?? '#e2e8f0'};">{activeSrc?.label ?? ''}</span>
      </div>
      <button on:click={() => showHealthPopover = false} style="padding:4px 8px; font-size:12px;">✕</button>
    </div>

    {#if healthLoading}
      <div style="text-align:center; padding:28px 0; color:#555; font-size:13px;">Checking endpoints...</div>
    {:else}
      {@const result = healthCache[healthPopoverSource]}
      {#if result}
        <!-- Summary -->
        <div style="display:flex; align-items:baseline; gap:6px; margin-bottom:18px;">
          <span style="font-size:32px; font-weight:700; line-height:1; color:{result.live > 0 ? (activeSrc?.text ?? '#4ade80') : '#f87171'};">{result.live}</span>
          <span style="font-size:13px; color:#555;">of {result.total} servers reachable</span>
        </div>
        <!-- Dot grid — no URLs, just alive/down dots with latency on hover -->
        <div class="health-dot-grid">
          {#each result.endpoints as ep}
            <span
              class="health-status-dot"
              class:dot-alive={ep.alive}
              class:dot-dead={!ep.alive}
              title={ep.alive ? `${ep.latency_ms}ms` : 'unreachable'}
            ></span>
          {/each}
        </div>
        <p style="font-size:10px; color:#333; margin:12px 0 0; text-align:center;">Hover dots for latency</p>
      {:else}
        <div style="text-align:center; padding:24px 0; color:#555; font-size:13px;">No data yet — click to check.</div>
      {/if}
    {/if}
  </div>
</div>
{/if}

<!-- ── Audio Quality Analyzer ──────────────────────────────────────────────── -->
<svelte:window on:keydown={analyzerHandleKey} />

{#if showAnalyzer}
<div class="modal-overlay" on:click={() => { analyzerReset(); showAnalyzer = false; }}>
  <div class="analyzer-shell" on:click|stopPropagation>

    <!-- Title bar -->
    <div class="analyzer-titlebar">
      <div style="display:flex;align-items:center;gap:10px;">
        <span style="color:var(--accent-color);font-weight:600;font-size:14px;">🔬 Audio Quality Analyzer</span>
        {#if analyzerTracks.length > 0}
          <span style="font-size:11px;color:#555;">{analyzerDoneCount} / {analyzerTracks.length} analyzed</span>
        {/if}
      </div>
      <div style="display:flex;gap:6px;align-items:center;">
        {#if analyzerTracks.length > 1}
          <button class="az-btn-sm" on:click={() => analyzerViewMode = analyzerViewMode === 'gallery' ? 'single' : 'gallery'}>
            {analyzerViewMode === 'gallery' ? '⊡ Single' : '▤ Gallery'}
          </button>
        {/if}
        {#if analyzerTracks.length > 0}
          <button class="az-btn-sm" on:click={analyzerPickFiles}>+ Add Files</button>
          <button class="az-btn-sm" on:click={analyzerExportCurrent}>Export PNG</button>
        {/if}
        {#if analyzerShowExportAll}
          <button class="az-btn-sm az-btn-accent" on:click={analyzerExportAll}>
            {analyzerExportStatus || 'Export All'}
          </button>
        {/if}
        <button class="az-btn-sm" on:click={() => { analyzerReset(); showAnalyzer = false; }}>✕</button>
      </div>
    </div>

    <!-- Body -->
    <div class="analyzer-body">

      <!-- Sidebar (only when 2+ tracks) -->
      {#if analyzerShowSidebar}
      <div class="analyzer-sidebar">
        {#each analyzerTracks as track, i}
          <div
            class="az-sidebar-item"
            class:active={analyzerCurrentIndex === i}
            on:click={() => { analyzerCurrentIndex = i; if (analyzerViewMode === 'gallery') analyzerViewMode = 'single'; }}
          >
            <span class="az-track-num">{String(i + 1).padStart(2, '0')}</span>
            <span class="az-track-name" title={track.fileName}>{track.fileName}</span>
            <span class="az-status-dot"
              class:dot-pending={track.status === 'pending'}
              class:dot-analyzing={track.status === 'analyzing'}
              class:dot-done={track.status === 'done'}
              class:dot-error={track.status === 'error'}
            ></span>
          </div>
        {/each}
      </div>
      {/if}

      <!-- Main content -->
      <div class="analyzer-main">

        <!-- Drop zone (when empty) -->
        {#if analyzerTracks.length === 0}
        <div
          class="az-dropzone"
          class:drag-over={analyzerDragOver}
          on:dragover|preventDefault={() => analyzerDragOver = true}
          on:dragleave={() => analyzerDragOver = false}
          on:drop={analyzerOnDrop}
          on:click={analyzerPickFiles}
        >
          <div class="az-drop-icon">🎵</div>
          <p>Drop audio files or a folder here</p>
          <p class="az-drop-sub">Supports .flac .mp3 .m4a .aac .alac .wav .aiff .ogg</p>
          <button class="az-btn-sm az-btn-accent" style="margin-top:12px;" on:click|stopPropagation={analyzerPickFiles}>Browse Files</button>
        </div>

        <!-- Single view -->
        {:else if analyzerViewMode === 'single'}
          {@const track = analyzerTracks[analyzerCurrentIndex]}
          <div class="az-single-view"
            on:dragover|preventDefault={() => analyzerDragOver = true}
            on:dragleave={() => analyzerDragOver = false}
            on:drop={analyzerOnDrop}
          >
            <div class="az-track-header">
              <div>
                <div class="az-track-title">{track.fileName}</div>
                {#if track.status === 'done'}
                  {@const badge = analyzerQualityBadge(track)}
                  <span class="az-quality-badge" style="color:{badge.color};border-color:{badge.color}40;">{badge.label}</span>
                {/if}
              </div>
              {#if analyzerTracks.length > 1}
              <div style="display:flex;gap:8px;">
                <button class="az-nav-btn" disabled={analyzerCurrentIndex === 0} on:click={() => analyzerCurrentIndex--}>←</button>
                <span style="font-size:12px;color:#555;align-self:center;">{analyzerCurrentIndex + 1} / {analyzerTracks.length}</span>
                <button class="az-nav-btn" disabled={analyzerCurrentIndex === analyzerTracks.length - 1} on:click={() => analyzerCurrentIndex++}>→</button>
              </div>
              {/if}
            </div>

            {#if track.status === 'pending' || track.status === 'analyzing'}
              <div class="az-loading">
                <div class="az-spinner"></div>
                <span>{track.status === 'analyzing' ? 'Analyzing...' : 'Queued'}</span>
              </div>
            {:else if track.status === 'error'}
              <div class="az-error">⚠ {track.error}</div>
            {:else if track.spectrogram}
              <img src={track.spectrogram} class="az-spectrogram" alt="spectrogram" />
            {/if}

            {#if track.status === 'done'}
              <div class="az-metadata-grid">
                {#each analyzerFormatProbe(track) as row}
                  <span class="az-meta-label">{row.label}</span>
                  <span class="az-meta-value">{row.value}</span>
                {/each}
              </div>
            {/if}
          </div>

        <!-- Gallery view -->
        {:else}
          <div class="az-gallery"
            on:dragover|preventDefault={() => analyzerDragOver = true}
            on:dragleave={() => analyzerDragOver = false}
            on:drop={analyzerOnDrop}
          >
            {#each analyzerTracks as track, i}
              <div class="az-gallery-item" on:click={() => { analyzerCurrentIndex = i; analyzerViewMode = 'single'; }}>
                <div class="az-gallery-label">
                  <span class="az-track-num">{String(i + 1).padStart(2, '0')}</span>
                  <span class="az-track-name">{track.fileName}</span>
                  {#if track.status === 'done'}
                    {@const badge = analyzerQualityBadge(track)}
                    <span class="az-quality-badge" style="color:{badge.color};border-color:{badge.color}40;">{badge.label}</span>
                  {/if}
                </div>
                {#if track.status === 'pending' || track.status === 'analyzing'}
                  <div class="az-loading az-loading-sm">
                    <div class="az-spinner"></div>
                    <span>{track.status === 'analyzing' ? 'Analyzing...' : 'Queued'}</span>
                  </div>
                {:else if track.status === 'error'}
                  <div class="az-error az-error-sm">⚠ {track.error}</div>
                {:else if track.spectrogram}
                  <img src={track.spectrogram} class="az-spectrogram az-spectrogram-gallery" alt="spectrogram for {track.fileName}" />
                {/if}
              </div>
            {/each}
          </div>
        {/if}

      </div><!-- /analyzer-main -->
    </div><!-- /analyzer-body -->
  </div><!-- /analyzer-shell -->
</div>
{/if}

<style>
  :global(html),
  :global(body),
  :global(#app) {
    margin: 0;
    width: 100%;
    height: 100%;
    overflow: hidden;
  }

  main {
    display: flex;
    flex-direction: column;
    height: 100%;
    max-height: 100%;
    padding: 24px;
    background: var(--bg-color);
    box-sizing: border-box;
    min-height: 0;
    overflow: hidden;
  }
  .loading {
    justify-content: center;
    align-items: center;
    color: var(--accent-color);
  }
  .setup {
    align-items: center;
    justify-content: flex-start;
    overflow-y: auto;
    padding-top: 48px;
    padding-bottom: 48px;
  }
  .logo pre {
    color: var(--accent-color);
    font-weight: bold;
    text-align: center;
  }
  .logo p {
    text-align: center;
    opacity: 0.8;
  }
  .setup-box {
    margin-top: 32px;
    background: rgba(255, 255, 255, 0.05);
    padding: 32px;
    border-radius: 8px;
    border: 1px solid rgba(255, 255, 255, 0.1);
    max-width: 500px;
    width: 100%;
  }
  label {
    display: block;
    margin-bottom: 8px;
    font-weight: 500;
  }

  .app {
    padding: 16px;
    min-height: 0;
    overflow: hidden;
  }

  .header {
    flex-shrink: 0;
  }

  .flex-header {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
    flex-shrink: 1 !important;
  }

  /* ── Playlist header ────────────────────────────────────────────────────── */
  .playlist-header {
    display: flex;
    align-items: flex-start;
    gap: 14px;
    padding: 14px 0 10px;
    flex-shrink: 0;
  }
  .playlist-cover {
    width: 76px;
    height: 76px;
    border-radius: 6px;
    object-fit: cover;
    flex-shrink: 0;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.6);
  }
  .playlist-meta {
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
    justify-content: center;
    padding-top: 2px;
  }
  .playlist-type {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.12em;
    color: #556;
    text-transform: uppercase;
    line-height: 1;
    margin-bottom: 1px;
  }
  .playlist-title {
    font-size: 17px;
    font-weight: 700;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: #e2e8f0;
    line-height: 1.2;
    max-width: 440px;
  }
  .playlist-artists {
    font-size: 13px;
    color: #94a3b8;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 440px;
  }
  .playlist-info-line {
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 12px;
    color: #556;
    line-height: 1;
    margin-top: 1px;
    flex-wrap: wrap;
  }
  .playlist-info-sep { opacity: 0.45; }
  .playlist-quality-badge {
    display: inline-block;
    font-size: 9px;
    font-weight: 800;
    letter-spacing: 0.12em;
    color: #0a0a0a;
    background: var(--accent-color);
    padding: 2px 6px;
    border-radius: 3px;
    margin-top: 4px;
    width: fit-content;
    text-transform: uppercase;
  }

  /* ── Tracklist ──────────────────────────────────────────────────────────── */
  .tracklist-empty {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #444;
    font-size: 13px;
    text-align: center;
  }
  .tracklist-empty strong { color: #666; }

  .track-row {
    background: rgba(0, 0, 0, 0.3);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 6px;
    padding: 10px 12px;
    transition: border-color 0.25s;
  }
  .track-row.track-done  { border-color: rgba(74, 222, 128, 0.35); }
  .track-row.track-failed { border-color: rgba(248, 113, 113, 0.35); }
  .track-row.track-skipped { opacity: 0.45; }

  .track-row-main {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 6px;
  }
  .track-row-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .track-row-side {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    min-width: 0;
  }
  .track-row-name {
    font-size: 13px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 1;
    min-width: 0;
  }
  .track-row-status {
    font-size: 11px;
    opacity: 0.55;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-align: left;
    width: 100%;
  }
  .track-retry-btn {
    padding: 4px 9px;
    font-size: 11px;
    line-height: 1;
    white-space: nowrap;
    background: rgba(248, 113, 113, 0.08);
    border-color: rgba(248, 113, 113, 0.28);
    color: #fda4af;
  }
  .track-retry-btn:hover:not(:disabled) {
    background: rgba(248, 113, 113, 0.16);
    border-color: rgba(248, 113, 113, 0.45);
  }
  .track-retry-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .progress-bar-bg {
    width: 100%;
    height: 3px;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 2px;
    overflow: hidden;
  }
  .progress-bar-fg {
    height: 100%;
    background: var(--accent-color);
    transition: width 0.35s ease;
  }
  .progress-bar-fg.error { background: var(--error-color); }

  /* ── Floating log toggle ─────────────────────────────────────────────────── */
  .log-toggle {
    position: fixed;
    bottom: 20px;
    right: 20px;
    padding: 7px 14px;
    font-size: 12px;
    background: rgba(10, 10, 10, 0.85);
    border: 1px solid rgba(0, 255, 204, 0.3);
    color: var(--accent-color);
    border-radius: 20px;
    cursor: pointer;
    z-index: 55;
    backdrop-filter: blur(10px);
    display: flex;
    align-items: center;
    gap: 6px;
    letter-spacing: 0.02em;
    transition: background 0.15s, border-color 0.15s;
  }
  .log-toggle:hover {
    background: rgba(0, 255, 204, 0.08);
    border-color: rgba(0, 255, 204, 0.55);
  }

  /* ── Slide-in log panel ──────────────────────────────────────────────────── */
  .log-panel {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    width: 380px;
    background: rgba(8, 8, 8, 0.97);
    border-left: 1px solid rgba(0, 255, 204, 0.18);
    display: flex;
    flex-direction: column;
    z-index: 60;
    backdrop-filter: blur(14px);
    box-shadow: -8px 0 32px rgba(0, 0, 0, 0.6);
  }

  .log-panel-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 16px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    font-size: 13px;
    font-weight: 500;
    flex-shrink: 0;
    color: #aaa;
    letter-spacing: 0.03em;
  }

  .log-panel-body {
    flex: 1;
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .log-line {
    font-family: var(--font-mono);
    font-size: 12px;
    opacity: 0.9;
    word-wrap: break-word;
    line-height: 1.5;
  }
  .log-line.error   { color: var(--error-color); }
  .log-line.warning { color: #facc15; }
  .log-line.success { color: #4ade80; }
  .log-line.info    { color: #94a3b8; }

  .prefix {
    color: var(--accent-color);
    margin-right: 6px;
  }

  .modal-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0, 0, 0, 0.6);
    backdrop-filter: blur(4px);
    display: flex;
    justify-content: center;
    align-items: center;
    z-index: 100;
  }
  .modal-content {
    background: #111;
    border: 1px solid rgba(0, 255, 204, 0.3);
    border-radius: 8px;
    padding: 24px;
    width: 100%;
    max-width: 450px;
    box-shadow: 0 10px 25px rgba(0,0,0,0.5);
  }
  .history-card {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 6px;
    padding: 12px;
  }

  /* ── Settings modal ─────────────────────────────────────────────────────── */
  .settings-modal { max-width: 520px; }

  .settings-section {
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 8px;
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .settings-section-title {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: #555;
    font-weight: 600;
    margin-bottom: 4px;
    display: flex;
    align-items: center;
  }

  .format-pill {
    display: inline-flex;
    align-items: center;
    padding: 5px 12px;
    border-radius: 20px;
    border: 1px solid rgba(255,255,255,0.1);
    font-size: 12px;
    color: #666;
    cursor: pointer;
    transition: all 0.15s;
    user-select: none;
  }
  .format-pill:hover { border-color: rgba(0,255,204,0.3); color: #aaa; }
  .format-pill.active {
    border-color: rgba(0,255,204,0.5);
    background: rgba(0,255,204,0.08);
    color: var(--accent-color);
  }

  /* ── Audio Quality Analyzer ──────────────────────────────────────────────── */
  .analyzer-shell {
    width: 92vw;
    max-width: 1200px;
    height: 88vh;
    display: flex;
    flex-direction: column;
    background: #0d0d0d;
    border: 1px solid rgba(0,255,204,0.15);
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 0 0 60px rgba(0,0,0,0.8);
  }

  .analyzer-titlebar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    background: rgba(255,255,255,0.02);
    flex-shrink: 0;
  }

  .analyzer-body {
    display: flex;
    flex: 1;
    overflow: hidden;
  }

  /* Sidebar */
  .analyzer-sidebar {
    width: 240px;
    flex-shrink: 0;
    border-right: 1px solid rgba(255,255,255,0.06);
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 1px;
    padding: 4px 0;
  }

  .az-sidebar-item {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 7px 10px;
    cursor: pointer;
    border-radius: 4px;
    margin: 0 4px;
    transition: background 0.12s;
    font-size: 11px;
  }
  .az-sidebar-item:hover { background: rgba(255,255,255,0.05); }
  .az-sidebar-item.active { background: rgba(0,255,204,0.08); }

  .az-track-num {
    color: #444;
    font-family: monospace;
    flex-shrink: 0;
    font-size: 10px;
  }
  .az-track-name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: #aaa;
    font-size: 11px;
  }
  .az-sidebar-item.active .az-track-name { color: #e2e8f0; }

  .az-status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .dot-pending  { background: #333; }
  .dot-analyzing { background: #facc15; animation: pulse-dot 1s infinite; }
  .dot-done     { background: #00ffcc; }
  .dot-error    { background: #f87171; }
  @keyframes pulse-dot {
    0%,100% { opacity: 1; }
    50%      { opacity: 0.4; }
  }

  /* Main area */
  .analyzer-main {
    flex: 1;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  /* Drop zone */
  .az-dropzone {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    border: 2px dashed rgba(0,255,204,0.15);
    border-radius: 8px;
    margin: 24px;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
    color: #555;
    text-align: center;
    padding: 40px;
  }
  .az-dropzone:hover, .az-dropzone.drag-over {
    border-color: rgba(0,255,204,0.4);
    background: rgba(0,255,204,0.03);
    color: #888;
  }
  .az-drop-icon { font-size: 40px; margin-bottom: 12px; }
  .az-drop-sub  { font-size: 12px; color: #444; margin-top: 4px; }

  /* Single view */
  .az-single-view {
    flex: 1;
    overflow-y: auto;
    padding: 16px 20px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .az-track-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }
  .az-track-title {
    font-size: 13px;
    font-weight: 500;
    color: #e2e8f0;
    word-break: break-all;
  }

  .az-quality-badge {
    display: inline-block;
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 10px;
    border: 1px solid;
    margin-top: 4px;
    letter-spacing: 0.04em;
  }

  .az-spectrogram {
    width: 100%;
    border-radius: 6px;
    image-rendering: crisp-edges;
    display: block;
  }
  .az-spectrogram-gallery {
    width: 100%;
  }

  /* Metadata grid */
  .az-metadata-grid {
    display: grid;
    grid-template-columns: 100px 1fr;
    gap: 4px 12px;
    font-size: 12px;
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 6px;
    padding: 12px 14px;
    background: rgba(255,255,255,0.02);
  }
  .az-meta-label { color: #555; }
  .az-meta-value { color: #94a3b8; font-family: monospace; font-size: 11px; }

  /* Loading */
  .az-loading {
    display: flex;
    align-items: center;
    gap: 10px;
    color: #555;
    font-size: 13px;
    padding: 40px 0;
    justify-content: center;
  }
  .az-loading-sm { padding: 16px 0; font-size: 12px; }
  .az-spinner {
    width: 16px;
    height: 16px;
    border: 2px solid rgba(0,255,204,0.2);
    border-top-color: var(--accent-color);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Error */
  .az-error {
    color: #f87171;
    font-size: 12px;
    padding: 16px;
    background: rgba(248,113,113,0.05);
    border-radius: 6px;
    border: 1px solid rgba(248,113,113,0.15);
  }
  .az-error-sm { padding: 8px 12px; }

  /* Gallery view */
  .az-gallery {
    flex: 1;
    overflow-y: auto;
    padding: 12px 16px;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  .az-gallery-item {
    display: flex;
    flex-direction: column;
    gap: 8px;
    cursor: pointer;
    border-radius: 6px;
    padding: 10px;
    border: 1px solid rgba(255,255,255,0.04);
    transition: border-color 0.15s, background 0.15s;
  }
  .az-gallery-item:hover {
    border-color: rgba(0,255,204,0.2);
    background: rgba(0,255,204,0.02);
  }

  .az-gallery-label {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11px;
  }

  /* Navigation buttons */
  .az-nav-btn {
    padding: 4px 10px;
    font-size: 14px;
    background: rgba(255,255,255,0.05);
    border-color: rgba(255,255,255,0.1);
  }
  .az-nav-btn:disabled { opacity: 0.25; cursor: not-allowed; }

  /* Small buttons in toolbar */
  .az-btn-sm {
    padding: 4px 10px;
    font-size: 12px;
    background: rgba(255,255,255,0.05);
    border-color: rgba(255,255,255,0.1);
  }
  .az-btn-accent {
    background: rgba(0,255,204,0.1);
    border-color: rgba(0,255,204,0.3);
    color: var(--accent-color);
  }

  /* ── Sponsor toast ───────────────────────────────────────────────────────── */
  .sponsor-toast {
    position: fixed;
    top: 58px;
    right: 16px;
    width: 260px;
    background: rgba(14, 10, 10, 0.96);
    border: 1px solid rgba(255, 94, 91, 0.25);
    border-radius: 10px;
    padding: 14px 14px 14px 12px;
    display: flex;
    gap: 10px;
    align-items: flex-start;
    z-index: 200;
    box-shadow: 0 8px 28px rgba(0, 0, 0, 0.55);
    backdrop-filter: blur(16px);
    animation: toast-in 0.35s cubic-bezier(0.22, 1, 0.36, 1) both;
  }
  .sponsor-toast.leaving {
    animation: toast-out 0.45s cubic-bezier(0.55, 0, 1, 0.45) both;
  }
  @keyframes toast-in {
    from { opacity: 0; transform: translateY(-10px) scale(0.96); }
    to   { opacity: 1; transform: translateY(0)    scale(1);    }
  }
  @keyframes toast-out {
    from { opacity: 1; transform: translateY(0)     scale(1);    }
    to   { opacity: 0; transform: translateY(-18px) scale(0.88); }
  }

  .sponsor-toast-icon {
    flex-shrink: 0;
    margin-top: 1px;
    opacity: 0.9;
  }

  .sponsor-toast-body {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 5px;
  }

  .sponsor-toast-title {
    margin: 0;
    font-size: 13px;
    font-weight: 600;
    color: #e2e8f0;
    line-height: 1.2;
  }

  .sponsor-toast-text {
    margin: 0;
    font-size: 11px;
    color: #6b7280;
    line-height: 1.55;
  }

  .sponsor-toast-btn {
    margin-top: 6px;
    padding: 5px 11px;
    font-size: 11px;
    font-weight: 600;
    background: rgba(255, 94, 91, 0.12);
    border: 1px solid rgba(255, 94, 91, 0.35);
    border-radius: 20px;
    color: #FF5E5B;
    cursor: pointer;
    align-self: flex-start;
    transition: background 0.15s, border-color 0.15s;
    letter-spacing: 0.02em;
  }
  .sponsor-toast-btn:hover {
    background: rgba(255, 94, 91, 0.22);
    border-color: rgba(255, 94, 91, 0.6);
  }

  .sponsor-toast-close {
    flex-shrink: 0;
    background: transparent;
    border: none;
    color: #444;
    font-size: 16px;
    line-height: 1;
    padding: 0;
    cursor: pointer;
    margin-top: -2px;
    transition: color 0.15s;
  }
  .sponsor-toast-close:hover { color: #888; }

  /* ── Ko-fi icon hover tooltip ────────────────────────────────────────────── */
  .kofi-wrap {
    position: relative;
    display: flex;
    align-items: center;
  }

  .kofi-tooltip {
    position: absolute;
    top: calc(100% + 10px);
    right: 0;
    width: 220px;
    background: rgba(14, 10, 10, 0.97);
    border: 1px solid rgba(255, 94, 91, 0.22);
    border-radius: 9px;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    z-index: 300;
    box-shadow: 0 6px 22px rgba(0, 0, 0, 0.55);
    backdrop-filter: blur(14px);
    animation: tooltip-in 0.18s ease both;
  }
  @keyframes tooltip-in {
    from { opacity: 0; transform: translateY(-4px); }
    to   { opacity: 1; transform: translateY(0);    }
  }

  .kofi-tooltip-title {
    margin: 0;
    font-size: 12px;
    font-weight: 600;
    color: #e2e8f0;
  }

  .kofi-tooltip-body {
    margin: 0;
    font-size: 11px;
    color: #6b7280;
    line-height: 1.5;
  }

  .kofi-tooltip-btn {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 600;
    background: rgba(255, 94, 91, 0.1);
    border: 1px solid rgba(255, 94, 91, 0.3);
    border-radius: 20px;
    color: #FF5E5B;
    cursor: pointer;
    align-self: flex-start;
    transition: background 0.15s;
    letter-spacing: 0.02em;
  }
  .kofi-tooltip-btn:hover {
    background: rgba(255, 94, 91, 0.2);
  }

  .support-close-btn {
    background: transparent;
    border: none;
    color: #7c8a8a;
    font-size: 16px;
    padding: 0;
    line-height: 1;
    min-width: 16px;
  }
  .support-close-btn:hover { color: #cbd5e1; }

  .support-progress-wrap {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-top: 4px;
  }

  .support-progress-head,
  .support-progress-foot {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    font-size: 11px;
    color: #94a3b8;
  }

  .support-progress-bar {
    width: 100%;
    height: 8px;
    border-radius: 999px;
    background: rgba(255,255,255,0.08);
    overflow: hidden;
    border: 1px solid rgba(255,255,255,0.06);
  }

  .support-progress-fill {
    height: 100%;
    background: linear-gradient(90deg, #FF5E5B, #ff8d62);
    border-radius: inherit;
    transition: width 0.25s ease;
  }

  .support-progress-toast {
    margin: 6px 0 2px;
  }

  /* ── Source health bar + format selector ────────────────────────────────── */
  .source-health-bar {
    display: flex;
    gap: 6px;
    margin-top: 8px;
    align-items: center;
  }

  .format-selector {
    display: flex;
    gap: 4px;
    align-items: center;
    margin-left: auto;
  }

  .format-pill {
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 3px 8px;
    border-radius: 4px;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.1);
    color: rgba(255,255,255,0.38);
    cursor: pointer;
    transition: all 0.12s;
  }
  .format-pill:hover { background: rgba(255,255,255,0.08); color: rgba(255,255,255,0.7); }
  .format-pill.active { background: rgba(0,255,204,0.12); border-color: var(--accent-color); color: var(--accent-color); }

  .health-chip {
    display: inline-flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    padding: 5px 10px 4px;
    border-radius: 10px;
    border: 1.5px solid rgba(255,255,255,0.1);
    font-size: 11px;
    cursor: pointer;
    transition: box-shadow 0.2s, border-color 0.2s, opacity 0.15s;
    letter-spacing: 0.02em;
    white-space: nowrap;
    min-width: 48px;
  }
  .health-chip:hover { filter: brightness(1.15); }

  /* Enabled chip: solid green border + layered glow */
  .health-chip-enabled {
    border-color: #22c55e !important;
    box-shadow:
      0 0 0 1px rgba(34,197,94,0.5),
      0 0 10px rgba(34,197,94,0.35),
      0 0 22px rgba(34,197,94,0.15);
  }
  .health-chip-enabled:hover {
    box-shadow:
      0 0 0 1px rgba(34,197,94,0.75),
      0 0 14px rgba(34,197,94,0.5),
      0 0 28px rgba(34,197,94,0.25);
    filter: brightness(1.12);
  }

  /* Disabled chip: dark red outline, dimmed, transparent bg */
  .health-chip-disabled {
    opacity: 0.5;
    border-color: #7a2020 !important;
    box-shadow: none;
  }
  .health-chip-disabled:hover {
    opacity: 0.85;
    filter: none;
    border-color: #e05555 !important;
    box-shadow: 0 0 0 1px rgba(220,50,50,0.3);
  }

  /* "on" / "off" status badges above the icon */
  .health-chip-on-badge {
    font-size: 8px;
    font-weight: 800;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #22c55e;
    line-height: 1;
  }
  .health-chip-off-badge {
    font-size: 8px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: rgba(255,255,255,0.28);
    line-height: 1;
  }

  .health-chip-count { font-weight: 700; font-size: 10px; line-height: 1; }
  .health-chip-idle { opacity: 0.3; font-size: 10px; line-height: 1; }
  .health-chip-icon { width: 20px; height: 20px; object-fit: contain; flex-shrink: 0; transition: opacity 0.2s; }

  /* ── Health popover dot grid ─────────────────────────────────────────────── */
  .health-dot-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .health-status-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    cursor: default;
    transition: transform 0.1s;
  }
  .health-status-dot:hover { transform: scale(1.4); }
  .dot-alive {
    background: #4ade80;
    box-shadow: 0 0 5px #4ade8066;
  }
  .dot-dead {
    background: #3a1a1a;
    border: 1px solid #553333;
  }

  /* ── Tracklist wrapper + scroll arrow ────────────────────────────────────── */
  .tracklist-wrapper {
    position: relative;
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }

  .tracklist {
    flex: 1;
    min-height: 0;
    margin-top: 16px;
    overflow-y: auto;
    overscroll-behavior: contain;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .tracklist-jump-btn {
    position: absolute;
    bottom: 12px;
    left: 50%;
    transform: translateX(-50%);
    width: 30px;
    height: 30px;
    border-radius: 50%;
    background: rgba(10, 10, 10, 0.25);
    border: 1px solid rgba(0, 255, 204, 0.2);
    color: rgba(0, 255, 204, 0.5);
    font-size: 15px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    z-index: 10;
    backdrop-filter: blur(4px);
    padding: 0;
    line-height: 1;
    transition: background 0.2s, border-color 0.2s, color 0.2s;
  }
  .tracklist-jump-btn:hover {
    background: rgba(10, 10, 10, 0.7);
    border-color: rgba(0, 255, 204, 0.55);
    color: var(--accent-color);
  }

  /* ── Log panel scroll arrow ──────────────────────────────────────────────── */
  .log-jump-btn {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    background: rgba(0, 255, 204, 0.08);
    border: 1px solid rgba(0, 255, 204, 0.3);
    color: var(--accent-color);
    font-size: 13px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    padding: 0;
    line-height: 1;
    transition: background 0.15s;
    flex-shrink: 0;
  }
  .log-jump-btn:hover { background: rgba(0, 255, 204, 0.18); }

  /* ── Album separator row in tracklist ────────────────────────────────────── */
  .tracklist-album-sep {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 4px 4px;
    margin-top: 6px;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
  }
  .sep-artwork {
    width: 28px;
    height: 28px;
    border-radius: 3px;
    object-fit: cover;
    flex-shrink: 0;
    opacity: 0.8;
  }
  .sep-title {
    font-size: 12px;
    font-weight: 600;
    color: #556;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* ── Downloaded music player ─────────────────────────────────────────────── */
  .downloaded-modal {
    max-width: 1100px;
    width: min(1100px, 96vw);
    max-height: min(88vh, 900px);
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .downloaded-modal-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
    border-bottom: 1px solid rgba(0,255,204,0.16);
    padding-bottom: 14px;
  }

  .downloaded-tabs {
    display: flex;
    gap: 8px;
  }

  .downloaded-tabs button {
    padding: 6px 12px;
    font-size: 12px;
    opacity: 0.65;
  }

  .downloaded-tabs button.active-tab {
    opacity: 1;
    border-color: var(--accent-color);
    background: rgba(0, 255, 204, 0.14);
  }

  .downloaded-layout {
    flex: 1;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(260px, 320px) minmax(0, 1fr);
    gap: 16px;
  }

  .downloaded-library-pane,
  .downloaded-detail-pane {
    min-height: 0;
    border: 1px solid rgba(255,255,255,0.08);
    background: rgba(255,255,255,0.02);
    border-radius: 10px;
    overflow: hidden;
  }

  .downloaded-list {
    height: 100%;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
  }

  .downloaded-card {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 12px;
    text-align: left;
    border-color: rgba(255,255,255,0.08);
    background: rgba(255,255,255,0.02);
    color: inherit;
    padding: 10px;
  }

  .downloaded-card.selected {
    border-color: rgba(0,255,204,0.35);
    background: rgba(0,255,204,0.08);
  }

  .downloaded-card-art,
  .downloaded-card-placeholder {
    width: 54px;
    height: 54px;
    border-radius: 8px;
    flex-shrink: 0;
  }

  .downloaded-card-art,
  .downloaded-release-art {
    object-fit: cover;
  }

  .downloaded-card-placeholder,
  .downloaded-release-placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(255,255,255,0.06);
    color: rgba(255,255,255,0.5);
  }

  .downloaded-card-copy,
  .downloaded-release-copy {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .downloaded-card-title,
  .downloaded-track-title {
    color: #d7f8f5;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .downloaded-card-meta,
  .downloaded-track-meta,
  .downloaded-release-copy p,
  .downloaded-player-meta {
    font-size: 12px;
    color: rgba(210, 230, 230, 0.58);
  }

  .downloaded-detail-pane {
    display: flex;
    flex-direction: column;
  }

  .downloaded-release-hero {
    display: flex;
    gap: 16px;
    padding: 16px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    background: linear-gradient(180deg, rgba(0,255,204,0.05), rgba(255,255,255,0.01));
  }

  .downloaded-release-art,
  .downloaded-release-placeholder {
    width: 120px;
    height: 120px;
    border-radius: 12px;
    flex-shrink: 0;
  }

  .downloaded-release-type {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: rgba(0,255,204,0.7);
  }

  .downloaded-release-copy h2 {
    margin: 0;
    color: #e7fffd;
    font-size: 28px;
  }

  .downloaded-tracks {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 8px 12px 12px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .downloaded-track-row {
    width: 100%;
    display: grid;
    grid-template-columns: 58px minmax(0, 1fr) auto;
    align-items: center;
    gap: 12px;
    text-align: left;
    background: rgba(255,255,255,0.02);
    border-color: rgba(255,255,255,0.06);
    color: inherit;
    padding: 10px 12px;
  }

  .downloaded-track-row.is-active {
    background: rgba(0,255,204,0.08);
    border-color: rgba(0,255,204,0.3);
  }

  .downloaded-track-index,
  .downloaded-track-duration {
    font-size: 12px;
    color: rgba(0,255,204,0.72);
  }

  .downloaded-track-copy {
    min-width: 0;
  }

  .downloaded-empty {
    height: 100%;
    min-height: 220px;
    display: flex;
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: 20px;
    color: rgba(210, 230, 230, 0.55);
  }

  .downloaded-player {
    display: grid;
    grid-template-columns: minmax(0, 1.4fr) auto minmax(220px, 1fr) 140px;
    gap: 14px;
    align-items: center;
    border-top: 1px solid rgba(0,255,204,0.16);
    padding-top: 14px;
  }

  .downloaded-player-title {
    color: #f0fffe;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .downloaded-player-controls {
    display: flex;
    gap: 8px;
  }

  .downloaded-player-timeline,
  .downloaded-player-volume {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    color: rgba(210, 230, 230, 0.58);
  }

  .downloaded-player-timeline input,
  .downloaded-player-volume input {
    width: 100%;
  }

  @media (max-width: 900px) {
    .downloaded-modal {
      width: min(96vw, 96vw);
      max-height: 92vh;
    }

    .downloaded-layout {
      grid-template-columns: 1fr;
    }

    .downloaded-player {
      grid-template-columns: 1fr;
    }

    .downloaded-release-hero {
      flex-direction: column;
      align-items: flex-start;
    }
  }

  /* Discovery Tab Styles */

  /* ── Discover full-screen overlay ──────────────────────────────────────── */
  .discover-overlay {
    position: fixed;
    inset: 0;
    z-index: 200;
    background: var(--bg-color, #0d0d0d);
    display: flex;
    flex-direction: column;
    animation: fadeIn 0.18s ease-out;
  }

  .discover-topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 20px;
    border-bottom: 1px solid rgba(255,255,255,0.07);
    flex-shrink: 0;
    gap: 12px;
    background: rgba(255,255,255,0.02);
  }

  .discover-topbar-left {
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 1;
    min-width: 0;
  }

  .discover-topbar-title {
    font-size: 15px;
    font-weight: 700;
    color: var(--accent-color);
    letter-spacing: 0.02em;
    white-space: nowrap;
    margin-right: 4px;
  }

  .discover-select {
    padding: 5px 10px;
    border-radius: 4px;
    border: 1px solid rgba(255,255,255,0.1);
    background: var(--surface-light, rgba(255,255,255,0.05));
    color: white;
    font-family: inherit;
    font-size: 12px;
  }

  .discover-select-genre {
    flex: 1;
    min-width: 0;
  }

  .discover-refresh-btn {
    padding: 5px 12px;
    font-size: 12px;
    white-space: nowrap;
  }

  .discover-close-btn {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.1);
    color: rgba(255,255,255,0.7);
    border-radius: 6px;
    padding: 5px 12px;
    font-size: 14px;
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.12s, color 0.12s;
  }
  .discover-close-btn:hover {
    background: rgba(255,255,255,0.12);
    color: #fff;
  }

  .discover-body {
    flex: 1;
    overflow-y: auto;
    padding: 24px 20px 32px;
    min-height: 0;
  }

  .discover-loading {
    padding: 80px 20px;
    text-align: center;
    color: rgba(255,255,255,0.4);
    font-family: var(--font-mono);
    letter-spacing: 0.05em;
  }

  .discover-empty {
    padding: 80px 20px;
    text-align: center;
    color: rgba(255,255,255,0.3);
    font-style: italic;
  }

  .discover-sections {
    display: flex;
    flex-direction: column;
    gap: 40px;
    animation: fadeIn 0.3s ease-out;
  }

  .discover-section {}

  .discover-section-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 18px;
  }

  .discover-section-title {
    margin: 0;
    font-size: 16px;
    font-weight: 700;
    color: var(--accent-color);
    letter-spacing: 0.02em;
    white-space: nowrap;
  }

  .discover-section-rule {
    flex: 1;
    height: 1px;
    background: linear-gradient(90deg, rgba(255,255,255,0.1), transparent);
  }

  .discover-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 18px;
  }

  .discovery-card {
    cursor: pointer;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 12px;
    padding: 12px;
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    display: flex;
    flex-direction: column;
    gap: 10px;
    height: 100%;
    box-sizing: border-box;
  }

  .discovery-card:hover {
    background: rgba(255, 255, 255, 0.08);
    border-color: var(--accent-color);
    transform: translateY(-4px);
    box-shadow: 0 10px 20px rgba(0, 0, 0, 0.3);
  }

  .discovery-artwork-wrapper {
    position: relative;
    width: 100%;
    aspect-ratio: 1;
    border-radius: 8px;
    overflow: hidden;
    background: rgba(0, 0, 0, 0.2);
  }

  .discovery-artwork {
    width: 100%;
    height: 100%;
    object-fit: cover;
    transition: transform 0.5s ease;
  }

  .discovery-card:hover .discovery-artwork {
    transform: scale(1.08);
  }

  .discovery-play-overlay {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.4);
    display: flex;
    align-items: center;
    justify-content: center;
    opacity: 0;
    transition: opacity 0.3s ease;
    backdrop-filter: blur(2px);
  }

  .discovery-card:hover .discovery-play-overlay {
    opacity: 1;
  }

  .discovery-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .discovery-title {
    font-size: 13px;
    font-weight: 600;
    color: #fff;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .discovery-subtitle {
    font-size: 11.5px;
    color: rgba(255, 255, 255, 0.5);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(10px); }
    to { opacity: 1; transform: translateY(0); }
  }
</style>
