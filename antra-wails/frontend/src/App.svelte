<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { GetConfig, SaveConfig, PickDirectory, StartDownload, RetryTrackDownload, CancelDownload, GetHistory, AddHistory, ClearHistory, ValidateTidalAuth, StartTidalOAuthLogin, StartAppleBrowserLogin, StartAmazonBrowserLogin, ConfirmAmazonLogin, CaptureSpDC } from '../wailsjs/go/main/App.js';
  import { ScanFolder, AnalyzeAudio, PickAnalyzerFiles, WriteFile, GetArtistDiscography, SearchArtists, GetSlskdWebUIInfo, GetDownloadedMusicLibrary, GetDownloadedRelease, GetSupportStatus, GetAlbumAvailability, GetSpotifyLibrary, GetAppleMusicLibrary, RunAutoSync, GetTrackLyrics, GetKeyInfo, ValidateKey, StartDeviceLogin, PollDeviceLogin, SignOutDevice, RenewDeviceTokenIfNeeded, GetDeviceAccountStatus } from '../wailsjs/go/main/App.js';
  import { EventsOn, BrowserOpenURL, ClipboardGetText } from '../wailsjs/runtime/runtime.js';
  import type { main } from '../wailsjs/go/models';
  import AntraLogo from './AntraLogo.svelte';
  import { playSaved, playFinished, preview as previewSfx, type SfxName } from './lib/sfx';

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
    amazon_region: 'us',
    qobuz_enabled: false,
    qobuz_email: '',
    qobuz_password: '',
    qobuz_app_id: '285473059',
    qobuz_app_secret: '',
    qobuz_user_auth_token: '',
    deezer_arl_token: '',
    deezer_bf_secret: 'g4el58wc0zvf9na1',
    output_format: 'lossless',
    max_retries: 3,
    library_mode: 'smart_dedup',
    prefer_explicit: true,
    folder_structure: 'standard',
    album_folder_structure: 'standard',
    playlist_folder_structure: 'standard',
    playlist_storage_mode: 'playlist_folder',
    single_track_structure: 'album_numbered',
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
    antra_api_key: '',
    antra_device_token: '',
    theme: '',
    font: '',
    notify_sound: 'crystal',
    notify_volume: 70,
    notify_batch_only: false,
    strict_matching: false,
    strict_format: true,
    prevent_lossy_transcode: true,
    write_antra_tags: true,
    download_source: 'auto',
    download_sources: ['auto'],
    save_cover_art_sidecar: false,
    single_track_filename_template: '{artist} - {title}',
    album_track_filename_template: '{track} - {title}',
    folder_structure_template: '{album_artist}/{year} - {album}',
    multi_disc_handling: '',
    track_number_padding: 2,
    illegal_character_replacement: '_',
    whitespace_handling: 'keep',
    fetch_lyrics: true,
    filename_conflict_behavior: 'skip',
    auto_sync_enabled: false,
    auto_sync_hour: 6,
    auto_sync_minute: 0,
    auto_sync_days: 127,
    tracked_playlists: [],
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
  let spDcCapture: BrowserLoginState = { phase: 'idle' };

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

  // ── Theme system ────────────────────────────────────────────────────────────
  const THEMES = [
    { id: 'antra',        label: 'Antra',        cat: 'original', desc: 'Midnight jade with polished glass surfaces and a vivid aqua edge.',  colors: ['#081412','#102320','#37e2c2'], icon: null, preview: 'linear-gradient(145deg, #081412 0%, #102320 56%, #183632 100%)', tone: '#dffdf6' },
    { id: 'linen',        label: 'Linen',        cat: 'original', desc: 'A proper light theme with soft paper whites and brass-green accents.', colors: ['#f6f1e7','#ebe2d2','#6c8a62'], icon: null, preview: 'linear-gradient(145deg, #f8f4ec 0%, #efe7d7 58%, #e5d7bd 100%)', tone: '#233126' },
    { id: 'ember',        label: 'Ember',        cat: 'original', desc: 'Burnished copper, smoked cacao, and warm lounge lighting.',           colors: ['#16100d','#231914','#cf7a3a'], icon: null, preview: 'linear-gradient(145deg, #16100d 0%, #231914 52%, #3b2318 100%)', tone: '#fff0e4' },
    { id: 'ocean',        label: 'Ocean',        cat: 'original', desc: 'Ink-blue depth with steel-blue highlights and cool contrast.',        colors: ['#07111d','#102033','#71a8e8'], icon: null, preview: 'linear-gradient(145deg, #07111d 0%, #102033 54%, #18314b 100%)', tone: '#edf6ff' },
    { id: 'graphite',     label: 'Graphite',     cat: 'original', desc: 'Refined monochrome with silver ink and gallery-style restraint.',    colors: ['#101113','#1a1c20','#b7bcc6'], icon: null, preview: 'linear-gradient(145deg, #101113 0%, #1a1c20 55%, #2a2d34 100%)', tone: '#f3f5f8' },
    { id: 'sunset',       label: 'Sunset',       cat: 'original', desc: 'Plum dusk, rose glow, and a softer golden finish.',                  colors: ['#150d1a','#24132b','#e091a3'], icon: null, preview: 'linear-gradient(145deg, #150d1a 0%, #24132b 54%, #4d2434 100%)', tone: '#fff0f5' },
    { id: 'spotify',      label: 'Spotify',      cat: 'service',  desc: 'Closer to Spotify itself: charcoal layers, sharp type, green only on action.', colors: ['#121212','#1b1b1b','#1ed760'], icon: null, preview: 'linear-gradient(145deg, #121212 0%, #181818 54%, #242424 100%)', tone: '#ffffff' },
    { id: 'tidal',        label: 'TIDAL',        cat: 'service',  desc: 'Luxury black-and-white with a restrained cool-aqua highlight.',      colors: ['#050607','#0d1014','#8ae5ff'], icon: '/icons/tidal.webp', preview: 'linear-gradient(145deg, #050607 0%, #0d1014 58%, #171d24 100%)', tone: '#f8fbff' },
    { id: 'qobuz',        label: 'Qobuz',        cat: 'service',  desc: 'Editorial navy, hi-fi blue, and a more premium liner-note feel.',    colors: ['#08101c','#0f1a2b','#3d7fe3'], icon: '/icons/qobuz.png', preview: 'linear-gradient(145deg, #08101c 0%, #0f1a2b 54%, #173253 100%)', tone: '#eef5ff' },
    { id: 'deezer',       label: 'Deezer',       cat: 'service',  desc: 'Deezer’s neon energy, but cleaner: aubergine depth with electric violet.', colors: ['#120d18','#1d1526','#a238ff'], icon: '/icons/deezer.webp', preview: 'linear-gradient(145deg, #120d18 0%, #1d1526 52%, #34204a 100%)', tone: '#f7efff' },
    { id: 'apple-music',  label: 'Apple Music',  cat: 'service',  desc: 'Apple Music with bright porcelain surfaces instead of another dark clone.', colors: ['#fbfbfd','#ececf1','#fa2d5d'], icon: '/icons/apple-music.png', preview: 'linear-gradient(145deg, #ffffff 0%, #f4f4f8 55%, #ebecef 100%)', tone: '#1d1d21' },
    { id: 'amazon-music', label: 'Amazon Music', cat: 'service',  desc: 'Amazon Music blue, modern slate panels, and a cleaner streaming-app look.', colors: ['#0b1220','#101a2b','#18a8ff'], icon: '/icons/amazon-music.jpg', preview: 'linear-gradient(145deg, #0b1220 0%, #101a2b 56%, #17365b 100%)', tone: '#edf6ff' },
  ];
  let showThemes = false;
  function applyTheme(id: string) {
    document.documentElement.setAttribute('data-theme', id || 'antra');
    config.theme = id;
    showThemes = false;
    SaveConfig(config);
  }

  // ── Typeface (v1.1.8 FEAT-10) ───────────────────────────────────────────────
  // Both non-system faces are bundled, so every option works offline.
  const FONTS = [
    { id: 'antra',      label: 'Antra Mono',   desc: 'The default. Fixed-width, keeps the activity log in neat columns.',   css: "'Fira Code','JetBrains Mono','Consolas',monospace" },
    { id: 'system',     label: 'System',       desc: 'Follows your OS interface font — Segoe UI on Windows, SF on macOS.',  css: "system-ui,-apple-system,'Segoe UI',Roboto,sans-serif" },
    { id: 'nunito',     label: 'Nunito',       desc: 'Rounded and soft. Easier for long browsing sessions.',                css: "'Nunito',system-ui,sans-serif" },
    { id: 'dyslexic',   label: 'OpenDyslexic', desc: 'Weighted letter bottoms to reduce swapping. Applies everywhere, including the log.', css: "'OpenDyslexic','Comic Sans MS',sans-serif" },
  ];
  // ── Notification sounds (v1.1.8 FEAT-13) ────────────────────────────────────
  const SOUNDS = [
    { id: 'crystal', icon: '✦', label: 'Crystal',  desc: 'FM bell with a soft sub and a short plate tail. The default.' },
    { id: 'pluck',   icon: '♪', label: 'Pluck',    desc: 'Warm filtered string. Subtle enough for long album runs.' },
    { id: 'blip',    icon: '•', label: 'Blip',     desc: 'Two quick ticks. Minimal and quiet.' },
    { id: 'off',     icon: '⏻', label: 'Off',      desc: 'No sound at all.' },
  ];
  function chooseSound(id: string) {
    config.notify_sound = id;
    config = { ...config };
    if (id !== 'off') previewSfx(id as SfxName, config.notify_volume ?? 70);
    SaveConfig(config);
  }
  let sfxVolumeSaveTimer: ReturnType<typeof setTimeout>;
  function onVolumeChange() {
    // Preview on release so dragging the slider is audible but not a stutter of
    // overlapping voices, and debounce the disk write.
    clearTimeout(sfxVolumeSaveTimer);
    sfxVolumeSaveTimer = setTimeout(() => {
      if (config.notify_sound && config.notify_sound !== 'off') {
        previewSfx((config.notify_sound || 'crystal') as SfxName, config.notify_volume ?? 70);
      }
      SaveConfig(config);
    }, 260);
  }

  // 'antra' writes no attribute at all, so an install that has never touched
  // this setting renders exactly as it did before the feature existed.
  function setFontAttr(id: string) {
    const el = document.documentElement;
    if (!id || id === 'antra') el.removeAttribute('data-font');
    else el.setAttribute('data-font', id);
  }
  // Separate from setFontAttr so startup can restore the face without writing
  // config.json back on every launch.
  function applyFont(id: string) {
    setFontAttr(id);
    config.font = id;
    SaveConfig(config);
  }

  // ── Filename template system ────────────────────────────────────────────────
  const TEMPLATE_DEMO = {
    title: 'Come Together',
    artist: 'The Beatles',
    album_artist: 'The Beatles',
    album: 'Abbey Road',
    year: '1969',
    track: '07',
    disc: '1',
    genre: 'Rock',
    composer: 'Lennon-McCartney',
    isrc: 'GBAYE6800032',
    codec: 'flac',
    bitrate: '1411',
    quality: 'LOSSLESS',
  };

  function renderPreview(template: string): string {
    if (!template) return '';
    return template.replace(/\{(\w+)\}/gi, (_m, key) => {
      const k = key.toLowerCase() as keyof typeof TEMPLATE_DEMO;
      return TEMPLATE_DEMO[k] ?? `{${key}}`;
    });
  }

  let focusedTemplateEl: HTMLInputElement | null = null;

  function insertToken(token: string) {
    const el = focusedTemplateEl;
    if (!el) return;
    const start = el.selectionStart ?? el.value.length;
    const end   = el.selectionEnd   ?? el.value.length;
    const before = el.value.slice(0, start);
    const after  = el.value.slice(end);
    el.value = before + token + after;
    // Trigger Svelte reactivity via an input event
    el.dispatchEvent(new Event('input', { bubbles: true }));
    const newPos = start + token.length;
    el.setSelectionRange(newPos, newPos);
    el.focus();
  }

  function restoreFolderDefaults() {
    config.single_track_filename_template  = '{artist} - {title}';
    config.album_track_filename_template   = '{track} - {title}';
    config.folder_structure_template       = '{album_artist}/{year} - {album}';
    config.multi_disc_handling             = '';
    config.track_number_padding            = 2;
    config.illegal_character_replacement   = '_';
    config.whitespace_handling             = 'keep';
  }

  let isLoading = true;
  let setupMode = false;
  let showHistory = false;
  let showSettings = false;

  // ── Supporter key validation (v1.1.8 BUG-5) ───────────────────────────────
  // keyState: 'idle' | 'validating' | 'valid' | 'invalid' | 'unverified'
  // 'unverified' means the server could not be reached — deliberately distinct
  // from 'invalid', because a network failure must never stop a genuinely valid
  // key from being saved.
  // Transient inline notice shown under the format selector (e.g. when a locked
  // supporter-only format is clicked).
  let settingsNotice = '';
  let settingsNoticeTimer: any = null;
  $: if (settingsNotice) {
    if (settingsNoticeTimer) clearTimeout(settingsNoticeTimer);
    settingsNoticeTimer = setTimeout(() => { settingsNotice = ''; }, 6000);
  }

  // ── Device-code account login (v1.1.8 FEAT-8) ─────────────────────────────
  // The app never handles a password: it shows a short code, opens the browser,
  // and polls until the user approves on the website.
  let deviceLoginState: 'idle' | 'starting' | 'waiting' | 'done' | 'error' = 'idle';
  let deviceUserCode = '';
  let deviceVerifyUrl = '';
  let deviceLoginError = '';
  let deviceLoginUser = '';
  let devicePollTimer: any = null;
  let deviceDeadline = 0;

  // The device token lives in its own config field. It used to share
  // antra_api_key with the pasted supporter key, so signing in overwrote the
  // key and then failed to validate against /api/keys/validate (a token is not
  // a keys.json entry) — reported as "Key not recognized" and, worse, silently
  // dropped the user to free tier. The legacy read is kept so an install that
  // already signed in still shows as signed in before its first save.
  $: isSignedIn = (config.antra_device_token || '').trim().startsWith('at_')
    || (config.antra_api_key || '').trim().startsWith('at_');

  // Account tier comes from the server's own verification of the token
  // (/api/device/status), never from a local decode of the payload — it is only
  // base64, so trusting it here would be a free supporter unlock.
  let accountStatus: any = null;

  async function loadAccountStatus() {
    if (!isSignedIn) { accountStatus = null; return; }
    try {
      accountStatus = await GetDeviceAccountStatus();
    } catch { accountStatus = null; }
  }

  function stopDevicePolling() {
    if (devicePollTimer) { clearInterval(devicePollTimer); devicePollTimer = null; }
  }

  async function beginDeviceLogin() {
    deviceLoginState = 'starting';
    deviceLoginError = '';
    try {
      const res: any = await StartDeviceLogin();
      if (res?.error) { deviceLoginState = 'error'; deviceLoginError = res.error; return; }
      deviceUserCode = res.user_code || '';
      deviceVerifyUrl = res.verification_url || '';
      deviceDeadline = Date.now() + ((res.expires_in || 600) * 1000);
      deviceLoginState = 'waiting';
      if (deviceVerifyUrl) BrowserOpenURL(deviceVerifyUrl);

      // Server enforces its own minimum interval and answers 429 -> treated as
      // still pending, so polling slightly faster than `interval` is harmless.
      const intervalMs = Math.max(3, res.interval || 3) * 1000;
      stopDevicePolling();
      devicePollTimer = setInterval(async () => {
        if (Date.now() > deviceDeadline) {
          stopDevicePolling();
          deviceLoginState = 'error';
          deviceLoginError = 'The code expired. Please try again.';
          return;
        }
        try {
          const t: any = await PollDeviceLogin(res.device_code);
          if (t?.status === 'approved') {
            stopDevicePolling();
            deviceLoginUser = t.username || '';
            deviceLoginState = 'done';
            // The Go side persisted the token in antra_device_token — the
            // supporter key field is untouched, so re-read config and refresh
            // BOTH credentials' status independently.
            config = await GetConfig();
            loadAccountStatus();
            if ((config.antra_api_key || '').trim()) validateSupporterKey(false);
          } else if (t?.status === 'error') {
            stopDevicePolling();
            deviceLoginState = 'error';
            deviceLoginError = t.error || 'Login failed.';
          }
        } catch (e) { /* transient — keep polling until the deadline */ }
      }, intervalMs);
    } catch (e) {
      deviceLoginState = 'error';
      deviceLoginError = String(e);
    }
  }

  async function signOutDevice() {
    stopDevicePolling();
    await SignOutDevice();
    config = await GetConfig();
    deviceLoginState = 'idle';
    deviceUserCode = '';
    deviceLoginUser = '';
    accountStatus = null;
    // Signing out must not discard a pasted supporter key — it is a separate
    // credential — so re-validate it rather than clearing keyState blindly.
    if ((config.antra_api_key || '').trim()) {
      validateSupporterKey(false);
    } else {
      keyState = 'idle';
      keyInfo = null;
    }
  }

  let keyState: string = 'idle';
  let keyInfo: any = null;
  let keyError = '';
  let keyValidateTimer: any = null;
  // Supporter status drives the Dolby Atmos gate (FEAT-1). It is only true once
  // the SERVER has confirmed the credential — never merely because one is
  // present. EITHER credential can carry the tier: a signed-in account whose
  // token says supporter, or a validated pasted key. Requiring the key alone is
  // what made signing in look like a downgrade.
  $: keyIsSupporter = keyState === 'valid' && !!keyInfo?.is_supporter;
  $: accountIsSupporter = !!accountStatus?.valid && !!accountStatus?.is_supporter;
  $: isSupporter = keyIsSupporter || accountIsSupporter;

  function formatKeyExpiry(iso: string): string {
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    } catch { return iso; }
  }

  async function validateSupporterKey(explicit = false) {
    const key = (config.antra_api_key || '').trim();
    if (!key) { keyState = 'idle'; keyInfo = null; keyError = ''; return; }
    if (key.startsWith('at_')) {
      // A device token from a pre-fix install still sitting in this field.
      // /api/keys/validate cannot recognise it, so asking would produce the
      // false "Key not recognized". Account state is reported separately.
      keyState = 'idle'; keyInfo = null; keyError = '';
      return;
    }
    keyState = 'validating';
    keyError = '';
    try {
      const res: any = await ValidateKey(key);
      keyInfo = res;
      if (res?.valid) {
        keyState = 'valid';
      } else if (res?.reachable) {
        keyState = 'invalid';
        keyError = res?.error || 'Key not recognized';
      } else {
        // Could not ask the server — keep the key, flag it as unverified.
        keyState = 'unverified';
        keyError = res?.error || '';
      }
    } catch (e) {
      keyState = 'unverified';
      keyError = String(e);
    }
  }

  // Debounced re-validation as the user types/pastes.
  function scheduleKeyValidation() {
    if (keyValidateTimer) clearTimeout(keyValidateTimer);
    keyState = 'idle';
    keyValidateTimer = setTimeout(() => validateSupporterKey(false), 600);
  }
  let showFolderSettings = false;
  let folderSettingsSaving = false;
  let settingsScrollTarget: string | null = null; // id of settings section to scroll to on open
  let showDownloadedMusic = false;
  let settingsButtonEl: HTMLButtonElement | null = null;
  let slskdWebUIInfo: {url: string, username: string, password: string} | null = null;
  let historyItems: any[] = [];
  let inputUrl = '';
  let inputUrlEl: HTMLTextAreaElement | null = null;
  let isDownloading = false;
  // v1.1.8 FEAT-13 — did this run actually save anything? Gates the batch
  // flourish so an all-skipped run stays silent.
  let sfxSawTrack = false;

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
    file_path?: string;
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

  // ── Synced Lyrics (SF-2) ────────────────────────────────────────────────────
  interface LyricsLine { time_ms: number; text: string; }
  let lyricsLines: LyricsLine[] = [];
  let lyricsSynced = false;
  let lyricsLoading = false;
  let showLyrics = false;
  let lyricsContainerEl: HTMLDivElement;

  // Index of the last lyrics line whose time_ms <= current playback position.
  $: activeLyricIdx = (lyricsSynced && lyricsLines.length > 0)
    ? lyricsLines.reduce((best, line, i) =>
        line.time_ms <= playerCurrentTime * 1000 ? i : best, -1)
    : -1;

  // Auto-scroll the active line into view when it changes.
  $: if (activeLyricIdx >= 0 && lyricsContainerEl) {
    const el = lyricsContainerEl.children[activeLyricIdx] as HTMLElement;
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // ── Spotify Library (My Mixes home screen section) ─────────────────────────
  interface SpotifyPlaylistItem {
    id: string;
    name: string;
    url: string;
    image_url?: string;
    track_count: number;
    owner_id: string;
    is_algorithmic: boolean;
    description?: string;
  }
  interface SpotifyAlbumItem {
    id: string;
    name: string;
    url: string;
    image_url?: string;
    artists?: string;
    year?: number;
  }
  interface SpotifyArtistItem {
    id: string;
    name: string;
    url: string;
    image_url?: string;
  }
  interface SpotifyLibraryData {
    liked_songs_count: number;
    playlists: SpotifyPlaylistItem[];
    saved_albums?: SpotifyAlbumItem[];
    followed_artists?: SpotifyArtistItem[];
  }
  let spotifyLibrary: SpotifyLibraryData | null = null;
  let spotifyLibraryLoading = false;
  let spotifyLibraryError = '';

  // Per-section collapse state for the My Library tab (false = expanded)
  let libMixesCollapsed = false;
  let libPlaylistsCollapsed = false;
  let libAlbumsCollapsed = false;
  let libArtistsCollapsed = false;

  // Apple Music library state
  interface AppleLibraryPlaylistItem {
    id: string;
    name: string;
    url: string;
    image_url: string | null;
    track_count: number;
    is_algorithmic: boolean;
  }
  interface AppleLibraryData {
    saved_songs_count: number;
    playlists: AppleLibraryPlaylistItem[];
  }
  let appleLibrary: AppleLibraryData | null = null;
  let appleLibraryLoading = false;
  let appleLibraryError = '';

  // Service switcher for My Library tab
  let libActiveService: 'spotify' | 'apple' = 'spotify';

  async function loadAppleMusicLibrary() {
    if (!config.apple_music_user_token || !config.apple_authorization_token) return;
    appleLibraryLoading = true;
    appleLibraryError = '';
    try {
      const raw = await GetAppleMusicLibrary();
      const data = typeof raw === 'string' ? JSON.parse(raw) : raw;
      if (data.error) {
        appleLibraryError = data.error;
      } else {
        appleLibrary = data as AppleLibraryData;
      }
    } catch (e: any) {
      appleLibraryError = e?.message || String(e);
    } finally {
      appleLibraryLoading = false;
    }
  }

  async function loadSpotifyLibrary() {
    if (!config.spotify_sp_dc) return;
    spotifyLibraryLoading = true;
    spotifyLibraryError = '';
    try {
      const raw = await GetSpotifyLibrary();
      const data = typeof raw === 'string' ? JSON.parse(raw) : raw;
      if (data.error) {
        spotifyLibraryError = data.error;
      } else {
        spotifyLibrary = data as SpotifyLibraryData;
      }
    } catch (e: any) {
      spotifyLibraryError = e?.message || String(e);
    } finally {
      spotifyLibraryLoading = false;
    }
  }

  // Trigger a download by pasting a URL and starting immediately
  function downloadPlaylistUrl(url: string) {
    inputUrl = url;
    activeTab = 'url';
    startDownload();
  }

  // ── Auto-sync ───────────────────────────────────────────────────────────────
  let autoSyncRunning = false;
  let autoSyncLastResult = '';

  async function runAutoSyncNow() {
    autoSyncRunning = true;
    autoSyncLastResult = '';
    try {
      const result = await RunAutoSync();
      autoSyncLastResult = result || 'Auto-sync complete.';
    } catch (e: any) {
      autoSyncLastResult = `Error: ${e?.message || e}`;
    } finally {
      autoSyncRunning = false;
    }
  }

  function togglePlaylistSync(pl: { url: string; name: string; artwork_url?: string; is_algorithmic?: boolean }) {
    const list = [...((config.tracked_playlists || []) as any[])];
    const idx = list.findIndex((p: any) => p.url === pl.url);
    if (idx >= 0) {
      list[idx] = { ...list[idx], sync_enabled: !list[idx].sync_enabled };
    } else {
      list.push({
        url: pl.url,
        name: pl.name,
        artwork_url: pl.artwork_url || '',
        is_algorithmic: pl.is_algorithmic || false,
        sync_enabled: true,
        last_track_ids: [],
        last_sync_ts: 0,
      });
    }
    config.tracked_playlists = list;
    SaveConfig(config);
  }

  function isPlaylistSyncing(url: string): boolean {
    const entry = ((config.tracked_playlists || []) as any[]).find((p: any) => p.url === url);
    if (!entry) return false;
    return entry.sync_enabled !== false;
  }

  function getTrackedEntry(url: string): any {
    return ((config.tracked_playlists || []) as any[]).find((p: any) => p.url === url);
  }

  // ── Mirror status ───────────────────────────────────────────────────────────
  // One chip, backed by the public status page rather than five client-side
  // probes. The page is the source of truth, needs no API key, and already
  // publishes the aggregate verdict — so there is no need to invent an "average"
  // here: `overall` is computed server-side from the same data the page renders.
  //   all_operational -> green   (every mirror up)
  //   degraded        -> amber   (something impaired, nothing down)
  //   partial_outage  -> red     (at least one mirror down)
  //   unreachable     -> grey    (we could not check — NOT reported as an outage)
  const STATUS_PAGE_URL = 'https://status.anandserver.cfd/';
  const STATUS_JSON_URL = 'https://status.anandserver.cfd/status.json';

  let mirrorStatus: { overall: string; up: number; total: number; avgMs: number; updatedAt: string } | null = null;
  let mirrorStatusFailed = false;

  async function fetchMirrorStatus() {
    try {
      const res = await fetch(STATUS_JSON_URL, { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      const d = await res.json();
      const meta = d.meta || {};
      mirrorStatus = {
        overall: d.overall || 'unknown',
        up: Number(meta.operational ?? 0),
        total: Number(meta.total_services ?? 0),
        avgMs: Number(meta.avg_latency_ms ?? 0),
        updatedAt: d.updated_at || '',
      };
      mirrorStatusFailed = false;
    } catch {
      // Never claim an outage we did not observe — "could not check" is its own
      // state, the same distinction the download validation makes.
      mirrorStatusFailed = true;
    }
  }

  // Every branch is an EXPLICIT match on a value `status_poller.py` actually
  // emits. An unrecognised `overall` falls through to unknown/grey rather than
  // degraded/amber — showing a warning colour for a value we do not understand
  // would be claiming a problem we never observed.
  $: mirrorStatusTone =
    mirrorStatusFailed || !mirrorStatus ? 'is-unknown'
    : mirrorStatus.overall === 'all_operational' ? 'is-ok'
    : mirrorStatus.overall === 'partial_outage' ? 'is-down'
    : mirrorStatus.overall === 'degraded' ? 'is-degraded'
    : 'is-unknown';

  $: mirrorStatusLabel =
    mirrorStatusFailed || !mirrorStatus ? 'Status'
    : `${mirrorStatus.up}/${mirrorStatus.total}`;

  $: mirrorStatusTitle =
    mirrorStatusFailed || !mirrorStatus
      ? 'Could not reach the status page — this does not mean the mirrors are down. Click to open it.'
      : `${mirrorStatus.up} of ${mirrorStatus.total} mirrors operational`
        + (mirrorStatus.avgMs ? ` · avg ${mirrorStatus.avgMs} ms` : '')
        + ' — click for the full status page';
  const downloadSourceOptions = [
    { value: 'auto',    label: 'Auto',        icon: null },
    { value: 'tidal',   label: 'Tidal',       icon: '/icons/tidal.webp' },
    { value: 'qobuz',   label: 'Qobuz',       icon: '/icons/qobuz.png' },
    { value: 'apple',   label: 'Apple Music', icon: '/icons/apple-music.png' },
    { value: 'amazon',  label: 'Amazon',      icon: '/icons/amazon-music.jpg' },
    { value: 'deezer',  label: 'Deezer',      icon: '/icons/deezer.webp' },
  ];
  const concreteDownloadSources = downloadSourceOptions.filter(src => src.value !== 'auto').map(src => src.value);
  let selectedDownloadSources: string[] = ['auto'];

  function normalizeDownloadSources(): string[] {
    const raw = config.download_sources && config.download_sources.length
      ? config.download_sources
      : [config.download_source || 'auto'];
    const cleaned = Array.from(new Set(raw.filter(Boolean)));
    if (!cleaned.length || cleaned.includes('auto')) return ['auto'];
    const known = cleaned.filter(src => concreteDownloadSources.includes(src));
    return known.length ? known : ['auto'];
  }

  function setDownloadSources(sources: string[]) {
    selectedDownloadSources = sources;
    config = {
      ...config,
      download_sources: sources,
      download_source: sources.length === 1 ? sources[0] : 'custom',
    };
  }

  function toggleDownloadSource(value: string) {
    if (value === 'auto') {
      setDownloadSources(['auto']);
      return;
    }
    let selected = selectedDownloadSources.filter(src => src !== 'auto');
    if (selected.includes(value)) {
      selected = selected.filter(src => src !== value);
    } else {
      selected = [...selected, value];
    }
    setDownloadSources(selected.length ? selected : ['auto']);
  }
  const formatOptions = [
    { value: 'auto',     name: 'Auto', label: 'Best available — lossless preferred, MP3 fallback' },
    { value: 'lossless', name: 'FLAC', label: 'FLAC lossless — highest quality from any source' },
    { value: 'alac',     name: 'ALAC', label: 'Apple Lossless .m4a — iPhone / Apple Music compatible' },
    { value: 'aac',      name: 'AAC',  label: '~320kbps AAC — uses JioSaavn directly' },
    { value: 'mp3',      name: 'MP3',  label: '~320kbps MP3 — uses JioSaavn / NetEase directly' },
  ];

  // Derive parent format and bit-depth from the stored output_format value
  // e.g. 'lossless-16' → parent='lossless', bitDepth='16'
  $: _fmtBase       = (config.output_format || 'auto').replace(/-16$|-24$/, '');
  $: _fmtBitDepth   = config.output_format?.endsWith('-16') ? '16' : config.output_format?.endsWith('-24') ? '24' : '';
  $: showBitDepthRow = _fmtBase === 'lossless' || _fmtBase === 'alac';

  async function setParentFormat(val: string) {
    // Preserve bit-depth selection when switching between FLAC and ALAC
    if ((val === 'lossless' || val === 'alac') && _fmtBitDepth) {
      config.output_format = val + '-' + _fmtBitDepth;
    } else {
      config.output_format = val;
    }
    await SaveConfig(config);
  }

  async function setBitDepth(depth: string) {
    config.output_format = _fmtBase + '-' + depth;
    await SaveConfig(config);
  }

  async function openSettingsAt(sectionId: string) {
    settingsScrollTarget = sectionId;
    showSettings = true;
    try { const raw = await GetSlskdWebUIInfo(); const info = JSON.parse(raw); slskdWebUIInfo = (info && info.url) ? info : null; } catch { slskdWebUIInfo = null; }
    setTimeout(() => {
      const el = document.getElementById(sectionId);
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      settingsScrollTarget = null;
    }, 80);
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

  async function pasteClipboardIntoUrlBox(event: MouseEvent) {
    if (isDownloading || !inputUrlEl) return;

    event.preventDefault();

    try {
      const clipboardText = await ClipboardGetText();
      if (!clipboardText) return;

      const start = inputUrlEl.selectionStart ?? inputUrl.length;
      const end = inputUrlEl.selectionEnd ?? inputUrl.length;
      inputUrl = inputUrl.slice(0, start) + clipboardText + inputUrl.slice(end);

      await tick();
      const caret = start + clipboardText.length;
      inputUrlEl.focus();
      inputUrlEl.setSelectionRange(caret, caret);
    } catch (error) {
      console.error('Right-click paste failed:', error);
    }
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
    link: string;
  }

  let supportStatus: SupportStatus = {
    enabled: true,
    title: 'Support Antra',
    message: 'Solo-maintained by one developer. Help fund bug fixes, updates, and endpoint costs.',
    link: 'https://patreon.com/AntraVerse'
  };
  let supportStatusLoading = false;

  function dismissSponsorToast() {
    sponsorToastLeaving = true;
    clearTimeout(sponsorToastTimer);
    setTimeout(() => { showSponsorToast = false; sponsorToastLeaving = false; }, 450);
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
  let trackLabels: Record<string, string> = {};
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
  let currentPlaylistTrackKeysByIndex: Record<number, string> = {};
  let currentPlaylistTrackCount = 0;

  // ── Failed Tracks Panel (ST-4) ──────────────────────────────────────────────
  let dismissedFailures = new Set<string>();
  let retryQueue: string[] = [];
  let retryQueueTotal = 0;
  let failedPanelCollapsed = false;

  $: failedEntries = trackOrder
    .filter(k => !k.startsWith('__SEP__') && activeTracks[k]?.status === 'failed' && !dismissedFailures.has(k))
    .map(k => ({
      key: k,
      label: trackLabels[k] || k,
      error: activeTracks[k]?.error || activeTracks[k]?.text || 'Failed',
      trackData: activeTracks[k]?.trackData,
    }));

  function makeTrackDisplayName(artist?: string | null, title?: string | null) {
    const artistPart = String(artist || '').trim();
    const titlePart = String(title || '').trim();
    if (artistPart && titlePart) return `${artistPart} - ${titlePart}`;
    return titlePart || artistPart || 'Unknown Track';
  }

  function normalizeTrackKeyPart(value: string) {
    return String(value || '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, ' ')
      .trim();
  }

  function fallbackTrackKey(artist?: string | null, title?: string | null, trackData?: any) {
    const artistPart = normalizeTrackKeyPart(artist || trackData?.artist_string || (Array.isArray(trackData?.artists) ? trackData.artists.join(' ') : ''));
    const titlePart = normalizeTrackKeyPart(title || trackData?.title || '');
    const durationPart = trackData?.duration_ms ? String(trackData.duration_ms) : '';
    return `fallback::${artistPart}::${titlePart}::${durationPart}`;
  }

  function resolveTrackEventKey(data: any) {
    const idx = Number(data?.track_index || 0);
    if (idx > 0 && currentPlaylistTrackKeysByIndex[idx]) {
      return currentPlaylistTrackKeysByIndex[idx];
    }
    const td = data?.track_data || {};
    if (td.spotify_id) return `spotify:${td.spotify_id}`;
    if (td.apple_music_id) return `apple:${td.apple_music_id}`;
    if (td.deezer_track_id) return `deezer:${td.deezer_track_id}`;
    if (td.tidal_track_id) return `tidal:${td.tidal_track_id}`;
    if (td.isrc && td.album_id && td.track_number) return `albumtrack:${td.album_id}:${td.disc_number || 1}:${td.track_number}:${td.isrc}`;
    return fallbackTrackKey(data?.artist, data?.track, td);
  }

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
      if (!config.album_folder_structure) {
        config.album_folder_structure = config.folder_structure || 'standard';
      }
      if (!config.playlist_folder_structure) {
        config.playlist_folder_structure = config.folder_structure || 'standard';
      }
      if (!config.playlist_storage_mode) {
        config.playlist_storage_mode = 'playlist_folder';
      }
      if (!config.single_track_structure) {
        config.single_track_structure = 'album_numbered';
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
      if (config.strict_matching === undefined || config.strict_matching === null) {
        config.strict_matching = false;
      }
      // v1.1.8 FEAT-3/2/4 — default ON, so an existing config.json that predates
      // these keys must be treated as "on", not as unchecked.
      if (config.strict_format === undefined || config.strict_format === null) {
        config.strict_format = true;
      }
      if (config.prevent_lossy_transcode === undefined || config.prevent_lossy_transcode === null) {
        config.prevent_lossy_transcode = true;
      }
      if (config.write_antra_tags === undefined || config.write_antra_tags === null) {
        config.write_antra_tags = true;
      }
      if (!config.download_source) {
        config.download_source = 'auto';
      }
      // v1.1.8 FEAT-13 — notification sounds. `== null` rather than a falsy
      // check throughout: volume 0 and notify_sound 'off' are deliberate user
      // choices and must not be re-defaulted on every launch.
      if (config.notify_sound == null)      config.notify_sound      = 'crystal';
      if (config.notify_volume == null)     config.notify_volume     = 70;
      if (config.notify_batch_only == null) config.notify_batch_only = false;
      selectedDownloadSources = normalizeDownloadSources();
      config = { ...config, download_sources: selectedDownloadSources };
      if (typeof config.save_cover_art_sidecar !== 'boolean') {
        config.save_cover_art_sidecar = false;
      }
      // Template defaults
      if (!config.single_track_filename_template) config.single_track_filename_template = '{artist} - {title}';
      if (!config.album_track_filename_template)  config.album_track_filename_template  = '{track} - {title}';
      // v1.1.8 FEAT-11 — `== null` on purpose, NOT a falsy check: an empty
      // string is a deliberate choice (save into the library root with no album
      // folder) and must survive a relaunch instead of being re-defaulted.
      if (config.folder_structure_template == null) config.folder_structure_template = '{album_artist}/{year} - {album}';
      if (!config.illegal_character_replacement)  config.illegal_character_replacement  = '_';
      if (!config.whitespace_handling)            config.whitespace_handling            = 'keep';
      if (!config.track_number_padding)           config.track_number_padding           = 2;
      // Apply saved theme + typeface
      applyTheme(config.theme || 'antra');
      setFontAttr(config.font || 'antra');

      // Auto-sync defaults
      if (config.auto_sync_enabled === undefined) config.auto_sync_enabled = false;
      if (!config.auto_sync_hour && config.auto_sync_hour !== 0) config.auto_sync_hour = 6;
      if (config.auto_sync_minute === undefined) config.auto_sync_minute = 0;
      if (!config.auto_sync_days) config.auto_sync_days = 127;
      if (!config.tracked_playlists) config.tracked_playlists = [];

    } catch (e) {
      console.error('Failed to load config', e);
      setupMode = true;
    }

    await loadSupportStatus();
    fetchMirrorStatus(); // non-blocking — the status chip fills in when it lands
    // Validate any stored supporter key at startup so supporter-gated features
    // (Dolby Atmos, FEAT-1) know the real tier rather than assuming from the
    // mere presence of a key (v1.1.8 BUG-5). Non-blocking.
    if ((config.antra_api_key || '').trim()) validateSupporterKey(false);
    // Account tier is a separate question from the pasted key — check both.
    loadAccountStatus();

    // v1.1.8 FEAT-12 — keep the session alive without the website. Renewal runs
    // against the mirrors, so it works even while the site is down. Deliberately
    // fire-and-forget: it must never delay startup, and only a positive server
    // rejection is worth telling the user about.
    RenewDeviceTokenIfNeeded().then((r: any) => {
      if (!r) return;
      if (r.status === 'renewed' && r.token) {
        config.antra_device_token = r.token;
        loadAccountStatus();
      } else if (r.status === 'reauth') {
        deviceLoginState = 'error';
        deviceLoginError = r.error || 'Your session has ended. Please sign in again.';
      }
      // 'unavailable' stays silent on purpose — the token still works, and
      // nagging about a transient outage is exactly the noise to avoid.
    }).catch(() => {});

    if (config.spotify_sp_dc) {
      loadSpotifyLibrary(); // non-blocking — Spotify mixes section updates when ready
    }
    if (config.apple_music_user_token && config.apple_authorization_token) {
      loadAppleMusicLibrary(); // non-blocking — Apple Music library updates when ready
    }
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
          appleLogin = { phase: 'success', message: payload.message || 'Apple Music connected!' };
          config.apple_enabled = true;
          if (payload.authorization_token) config.apple_authorization_token = payload.authorization_token;
          if (payload.music_user_token) config.apple_music_user_token = payload.music_user_token;
          if (payload.storefront) config.apple_storefront = payload.storefront;
          SaveConfig(config);
          if (!appleLibrary && !appleLibraryLoading) loadAppleMusicLibrary();
          setTimeout(() => { appleLogin = { phase: 'idle' }; }, 4000);
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

    EventsOn("sp-dc-event", (payload: any) => {
      if (!payload || !payload.type) return;
      switch (payload.type) {
        case 'sp_dc_status':
          spDcCapture = { phase: payload.status === 'waiting' ? 'waiting_for_user' : 'starting', message: payload.message || 'Opening browser...' };
          break;
        case 'sp_dc_captured':
          config.spotify_sp_dc = payload.sp_dc;
          spDcCapture = { phase: 'success', message: 'Spotify account connected!' };
          SaveConfig(config);
          if (!spotifyLibrary && !spotifyLibraryLoading) loadSpotifyLibrary();
          setTimeout(() => { spDcCapture = { phase: 'idle' }; }, 4000);
          break;
        case 'sp_dc_error':
          spDcCapture = { phase: 'error', message: payload.message || 'Failed to capture sp_dc.' };
          break;
        case 'sp_dc_done':
          if (spDcCapture.phase === 'starting' || spDcCapture.phase === 'waiting_for_user') {
            spDcCapture = { phase: 'error', message: 'Login ended without capturing a session. Try again.' };
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

    // Refresh the mirror status periodically. The poller behind the page runs on
    // a 60s cycle and the payload is ~5 KB, so 5 minutes is plenty and costs
    // nothing; the five per-source probes this replaced hit every endpoint.
    const statusTimer = setInterval(() => { fetchMirrorStatus(); }, 300000);

    const handleWindowResize = () => {
    };
    window.addEventListener('resize', handleWindowResize);
    return () => {
      clearInterval(statusTimer);
      window.removeEventListener('resize', handleWindowResize);
    };
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

      currentPlaylistTrackKeysByIndex = {};
      currentPlaylistTrackCount = 0;

      // Pre-populate the full tracklist in waiting state (Set-based O(N) dedup)
      const seen = new Set(trackOrder);
      const newTracks: string[] = [];
      trkList.forEach((t: any, idx: number) => {
        const rowKey = `track:${Date.now()}:${idx + 1}`;
        const label = makeTrackDisplayName(t.artist, t.title);
        currentPlaylistTrackKeysByIndex[idx + 1] = rowKey;
        currentPlaylistTrackCount = idx + 1;
        trackLabels[rowKey] = label;
        if (!seen.has(rowKey)) {
          seen.add(rowKey);
          newTracks.push(rowKey);
          if (!activeTracks[rowKey]) {
            activeTracks[rowKey] = { mode: 'status', text: 'Waiting...', status: 'resolving' };
          }
        }
      });
      if (newTracks.length > 0) {
        trackOrder = [...trackOrder, ...newTracks];
      }
      trackLabels = { ...trackLabels };
      activeTracks = { ...activeTracks };
      return;
    }

    if (payload.type === 'tracks_appended') {
      // Progressive playlist loading: append new tracks without resetting existing rows
      const trkList2: any[] = payload.tracks || [];
      const seen2 = new Set(trackOrder);
      const newTracks2: string[] = [];
      trkList2.forEach((t: any, idx: number) => {
        const absoluteIndex = currentPlaylistTrackCount + idx + 1;
        const rowKey = `track:${Date.now()}:${absoluteIndex}`;
        const label = makeTrackDisplayName(t.artist, t.title);
        currentPlaylistTrackKeysByIndex[absoluteIndex] = rowKey;
        trackLabels[rowKey] = label;
        if (!seen2.has(rowKey)) {
          seen2.add(rowKey);
          newTracks2.push(rowKey);
          if (!activeTracks[rowKey]) {
            activeTracks[rowKey] = { mode: 'status', text: 'Waiting...', status: 'resolving' };
          }
        }
      });
      if (newTracks2.length > 0) {
        trackOrder = [...trackOrder, ...newTracks2];
        activeTracks = { ...activeTracks };
        trackLabels = { ...trackLabels };
        playlistTotalTracks = (playlistTotalTracks || 0) + newTracks2.length;
        playlistTotalDurationMs = (playlistTotalDurationMs || 0) + trkList2.reduce((s: number, t: any) => s + (t.duration_ms || 0), 0);
        currentPlaylistTrackCount += newTracks2.length;
      }
      return;
    }

    if (payload.type === 'process_ended') {
      isDownloading = false;
      Object.keys(activeTracks).forEach(clearTrackInterval);
      if (payload.status === 'cancelled') {
        trackOrder = [];
        trackLabels = {};
        activeTracks = {};
        currentPlaylistTrackKeysByIndex = {};
        currentPlaylistTrackCount = 0;
        dismissedFailures = new Set();
        retryQueue = [];
        retryQueueTotal = 0;
        addLog('warning', '■ Library sync stopped');
      } else if (payload.status === 'failed') {
        addLog('error', '✖ Library sync stopped with errors');
        if (retryQueue.length > 0) { processRetryQueue(); }
      } else {
        addLog('success', '✔ Library updated successfully');
        // v1.1.8 FEAT-13 — batch flourish. Only when at least one track was
        // actually saved, so a fully-skipped ("already in library") run stays
        // silent rather than congratulating the user for doing nothing.
        if (config.notify_sound && config.notify_sound !== 'off' && sfxSawTrack) {
          playFinished(config.notify_volume ?? 70);
        }
        if (retryQueue.length > 0) { processRetryQueue(); }
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
      // Hide adapter startup / source chain logs — the health chips already show this info
      if (typeof payload.message === 'string') {
        const msg = payload.message;
        if (
          /^\[OK\].*adapter enabled/.test(msg) ||
          /^\[Sources\] Active download chain:/.test(msg) ||
          /^Enriching tracks with album metadata/.test(msg) ||
          /^\[Spotify\] Partner API: \d+ tracks for album/.test(msg) ||
          /^\[Spotify\] Used partner GraphQL API for album/.test(msg)
        ) {
          return;
        }
      }
      addLog(payload.level, payload.message);
    } else if (payload.type === 'progress') {
      addLog('info', `[Bulk Progress] ${payload.message}`);
    } else if (payload.type === 'event') {
      const name = payload.name;
      const data = payload.payload;

      const trackKey = resolveTrackEventKey(data);
      const trackLabel = makeTrackDisplayName(data.artist, data.track);
      if (!trackLabels[trackKey]) {
        trackLabels[trackKey] = trackLabel;
        trackLabels = { ...trackLabels };
      }

      if (name === 'track_started') {
        if (!trackOrder.includes(trackKey)) {
          trackOrder = [...trackOrder, trackKey];
        }
        updateActiveTrack(trackKey, {
          mode: 'status',
          progress: undefined,
          text: 'Resolving best source...',
          status: 'resolving',
          retrying: false,
          trackData: data.track_data || activeTracks[trackKey]?.trackData,
        });

      } else if (name === 'track_resolved') {
        clearTrackInterval(trackKey);
        let displaySource = data.source || 'auto';
        if (displaySource === 'hifi') displaySource = 'Tidal';
        else if (displaySource === 'apple') displaySource = 'Apple';
        else if (displaySource === 'amazon') displaySource = 'Amazon';
        else displaySource = displaySource.charAt(0).toUpperCase() + displaySource.slice(1);

        updateActiveTrack(trackKey, {
          mode: 'status',
          progress: undefined,
          text: `Accepted via ${displaySource}${data.quality_label ? ` • ${data.quality_label}` : ''}`,
          status: 'resolving',
          retrying: false,
          trackData: data.track_data || activeTracks[trackKey]?.trackData,
        });

      } else if (name === 'track_download_attempt') {
        const source = String(data.source || 'auto');
        const attempt = data.attempt ?? 1;
        clearTrackInterval(trackKey);

        if (source.startsWith('soulseek')) {
          updateActiveTrack(trackKey, {
            mode: 'status',
            progress: undefined,
            text: 'Waiting for Soulseek transfer...',
            status: 'downloading',
            retrying: false,
            trackData: data.track_data || activeTracks[trackKey]?.trackData,
          });
          return;
        }

        const attemptSuffix = attempt > 1 ? ` • Retry ${attempt}` : '';
        let displaySource = source;
        if (displaySource === 'hifi') displaySource = 'Tidal';
        else if (displaySource === 'apple') displaySource = 'Apple';
        else if (displaySource === 'amazon') displaySource = 'Amazon';
        else displaySource = displaySource.charAt(0).toUpperCase() + displaySource.slice(1);

        updateActiveTrack(trackKey, {
          mode: 'progress',
          progress: 8,
          text: `Downloading from ${displaySource}${data.quality_label ? ` • ${data.quality_label}` : ''}${attemptSuffix}`,
          status: 'downloading',
          retrying: false,
          trackData: data.track_data || activeTracks[trackKey]?.trackData,
        });

        const intervalId = setInterval(() => {
          if (activeTracks[trackKey] && activeTracks[trackKey].mode === 'progress' && (activeTracks[trackKey].progress ?? 0) < 85) {
            updateActiveTrack(trackKey, {
              progress: Math.min(85, (activeTracks[trackKey].progress ?? 0) + Math.random() * 5)
            });
          } else {
            clearInterval(intervalId);
          }
        }, 800);

        (activeTracks[trackKey] as any)._intervalId = intervalId;
        activeTracks = { ...activeTracks };

      } else if (name === 'track_completed') {
        addLog('success', `[✓] Added to library: ${trackLabel}`);
        clearTrackInterval(trackKey);
        // v1.1.8 FEAT-13 — per-track "saved" sound. Suppressed in batch-only
        // mode; otherwise throttled inside playSaved() so a fast album cannot
        // machine-gun the speaker.
        sfxSawTrack = true;
        if (!config.notify_batch_only) {
          playSaved((config.notify_sound || 'crystal') as SfxName, config.notify_volume ?? 70);
        }
        updateActiveTrack(trackKey, {
          mode: 'progress',
          progress: 100,
          text: '✓ Added to library',
          error: undefined,
          status: 'done',
          retrying: false,
          trackData: data.track_data || activeTracks[trackKey]?.trackData,
        });
      } else if (name === 'track_failed') {
        addLog('error', `[FAIL] ${trackLabel} - ${data.error}`);
        clearTrackInterval(trackKey);
        updateActiveTrack(trackKey, {
          mode: 'status',
          progress: undefined,
          text: 'Download failed',
          error: data.error || 'Failed',
          status: 'failed',
          retrying: false,
          trackData: data.track_data || activeTracks[trackKey]?.trackData,
        });
      } else if (name === 'track_skipped') {
        addLog('warning', `[—] Already in library: ${trackLabel}`);
        updateActiveTrack(trackKey, {
          mode: 'status',
          text: 'Already in library',
          status: 'skipped',
          retrying: false,
          trackData: data.track_data || activeTracks[trackKey]?.trackData,
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
    stats?: any;   // AudioStats: peakDb, rmsDb, truePeakDb, lufsI, lufsLRA, cutoffHz
    // v1.1.8 FEAT-5 — SpectralAnalysis: verdict, cutoffConfidence, shelfDropDb,
    // evidence[], avgSpectrumDb[]. Carries its own justification so the UI can
    // show why it reached a conclusion instead of asserting one.
    spectral?: any;
    spectralError?: string;
    error?: string;
  }

  // Tab mode
  let activeTab: 'library' | 'url' | 'artist' | 'discover' = 'library';
  let searchQuery = '';

  // Discovery variables
  let discoveryRegion = 'in';
  let discoveryGenre = '';
  let discoveryData: any = null;
  let discoveryLoading = false;
  // Which service supplies Discover. Spotify uses the personalised home feed,
  // so it needs a connected account; the backend falls back to Apple on its
  // own and reports which source it actually used.
  let discoverySource: 'apple' | 'spotify' = 'apple';
  let discoveryActualSource: string = 'apple';
  let discoveryGenres: any[] = [];
  let discoveryGenresLoading = false;

  async function loadDiscoveryGenres() {
    discoveryGenresLoading = true;
    try {
      // @ts-ignore
      const raw = await window.go.main.App.GetDiscoveryGenres(discoveryRegion, discoverySource);
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
      const raw = await window.go.main.App.GetDiscoveryData(discoveryRegion, discoveryGenre, discoveryGenres.find(g => g.id === discoveryGenre)?.name || '', discoverySource);
      const parsed = JSON.parse(raw);
      if (parsed.type === 'discovery') {
        discoveryData = parsed.data;
        discoveryActualSource = parsed.source || 'apple';
        if (discoverySource === 'spotify' && discoveryActualSource !== 'spotify') {
          addLog('warn', 'Spotify Discover unavailable — showing Apple Music charts. Connect your Spotify account in Settings.');
        }
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

  // ── Album Availability Studio ───────────────────────────────────────────────
  let showAvailability = false;
  let availabilityUrl = '';
  let availabilityLoading = false;
  let availabilityResult: any = null;
  let availabilityError = '';

  async function inspectAlbum() {
    if (!availabilityUrl.trim()) return;
    availabilityLoading = true;
    availabilityError = '';
    availabilityResult = null;
    try {
      const raw = await GetAlbumAvailability(availabilityUrl.trim());
      const parsed = JSON.parse(raw);
      if (parsed.error) {
        availabilityError = parsed.error;
      } else {
        availabilityResult = parsed;
      }
    } catch (e) {
      availabilityError = String((e as any)?.message || e);
    } finally {
      availabilityLoading = false;
    }
  }

  function availabilityToneColor(tone: string): string {
    if (tone === 'ok')   return '#22c55e';
    if (tone === 'warn') return '#f59e0b';
    return '#555';
  }

  let showAnalyzer = false;
  let analyzerTracks: TrackAnalysis[] = [];
  let analyzerCurrentIndex = 0;
  // Single is the DEFAULT and the only view that carries the metadata grid,
  // audio stats, spectral verdict and the interactive spectrogram. Gallery is a
  // contact sheet — it shows a thumbnail and a badge and nothing else — so it is
  // only ever chosen automatically for 3+ files. Defaulting to gallery made
  // analysing one file strictly worse than v1.1.7: every panel was hidden, and
  // with a single track the Gallery/Single toggle is not even rendered, so there
  // was no way back.
  let analyzerViewMode: ViewMode = 'single';
  let analyzerDragOver = false;
  let analyzerProcessing = false;
  let analyzerExportStatus = '';

  $: analyzerDoneCount = analyzerTracks.filter(t => t.status === 'done').length;
  $: analyzerShowSidebar = analyzerTracks.length > 1;
  $: analyzerShowExportAll = analyzerTracks.length >= 2;

  function analyzerReset() {
    analyzerTracks = [];
    analyzerCurrentIndex = 0;
    analyzerViewMode = 'single';
    analyzerExportStatus = '';
  }

  function analyzerRemoveTrack(i: number) {
    analyzerTracks = analyzerTracks.filter((_, idx) => idx !== i);
    if (analyzerCurrentIndex >= analyzerTracks.length) {
      analyzerCurrentIndex = Math.max(0, analyzerTracks.length - 1);
    }
    if (analyzerTracks.length < 3) analyzerViewMode = 'single';
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

    // 3+ files is a contact sheet; 1–2 go straight to the detailed view.
    analyzerViewMode = analyzerTracks.length >= 3 ? 'gallery' : 'single';

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
          stats: result.stats,
          spectral: result.spectral,       // v1.1.8 FEAT-5 — FFT analysis + evidence
          spectralError: result.spectralError,
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

  function analyzerFormatStats(track: TrackAnalysis): { label: string; value: string }[] {
    const s = track.stats;
    if (!s) return [];
    const fmt = (v: number, suffix: string) => (v <= -999 || v === null || v === undefined) ? '—' : `${v.toFixed(1)} ${suffix}`;
    const rows: { label: string; value: string }[] = [];
    rows.push({ label: 'Peak',        value: fmt(s.peakDb,     'dBFS') });
    rows.push({ label: 'RMS',         value: fmt(s.rmsDb,      'dBFS') });
    rows.push({ label: 'True Peak',   value: fmt(s.truePeakDb, 'dBTP') });
    rows.push({ label: 'Loudness',    value: fmt(s.lufsI,      'LUFS') });
    rows.push({ label: 'LRA',         value: fmt(s.lufsLRA,    'LU')   });
    rows.push({ label: 'Freq. Cutoff', value: s.cutoffHz > 0 ? `${(s.cutoffHz / 1000).toFixed(0)} kHz` : '—' });
    return rows;
  }

  // ── Interactive spectrogram (v1.1.8 FEAT-5 UI) ─────────────────────────────
  // Rendered from the byte matrix the Go analyser returns. Keeping the data
  // client-side means zoom, pan and hover cost no backend round-trip — the one
  // idea worth taking from SpotiFLAC's approach, without their full-PCM payload.
  let specCanvas: HTMLCanvasElement | null = null;
  let specZoom = 1;        // 1 = whole analysed window
  let specPan = 0;         // 0..1 fraction of the scrollable width
  let specPalette = 'magma';
  let specReadout = '';
  let specDragging = false;
  let specDragX = 0;

  const SPEC_PALETTES: Record<string, (v: number) => [number, number, number]> = {
    // Perceptually-ordered ramps: dark = quiet, bright = loud. Magma and
    // viridis are used because they stay monotonic in luminance, so a louder
    // band always looks louder — rainbow maps do not, and misread badly.
    magma: (v) => [
      Math.min(255, Math.round(255 * Math.pow(v, 0.75))),
      Math.round(200 * Math.pow(Math.max(0, v - 0.25) / 0.75, 1.6)),
      Math.round(180 * Math.pow(Math.max(0, v - 0.55) / 0.45, 1.2) + 40 * v),
    ],
    viridis: (v) => [
      Math.round(70 * (1 - v) + 253 * Math.pow(v, 2.2)),
      Math.round(20 + 210 * Math.pow(v, 0.85)),
      Math.round(90 + 120 * (1 - Math.pow(v, 0.6))),
    ],
    grey: (v) => {
      const g = Math.round(255 * Math.pow(v, 0.8));
      return [g, g, g];
    },
  };

  // Go's encoding/json marshals a []byte as a BASE64 STRING, so SpectralAnalysis's
  // `Columns [][]byte` arrives here as string[], not number[][]. The first cut of
  // this code indexed those strings directly, so every cell read a character
  // instead of a byte, every value came out NaN, and the canvas painted solid
  // black. The Go test asserted on the Go struct and never crossed the JSON
  // bridge, which is exactly why it passed. Decode explicitly, once per analysis.
  const specColumnCache = new WeakMap<object, Uint8Array[]>();
  function specColumns(spec: any): Uint8Array[] {
    if (!spec || !spec.columns?.length) return [];
    const cached = specColumnCache.get(spec);
    if (cached) return cached;
    const out: Uint8Array[] = spec.columns.map((c: any) => {
      if (typeof c !== 'string') return Uint8Array.from(c || []);
      const bin = atob(c);
      const arr = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      return arr;
    });
    specColumnCache.set(spec, out);
    return out;
  }

  function drawSpectrogram(track: any) {
    // Alias to a plain local BEFORE mutating anything on it. `specCanvas` is a
    // component-level reactive variable, so Svelte compiles `specCanvas.width =
    // …` into `$$invalidate(specCanvas, …)`. That marks the component dirty,
    // which re-runs the reactive block below (it depends on specCanvas), which
    // calls this function again, which invalidates again — an unbounded loop of
    // full-canvas repaints that permanently wedges the UI thread and forces the
    // user to kill the app. Mutating a local does not invalidate anything.
    const cv = specCanvas;
    if (!cv) return;
    const spec = track?.spectral;
    const cols = specColumns(spec);
    if (!cols.length) return;
    const rows = cols[0]?.length || 0;
    if (!rows) return;

    const cssW = cv.clientWidth || 640;
    const cssH = 220;
    // Cap DPR: a full-width spectrogram at 3x on a hi-dpi screen is a lot of
    // pixels for no visible benefit.
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.round(cssW * dpr);
    const h = Math.round(cssH * dpr);
    if (w <= 0 || h <= 0) return;
    cv.width = w;
    cv.height = h;
    cv.style.height = cssH + 'px';

    const ctx = cv.getContext('2d');
    if (!ctx) return;
    const img = ctx.createImageData(w, h);
    const pal = SPEC_PALETTES[specPalette] || SPEC_PALETTES.magma;

    const visible = cols.length / specZoom;
    const startCol = Math.max(0, Math.min(cols.length - visible, specPan * (cols.length - visible)));

    for (let px = 0; px < w; px++) {
      const c = Math.min(cols.length - 1, Math.floor(startCol + (px / w) * visible));
      const col = cols[c];
      for (let py = 0; py < h; py++) {
        // Low frequencies at the bottom, as every spectrogram convention expects.
        const r = Math.min(rows - 1, Math.floor((1 - py / h) * rows));
        const v = (col[r] || 0) / 255;
        const [rr, gg, bb] = pal(v);
        const o = (py * w + px) * 4;
        img.data[o] = rr; img.data[o + 1] = gg; img.data[o + 2] = bb; img.data[o + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
  }

  function specGeometry(track: any) {
    const spec = track?.spectral;
    const cols = specColumns(spec);
    const visible = cols.length / specZoom;
    const startCol = Math.max(0, Math.min(cols.length - visible, specPan * (cols.length - visible)));
    return { spec, cols, visible, startCol };
  }

  function onSpecHover(e: MouseEvent, track: any) {
    if (!specCanvas) return;
    const { spec, cols, visible, startCol } = specGeometry(track);
    if (!spec || !cols.length) return;
    const rect = specCanvas.getBoundingClientRect();
    const fx = (e.clientX - rect.left) / rect.width;
    const fy = 1 - (e.clientY - rect.top) / rect.height;
    const c = Math.max(0, Math.min(cols.length - 1, Math.floor(startCol + fx * visible)));
    const rows = cols[0].length;
    const r = Math.max(0, Math.min(rows - 1, Math.floor(fy * rows)));
    const totalSec = (spec.timeEndSec ?? 0) - (spec.timeStartSec ?? 0);
    const t = (spec.timeStartSec ?? 0) + (c / cols.length) * totalSec;
    const hz = r * (spec.freqBinHz ?? 0);
    const db = spec.specFloorDb + ((cols[c][r] || 0) / 255) * (0 - spec.specFloorDb);
    specReadout = `${t.toFixed(1)}s · ${(hz / 1000).toFixed(2)} kHz · ${db.toFixed(0)} dB`;
  }

  // A full repaint is a per-pixel loop over the whole canvas (~800k iterations at
  // 2x DPR). Calling it straight from mousemove queues repaints faster than they
  // can run and locks the UI thread solid, so coalesce to one repaint per frame.
  let specRafPending = false;
  function scheduleSpecDraw(track: any) {
    if (specRafPending) return;
    specRafPending = true;
    requestAnimationFrame(() => {
      specRafPending = false;
      drawSpectrogram(track);
    });
  }

  function onSpecZoom(e: WheelEvent, track: any) {
    const prev = specZoom;
    specZoom = Math.max(1, Math.min(16, specZoom * (e.deltaY < 0 ? 1.25 : 0.8)));
    if (specZoom !== prev) scheduleSpecDraw(track);
  }

  function onSpecDragStart(e: MouseEvent) { specDragging = true; specDragX = e.clientX; }
  // Bound on window, not just the canvas: releasing the button anywhere else —
  // which is the normal way a drag ends — otherwise left specDragging stuck true
  // forever, and every subsequent mouse move over the canvas repainted it.
  function onSpecDragEnd() { specDragging = false; }
  function onSpecDrag(e: MouseEvent, track: any) {
    if (!specDragging || !specCanvas) return;
    const dx = e.clientX - specDragX;
    specDragX = e.clientX;
    specPan = Math.max(0, Math.min(1, specPan - dx / specCanvas.clientWidth / specZoom));
    scheduleSpecDraw(track);
  }

  async function exportSpectrogramPng(track: any) {
    if (!specCanvas) return;
    try {
      const data = specCanvas.toDataURL('image/png').split(',')[1];
      const name = (track.fileName || 'spectrogram').replace(/\.[^.]+$/, '') + '-spectrogram.png';
      await WriteFile(name, data);
      specReadout = `saved ${name}`;
    } catch (e) {
      specReadout = 'PNG export failed';
    }
  }

  // Redraw whenever the canvas mounts, the viewed track changes, or the palette
  // changes. The canvas is only bound once Svelte has rendered it, so this
  // reactive statement is what actually paints the first frame.
  $: if (specCanvas && analyzerTracks[analyzerCurrentIndex]?.spectral) {
    // reference specPalette/specZoom so the statement re-runs on their change
    specPalette; specZoom;
    tick().then(() => drawSpectrogram(analyzerTracks[analyzerCurrentIndex]));
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
    const cutoff = track.stats?.cutoffHz ?? 0;

    const spectral: any = (track as any).spectral;

    if (codec === 'flac' || codec === 'alac') {
      // v1.1.8 FEAT-5: only assert "Fake Lossless" on real FFT evidence — a
      // sharp low-pass shelf well below Nyquist, found with high confidence.
      // The old rule was `cutoff < 14000` from a six-point probe that could only
      // return {8k,12k,16k,17k,19k,21k}, so it both missed most transcodes and
      // libelled genuine masters with little treble. Wrongly branding a real
      // master is the worse error, so uncertainty now reads as "Inconclusive".
      if (spectral) {
        if (spectral.verdict === 'lossy' && spectral.cutoffConfidence >= 0.75) {
          return { label: 'Fake Lossless', color: '#f87171' };
        }
        if (spectral.verdict === 'inconclusive' && spectral.cutoffConfidence < 0.55) {
          return { label: 'Lossless (unverified)', color: '#facc15' };
        }
        if (spectral.verdict === 'lossless') {
          // Real spectral evidence beats the bitrate heuristics below, which are
          // crude enough to libel a genuine quiet or mono master as fake.
          return (bits >= 24 && sr >= 88200)
            ? { label: 'Hi-Res Lossless', color: '#a78bfa' }
            : { label: 'Lossless', color: '#00ffcc' };
        }
      } else if (cutoff > 0 && cutoff < 14000) {
        // Legacy coarse probe only — flag as suspect, never assert.
        return { label: 'Suspect (low cutoff)', color: '#facc15' };
      }
      if (bits >= 24 && sr >= 88200) {
        if (br > 0 && br < 400000) return { label: 'Suspect (Low Bitrate)', color: '#f87171' };
        return { label: 'Hi-Res Lossless', color: '#a78bfa' };
      }
      // Standard lossless: below ~250kbps almost always a transcode
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

  async function loadLyrics(filePath: string | undefined) {
    lyricsLines = [];
    lyricsSynced = false;
    if (!filePath) return;
    lyricsLoading = true;
    try {
      const raw = await GetTrackLyrics(filePath);
      const parsed = JSON.parse(raw) as { lines: LyricsLine[]; synced: boolean };
      lyricsLines = parsed.lines || [];
      lyricsSynced = parsed.synced || false;
      // Auto-show the panel when the track has lyrics; auto-hide when it doesn't.
      if (lyricsLines.length > 0) showLyrics = true;
    } catch { lyricsLines = []; }
    finally { lyricsLoading = false; }
  }

  // Load lyrics whenever the active track changes.
  $: loadLyrics(currentPlayerTrack?.file_path);

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

  // ── Sign-in gate (v1.1.8 FEAT-8 Phase B) ──────────────────────────────────
  // From this release the shared key published in the gist manifest is retired,
  // so a download needs a credential of the user's own: an Antra account, or a
  // supporter key (those are NOT affected by the sunset and keep working).
  //
  // Deliberately permissive: this checks that a credential EXISTS, not that the
  // server has blessed it, and an unreachable server never blocks anyone. The
  // real enforcement is server-side — the mirrors refuse the retired key
  // themselves. This gate is guidance so users get a clear prompt instead of a
  // wall of per-track auth failures; making it strict would only add a way to
  // lock out someone whose credential is fine while they are briefly offline.
  $: hasOwnCredential = isSignedIn
    || ((config.antra_api_key || '').trim() !== '' && !(config.antra_api_key || '').trim().startsWith('at_'));

  function promptSignIn() {
    // Reuse the existing opener so the panel actually scrolls to the account
    // section rather than dumping the user at the top of Settings.
    openSettingsAt('access-key-section');
  }

  async function startDownload() {
    if (!inputUrl) return;
    if (!hasOwnCredential) {
      promptSignIn();
      return;
    }

    // Accept one URL per line, or comma-separated, or both
    let urls = inputUrl
      .split(/[\n,]+/)
      .map(s => s.trim())
      .filter(s => s.startsWith('http') || s.startsWith('apple-music://'));
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
    sfxSawTrack = false;
      logs = [];
      trackOrder = [];
      trackLabels = {};
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
      currentPlaylistTrackKeysByIndex = {};
      currentPlaylistTrackCount = 0;
      separatorMeta = {};
      dismissedFailures = new Set();
      retryQueue = [];
      retryQueueTotal = 0;
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
        // Spotify's public search is rate-limited on some networks and the
        // backend falls back to Apple. Say so rather than presenting Apple
        // results under a "Spotify" label.
        if (searchSource === 'spotify' && artistSearchResults.length
            && artistSearchResults[0]?.source && artistSearchResults[0].source !== 'spotify') {
          addLog('warn', 'Spotify artist search was unavailable — showing Apple Music results instead.');
        }
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

  // Open the discography modal directly from a Spotify artist URL
  // (used by the Followed Artists cards in the My Library tab).
  async function openArtistFromUrl(profileUrl: string) {
    if (!profileUrl) return;
    await openArtistFromSearch({ profile_url: profileUrl });
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
    const hadSpDc = !!config.spotify_sp_dc;
    await SaveConfig(config);
    showSettings = false;
    // auto-load library when sp_dc is first saved
    if (hadSpDc && !spotifyLibrary && !spotifyLibraryLoading) {
      loadSpotifyLibrary();
    }
  }

  async function openFolderSettings(event?: MouseEvent) {
    event?.stopPropagation();
    if (folderSettingsSaving) return;
    focusedTemplateEl = null;
    showFolderSettings = true;
    await tick();
  }

  function closeFolderSettings() {
    if (folderSettingsSaving) return;
    focusedTemplateEl = null;
    showFolderSettings = false;
  }

  async function saveFolderSettings() {
    if (folderSettingsSaving) return;
    folderSettingsSaving = true;
    focusedTemplateEl = null;
    try {
      await SaveConfig(config);
      showFolderSettings = false;
    } finally {
      folderSettingsSaving = false;
    }
  }

  async function openSettings() {
    showSettings = true;
    try {
      const raw = await GetSlskdWebUIInfo();
      const info = JSON.parse(raw);
      slskdWebUIInfo = (info && info.url) ? info : null;
    } catch {
      slskdWebUIInfo = null;
    }
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
    sfxSawTrack = false;
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

  function dismissFailure(key: string) {
    dismissedFailures = new Set([...dismissedFailures, key]);
  }

  function dismissAllFailures() {
    dismissedFailures = new Set([...dismissedFailures, ...failedEntries.map(e => e.key)]);
  }

  async function processRetryQueue() {
    if (retryQueue.length === 0 || isDownloading) return;
    const nextKey = retryQueue[0];
    retryQueue = retryQueue.slice(1);
    if (nextKey) await retryFailedTrack(nextKey);
  }

  async function retryAllFailed() {
    const eligible = failedEntries.filter(e => e.trackData);
    if (eligible.length === 0 || isDownloading) return;
    retryQueue = eligible.map(e => e.key);
    retryQueueTotal = retryQueue.length;
    await processRetryQueue();
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
      <AntraLogo />
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
      <!-- v1.1.8 FEAT-8 Phase B — account step at first run. Deliberately not a
           hard wall: a supporter with a key does not need an account, and
           someone who skips it is prompted again on the main screen rather than
           being stuck on setup with no way forward. -->
      <div class="field" style="margin-top: 18px;">
        <label>Antra Account</label>
        <p style="font-size: 11px; color: #555; margin: 0 0 10px; line-height: 1.5;">
          Downloads go through your own Antra account. Your password is never entered into this
          app — the browser handles sign-in and this device gets its own revocable token.
        </p>
        {#if isSignedIn}
          <p style="font-size: 12px; color: #86efac; margin: 0;">
            ✓ Signed in{accountStatus?.username ? ` as ${accountStatus.username}` : ''}
          </p>
        {:else if deviceLoginState === 'waiting'}
          <p style="font-size: 11px; color: #666; margin: 0 0 8px;">Enter this code in your browser:</p>
          <p style="font-family: monospace; font-size: 20px; letter-spacing: 3px; color: var(--accent-color); margin: 0 0 6px;">{deviceUserCode}</p>
          <p style="font-size: 11px; color: #888; margin: 0;">Waiting for approval…</p>
        {:else}
          <button on:click={beginDeviceLogin} disabled={deviceLoginState === 'starting'} style="width: 100%;">
            {deviceLoginState === 'starting' ? 'Opening browser…' : 'Log in with Antra'}
          </button>
          {#if deviceLoginError}
            <p style="font-size: 11px; color: #fca5a5; margin: 8px 0 0;">{deviceLoginError}</p>
          {/if}
        {/if}
      </div>

      <button style="margin-top: 24px; width: 100%;" on:click={saveSetup}>Build My Library →</button>
      {#if !hasOwnCredential}
        <p style="font-size: 10.5px; color: #555; text-align: center; margin: 10px 0 0;">
          You can sign in later from Settings — downloads need an account or a supporter key.
        </p>
      {/if}
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
          <button on:click={() => { showAvailability = true; availabilityResult = null; availabilityError = ''; }} title="Album Availability Studio" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">🌍</button>
          <button on:click={() => showThemes = true} title="Themes" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">🎨</button>
          <button on:click={openFolderSettings} title="Library & Folder Settings" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">📁</button>
          <button bind:this={settingsButtonEl} on:click={openSettings} title="Settings" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">⚙️</button>
          <div style="width: 1px; height: 20px; background: rgba(255,255,255,0.1); margin: 0 2px;"></div>
          <div class="kofi-wrap">
            <button title="Support Antra on Patreon" on:click={() => kofiTooltipVisible = !kofiTooltipVisible} style="background: transparent; border: none; padding: 4px 6px; cursor: pointer; display: flex; align-items: center; opacity: 0.7; transition: opacity 0.15s;" on:mouseenter={(e) => e.currentTarget.style.opacity='1'} on:mouseleave={(e) => e.currentTarget.style.opacity='0.7'}>
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <circle cx="15.1" cy="10.3" r="6.4" fill="#FF424D"/>
                <rect x="2.6" y="3.3" width="4" height="17.4" fill="#ffffff"/>
              </svg>
            </button>
            {#if kofiTooltipVisible && supportStatus.enabled}
              <div class="kofi-tooltip">
                <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px;">
                  <p class="kofi-tooltip-title">{supportStatus.title}</p>
                  <button class="support-close-btn" on:click={() => kofiTooltipVisible = false} title="Close">×</button>
                </div>
                <p class="kofi-tooltip-body">{supportStatus.message}</p>
                {#if supportStatusLoading}
                  <p class="kofi-tooltip-refreshing">Refreshing…</p>
                {/if}
                <button class="kofi-tooltip-btn" on:click={() => BrowserOpenURL(supportStatus.link)}>
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><circle cx="15.1" cy="10.3" r="6.4" fill="#FF424D"/><rect x="2.6" y="3.3" width="4" height="17.4" fill="#ffffff"/></svg>
                  Support on Patreon
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
          <button title="Join our Discord community" on:click={() => BrowserOpenURL('https://discord.com/invite/UcY5cqMuE')} style="background: transparent; border: none; padding: 4px 6px; cursor: pointer; display: flex; align-items: center; opacity: 0.7; transition: opacity 0.15s;" on:mouseenter={(e) => e.currentTarget.style.opacity='1'} on:mouseleave={(e) => e.currentTarget.style.opacity='0.7'}>
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <circle cx="12" cy="12" r="12" fill="#5865F2"/>
              <path d="M8.12 7.75c1.02-.45 2.08-.7 3.14-.76l.15.29c-1.19.17-1.74.5-1.74.5s.15-.08.4-.18c.73-.31 1.31-.39 1.55-.41.04 0 .07-.01.11-.01.41-.05.88-.06 1.37-.01.64.07 1.33.26 2.03.61 0 0-.52-.31-1.65-.49l.21-.32c1.06.06 2.12.31 3.14.76 0 0 1.68 2.42 1.68 5.39 0 0-.98 1.67-3.55 1.76 0 0-.42-.5-.77-.93 1.54-.46 2.12-1.42 2.12-1.42-.48.32-.94.55-1.35.71-.58.24-1.14.39-1.69.47-1.12.2-2.15.14-3.03-.01-.67-.13-1.25-.31-1.73-.52-.27-.11-.57-.25-.87-.43-.04-.02-.08-.04-.12-.07-.02-.01-.03-.02-.05-.03-.22-.12-.34-.21-.34-.21s.56.93 2.06 1.4c-.35.44-.78.97-.78.97-2.57-.09-3.54-1.76-3.54-1.76 0-2.97 1.67-5.39 1.67-5.39Zm2.25 4.56c.66 0 1.19-.58 1.19-1.29 0-.71-.52-1.29-1.19-1.29-.66 0-1.19.58-1.19 1.29 0 .71.53 1.29 1.19 1.29Zm4.26 0c.66 0 1.19-.58 1.19-1.29 0-.71-.52-1.29-1.19-1.29-.66 0-1.19.58-1.19 1.29 0 .71.53 1.29 1.19 1.29Z" fill="white"/>
            </svg>
          </button>
        </div>
      </div>
      <!-- Mode toggle -->
      <div style="margin-top: 16px; display: flex; gap: 6px; margin-bottom: 8px;">
        <button
          on:click={() => { activeTab = 'library'; inputUrl = ''; if (!spotifyLibrary && !spotifyLibraryLoading && config.spotify_sp_dc) loadSpotifyLibrary(); }}
          style="font-size: 12px; padding: 4px 12px; opacity: {activeTab === 'library' ? 1 : 0.45}; border-color: {activeTab === 'library' ? 'var(--accent-color)' : 'rgba(255,255,255,0.1)'};">
          🎵 My Library
        </button>
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

      {#if activeTab === 'library'}
        <!-- My Library tab content rendered below health chips -->
      {:else if activeTab === 'artist'}
        <!-- Artist search input -->
        <div class="input-bar" style="display: flex; gap: 8px; align-items: center;">
          <input
            bind:value={searchQuery}
            placeholder="Artist name..."
            on:keydown={(e) => e.key === 'Enter' && startArtistSearch()}
            style="flex: 1; min-width: 0; font-family: inherit; font-size: 13px; height: 38px;"
          />
          <select bind:value={searchSource} class="discover-select"
                  style="height: 38px; width: auto; flex: 0 0 auto; min-width: 148px;"
                  title="Which catalogue to search">
            <option value="apple">Apple Music</option>
            <option value="spotify" disabled={!config.spotify_sp_dc}>Spotify{config.spotify_sp_dc ? '' : ' (connect account)'}</option>
          </select>
          <button on:click={startArtistSearch} disabled={!searchQuery.trim() || artistSearchLoading} style="height: 38px; white-space: nowrap;">
            {artistSearchLoading ? 'Searching...' : 'Search'}
          </button>
        </div>
      {:else if activeTab === 'discover'}
      {:else}
        <!-- v1.1.8 FEAT-8 Phase B — shown BEFORE the user pastes anything, so
             they are never surprised by a refusal after queueing an album. -->
        {#if !hasOwnCredential}
          <div class="signin-banner">
            <div style="flex: 1; min-width: 0;">
              <p style="margin: 0 0 4px; font-size: 12.5px; font-weight: 600; color: var(--accent-color);">Sign in to start downloading</p>
              <p style="margin: 0; font-size: 11px; color: #888; line-height: 1.5;">
                Antra now downloads through your own Antra account. It takes about 20 seconds —
                your password is never entered into this app, the browser handles it.
                Already a supporter? Pasting your supporter key works too.
              </p>
            </div>
            <button on:click={promptSignIn} style="flex-shrink: 0; background: var(--accent-color); color: #000; font-weight: 600; font-size: 12px; padding: 8px 16px;">
              Log in with Antra
            </button>
          </div>
        {/if}
        <!-- URL input -->
        <div class="input-bar" style="display: flex; gap: 8px; align-items: flex-start;">
          <textarea
            bind:this={inputUrlEl}
            bind:value={inputUrl}
            placeholder="Paste one or more Spotify / Apple Music / SoundCloud / Amazon Music URLs (one per line or comma-separated)..."
            disabled={isDownloading}
            rows="4"
            on:keydown={(e) => e.key === 'Enter' && e.ctrlKey && startDownload()}
            on:contextmenu={pasteClipboardIntoUrlBox}
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

      <!-- Single mirror-status chip + format selector.
           Replaces the five per-source chips: the public status page is now the
           source of truth, it carries far more detail than a colour could, and
           five chips meant five startup probes plus the endpoint list living in
           the client. `flex-shrink: 0` matters — the chips used to be squeezed
           whenever the format selector grew (e.g. the Atmos note). -->
      <div class="source-health-bar">
        <button
          class="mirror-status-chip {mirrorStatusTone}"
          on:click={() => BrowserOpenURL(STATUS_PAGE_URL)}
          title={mirrorStatusTitle}
        >
          <span class="mirror-status-dot"></span>
          <span class="mirror-status-label">{mirrorStatusLabel}</span>
        </button>
        <div class="format-selector">
          <div class="format-main-row">
            {#each formatOptions as fmt}
              <button
                class="format-pill"
                class:active={_fmtBase === fmt.value}
                title={fmt.label}
                on:click={() => setParentFormat(fmt.value)}
              >{fmt.name}</button>
            {/each}
            <!-- Dolby Atmos (v1.1.8 FEAT-1) — supporter-gated. isSupporter is
                 only true once the SERVER has confirmed the key (BUG-5), never
                 merely because a key string is present. -->
            <button
              class="format-pill format-pill--atmos"
              class:active={_fmtBase === 'atmos'}
              class:locked={!isSupporter}
              title={isSupporter
                ? 'Dolby Atmos — spatial mix from Tidal, Apple Music or Amazon Music, chosen by the link you paste'
                : 'Dolby Atmos is a supporter feature. Add a valid supporter key in Settings to unlock it.'}
              on:click={() => isSupporter
                ? setParentFormat('atmos')
                : (settingsNotice = 'Dolby Atmos requires a supporter key. Add yours in Settings → Supporter Key.')}
            >{isSupporter ? 'Atmos' : '🔒 Atmos'}</button>
          </div>
          {#if showBitDepthRow}
            <div class="format-sub-row">
              <button
                class="format-pill format-pill--sub"
                class:active={_fmtBitDepth === '16'}
                title="{_fmtBase === 'lossless' ? 'FLAC 16-bit — Deezer first, other mirrors as fallback' : 'ALAC 16-bit — standard lossless'}"
                on:click={() => setBitDepth('16')}
              >16-bit</button>
              <button
                class="format-pill format-pill--sub"
                class:active={_fmtBitDepth === '24'}
                title="{_fmtBase === 'lossless' ? 'FLAC 24-bit Hi-Res — Tidal / Qobuz preferred' : 'ALAC 24-bit Hi-Res — Apple Hi-Res Lossless'}"
                on:click={() => setBitDepth('24')}
              >24-bit</button>
            </div>
          {/if}
        </div>
      </div>
      <!-- Notices live OUTSIDE the flex row on purpose. While they sat inside it,
           selecting Atmos grew the format selector and visibly squeezed the
           status chips next to it — the layout jumped on every format change. -->
      {#if settingsNotice}
        <p class="format-notice">{settingsNotice}</p>
      {/if}
      {#if _fmtBase === 'atmos'}
        <p class="format-note">
          The Atmos mix is sourced from <strong>Tidal</strong>, <strong>Apple Music</strong> or
          <strong>Amazon Music</strong> — whichever service you paste a link from. Tracks with
          no Atmos mix will fail rather than silently download in stereo.
        </p>
      {/if}
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

    <!-- My Library tab: full library grid (Spotify / Apple Music switcher) -->
    {#if activeTab === 'library'}
    <div class="lib-scroll-area">
      <!-- Service switcher tabs -->
      <div class="lib-service-tabs">
        <button
          class="lib-service-tab"
          class:active={libActiveService === 'spotify'}
          on:click={() => { libActiveService = 'spotify'; if (config.spotify_sp_dc && !spotifyLibrary && !spotifyLibraryLoading) loadSpotifyLibrary(); }}
        >
          <span class="lib-tab-icon lib-tab-icon-spotify">
            <img src="/icons/spotify.png" alt="" />
          </span>
          Spotify
        </button>
        <button
          class="lib-service-tab"
          class:active={libActiveService === 'apple'}
          on:click={() => { libActiveService = 'apple'; if (config.apple_music_user_token && config.apple_authorization_token && !appleLibrary && !appleLibraryLoading) loadAppleMusicLibrary(); }}
        >
          <span class="lib-tab-icon lib-tab-icon-apple">
            <img src="/icons/apple-music.png" alt="" />
          </span>
          Apple Music
        </button>
      </div>

      {#if libActiveService === 'spotify'}
      {#if !config.spotify_sp_dc}
        <div class="discover-empty" style="margin-top: 24px;">
          <div style="font-size: 36px; margin-bottom: 12px; opacity: 0.3;">
            <img src="/icons/spotify.png" alt="Spotify" style="width:40px;height:40px;object-fit:contain;opacity:0.3;" />
          </div>
          <p style="opacity: 0.6;">Connect your Spotify account in Settings to see your library here.</p>
          <button on:click={() => { showSettings = true; }} style="margin-top: 8px; font-size: 12px;">Open Settings</button>
        </div>
      {:else if spotifyLibraryLoading}
        <div class="discover-empty" style="margin-top: 24px;">
          <div style="font-size: 28px; margin-bottom: 12px; opacity: 0.4;">⏳</div>
          <p style="opacity: 0.5;">Loading your Spotify library…</p>
        </div>
      {:else if spotifyLibraryError}
        <div class="discover-empty" style="margin-top: 24px;">
          <div style="font-size: 28px; margin-bottom: 12px; opacity: 0.4;">⚠️</div>
          <p style="opacity: 0.7; color: var(--error-color);">{spotifyLibraryError}</p>
          <button on:click={loadSpotifyLibrary} style="margin-top: 8px; font-size: 12px;">Retry</button>
        </div>
      {:else if spotifyLibrary}
        <div class="discover-sections" style="margin-top: 8px;">
          <!-- Liked Songs + Algorithmic mixes -->
          <div class="discover-section">
            <div class="discover-section-header">
              <button class="lib-collapse-btn" on:click={() => libMixesCollapsed = !libMixesCollapsed} title={libMixesCollapsed ? 'Expand' : 'Collapse'}>{libMixesCollapsed ? '▸' : '▾'}</button>
              <h3 class="discover-section-title">Your Mixes</h3>
              <div class="discover-section-rule"></div>
              <button class="lib-refresh-btn" on:click={loadSpotifyLibrary} disabled={spotifyLibraryLoading} title="Refresh library">↺</button>
            </div>
            {#if !libMixesCollapsed}
            <div class="discover-grid">
              <!-- Liked Songs card -->
              <!-- svelte-ignore a11y-click-events-have-key-events -->
              <!-- svelte-ignore a11y-no-static-element-interactions -->
              <div class="discovery-card lib-card" on:click={() => downloadPlaylistUrl('https://open.spotify.com/collection/tracks')}>
                <div class="discovery-artwork-wrapper lib-liked-bg">
                  <div class="lib-liked-icon">♥</div>
                  <div class="discovery-play-overlay">
                    <svg width="32" height="32" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
                  </div>
                </div>
                <div class="discovery-info">
                  <div class="discovery-title">Liked Songs</div>
                  <div class="discovery-subtitle">{spotifyLibrary.liked_songs_count} tracks</div>
                </div>
                <button
                  class="lib-sync-pill"
                  class:active={isPlaylistSyncing('https://open.spotify.com/collection/tracks')}
                  on:click|stopPropagation={() => togglePlaylistSync({ url: 'https://open.spotify.com/collection/tracks', name: 'Liked Songs', artwork_url: '', is_algorithmic: false })}
                  title={isPlaylistSyncing('https://open.spotify.com/collection/tracks') ? 'Auto-sync on — click to disable' : 'Enable auto-sync'}
                >↺</button>
              </div>
              <!-- Algorithmic playlists -->
              {#each spotifyLibrary.playlists.filter(p => p.is_algorithmic) as pl}
                <!-- svelte-ignore a11y-click-events-have-key-events -->
                <!-- svelte-ignore a11y-no-static-element-interactions -->
                <div class="discovery-card lib-card" on:click={() => downloadPlaylistUrl(pl.url)}>
                  <div class="discovery-artwork-wrapper">
                    {#if pl.image_url}
                      <img src={pl.image_url} alt={pl.name} class="discovery-artwork" loading="lazy" />
                    {:else}
                      <div class="lib-art-fallback">♫</div>
                    {/if}
                    <div class="discovery-play-overlay">
                      <svg width="32" height="32" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
                    </div>
                  </div>
                  <div class="discovery-info">
                    <div class="discovery-title" title={pl.name}>{pl.name}</div>
                    <div class="discovery-subtitle">{pl.track_count > 0 ? `${pl.track_count} tracks` : 'Playlist'}</div>
                  </div>
                  <button
                    class="lib-sync-pill"
                    class:active={isPlaylistSyncing(pl.url)}
                    on:click|stopPropagation={() => togglePlaylistSync(pl)}
                    title={isPlaylistSyncing(pl.url) ? 'Auto-sync on — click to disable' : 'Enable auto-sync'}
                  >↺</button>
                </div>
              {/each}
            </div>
            {/if}
          </div>

          <!-- Personal playlists -->
          {#if spotifyLibrary.playlists.filter(p => !p.is_algorithmic).length > 0}
            <div class="discover-section">
              <div class="discover-section-header">
                <button class="lib-collapse-btn" on:click={() => libPlaylistsCollapsed = !libPlaylistsCollapsed} title={libPlaylistsCollapsed ? 'Expand' : 'Collapse'}>{libPlaylistsCollapsed ? '▸' : '▾'}</button>
                <h3 class="discover-section-title">Your Playlists</h3>
                <span class="lib-section-count">{spotifyLibrary.playlists.filter(p => !p.is_algorithmic).length}</span>
                <div class="discover-section-rule"></div>
              </div>
              {#if !libPlaylistsCollapsed}
              <div class="discover-grid">
                {#each spotifyLibrary.playlists.filter(p => !p.is_algorithmic) as pl}
                  <!-- svelte-ignore a11y-click-events-have-key-events -->
                  <!-- svelte-ignore a11y-no-static-element-interactions -->
                  <div class="discovery-card lib-card" on:click={() => downloadPlaylistUrl(pl.url)}>
                    <div class="discovery-artwork-wrapper">
                      {#if pl.image_url}
                        <img src={pl.image_url} alt={pl.name} class="discovery-artwork" loading="lazy" />
                      {:else}
                        <div class="lib-art-fallback">♫</div>
                      {/if}
                      <div class="discovery-play-overlay">
                        <svg width="32" height="32" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
                      </div>
                    </div>
                    <div class="discovery-info">
                      <div class="discovery-title" title={pl.name}>{pl.name}</div>
                      <div class="discovery-subtitle">{pl.track_count > 0 ? `${pl.track_count} tracks` : 'Playlist'}</div>
                    </div>
                    <button
                      class="lib-sync-pill"
                      class:active={isPlaylistSyncing(pl.url)}
                      on:click|stopPropagation={() => togglePlaylistSync(pl)}
                      title={isPlaylistSyncing(pl.url) ? 'Auto-sync on — click to disable' : 'Enable auto-sync'}
                    >↺</button>
                  </div>
                {/each}
              </div>
              {/if}
            </div>
          {/if}

          <!-- Saved Albums -->
          {#if spotifyLibrary.saved_albums && spotifyLibrary.saved_albums.length > 0}
            <div class="discover-section">
              <div class="discover-section-header">
                <button class="lib-collapse-btn" on:click={() => libAlbumsCollapsed = !libAlbumsCollapsed} title={libAlbumsCollapsed ? 'Expand' : 'Collapse'}>{libAlbumsCollapsed ? '▸' : '▾'}</button>
                <h3 class="discover-section-title">Saved Albums</h3>
                <span class="lib-section-count">{spotifyLibrary.saved_albums.length}</span>
                <div class="discover-section-rule"></div>
              </div>
              {#if !libAlbumsCollapsed}
              <div class="discover-grid">
                {#each spotifyLibrary.saved_albums as al}
                  <!-- svelte-ignore a11y-click-events-have-key-events -->
                  <!-- svelte-ignore a11y-no-static-element-interactions -->
                  <div class="discovery-card lib-card" on:click={() => downloadPlaylistUrl(al.url)}>
                    <div class="discovery-artwork-wrapper">
                      {#if al.image_url}
                        <img src={al.image_url} alt={al.name} class="discovery-artwork" loading="lazy" />
                      {:else}
                        <div class="lib-art-fallback">♫</div>
                      {/if}
                      <div class="discovery-play-overlay">
                        <svg width="32" height="32" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
                      </div>
                    </div>
                    <div class="discovery-info">
                      <div class="discovery-title" title={al.name}>{al.name}</div>
                      <div class="discovery-subtitle" title={al.artists}>{al.artists || ''}{al.year ? ` · ${al.year}` : ''}</div>
                    </div>
                  </div>
                {/each}
              </div>
              {/if}
            </div>
          {/if}

          <!-- Followed Artists -->
          {#if spotifyLibrary.followed_artists && spotifyLibrary.followed_artists.length > 0}
            <div class="discover-section">
              <div class="discover-section-header">
                <button class="lib-collapse-btn" on:click={() => libArtistsCollapsed = !libArtistsCollapsed} title={libArtistsCollapsed ? 'Expand' : 'Collapse'}>{libArtistsCollapsed ? '▸' : '▾'}</button>
                <h3 class="discover-section-title">Followed Artists</h3>
                <span class="lib-section-count">{spotifyLibrary.followed_artists.length}</span>
                <div class="discover-section-rule"></div>
              </div>
              {#if !libArtistsCollapsed}
              <div class="discover-grid">
                {#each spotifyLibrary.followed_artists as ar}
                  <!-- svelte-ignore a11y-click-events-have-key-events -->
                  <!-- svelte-ignore a11y-no-static-element-interactions -->
                  <div class="discovery-card lib-card" on:click={() => openArtistFromUrl(ar.url)}>
                    <div class="discovery-artwork-wrapper lib-artist-art">
                      {#if ar.image_url}
                        <img src={ar.image_url} alt={ar.name} class="discovery-artwork" loading="lazy" />
                      {:else}
                        <div class="lib-art-fallback">🎤</div>
                      {/if}
                      <div class="discovery-play-overlay">
                        <svg width="32" height="32" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
                      </div>
                    </div>
                    <div class="discovery-info">
                      <div class="discovery-title" title={ar.name}>{ar.name}</div>
                      <div class="discovery-subtitle">Artist</div>
                    </div>
                  </div>
                {/each}
              </div>
              {/if}
            </div>
          {/if}
        </div>
      {:else}
        <div class="discover-empty" style="margin-top: 24px;">
          <div style="font-size: 28px; margin-bottom: 12px; opacity: 0.4;">🎵</div>
          <p style="opacity: 0.5;">Click Refresh to load your library.</p>
          <button on:click={loadSpotifyLibrary} style="margin-top: 8px; font-size: 12px;">Load Library</button>
        </div>
      {/if}
      {/if}<!-- /spotify -->

      <!-- Apple Music library -->
      {#if libActiveService === 'apple'}
      {#if !config.apple_music_user_token || !config.apple_authorization_token}
        <div class="discover-empty" style="margin-top: 24px;">
          <div style="font-size: 36px; margin-bottom: 12px; opacity: 0.3;">
            <img src="/icons/apple-music.png" alt="Apple Music" style="width:40px;height:40px;object-fit:contain;opacity:0.3;" />
          </div>
          <p style="opacity: 0.6;">Connect your Apple Music account in Settings to see your library here.</p>
          <button on:click={() => { showSettings = true; }} style="margin-top: 8px; font-size: 12px;">Open Settings</button>
        </div>
      {:else if appleLibraryLoading}
        <div class="discover-empty" style="margin-top: 24px;">
          <div style="font-size: 28px; margin-bottom: 12px; opacity: 0.4;">⏳</div>
          <p style="opacity: 0.5;">Loading your Apple Music library…</p>
        </div>
      {:else if appleLibraryError}
        <div class="discover-empty" style="margin-top: 24px;">
          <div style="font-size: 28px; margin-bottom: 12px; opacity: 0.4;">⚠️</div>
          <p style="opacity: 0.7; color: var(--error-color);">{appleLibraryError}</p>
          <button on:click={loadAppleMusicLibrary} style="margin-top: 8px; font-size: 12px;">Retry</button>
        </div>
      {:else if appleLibrary}
        <div class="discover-sections" style="margin-top: 8px;">
          <!-- Saved Songs -->
          <div class="discover-section">
            <div class="discover-section-header">
              <h3 class="discover-section-title">Your Music</h3>
              <div class="discover-section-rule"></div>
              <button class="lib-refresh-btn" on:click={loadAppleMusicLibrary} disabled={appleLibraryLoading} title="Refresh library">↺</button>
            </div>
            <div class="discover-grid">
              <!-- Saved Songs card -->
              <!-- svelte-ignore a11y-click-events-have-key-events -->
              <!-- svelte-ignore a11y-no-static-element-interactions -->
              <div class="discovery-card lib-card" on:click={() => downloadPlaylistUrl('apple-music://library/songs')}>
                <div class="discovery-artwork-wrapper lib-apple-saved-bg">
                  <div class="lib-liked-icon">♥</div>
                  <div class="discovery-play-overlay">
                    <svg width="32" height="32" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
                  </div>
                </div>
                <div class="discovery-info">
                  <div class="discovery-title">Saved Songs</div>
                  <div class="discovery-subtitle">{appleLibrary.saved_songs_count} tracks</div>
                </div>
              </div>
              <!-- Algorithmic playlists -->
              {#each appleLibrary.playlists.filter(p => p.is_algorithmic) as pl}
                <!-- svelte-ignore a11y-click-events-have-key-events -->
                <!-- svelte-ignore a11y-no-static-element-interactions -->
                <div class="discovery-card lib-card" on:click={() => downloadPlaylistUrl(pl.url)}>
                  <div class="discovery-artwork-wrapper">
                    {#if pl.image_url}
                      <img src={pl.image_url} alt={pl.name} class="discovery-artwork" loading="lazy" />
                    {:else}
                      <div class="lib-art-fallback">♫</div>
                    {/if}
                    <div class="discovery-play-overlay">
                      <svg width="32" height="32" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
                    </div>
                  </div>
                  <div class="discovery-info">
                    <div class="discovery-title" title={pl.name}>{pl.name}</div>
                    <div class="discovery-subtitle">{pl.track_count > 0 ? `${pl.track_count} tracks` : 'Playlist'}</div>
                  </div>
                </div>
              {/each}
            </div>
          </div>

          <!-- Personal playlists -->
          {#if appleLibrary.playlists.filter(p => !p.is_algorithmic).length > 0}
            <div class="discover-section">
              <div class="discover-section-header">
                <h3 class="discover-section-title">Your Playlists</h3>
                <div class="discover-section-rule"></div>
              </div>
              <div class="discover-grid">
                {#each appleLibrary.playlists.filter(p => !p.is_algorithmic) as pl}
                  <!-- svelte-ignore a11y-click-events-have-key-events -->
                  <!-- svelte-ignore a11y-no-static-element-interactions -->
                  <div class="discovery-card lib-card" on:click={() => downloadPlaylistUrl(pl.url)}>
                    <div class="discovery-artwork-wrapper">
                      {#if pl.image_url}
                        <img src={pl.image_url} alt={pl.name} class="discovery-artwork" loading="lazy" />
                      {:else}
                        <div class="lib-art-fallback">♫</div>
                      {/if}
                      <div class="discovery-play-overlay">
                        <svg width="32" height="32" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
                      </div>
                    </div>
                    <div class="discovery-info">
                      <div class="discovery-title" title={pl.name}>{pl.name}</div>
                      <div class="discovery-subtitle">{pl.track_count > 0 ? `${pl.track_count} tracks` : 'Playlist'}</div>
                    </div>
                  </div>
                {/each}
              </div>
            </div>
          {/if}
        </div>
      {:else}
        <div class="discover-empty" style="margin-top: 24px;">
          <div style="font-size: 28px; margin-bottom: 12px; opacity: 0.4;">🎵</div>
          <p style="opacity: 0.5;">Click Refresh to load your Apple Music library.</p>
          <button on:click={loadAppleMusicLibrary} style="margin-top: 8px; font-size: 12px;">Load Library</button>
        </div>
      {/if}
      {/if}<!-- /apple -->

    </div><!-- /lib-scroll-area -->
    {/if}

    <!-- Tracklist: shows all tracks that have started, persists until next download -->
    {#if activeTab !== 'library'}
    <div class="tracklist-wrapper">
      <div class="tracklist" bind:this={tracklistEl} on:scroll={updateTracklistScroll}>
        {#if trackOrder.length === 0}
          <div class="tracklist-empty">
            {#if !config.spotify_sp_dc && historyItems.length === 0}
              <p>Paste a URL above and press <strong>Add to Library</strong></p>
            {/if}
          </div>
        {:else}
          {#each trackOrder as trackKey (trackKey)}
            {#if trackKey.startsWith('__SEP__')}
              {@const sep = separatorMeta[trackKey]}
              <div class="tracklist-album-sep">
                {#if sep?.artwork}
                  <img src={sep.artwork} alt="" class="sep-artwork" />
                {/if}
                <span class="sep-title">{sep?.title || 'Next album'}</span>
              </div>
            {:else}
              {@const state = activeTracks[trackKey]}
              {@const trackLabel = trackLabels[trackKey] || trackKey}
              {#if state}
                <div class="track-row"
                  class:track-done={state.status === 'done'}
                  class:track-failed={state.status === 'failed'}
                  class:track-skipped={state.status === 'skipped'}>
                  <div class="track-row-main">
                    <div class="track-row-head">
                      <span class="track-row-name">{trackLabel}</span>
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
    {/if}<!-- /activeTab !== 'library' -->

    <!-- ── Failed Tracks Panel (ST-4) ──────────────────────────────────────── -->
    {#if activeTab !== 'library' && failedEntries.length > 0}
    <div class="failed-panel">
      <div class="failed-panel-head">
        <button class="failed-collapse-btn"
          on:click={() => failedPanelCollapsed = !failedPanelCollapsed}
          title={failedPanelCollapsed ? 'Expand failed tracks' : 'Minimize failed tracks'}>
          {failedPanelCollapsed ? '▸' : '▾'}
        </button>
        <span class="failed-panel-title">✗ Failed ({failedEntries.length})</span>
        <div class="failed-panel-actions">
          {#if retryQueue.length > 0}
            <span class="failed-retry-progress">Retrying {retryQueueTotal - retryQueue.length + 1} / {retryQueueTotal}…</span>
          {:else}
            <button class="failed-action-btn"
              on:click={retryAllFailed}
              disabled={isDownloading || failedEntries.every(e => !e.trackData)}
              title="Retry all failed tracks one by one">↻ Retry All</button>
          {/if}
          <button class="failed-action-btn failed-dismiss-all"
            on:click={dismissAllFailures}
            title="Dismiss all failed tracks from this view">× Dismiss All</button>
        </div>
      </div>
      {#if !failedPanelCollapsed}
      <div class="failed-list">
        {#each failedEntries as entry (entry.key)}
          <div class="failed-entry">
            <div class="failed-entry-info">
              <span class="failed-entry-label">{entry.label}</span>
              <span class="failed-entry-error">{entry.error}</span>
            </div>
            <div class="failed-entry-btns">
              <button class="failed-retry-btn"
                on:click={() => retryFailedTrack(entry.key)}
                disabled={isDownloading || !entry.trackData}
                title="Retry this track">↻</button>
              <button class="failed-close-btn"
                on:click={() => dismissFailure(entry.key)}
                title="Dismiss">×</button>
            </div>
          </div>
        {/each}
      </div>
      {/if}
    </div>
    {/if}<!-- /failed panel -->

    <!-- Floating log toggle button (not shown on Library tab) -->
    {#if activeTab !== 'library'}
    <button class="log-toggle" on:click={() => showLog = !showLog} title="Activity Log">
      📋 {showLog ? 'Hide Log' : 'Log'}
    </button>
    {/if}
  </main>

  <!-- ── Discover full-screen overlay ──────────────────────────────────────── -->
  {#if activeTab === 'discover'}
  <div class="discover-overlay">
    <!-- Top bar: title + filters + close -->
    <div class="discover-topbar">
      <div class="discover-topbar-left">
        <span class="discover-topbar-title">🌟 Discover</span>
        <select bind:value={discoverySource}
                on:change={() => { loadDiscoveryGenres(); loadDiscoveryData(); }}
                class="discover-select" title="Where Discover content comes from">
          <option value="apple">Apple Music</option>
          <option value="spotify" disabled={!config.spotify_sp_dc}>Spotify{config.spotify_sp_dc ? '' : ' (connect account)'}</option>
        </select>
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
        {#if discoveryGenres.length}
          <select bind:value={discoveryGenre} on:change={loadDiscoveryData} class="discover-select discover-select-genre">
            <option value="">Top Charts (All Genres)</option>
            {#each discoveryGenres as genre}
              <option value={genre.id}>{genre.name}</option>
            {/each}
          </select>
        {/if}
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
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none"><circle cx="15.1" cy="10.3" r="6.4" fill="#FF424D"/><rect x="2.6" y="3.3" width="4" height="17.4" fill="#ffffff"/></svg>
    </div>
    <div class="sponsor-toast-body">
      <p class="sponsor-toast-title">{supportStatus.title}</p>
      <p class="sponsor-toast-text">{supportStatus.message}</p>
      <button class="sponsor-toast-btn" on:click={() => { BrowserOpenURL(supportStatus.link); dismissSponsorToast(); }}>
        Support on Patreon
      </button>
    </div>
    <button class="sponsor-toast-close" on:click={dismissSponsorToast} title="Dismiss">×</button>
  </div>
{/if}

<!-- ── Album Availability Studio ─────────────────────────────────────────── -->
{#if showAvailability}
<div class="modal-overlay" on:click={() => showAvailability = false}>
  <div class="modal-content" on:click|stopPropagation style="max-width: 700px; width: 100%; max-height: 88vh; display: flex; flex-direction: column;">

    <!-- Header -->
    <div style="display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid rgba(255,255,255,0.08); padding-bottom:16px; margin-bottom:16px; flex-shrink:0;">
      <div>
        <h3 style="margin:0 0 4px; color:var(--accent-color); font-size:16px;">🌍 Album Availability Studio</h3>
        <p style="margin:0; font-size:12px; color:#555; line-height:1.4;">Paste a single Spotify or Deezer album link to inspect label, UPC, country coverage, and a live market view.</p>
      </div>
      <button on:click={() => showAvailability = false} style="padding:4px 10px; font-size:12px; flex-shrink:0; margin-left:16px;">✕ Close</button>
    </div>

    <!-- URL input row -->
    <div style="display:flex; gap:8px; flex-shrink:0; margin-bottom:20px;">
      <input
        type="text"
        bind:value={availabilityUrl}
        placeholder="https://open.spotify.com/album/... or deezer.com/album/..."
        style="flex:1; font-size:13px; box-sizing:border-box;"
        on:keydown={(e) => e.key === 'Enter' && !availabilityLoading && inspectAlbum()}
      />
      <button
        on:click={inspectAlbum}
        disabled={availabilityLoading || !availabilityUrl.trim()}
        style="padding:7px 16px; font-size:13px; white-space:nowrap; flex-shrink:0;"
      >{availabilityLoading ? '⏳ Inspecting…' : '🔍 Inspect Album'}</button>
    </div>

    <!-- Results / empty state -->
    <div style="flex:1; overflow-y:auto; padding-right:4px;">

      {#if availabilityError}
        <div style="background:rgba(255,85,85,0.08); border:1px solid rgba(255,85,85,0.25); border-radius:8px; padding:14px 16px; color:#ff8888; font-size:13px;">
          ⚠ {availabilityError}
        </div>

      {:else if availabilityResult}
        <!-- Album header -->
        <div style="display:flex; gap:16px; align-items:flex-start; margin-bottom:20px;">
          {#if availabilityResult.artwork_url}
            <img src={availabilityResult.artwork_url} alt="" style="width:80px; height:80px; border-radius:8px; object-fit:cover; flex-shrink:0;" />
          {/if}
          <div style="flex:1; min-width:0;">
            <p style="margin:0 0 2px; font-size:16px; font-weight:700; color:var(--text-primary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{availabilityResult.release_name}</p>
            <p style="margin:0 0 2px; font-size:13px; color:#888;">{availabilityResult.artist}{availabilityResult.year ? ` · ${availabilityResult.year}` : ''}</p>
            {#if availabilityResult.label}
              <p style="margin:0 0 2px; font-size:11px; color:#555;">Label: {availabilityResult.label}</p>
            {/if}
            {#if availabilityResult.upc}
              <p style="margin:0; font-size:11px; color:#555; font-family:monospace;">UPC: {availabilityResult.upc}</p>
            {/if}
          </div>
        </div>

        <!-- Stats row -->
        {#if availabilityResult.stats?.length}
          <div style="display:flex; flex-wrap:wrap; gap:10px; margin-bottom:20px;">
            {#each availabilityResult.stats as stat}
              <div style="background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.08); border-radius:8px; padding:10px 16px; text-align:center; flex:1; min-width:90px;">
                <div style="font-size:20px; font-weight:700; color:var(--accent-color);">{stat.value}</div>
                <div style="font-size:10px; color:#666; margin-top:2px; white-space:nowrap;">{stat.label}</div>
              </div>
            {/each}
          </div>
        {/if}

        <!-- Segments -->
        {#each (availabilityResult.segments || []) as seg}
          {#if seg.codes?.length}
            <div style="margin-bottom:16px;">
              <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px;">
                <div style="width:10px; height:10px; border-radius:50%; background:{availabilityToneColor(seg.tone)}; flex-shrink:0;"></div>
                <span style="font-size:12px; font-weight:600; color:var(--text-primary);">{seg.label}</span>
                <span style="font-size:11px; color:#555;">({seg.codes.length})</span>
              </div>
              <div style="display:flex; flex-wrap:wrap; gap:4px; padding-left:18px;">
                {#each seg.codes as code}
                  <span style="font-size:11px; font-family:monospace; background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.08); border-radius:4px; padding:2px 6px; color:{availabilityToneColor(seg.tone)};">{code}</span>
                {/each}
              </div>
            </div>
          {/if}
        {/each}

        <!-- Notes -->
        {#if availabilityResult.notes?.length}
          <div style="margin-top:16px; border-top:1px solid rgba(255,255,255,0.05); padding-top:12px;">
            {#each availabilityResult.notes as note}
              <p style="margin:0 0 6px; font-size:11px; color:#444; line-height:1.4;">{note}</p>
            {/each}
          </div>
        {/if}

      {:else if !availabilityLoading}
        <!-- Empty state -->
        <div style="text-align:center; padding:60px 0; color:#444;">
          <div style="font-size:52px; margin-bottom:16px; opacity:0.3;">🌍</div>
          <p style="margin:0; font-size:13px;">Paste a Spotify or Deezer album URL above to see<br>country-by-country availability.</p>
        </div>
      {:else}
        <div style="text-align:center; padding:60px 0; color:#555; font-size:13px;">
          Probing markets in parallel… this takes up to 30 seconds for Spotify.
        </div>
      {/if}
    </div>

  </div>
</div>
{/if}

<!-- ── Themes Panel ────────────────────────────────────────────────────────── -->
{#if showThemes}
<div class="themes-overlay" on:click={() => showThemes = false}>
  <div class="themes-panel" on:click|stopPropagation>
    <div class="themes-header">
      <div>
        <h2 class="themes-title">🎨 Themes</h2>
        <p class="themes-subtitle">Choose from Antra originals or streaming-service-inspired looks. Your selection saves instantly.</p>
      </div>
      <button on:click={() => showThemes = false} style="padding: 6px 14px; font-size: 13px; flex-shrink: 0;">Close</button>
    </div>
    <div class="themes-body">
      <!-- Typeface (v1.1.8 FEAT-10) -->
      <div class="themes-section">
        <div class="themes-section-head">
          <span class="themes-section-label">TYPEFACE</span>
          <span class="themes-section-sub">Applies across the whole app</span>
        </div>
        <div class="font-grid">
          {#each FONTS as f}
            <button
              class="font-card{(config.font || 'antra') === f.id ? ' font-card--active' : ''}"
              on:click={() => applyFont(f.id)}
            >
              <!-- Each card previews in its own face, so the choice is visible
                   before it is applied. -->
              <span class="font-card-sample" style="font-family:{f.css};">Aa</span>
              <span class="font-card-body">
                <span class="font-card-name" style="font-family:{f.css};">{f.label}</span>
                <span class="font-card-desc">{f.desc}</span>
              </span>
              {#if (config.font || 'antra') === f.id}<span class="font-card-badge">ACTIVE</span>{/if}
            </button>
          {/each}
        </div>
      </div>

      <!-- Notification sound (v1.1.8 FEAT-13) -->
      <div class="themes-section">
        <div class="themes-section-head">
          <span class="themes-section-label">NOTIFICATION SOUND</span>
          <span class="themes-section-sub">Plays when a track is saved · click a card to hear it</span>
        </div>
        <div class="font-grid">
          {#each SOUNDS as s}
            <button
              class="font-card{(config.notify_sound || 'crystal') === s.id ? ' font-card--active' : ''}"
              on:click={() => chooseSound(s.id)}
            >
              <span class="font-card-sample sfx-glyph">{s.icon}</span>
              <span class="font-card-body">
                <span class="font-card-name">{s.label}</span>
                <span class="font-card-desc">{s.desc}</span>
              </span>
              {#if (config.notify_sound || 'crystal') === s.id}<span class="font-card-badge">ACTIVE</span>{/if}
            </button>
          {/each}
        </div>

        {#if (config.notify_sound || 'crystal') !== 'off'}
          <div class="sfx-controls">
            <label class="sfx-vol">
              <span class="sfx-vol-label">Volume</span>
              <input
                type="range" min="0" max="100" step="5"
                bind:value={config.notify_volume}
                on:input={onVolumeChange}
              />
              <span class="sfx-vol-num">{config.notify_volume ?? 70}%</span>
            </label>
            <label class="sfx-check">
              <input
                type="checkbox"
                bind:checked={config.notify_batch_only}
                on:change={() => SaveConfig(config)}
              />
              <span>
                Only when the whole download finishes
                <small>Silences the per-track sound — useful for large albums. The finish flourish still plays.</small>
              </span>
            </label>
          </div>
        {/if}
      </div>

      <div class="themes-section">
        <div class="themes-section-head">
          <span class="themes-section-label">ANTRA ORIGINALS</span>
          <span class="themes-section-sub">Original Themes</span>
        </div>
        <div class="themes-grid">
          {#each THEMES.filter(t => t.cat === 'original') as t}
            <button
              class="theme-card{config.theme === t.id || (!config.theme && t.id === 'antra') ? ' theme-card--active' : ''}"
              on:click={() => applyTheme(t.id)}
            >
              <div class="theme-card-preview" style="background:{t.preview}; color:{t.tone};">
                <div class="theme-card-preview-top">
                  <span class="theme-card-preview-pill"></span>
                  <span class="theme-card-preview-pill"></span>
                  <span class="theme-card-preview-pill"></span>
                </div>
                <div class="theme-card-preview-lines">
                  <span style="width:68%;"></span>
                  <span style="width:48%;"></span>
                </div>
              </div>
              <div class="theme-card-swatches">
                {#each t.colors as c}
                  <div class="theme-card-swatch" style="background:{c};"></div>
                {/each}
              </div>
              <div class="theme-card-name">{t.label}</div>
              <div class="theme-card-desc">{t.desc}</div>
              {#if config.theme === t.id || (!config.theme && t.id === 'antra')}
                <div class="theme-card-active-badge">ACTIVE</div>
              {/if}
            </button>
          {/each}
        </div>
      </div>
      <div class="themes-section">
        <div class="themes-section-head">
          <span class="themes-section-label">STREAMING SERVICES</span>
          <span class="themes-section-sub">Resolver-Inspired Themes</span>
        </div>
        <div class="themes-grid">
          {#each THEMES.filter(t => t.cat === 'service') as t}
            <button
              class="theme-card{config.theme === t.id ? ' theme-card--active' : ''}"
              on:click={() => applyTheme(t.id)}
            >
              <div class="theme-card-preview" style="background:{t.preview}; color:{t.tone};">
                <div class="theme-card-preview-top">
                  <span class="theme-card-preview-pill"></span>
                  <span class="theme-card-preview-pill"></span>
                  <span class="theme-card-preview-pill"></span>
                </div>
                <div class="theme-card-preview-lines">
                  <span style="width:68%;"></span>
                  <span style="width:48%;"></span>
                </div>
              </div>
              <div class="theme-card-swatches">
                {#each t.colors as c}
                  <div class="theme-card-swatch" style="background:{c};"></div>
                {/each}
              </div>
              <div style="display:flex; align-items:center; gap:6px; margin-top:2px;">
                {#if t.icon}
                  <img src={t.icon} alt="" class="theme-card-icon" />
                {:else}
                  <div style="width:14px; height:14px; border-radius:50%; background:{t.colors[2]}; flex-shrink:0; opacity:0.9;"></div>
                {/if}
                <div class="theme-card-name">{t.label}</div>
              </div>
              <div class="theme-card-desc">{t.desc}</div>
              {#if config.theme === t.id}
                <div class="theme-card-active-badge">ACTIVE</div>
              {/if}
            </button>
          {/each}
        </div>
      </div>
    </div>
  </div>
</div>
{/if}

<!-- ── Downloaded Music Modal ─────────────────────────────────────────────── -->
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

    {#if showLyrics && currentPlayerTrack}
      <div class="lyrics-panel">
        {#if lyricsLoading}
          <div class="lyrics-empty">Loading lyrics…</div>
        {:else if lyricsLines.length === 0}
          <div class="lyrics-empty">No lyrics embedded in this track.</div>
        {:else}
          <div class="lyrics-lines" bind:this={lyricsContainerEl}>
            {#each lyricsLines as line, i}
              <div class="lyrics-line" class:lyrics-active={i === activeLyricIdx}>
                {line.text || ' '}
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {/if}

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
        <button
          on:click={() => showLyrics = !showLyrics}
          class:lyrics-btn-active={showLyrics}
          title="Toggle lyrics"
          style="font-size:14px; padding: 3px 8px;"
        >♪</button>
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

      <!-- ── Antra Access Key ──────────────────────────────────────────────── -->
      <div id="access-key-section" class="field" style="display:flex; flex-direction:column; gap:12px; padding:0; background:none; border:none;">

        <!-- Patreon promo box — hidden for anyone who already has supporter
             tier by EITHER route, not merely anyone with a key in the box. -->
        {#if !isSupporter}
        <div style="background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1); border-radius: 10px; padding: 14px 16px;">
          <p style="font-size: 13px; font-weight: 700; margin: 0 0 6px; color: var(--accent-color);">🎁 Want the full experience?</p>
          <p style="font-size: 11.5px; color: var(--text-secondary); margin: 0 0 12px; line-height: 1.5;">
            Need <strong>unlimited downloads</strong>, <strong>2× faster speed</strong>, and <strong>concurrent downloads</strong>? Support on Patreon to get a supporter key with no limits — it’s emailed to you automatically after your payment.
          </p>
          <button
            on:click={() => BrowserOpenURL('https://patreon.com/AntraVerse')}
            style="font-size: 12px; padding: 7px 14px; background: var(--accent-color); color: #000; border: none; border-radius: 6px; cursor: pointer; font-weight: 600;"
          >♥ Support on Patreon →</button>
        </div>
        {/if}

        <!-- Key input -->
        <!-- v1.1.8 FEAT-8 — account login. The app never sees a password: it
             shows a short code and the browser does the actual sign-in. -->
        <div style="background: rgba(0,255,204,0.03); border: 1px solid rgba(0,255,204,0.12); border-radius: 10px; padding: 14px 16px; margin-bottom: 14px;">
          <p style="font-size: 13px; font-weight: 600; margin: 0 0 4px; color: var(--accent-color);">👤 Antra Account</p>
          {#if isSignedIn}
            <p style="font-size: 11px; color: #86efac; margin: 0 0 4px;">
              ✓ Signed in{(accountStatus?.username || deviceLoginUser) ? ` as ${accountStatus?.username || deviceLoginUser}` : ''}{accountIsSupporter ? ' — supporter' : ''}
            </p>
            <!-- Tier is reported from the server's verification of the token.
                 "Could not check" is deliberately distinct from "free": an
                 outage must never look like a downgrade, and it must never
                 discard a separately-valid supporter key. -->
            {#if accountStatus && accountStatus.reachable && !accountStatus.valid}
              <p style="font-size: 11px; color: #fca5a5; margin: 0 0 10px;">
                ✗ This session has ended — sign in again.
              </p>
            {:else if accountStatus && !accountStatus.reachable}
              <p style="font-size: 11px; color: #fcd34d; margin: 0 0 10px;">
                ⚠ Could not check your account tier right now{keyIsSupporter ? ' — your supporter key is active, so supporter features stay on.' : '.'}
              </p>
            {:else if accountStatus?.valid && !accountIsSupporter}
              <p style="font-size: 11px; color: #888; margin: 0 0 10px;">
                Free tier{keyIsSupporter ? ' — supporter features come from your key below.' : '. Claim your supporter key on the website to link it to this account.'}
              </p>
            {/if}
            <button on:click={signOutDevice} style="font-size: 11px; padding: 6px 12px;">Sign out</button>
          {:else if deviceLoginState === 'waiting'}
            <p style="font-size: 11px; color: #666; margin: 0 0 8px; line-height: 1.45;">
              Your browser has been opened. Sign in, then enter this code:
            </p>
            <p style="font-family: monospace; font-size: 22px; letter-spacing: 3px; color: var(--accent-color); margin: 0 0 8px;">{deviceUserCode}</p>
            <p style="font-size: 11px; color: #888; margin: 0 0 8px;">Waiting for approval…</p>
            <button on:click={() => { stopDevicePolling(); deviceLoginState = 'idle'; }} style="font-size: 11px; padding: 6px 12px;">Cancel</button>
          {:else}
            <p style="font-size: 11px; color: #666; margin: 0 0 12px; line-height: 1.45;">
              Sign in with your Antra website account. Your password is never entered into
              this app — the browser handles it, and this device gets its own revocable token.
            </p>
            <button
              on:click={beginDeviceLogin}
              disabled={deviceLoginState === 'starting'}
              style="font-size: 12px; padding: 8px 16px; background: var(--accent-color); color: #000; font-weight: 600;"
            >{deviceLoginState === 'starting' ? 'Opening browser…' : 'Log in with Antra'}</button>
            {#if deviceLoginState === 'error'}
              <p style="font-size: 11px; color: #fca5a5; margin: 8px 0 0;">{deviceLoginError}</p>
            {/if}
          {/if}
        </div>

        <div style="background: rgba(0,255,204,0.03); border: 1px solid rgba(0,255,204,0.12); border-radius: 10px; padding: 14px 16px;">
          <p style="font-size: 13px; font-weight: 600; margin: 0 0 4px; color: var(--accent-color);">🔑 Supporter Key</p>
          <p style="font-size: 11px; color: #666; margin: 0 0 12px; line-height: 1.45;">
            {#if accountIsSupporter}
              Your account already carries supporter tier, so you don’t need a key here. Pasting one is harmless.
            {:else}
              Already a supporter? Your key is emailed to you automatically after your Patreon payment — paste it below. Message me on Telegram or Discord if it hasn’t arrived.
            {/if}
          </p>
          <div style="display: flex; gap: 8px; align-items: center;">
            <input
              type="password"
              bind:value={config.antra_api_key}
              on:input={scheduleKeyValidation}
              placeholder="Paste your supporter key here…"
              style="flex: 1; box-sizing: border-box; font-family: monospace; font-size: 12px;"
            />
            <button
              on:click={() => validateSupporterKey(true)}
              disabled={keyState === 'validating' || !config.antra_api_key}
              style="flex-shrink: 0; font-size: 11px; padding: 6px 12px;"
            >{keyState === 'validating' ? 'Checking…' : 'Verify'}</button>
          </div>
          <!-- v1.1.8 BUG-5: the key is validated against the server. The old UI
               showed "✓ Supporter" for ANY non-empty string, so a typo looked
               like success. Note "could not reach the server" is deliberately
               NOT treated as an invalid key — a genuinely valid key must still
               be saveable while offline. -->
          {#if keyState === 'validating'}
            <p style="font-size: 11px; color: #888; margin: 8px 0 0;">Validating key…</p>
          {:else if keyState === 'valid'}
            <p style="font-size: 11px; color: #86efac; margin: 8px 0 0;">
              ✓ {keyInfo?.key_type === 'premium' ? 'Premium' : 'Supporter'} key active{keyInfo?.expires_at ? ` — expires ${formatKeyExpiry(keyInfo.expires_at)}` : ' — no expiry'}
              {#if keyInfo && keyInfo.download_limit}
                <br />Downloads: {keyInfo.download_count ?? 0} / {keyInfo.download_limit}
              {:else if keyInfo?.is_supporter}
                <br />Unlimited downloads
              {/if}
            </p>
          {:else if keyState === 'invalid'}
            <p style="font-size: 11px; color: #fca5a5; margin: 8px 0 0;">
              ✗ {keyError || 'Key not recognized'} — this key will not unlock supporter features.
            </p>
          {:else if keyState === 'unverified'}
            <p style="font-size: 11px; color: #fcd34d; margin: 8px 0 0;">
              ⚠ Could not reach the Antra servers, so this key could not be verified. It has been
              saved — it will be checked again next time you are online.
            </p>
          {/if}
        </div>
      </div>

      <div class="field">
        <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
          <input type="checkbox" bind:checked={config.prefer_explicit} style="margin-top: 2px;" />
          <div>
            <span style="font-weight: 500; font-size: 13px;">Prefer explicit versions</span>
            <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Avoid radio edits and clean versions. When a track is marked explicit, Antra keeps searching if the first result looks censored.</p>
          </div>
        </label>

        <div style="margin-top: 14px;">
          <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
            <input type="checkbox" bind:checked={config.strict_matching} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">Strict matching mode</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Opt-in safety mode for niche music. Antra uses stricter duration and confidence checks, and will fail uncertain tracks instead of downloading risky matches.</p>
            </div>
          </label>
        </div>

        <div style="margin-top: 14px;">
          <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
            <input type="checkbox" bind:checked={config.strict_format} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">Strict format — never accept a lower-quality substitute</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">When you ask for a lossless format and only a lossy copy is available, fail the track instead of silently saving an MP3/AAC. Failed tracks appear in the Failed panel with a retry button and the reason.</p>
            </div>
          </label>
        </div>

        <div style="margin-top: 14px;">
          <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
            <input type="checkbox" bind:checked={config.prevent_lossy_transcode} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">Never re-encode lossy audio</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Converting one lossy format into another (e.g. AAC → MP3) loses quality twice. With this on, Antra keeps the original lossy file rather than re-encoding it. Lossless → MP3/AAC is unaffected, and so is same-format output.</p>
            </div>
          </label>
        </div>

        <div style="margin-top: 14px;">
          <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
            <input type="checkbox" bind:checked={config.write_antra_tags} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">Record Antra version in downloaded files</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Writes ANTRA_VERSION, ANTRA_SOURCE and ANTRA_DOWNLOADED tags so you can find and re-fetch folders written by an older version. Invisible in normal players.</p>
            </div>
          </label>
        </div>

        <div style="margin-top: 14px;">
          <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
            <input type="checkbox" bind:checked={config.save_cover_art_sidecar} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">Save cover art file</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Save a high-resolution cover image as cover.jpg or cover.png inside the same folder as downloaded album and single tracks.</p>
            </div>
          </label>
        </div>

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

      <!-- ── Download Source ──────────────────────────────────────────────── -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 6px;">Download Source</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 10px;">Choose one or more services to use. Auto uses the full resolver chain.</p>
        <div style="display: flex; flex-wrap: wrap; gap: 6px;">
          {#each downloadSourceOptions as src}
            <button
              type="button"
              on:click={() => { toggleDownloadSource(src.value); }}
              class:selected-download-source={selectedDownloadSources.includes(src.value)}
              aria-pressed={selectedDownloadSources.includes(src.value)}
              style="display:inline-flex; align-items:center; gap:5px; padding:5px 10px; font-size:12px; border-radius:99px; border: 1px solid {selectedDownloadSources.includes(src.value) ? 'var(--accent-color)' : 'rgba(255,255,255,0.12)'}; background:{selectedDownloadSources.includes(src.value) ? 'rgba(0,255,204,0.1)' : 'transparent'}; color:{selectedDownloadSources.includes(src.value) ? 'var(--accent-color)' : 'inherit'}; cursor:pointer;"
            >
              {#if src.icon}
                <img src={src.icon} alt="" style="width:14px; height:14px; border-radius:50%; object-fit:contain;" />
              {:else}
                <span style="font-weight:700; font-size:11px; width:14px; height:14px; display:inline-flex; align-items:center; justify-content:center; background:rgba(255,255,255,0.15); border-radius:50%;">A</span>
              {/if}
              {src.label}
            </button>
          {/each}
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

      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;" id="settings-spotify-account">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 4px;">Spotify Account</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 12px;">Connect your Spotify account to unlock your personal library, Liked Songs, podcast downloads, and scheduled auto-sync.</p>

        <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 10px; flex-wrap: wrap;">
          <button
            style="font-size: 11px; padding: 5px 12px; background: #1DB954; color: #000; border: none; border-radius: 4px; cursor: pointer; font-weight: 600; opacity: {spDcCapture.phase === 'starting' || spDcCapture.phase === 'waiting_for_user' ? 0.6 : 1}; pointer-events: {spDcCapture.phase === 'starting' || spDcCapture.phase === 'waiting_for_user' ? 'none' : 'auto'};"
            on:click={async () => { spDcCapture = { phase: 'starting', message: 'Opening browser...' }; try { await CaptureSpDC(); } catch(e) { spDcCapture = { phase: 'error', message: String(e) }; } }}
          >{spDcCapture.phase === 'starting' ? 'Opening browser…' : spDcCapture.phase === 'waiting_for_user' ? 'Waiting for login…' : 'Connect Spotify Account'}</button>
          {#if spDcCapture.phase === 'idle' || !spDcCapture.phase}
            <span style="font-size: 11px; color: #555;">Auto-captures your session after login.</span>
          {:else if spDcCapture.phase === 'success'}
            <span style="font-size: 11px; color: #00ffcc;">✓ {spDcCapture.message}</span>
          {:else if spDcCapture.phase === 'error'}
            <span style="font-size: 11px; color: #ff6b6b;">{spDcCapture.message}</span>
          {:else}
            <span style="font-size: 11px; color: #aaa;">{spDcCapture.message}</span>
          {/if}
        </div>

        <label for="spDcInput" style="font-size: 12px; opacity: 0.7;">sp_dc cookie (manual)</label>
        <input
          id="spDcInput"
          type="password"
          bind:value={config.spotify_sp_dc}
          placeholder="AQ..."
          style="width: 100%; box-sizing: border-box; margin-top: 6px; font-family: monospace; font-size: 12px;"
        />
        <p style="font-size: 11px; color: #555; margin: 6px 0 0;">
          Or paste manually: DevTools (F12) → Application → Cookies → find <code>sp_dc</code>. Valid for ~1 year.
        </p>
        {#if config.spotify_sp_dc}
          <p style="font-size: 11px; color: #00ffcc; margin: 6px 0 0;">● Connected — library, podcasts, and auto-sync enabled</p>
        {:else}
          <p style="font-size: 11px; color: #555; margin: 6px 0 0;">○ Not connected</p>
        {/if}
      </div>

      <!-- ── Apple Music Account ────────────────────────────────────────────── -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;" id="settings-apple-account">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 4px;">Apple Music Account</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 12px;">Connect your Apple Music account to unlock your personal library and playlist downloads.</p>

        <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 10px; flex-wrap: wrap;">
          <button
            style="font-size: 11px; padding: 5px 12px; background: #fc3c44; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-weight: 600; opacity: {appleLogin.phase === 'starting' || appleLogin.phase === 'waiting_for_user' ? 0.6 : 1}; pointer-events: {appleLogin.phase === 'starting' || appleLogin.phase === 'waiting_for_user' ? 'none' : 'auto'};"
            on:click={startAppleLogin}
          >{appleLogin.phase === 'starting' ? 'Opening browser…' : appleLogin.phase === 'waiting_for_user' ? 'Waiting for login…' : 'Connect Apple Music Account'}</button>
          {#if appleLogin.phase === 'idle' || !appleLogin.phase}
            <span style="font-size: 11px; color: #555;">Auto-captures your session after login.</span>
          {:else if appleLogin.phase === 'success'}
            <span style="font-size: 11px; color: #00ffcc;">✓ {appleLogin.message}</span>
          {:else if appleLogin.phase === 'error'}
            <span style="font-size: 11px; color: #ff6b6b;">{appleLogin.message}</span>
          {:else}
            <span style="font-size: 11px; color: #aaa;">{appleLogin.message}</span>
          {/if}
        </div>

        <label for="appleMusicUserTokenInput" style="font-size: 12px; opacity: 0.7;">Music-User-Token (manual)</label>
        <input
          id="appleMusicUserTokenInput"
          type="password"
          bind:value={config.apple_music_user_token}
          placeholder="Paste your Music-User-Token here…"
          style="font-size: 11px; width: 100%; margin-top: 4px; margin-bottom: 6px;"
          on:change={() => { if (config.apple_music_user_token && config.apple_authorization_token) { SaveConfig(config); appleLibrary = null; loadAppleMusicLibrary(); } }}
        />
        <p style="font-size: 11px; color: #555; margin: 0 0 4px;">
          DevTools (F12) → Network → filter <code>amp-api.music.apple.com</code> → copy the <code>Music-User-Token</code> request header. Valid for ~30 days.
        </p>
        {#if config.apple_music_user_token && config.apple_authorization_token}
          <p style="font-size: 11px; color: #00ffcc; margin: 6px 0 0;">● Connected — Apple Music library enabled</p>
        {:else}
          <p style="font-size: 11px; color: #555; margin: 6px 0 0;">○ Not connected</p>
        {/if}
      </div>

      <!-- ── Auto-Sync / Scheduled Downloads ───────────────────────────────── -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;" id="settings-auto-sync">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 4px;">Auto-Sync / Scheduled Downloads</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 12px;">Automatically check your Spotify library for new tracks and download them on a schedule. Enable sync per-playlist using the ↺ button on each card.</p>

        <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; margin-bottom: 12px;">
          <input type="checkbox" bind:checked={config.auto_sync_enabled} on:change={() => SaveConfig(config)} />
          <span style="font-size: 12px;">Enable scheduled auto-sync</span>
        </label>

        {#if config.auto_sync_enabled}
          <div style="display: flex; gap: 12px; align-items: center; margin-bottom: 12px;">
            <label style="font-size: 12px;">Time</label>
            <input type="number" min="0" max="23" bind:value={config.auto_sync_hour}
              style="width: 52px; text-align: center;"
              on:change={() => SaveConfig(config)} />
            <span style="font-size: 12px; opacity: 0.6;">:</span>
            <input type="number" min="0" max="59" bind:value={config.auto_sync_minute}
              style="width: 52px; text-align: center;"
              on:change={() => SaveConfig(config)} />
          </div>

          <p style="font-size: 12px; margin: 0 0 6px; opacity: 0.7;">Days</p>
          <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px;">
            {#each ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'] as day, i}
              <label style="display: flex; align-items: center; gap: 4px; font-size: 11px; cursor: pointer;">
                <input type="checkbox"
                  checked={!!((config.auto_sync_days ?? 127) & (1 << i))}
                  on:change={(e) => {
                    const days = config.auto_sync_days ?? 127;
                    const checked = e.target.checked;
                    config.auto_sync_days = checked ? days | (1 << i) : days & ~(1 << i);
                    SaveConfig(config);
                  }} />
                {day}
              </label>
            {/each}
          </div>
        {/if}

        <!-- Playlists to sync — drawn from the live Spotify library -->
        <p style="font-size: 12px; font-weight: 600; margin: 0 0 8px;">Playlists to Sync</p>
        {#if !config.spotify_sp_dc}
          <p style="font-size: 11px; color: #555; margin: 0 0 8px;">Configure your Spotify account in the section above to see your library here.</p>
        {:else if spotifyLibraryLoading}
          <p style="font-size: 11px; color: #555; margin: 0 0 8px;">Loading your library…</p>
        {:else if spotifyLibrary}
          <div style="display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; max-height: 260px; overflow-y: auto;">
            <!-- Liked Songs -->
            <label style="display: flex; align-items: center; gap: 8px; padding: 6px 8px; background: rgba(255,255,255,0.04); border-radius: 6px; cursor: pointer;">
              <input type="checkbox"
                checked={isPlaylistSyncing('https://open.spotify.com/collection/tracks')}
                on:change={() => togglePlaylistSync({ url: 'https://open.spotify.com/collection/tracks', name: 'Liked Songs', artwork_url: '', is_algorithmic: false })} />
              <span style="font-size: 12px; flex: 1;">♥ Liked Songs</span>
              <span style="font-size: 11px; opacity: 0.4;">{spotifyLibrary.liked_songs_count} tracks</span>
              {#if getTrackedEntry('https://open.spotify.com/collection/tracks')?.last_sync_ts}
                <span style="font-size: 10px; opacity: 0.35;">{new Date(getTrackedEntry('https://open.spotify.com/collection/tracks').last_sync_ts * 1000).toLocaleDateString()}</span>
              {/if}
            </label>
            {#each spotifyLibrary.playlists as pl}
              <label style="display: flex; align-items: center; gap: 8px; padding: 6px 8px; background: rgba(255,255,255,0.04); border-radius: 6px; cursor: pointer;">
                <input type="checkbox"
                  checked={isPlaylistSyncing(pl.url)}
                  on:change={() => togglePlaylistSync(pl)} />
                <span style="font-size: 12px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                  {#if pl.is_algorithmic}<span style="opacity: 0.45; margin-right: 3px;">✦</span>{/if}{pl.name}
                </span>
                {#if pl.track_count}
                  <span style="font-size: 11px; opacity: 0.4; flex-shrink: 0;">{pl.track_count}</span>
                {/if}
                {#if getTrackedEntry(pl.url)?.last_sync_ts}
                  <span style="font-size: 10px; opacity: 0.35; flex-shrink: 0;">{new Date(getTrackedEntry(pl.url).last_sync_ts * 1000).toLocaleDateString()}</span>
                {/if}
              </label>
            {/each}
          </div>
        {:else}
          <p style="font-size: 11px; color: #555; margin: 0 0 8px;">Library not loaded — return to the home screen and click ↺ to refresh.</p>
        {/if}

        <div style="display: flex; gap: 8px; margin-bottom: 10px;">
          <button
            style="font-size: 11px; padding: 5px 10px;"
            on:click={runAutoSyncNow}
            disabled={autoSyncRunning}
          >
            {autoSyncRunning ? 'Syncing…' : '↺ Sync Now'}
          </button>
          {#if autoSyncLastResult}
            <span style="font-size: 11px; opacity: 0.7; align-self: center;">{autoSyncLastResult}</span>
          {/if}
        </div>
      </div>

    </div>

    <div style="flex-shrink: 0; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.05); margin-top: 8px; display:flex; flex-direction:column; gap:8px; align-items:center;">
      <p style="text-align: center; font-size: 11px; color: rgba(255,255,255,0.2); margin: 0;">Antra v1.1.8</p>
    </div>
  </div>
</div>
{/if}

<!-- ── Folder & Library Settings Modal ───────────────────────────────────── -->
{#if showFolderSettings}
<div class="modal-overlay" on:click|self={closeFolderSettings} on:keydown={(e) => e.key === 'Escape' && closeFolderSettings()} role="dialog" aria-modal="true" tabindex="-1">
  <div class="modal-content" on:click|stopPropagation on:keydown|stopPropagation style="max-height: 88vh; display: flex; flex-direction: column;">
    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(0,255,204,0.2); padding-bottom: 16px; margin-bottom: 16px; flex-shrink: 0;">
      <h3 style="margin:0; color:var(--accent-color);">📁 Folder Structure</h3>
      <button on:click={saveFolderSettings} disabled={folderSettingsSaving} style="padding: 4px 8px; font-size: 12px;">{folderSettingsSaving ? 'Saving…' : 'Save & Close'}</button>
    </div>
    <div style="display: flex; flex-direction: column; gap: 16px; overflow-y: auto; flex: 1; padding-right: 4px;">

      <!-- Music Folder -->
      <div class="field">
        <label for="outDirFolder">Music Library Folder</label>
        <p style="font-size: 11px; color: #555; margin: 4px 0 8px;">Navidrome / Jellyfin compatible — point this at your media server's music directory.</p>
        <div style="display: flex; gap: 8px; margin-top: 4px;">
          <input id="outDirFolder" readonly type="text" value={config.download_path} />
          <button on:click={pickDir}>Browse</button>
        </div>
      </div>

      <!-- Library Mode -->
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

      <!-- Filenames -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 12px;">Filenames</p>

        <div style="display:flex; flex-direction:column; gap:14px;">
          <div>
            <label for="singleTplInput" style="font-size:12px; font-weight:500; opacity:0.8; margin-bottom:5px; display:block;">Single track filename</label>
            <input
              id="singleTplInput"
              type="text"
              bind:value={config.single_track_filename_template}
              placeholder={'{artist} - {title}'}
              style="width:100%; box-sizing:border-box; font-family:monospace; font-size:12px;"
              on:focus={(e) => focusedTemplateEl = e.currentTarget}
            />
            {#if config.single_track_filename_template}
              <p class="tpl-preview">Preview: {renderPreview(config.single_track_filename_template)}.flac</p>
            {/if}
          </div>

          <div>
            <label for="albumTplInput" style="font-size:12px; font-weight:500; opacity:0.8; margin-bottom:5px; display:block;">Track filename inside album folder</label>
            <input
              id="albumTplInput"
              type="text"
              bind:value={config.album_track_filename_template}
              placeholder={'{track} - {title}'}
              style="width:100%; box-sizing:border-box; font-family:monospace; font-size:12px;"
              on:focus={(e) => focusedTemplateEl = e.currentTarget}
            />
            {#if config.album_track_filename_template}
              <p class="tpl-preview">Preview: {renderPreview(config.album_track_filename_template)}.flac</p>
            {/if}
          </div>
        </div>
      </div>

      <!-- Folder Structure -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 12px;">Folder Structure</p>

        <div>
          <label for="folderTplInput" style="font-size:12px; font-weight:500; opacity:0.8; margin-bottom:5px; display:block;">Folder / directory structure</label>
          <input
            id="folderTplInput"
            type="text"
            bind:value={config.folder_structure_template}
            placeholder="Leave empty to save straight into the library folder"
            style="width:100%; box-sizing:border-box; font-family:monospace; font-size:12px;"
            on:focus={(e) => focusedTemplateEl = e.currentTarget}
          />
          {#if config.folder_structure_template}
            <p class="tpl-preview">Preview: {renderPreview(config.folder_structure_template)}/</p>
          {:else}
            <p class="tpl-preview">No album folder &mdash; tracks are saved directly in your library folder. Good for loose singles.</p>
          {/if}
        </div>
      </div>

      <!-- Playlist Storage -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 8px;">Playlist Storage</p>
        <div style="display: flex; flex-direction: column; gap: 10px;">
          <label style="display: flex; align-items: flex-start; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="playlist_folder" bind:group={config.playlist_storage_mode} style="margin-top: 2px;" />
            <div>
              Playlist folder
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Save playlist tracks in a folder named after the playlist.</p>
            </div>
          </label>
          <label style="display: flex; align-items: flex-start; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="library" bind:group={config.playlist_storage_mode} style="margin-top: 2px;" />
            <div>
              Library structure
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">Use the configured folder structure and filename format for each track.</p>
            </div>
          </label>
        </div>
      </div>

      <!-- Formatting & Sanitization -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 12px;">Formatting &amp; Sanitization</p>

        <div style="display:flex; flex-direction:column; gap:12px;">
          <div style="display:flex; gap:12px; align-items:flex-start;">
            <div style="flex:1;">
              <label for="multiDiscHandlingSelect" style="font-size:12px; opacity:0.7; display:block; margin-bottom:4px;">Multi-disc handling</label>
              <select id="multiDiscHandlingSelect" bind:value={config.multi_disc_handling} style="width:100%;">
                <option value="">Prefix (2-05 dash format)</option>
                <option value="dash">2-05 (disc-dash-track)</option>
                <option value="track_only">05 (track only, no disc)</option>
                <option value="offset">101 / 201 (disc as leading digit)</option>
              </select>
            </div>
            <div style="flex-shrink:0;">
              <label for="padInput2" style="font-size:12px; opacity:0.7; display:block; margin-bottom:4px;">Padding digits</label>
              <input id="padInput2" type="number" min="1" max="4" bind:value={config.track_number_padding} placeholder="2" style="width:64px;" />
            </div>
          </div>

          <div style="display:flex; gap:12px; align-items:flex-start;">
            <div style="flex:1;">
              <label for="illegalCharReplacementInput" style="font-size:12px; opacity:0.7; display:block; margin-bottom:4px;">Illegal character replacement</label>
              <input id="illegalCharReplacementInput" type="text" bind:value={config.illegal_character_replacement} placeholder="_" maxlength="4" style="width:100%; box-sizing:border-box; font-family:monospace;" />
            </div>
            <div style="flex:1;">
              <label for="whitespaceHandlingSelect" style="font-size:12px; opacity:0.7; display:block; margin-bottom:4px;">Whitespace handling</label>
              <select id="whitespaceHandlingSelect" bind:value={config.whitespace_handling} style="width:100%;">
                <option value="keep">Preserve spaces</option>
                <option value="underscore">Replace with _</option>
                <option value="dash">Replace with -</option>
              </select>
            </div>
          </div>
        </div>
      </div>

      <!-- Variables / token chips -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 6px;">Variables</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 10px;">Click a token to insert it at the cursor in the focused template field above.</p>
        <div style="display:flex; flex-wrap:wrap; gap:6px;">
          {#each ['{title}','{artist}','{album_artist}','{album}','{year}','{track}','{disc}','{genre}','{composer}','{isrc}','{codec}','{bitrate}','{quality}'] as tok}
            <button
              on:click={() => insertToken(tok)}
              style="font-family:monospace; font-size:11px; padding:3px 9px; border-radius:6px; background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.12); cursor:pointer; color:var(--accent-color);"
            >{tok}</button>
          {/each}
        </div>
      </div>

      <!-- Restore defaults -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 12px;">
        <button
          on:click={restoreFolderDefaults}
          disabled={folderSettingsSaving}
          style="width:100%; font-size:12px; padding:8px; border-color:rgba(255,255,255,0.15); color:#888; background:rgba(255,255,255,0.02);"
        >↺ Restore folder defaults</button>
      </div>

    </div>
    <div style="flex-shrink: 0; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.05); margin-top: 8px;">
      <button style="width: 100%;" on:click={saveFolderSettings} disabled={folderSettingsSaving}>{folderSettingsSaving ? 'Saving…' : 'Save Preferences'}</button>
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
    sfxSawTrack = false;
          logs = [];
          trackOrder = [];
          trackLabels = {};
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
          currentPlaylistTrackKeysByIndex = {};
          currentPlaylistTrackCount = 0;
          separatorMeta = {};
          dismissedFailures = new Set();
          retryQueue = [];
          retryQueueTotal = 0;
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

<!-- ── Audio Quality Analyzer ──────────────────────────────────────────────── -->
<svelte:window on:keydown={analyzerHandleKey} on:mouseup={onSpecDragEnd} />

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
        {#if analyzerTracks.length > 0}
          <button class="az-btn-sm" on:click={analyzerReset} title="Remove all files" style="color:#f87171;border-color:rgba(248,113,113,0.25);">Clear All</button>
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
            <button class="az-remove-btn" on:click|stopPropagation={() => analyzerRemoveTrack(i)} title="Remove">✕</button>
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
              {#if track.stats}
                <div class="az-stats-panel">
                  <div class="az-stats-title">📊 Audio Stats</div>
                  <div class="az-stats-grid">
                    {#each analyzerFormatStats(track) as row}
                      <span class="az-stat-label">{row.label}</span>
                      <span class="az-stat-value">{row.value}</span>
                    {/each}
                  </div>
                </div>
              {/if}
              <!-- v1.1.8 FEAT-5: show the working. A verdict without its evidence
                   is exactly what made the old analyzer untrustworthy. -->
              {#if track.spectral}
                <div class="az-stats-panel">
                  <div class="az-stats-title">🔬 Spectral Analysis</div>
                  <div class="az-stats-grid">
                    <span class="az-stat-label">Verdict</span>
                    <span class="az-stat-value" style="color:{track.spectral.verdict === 'lossless' ? '#86efac' : track.spectral.verdict === 'lossy' ? '#f87171' : '#facc15'};">
                      {track.spectral.verdict === 'lossless' ? 'Consistent with lossless'
                        : track.spectral.verdict === 'lossy' ? `Lossy${track.spectral.likelySource ? ' — ' + track.spectral.likelySource : ''}`
                        : 'Inconclusive'}
                    </span>
                    <span class="az-stat-label">Confidence</span>
                    <span class="az-stat-value">{Math.round((track.spectral.cutoffConfidence || 0) * 100)}%</span>
                    <!-- "Content to" rather than "Cutoff": this is the highest
                         frequency still carrying energy — the point where a Spek
                         plot goes black — not a filter corner. -->
                    <span class="az-stat-label">Content to</span>
                    <span class="az-stat-value">{(track.spectral.cutoffHz / 1000).toFixed(2)} kHz</span>
                    <span class="az-stat-label">Nyquist</span>
                    <span class="az-stat-value">{(track.spectral.nyquistHz / 1000).toFixed(1)} kHz</span>
                    <span class="az-stat-label">Steepest roll-off</span>
                    <span class="az-stat-value">
                      {(track.spectral.shelfDropDb || 0).toFixed(1)} dB/kHz{track.spectral.cliffHz ? ` at ${(track.spectral.cliffHz / 1000).toFixed(2)} kHz` : ''}
                    </span>
                    <!-- The decisive number: a codec zeroes the bins above its
                         wall, so they collapse to the numerical floor. A steep
                         but natural mastering roll-off leaves noise behind. -->
                    <span class="az-stat-label">Level above</span>
                    <span class="az-stat-value">{(track.spectral.energyAboveCutoffDb ?? 0).toFixed(0)} dB</span>
                  </div>
                  {#if track.spectral.evidence?.length}
                    <ul style="margin: 10px 0 0; padding-left: 16px; font-size: 11px; color: #888; line-height: 1.6;">
                      {#each track.spectral.evidence as line}<li>{line}</li>{/each}
                    </ul>
                  {/if}

                  <!-- Interactive spectrogram (v1.1.8 FEAT-5 UI). Rendered from
                       the byte-quantised time x frequency matrix the Go side
                       produces, so zoom/pan/hover need no backend round-trip. -->
                  {#if track.spectral.columns?.length}
                    <div class="az-spec-wrap">
                      <div class="az-spec-toolbar">
                        <span class="az-spec-hint">drag to pan · wheel to zoom · hover to read</span>
                        <span class="az-spec-readout">{specReadout}</span>
                        <select bind:value={specPalette} on:change={() => drawSpectrogram(track)}>
                          <option value="magma">Magma</option>
                          <option value="viridis">Viridis</option>
                          <option value="grey">Greyscale</option>
                        </select>
                        <button on:click={() => { specZoom = 1; specPan = 0; drawSpectrogram(track); }}>Reset</button>
                        <button on:click={() => exportSpectrogramPng(track)}>PNG</button>
                      </div>
                      <canvas
                        class="az-spec-canvas"
                        bind:this={specCanvas}
                        on:mousemove={(e) => onSpecHover(e, track)}
                        on:mouseleave={() => (specReadout = '')}
                        on:wheel|preventDefault={(e) => onSpecZoom(e, track)}
                        on:mousedown={onSpecDragStart}
                        on:mousemove={(e) => onSpecDrag(e, track)}
                        on:mouseup={onSpecDragEnd}
                      ></canvas>
                      <div class="az-spec-axis">
                        <span>{track.spectral.timeStartSec?.toFixed(0)}s</span>
                        <span>frequency ↑ 0 – {(track.spectral.nyquistHz / 1000).toFixed(1)} kHz</span>
                        <span>{track.spectral.timeEndSec?.toFixed(0)}s</span>
                      </div>
                    </div>
                  {/if}
                </div>
              {:else if track.spectralError}
                <div class="az-stats-panel">
                  <div class="az-stats-title">🔬 Spectral Analysis</div>
                  <p style="font-size: 11px; color: #888; margin: 6px 0 0;">
                    Could not run FFT analysis: {track.spectralError}
                  </p>
                </div>
              {/if}
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
  /* ── Sign-in prompt (v1.1.8 FEAT-8 Phase B) ─────────────────────────────── */
  .signin-banner {
    display: flex;
    align-items: center;
    gap: 14px;
    margin: 0 0 12px;
    padding: 12px 14px;
    border: 1px solid rgba(0, 255, 204, 0.22);
    border-radius: 8px;
    background: rgba(0, 255, 204, 0.05);
  }
  @media (max-width: 620px) {
    .signin-banner { flex-direction: column; align-items: stretch; }
  }
  /* ── Failed Tracks Panel (ST-4) ──────────────────────────────────────────── */
  .failed-panel {
    /* bottom margin keeps the last entry's buttons clear of the floating Log button */
    margin: 0 0 64px 0;
    border: 1px solid rgba(248, 113, 113, 0.25);
    border-radius: 8px;
    background: rgba(248, 113, 113, 0.04);
    overflow: hidden;
  }
  .failed-panel-head {
    display: flex;
    align-items: center;
    padding: 7px 10px;
    background: rgba(248, 113, 113, 0.08);
    border-bottom: 1px solid rgba(248, 113, 113, 0.15);
    gap: 8px;
  }
  .failed-panel-actions { margin-left: auto; }
  .failed-collapse-btn {
    flex-shrink: 0;
    width: 20px;
    height: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    line-height: 1;
    background: transparent;
    border: none;
    color: #fda4af;
    cursor: pointer;
    padding: 0;
  }
  .failed-collapse-btn:hover { color: #fecdd3; }
  .failed-panel-title {
    font-size: 11px;
    font-weight: 600;
    color: #fda4af;
    letter-spacing: 0.03em;
    white-space: nowrap;
  }
  .failed-panel-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
  }
  .failed-retry-progress {
    font-size: 11px;
    color: #fda4af;
    opacity: 0.8;
    white-space: nowrap;
  }
  .failed-action-btn {
    padding: 3px 8px;
    font-size: 11px;
    line-height: 1;
    white-space: nowrap;
    background: rgba(248, 113, 113, 0.1);
    border: 1px solid rgba(248, 113, 113, 0.3);
    border-radius: 4px;
    color: #fda4af;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
  }
  .failed-action-btn:hover:not(:disabled) {
    background: rgba(248, 113, 113, 0.2);
    border-color: rgba(248, 113, 113, 0.5);
  }
  .failed-action-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .failed-action-btn.failed-dismiss-all {
    background: rgba(255,255,255,0.04);
    border-color: rgba(255,255,255,0.12);
    color: rgba(255,255,255,0.5);
  }
  .failed-action-btn.failed-dismiss-all:hover:not(:disabled) {
    background: rgba(255,255,255,0.08);
    border-color: rgba(255,255,255,0.22);
    color: rgba(255,255,255,0.7);
  }
  .failed-list {
    max-height: 180px;
    overflow-y: auto;
  }
  .failed-entry {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 7px 10px;
    gap: 8px;
    border-bottom: 1px solid rgba(248, 113, 113, 0.08);
  }
  .failed-entry:last-child { border-bottom: none; }
  .failed-entry-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1;
  }
  .failed-entry-label {
    font-size: 12px;
    color: rgba(255,255,255,0.85);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .failed-entry-error {
    font-size: 10.5px;
    color: rgba(248, 113, 113, 0.7);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .failed-entry-btns {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
  }
  .failed-retry-btn, .failed-close-btn {
    width: 26px;
    height: 26px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 13px;
    line-height: 1;
    border-radius: 4px;
    cursor: pointer;
    transition: background 0.15s;
  }
  .failed-retry-btn {
    background: rgba(248, 113, 113, 0.1);
    border: 1px solid rgba(248, 113, 113, 0.25);
    color: #fda4af;
  }
  .failed-retry-btn:hover:not(:disabled) {
    background: rgba(248, 113, 113, 0.22);
  }
  .failed-retry-btn:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }
  .failed-close-btn {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.1);
    color: rgba(255,255,255,0.4);
  }
  .failed-close-btn:hover {
    background: rgba(255,255,255,0.1);
    color: rgba(255,255,255,0.7);
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

  /* Stats panel */
  .az-stats-panel {
    border: 1px solid rgba(0,255,204,0.1);
    border-radius: 6px;
    padding: 12px 14px;
    background: rgba(0,255,204,0.02);
  }
  .az-stats-title {
    font-size: 11px;
    font-weight: 600;
    color: #555;
    letter-spacing: 0.06em;
    margin-bottom: 8px;
  }
  .az-stats-grid {
    display: grid;
    grid-template-columns: 100px 1fr;
    gap: 4px 12px;
    font-size: 12px;
  }
  .az-stat-label { color: #555; }
  .az-stat-value { color: var(--accent-color); font-family: monospace; font-size: 11px; }

  /* Remove button in sidebar */
  .az-remove-btn {
    background: none;
    border: none;
    color: #444;
    font-size: 10px;
    padding: 1px 3px;
    cursor: pointer;
    border-radius: 3px;
    flex-shrink: 0;
    line-height: 1;
    opacity: 0;
    transition: opacity 0.12s, color 0.12s;
  }
  .az-sidebar-item:hover .az-remove-btn { opacity: 1; }
  .az-remove-btn:hover { color: #f87171 !important; }

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

  /* ── Access key reminder toast ───────────────────────────────────────────── */
  .key-reminder-toast {
    position: fixed;
    width: 320px;
    background: rgba(8, 14, 12, 0.97);
    border: 1px solid rgba(0, 255, 204, 0.28);
    border-radius: 12px;
    padding: 14px 14px 14px 12px;
    display: flex;
    gap: 10px;
    align-items: flex-start;
    z-index: 250;
    box-shadow:
      0 0 0 1px rgba(0, 255, 204, 0.12),
      0 8px 32px rgba(0, 0, 0, 0.65),
      0 0 24px rgba(0, 255, 204, 0.08);
    backdrop-filter: blur(18px);
    cursor: pointer;
    animation: key-toast-in 0.4s cubic-bezier(0.22, 1, 0.36, 1) both;
    transition: box-shadow 0.2s, border-color 0.2s;
    transform-origin: top right;
  }
  .key-reminder-toast:hover {
    border-color: rgba(0, 255, 204, 0.5);
    box-shadow:
      0 0 0 1px rgba(0, 255, 204, 0.25),
      0 10px 36px rgba(0, 0, 0, 0.7),
      0 0 32px rgba(0, 255, 204, 0.14);
  }
  .key-reminder-toast.leaving {
    animation: key-toast-out 0.45s cubic-bezier(0.55, 0, 1, 0.45) both;
  }
  @keyframes key-toast-in {
    from { opacity: 0; transform: translate(18px, -20px) scale(0.7); }
    to   { opacity: 1; transform: translate(0, 0) scale(1); }
  }
  @keyframes key-toast-out {
    from { opacity: 1; transform: translate(0, 0) scale(1); }
    to   { opacity: 0; transform: translate(18px, -20px) scale(0.28); }
  }

  .key-reminder-icon {
    font-size: 22px;
    flex-shrink: 0;
    margin-top: 1px;
    filter: drop-shadow(0 0 6px rgba(0, 255, 204, 0.4));
  }

  .key-reminder-body {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .key-reminder-title {
    margin: 0;
    font-size: 13px;
    font-weight: 700;
    color: var(--accent-color);
    line-height: 1.2;
    letter-spacing: 0.01em;
  }

  .key-reminder-text {
    margin: 0;
    font-size: 11px;
    color: #6b7280;
    line-height: 1.55;
  }

  .key-reminder-cta {
    margin-top: 4px;
    font-size: 11px;
    font-weight: 600;
    color: rgba(0, 255, 204, 0.7);
    letter-spacing: 0.03em;
    transition: color 0.15s;
  }
  .key-reminder-toast:hover .key-reminder-cta {
    color: var(--accent-color);
  }

  .key-reminder-close {
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
  .key-reminder-close:hover { color: #888; }

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

  .kofi-tooltip-refreshing {
    margin: -2px 0 6px;
    font-size: 11px;
    color: #94a3b8;
  }

  /* ── Source health bar + format selector ────────────────────────────────── */
  .source-health-bar {
    display: flex;
    gap: 6px;
    margin-top: 8px;
    /* flex-start, NOT center: the format selector changes height (the bit-depth
       sub-row appears for FLAC/ALAC), and centring made the status chip hop
       vertically on every format change. Pinned to the top it never moves. */
    align-items: flex-start;
  }

  .format-selector {
    display: flex;
    flex-direction: column;
    gap: 3px;
    align-items: flex-end;
    margin-left: auto;
  }
  .format-main-row {
    display: flex;
    gap: 4px;
    align-items: center;
  }
  .format-sub-row {
    display: flex;
    gap: 4px;
    align-items: center;
    padding-right: 1px;
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
  .format-pill--sub { font-size: 9px; padding: 2px 6px; opacity: 0.75; }
  .format-pill--sub:hover { opacity: 1; }
  .format-pill--sub.active { opacity: 1; }
  /* Dolby Atmos pill (v1.1.8 FEAT-1) — gold to read as premium, dimmed + not
     highlighted while locked so it is clearly unavailable rather than broken. */
  .format-pill--atmos {
    border-color: rgba(212, 175, 55, 0.45);
    color: #d4af37;
  }
  .format-pill--atmos.active {
    background: rgba(212, 175, 55, 0.16);
    border-color: #d4af37;
    color: #f5d76e;
  }
  .format-pill--atmos.locked {
    opacity: 0.5;
    border-color: rgba(255, 255, 255, 0.12);
    color: #7a7a7a;
    cursor: not-allowed;
  }
  .format-pill--atmos.locked:hover { background: transparent; }

  /* Interactive spectrogram (v1.1.8 FEAT-5 UI) */
  .az-spec-wrap { margin-top: 12px; }
  .az-spec-toolbar {
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 6px; font-size: 11px; color: #888;
  }
  .az-spec-hint { opacity: 0.6; }
  .az-spec-readout {
    margin-left: auto; font-family: monospace; color: var(--accent-color, #0fc);
    min-width: 190px; text-align: right;
  }
  .az-spec-toolbar select, .az-spec-toolbar button {
    font-size: 11px; padding: 3px 8px;
  }
  .az-spec-canvas {
    width: 100%; height: 220px; display: block;
    border: 1px solid rgba(255,255,255,0.08); border-radius: 6px;
    background: #000; cursor: crosshair;
  }
  .az-spec-axis {
    display: flex; justify-content: space-between;
    font-size: 10px; color: #666; margin-top: 4px;
  }

  /* ── Mirror status chip ───────────────────────────────────────────────────
     One chip replacing the five per-source ones. flex-shrink:0 is load-bearing:
     the old chips lived in the same flex row as the format selector and were
     visibly squeezed whenever it grew (selecting Atmos added a note), so the
     layout jumped on every format change. */
  .mirror-status-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
    padding: 5px 10px;
    border-radius: 6px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.04em;
    font-family: var(--font-mono);
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.12);
    color: rgba(255,255,255,0.45);
    cursor: pointer;
    transition: border-color 0.15s, color 0.15s, background 0.15s;
  }
  .mirror-status-chip:hover { background: rgba(255,255,255,0.07); }
  .mirror-status-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
    background: currentColor;
  }
  .mirror-status-chip.is-ok       { border-color: rgba(74,222,128,0.5);  color: #4ade80; }
  .mirror-status-chip.is-ok .mirror-status-dot { box-shadow: 0 0 6px #4ade8080; }
  .mirror-status-chip.is-degraded { border-color: rgba(251,146,60,0.6);  color: #fb923c; }
  .mirror-status-chip.is-degraded .mirror-status-dot { box-shadow: 0 0 6px #fb923c80; }
  .mirror-status-chip.is-down     { border-color: rgba(248,113,113,0.6); color: #f87171; }
  .mirror-status-chip.is-down .mirror-status-dot { box-shadow: 0 0 6px #f8717180; }
  /* Unknown is deliberately neutral grey, not red: failing to reach the status
     page is not evidence that the mirrors are down. */
  .mirror-status-chip.is-unknown  { border-color: rgba(255,255,255,0.12); color: rgba(255,255,255,0.35); }
  .mirror-status-label { line-height: 1; }

  /* Format notices sit below the bar, so they can never resize the row. */
  .format-notice { font-size: 11px; color: #fcd34d; margin: 8px 0 0; }
  .format-note   { font-size: 11px; color: #888; margin: 8px 0 0; line-height: 1.5; }

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

  /* ── Lyrics panel (SF-2) ───────────────────────────────────────────────── */
  .lyrics-panel {
    border-top: 1px solid rgba(0,255,204,0.12);
    padding: 0;
    max-height: 200px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .lyrics-lines {
    overflow-y: auto;
    padding: 10px 0;
    scroll-behavior: smooth;
  }

  .lyrics-line {
    text-align: center;
    padding: 5px 24px;
    font-size: 13px;
    color: rgba(210, 230, 230, 0.38);
    line-height: 1.5;
    transition: color 0.25s, font-size 0.25s, opacity 0.25s;
    cursor: default;
    white-space: pre-wrap;
  }

  .lyrics-line.lyrics-active {
    color: var(--accent-color, #00ffcc);
    font-size: 14px;
    opacity: 1;
  }

  .lyrics-empty {
    text-align: center;
    padding: 16px;
    font-size: 12px;
    color: rgba(210, 230, 230, 0.35);
  }

  .lyrics-btn-active {
    color: var(--accent-color, #00ffcc);
    border-color: rgba(0,255,204,0.4);
    background: rgba(0,255,204,0.08);
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
    border: 1px solid rgba(255,255,255,0.15);
    background: var(--bg-elevated, #1a1a1a);
    color: var(--text-primary, #fff);
    font-family: inherit;
    font-size: 12px;
    appearance: none;
    -webkit-appearance: none;
    cursor: pointer;
  }
  .discover-select:focus {
    outline: none;
    border-color: var(--accent-color, rgba(0,255,204,0.5));
  }

  .discover-select-genre {
    flex: 1;
    min-width: 120px;
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

  /* ── Library tab ────────────────────────────────────────────────────────── */
  .lib-service-tabs {
    display: flex;
    gap: 6px;
    padding: 10px 0 6px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    margin-bottom: 4px;
    flex-shrink: 0;
  }

  .lib-service-tab {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 12px;
    font-weight: 500;
    padding: 5px 14px;
    border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.1);
    background: rgba(255,255,255,0.03);
    color: rgba(255,255,255,0.45);
    cursor: pointer;
    transition: all 0.15s;
  }

  .lib-service-tab:hover {
    border-color: rgba(255,255,255,0.22);
    color: rgba(255,255,255,0.8);
    background: rgba(255,255,255,0.06);
  }

  .lib-service-tab.active {
    border-color: var(--accent-color);
    color: #fff;
    background: rgba(0,255,204,0.07);
  }

  .lib-tab-icon {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    overflow: hidden;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .lib-tab-icon img {
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
  }

  .lib-tab-icon-spotify {
    background: #1DB954;
    border-radius: 50%;
    padding: 2px;
  }

  .lib-tab-icon-apple {
    background: transparent;
  }

  .lib-apple-saved-bg {
    background: linear-gradient(135deg, #fc3c44, #ff6b8a);
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .lib-scroll-area {
    flex: 1;
    overflow-y: auto;
    min-height: 0;
    padding-bottom: 16px;
    scrollbar-width: thin;
    scrollbar-color: rgba(255,255,255,0.15) transparent;
  }

  .lib-card {
    position: relative;
  }

  .lib-liked-bg {
    background: linear-gradient(135deg, #450af5, #c13584);
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .lib-liked-icon {
    font-size: 40px;
    color: rgba(255, 255, 255, 0.9);
    user-select: none;
    pointer-events: none;
  }

  .lib-art-fallback {
    width: 100%;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 32px;
    color: rgba(255, 255, 255, 0.25);
    background: rgba(255, 255, 255, 0.05);
  }

  .lib-sync-pill {
    position: absolute;
    top: 8px;
    right: 8px;
    background: rgba(0, 0, 0, 0.6);
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 50%;
    color: rgba(255, 255, 255, 0.5);
    width: 24px;
    height: 24px;
    font-size: 13px;
    line-height: 1;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    opacity: 0;
    transition: opacity 0.2s ease, color 0.2s ease, border-color 0.2s ease;
    padding: 0;
  }

  .lib-card:hover .lib-sync-pill {
    opacity: 1;
  }

  .lib-sync-pill.active {
    opacity: 1;
    color: #1ed760;
    border-color: #1ed760;
    background: rgba(30, 215, 96, 0.15);
  }

  .lib-refresh-btn {
    background: none;
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 6px;
    color: rgba(255, 255, 255, 0.5);
    font-size: 14px;
    padding: 2px 8px;
    cursor: pointer;
    transition: all 0.2s ease;
    flex-shrink: 0;
  }

  .lib-refresh-btn:hover:not(:disabled) {
    color: var(--accent-color);
    border-color: var(--accent-color);
  }

  .lib-refresh-btn:disabled {
    opacity: 0.3;
    cursor: default;
  }

  .lib-collapse-btn {
    background: none;
    border: none;
    color: var(--text-secondary, rgba(255,255,255,0.6));
    cursor: pointer;
    font-size: 13px;
    line-height: 1;
    padding: 2px 4px;
    margin-right: 2px;
    flex-shrink: 0;
    transition: color 0.15s;
  }

  .lib-collapse-btn:hover {
    color: var(--accent-color);
  }

  .lib-section-count {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-secondary, rgba(255,255,255,0.5));
    background: rgba(255,255,255,0.08);
    border-radius: 10px;
    padding: 1px 8px;
    flex-shrink: 0;
  }

  /* Followed-artist tiles render the avatar as a circle */
  .lib-artist-art {
    border-radius: 50% !important;
    overflow: hidden;
  }
  .lib-artist-art .discovery-artwork {
    border-radius: 50%;
  }

  /* ── Template preview ───────────────────────────────────────────────────── */
  .tpl-preview {
    font-size: 11px;
    font-family: monospace;
    color: var(--accent-color);
    opacity: 0.7;
    margin: 5px 0 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* ── Themes Panel ────────────────────────────────────────────────────────── */
  .themes-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.70);
    backdrop-filter: blur(6px);
    z-index: 9990;
    display: flex;
    align-items: stretch;
    justify-content: stretch;
  }
  .themes-panel {
    background: var(--bg-color);
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .themes-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    padding: 24px 32px 18px;
    border-bottom: 1px solid rgba(255,255,255,0.07);
    flex-shrink: 0;
  }
  .themes-title {
    margin: 0 0 4px;
    font-size: 20px;
    font-weight: 700;
    color: var(--text-primary);
  }
  .themes-subtitle {
    margin: 0;
    font-size: 13px;
    color: var(--text-muted);
  }
  /* ── Notification sound (v1.1.8 FEAT-13) ─────────────────────────────────── */
  .sfx-glyph {
    font-size: 22px;
    line-height: 1;
    color: var(--accent-color, #00ffcc);
  }
  .sfx-controls {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-top: 14px;
    padding: 12px 14px;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 10px;
  }
  .sfx-vol {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .sfx-vol-label {
    font-size: 12px;
    color: var(--text-secondary, #999);
    min-width: 54px;
  }
  .sfx-vol input[type="range"] {
    flex: 1;
    accent-color: var(--accent-color, #00ffcc);
    cursor: pointer;
  }
  .sfx-vol-num {
    font-size: 12px;
    color: var(--accent-color, #00ffcc);
    min-width: 40px;
    text-align: right;
  }
  .sfx-check {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    cursor: pointer;
    font-size: 12.5px;
  }
  .sfx-check input { margin-top: 2px; accent-color: var(--accent-color, #00ffcc); cursor: pointer; }
  .sfx-check small {
    display: block;
    margin-top: 3px;
    font-size: 11px;
    color: var(--text-secondary, #888);
    line-height: 1.45;
  }

  /* ── Typeface picker (v1.1.8 FEAT-10) ────────────────────────────────────── */
  .font-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 10px;
  }
  .font-card {
    display: flex;
    align-items: center;
    gap: 12px;
    position: relative;
    padding: 12px 14px;
    text-align: left;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: var(--border-radius);
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
  }
  .font-card:hover { background: var(--surface-light); border-color: var(--border-strong); }
  .font-card--active { border-color: var(--accent-border); background: var(--surface-accent); }
  .font-card-sample {
    flex-shrink: 0;
    width: 42px;
    height: 42px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    border-radius: 6px;
    background: var(--surface-light);
    color: var(--accent-color);
  }
  .font-card-body { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .font-card-name { font-size: 13px; color: var(--text-primary); }
  .font-card-desc { font-size: 11px; line-height: 1.45; color: var(--text-muted); }
  .font-card-badge {
    position: absolute;
    top: 8px;
    right: 10px;
    font-size: 9px;
    letter-spacing: 0.08em;
    color: var(--accent-color);
  }

  .themes-body {
    flex: 1;
    overflow-y: auto;
    padding: 28px 32px 32px;
    display: flex;
    flex-direction: column;
    gap: 36px;
  }
  .themes-section-head {
    display: flex;
    align-items: baseline;
    gap: 10px;
    margin-bottom: 16px;
  }
  .themes-section-label {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.14em;
    color: var(--accent-color);
    text-transform: uppercase;
  }
  .themes-section-sub {
    font-size: 12px;
    color: var(--text-muted);
  }
  .themes-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(155px, 1fr));
    gap: 12px;
  }
  .theme-card {
    display: flex;
    flex-direction: column;
    gap: 9px;
    padding: 14px 14px 13px;
    background:
      linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02)),
      rgba(255,255,255,0.025);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 16px;
    cursor: pointer;
    text-align: left;
    transition: background 0.18s ease, border-color 0.18s ease, transform 0.18s ease, box-shadow 0.18s ease;
    position: relative;
    box-shadow: 0 14px 30px rgba(0,0,0,0.16);
  }
  .theme-card:hover {
    background:
      linear-gradient(180deg, rgba(255,255,255,0.06), rgba(255,255,255,0.03)),
      rgba(255,255,255,0.035);
    border-color: rgba(255,255,255,0.18);
    transform: translateY(-3px);
    box-shadow: 0 18px 34px rgba(0,0,0,0.22);
    color: inherit;
  }
  .theme-card--active {
    border-color: var(--accent-color) !important;
    background:
      linear-gradient(180deg, rgba(255,255,255,0.07), rgba(255,255,255,0.03)),
      rgba(255,255,255,0.04) !important;
    box-shadow: 0 0 0 1px var(--accent-color) inset, 0 18px 34px rgba(0,0,0,0.2);
  }
  .theme-card-preview {
    height: 74px;
    border-radius: 12px;
    padding: 10px 11px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    position: relative;
    overflow: hidden;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.12);
  }
  .theme-card-preview::after {
    content: '';
    position: absolute;
    inset: 0;
    background:
      radial-gradient(circle at top right, rgba(255,255,255,0.18), transparent 38%),
      linear-gradient(180deg, rgba(255,255,255,0.08), transparent 55%);
    pointer-events: none;
  }
  .theme-card-preview-top {
    display: flex;
    gap: 5px;
    position: relative;
    z-index: 1;
  }
  .theme-card-preview-pill {
    width: 18px;
    height: 5px;
    border-radius: 999px;
    background: currentColor;
    opacity: 0.28;
  }
  .theme-card-preview-lines {
    display: flex;
    flex-direction: column;
    gap: 6px;
    position: relative;
    z-index: 1;
  }
  .theme-card-preview-lines span {
    display: block;
    height: 6px;
    border-radius: 999px;
    background: currentColor;
    opacity: 0.18;
  }
  .theme-card-swatches {
    display: flex;
    gap: 4px;
    height: 10px;
    border-radius: 6px;
    overflow: hidden;
    flex-shrink: 0;
  }
  .theme-card-swatch {
    flex: 1;
  }
  .theme-card-name {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-primary);
  }
  .theme-card-desc {
    font-size: 11px;
    color: var(--text-muted);
    line-height: 1.45;
  }
  .theme-card-icon {
    width: 14px;
    height: 14px;
    object-fit: contain;
    border-radius: 3px;
    flex-shrink: 0;
  }
  .theme-card-active-badge {
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.1em;
    color: var(--accent-color);
    border: 1px solid var(--accent-color);
    border-radius: 4px;
    padding: 2px 6px;
    align-self: flex-start;
    opacity: 0.9;
  }
</style>
