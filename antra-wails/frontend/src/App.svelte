<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { GetAppVersion, GetConfig, SaveConfig, PickDirectory, RetryTrackDownload, CancelDownload, GetHistory, AddHistory, ClearHistory, ValidateTidalAuth, StartTidalOAuthLogin, StartAppleBrowserLogin, StartAmazonBrowserLogin, ConfirmAmazonLogin, CaptureSpDC } from '../wailsjs/go/main/App.js';
  import { ExportPlaylist, SaveActivityLog, PreviewTrack, PreviewAlbum, PickPlaylistFile } from '../wailsjs/go/main/App.js';
  // v2.0.0 SF-1 — persisted download queue (owned by Go, see download_queue.go)
  import { EnqueueDownload, EnqueueImport, GetQueue, RemoveQueueItem, ClearQueue, ResumeQueue, StartNextQueued, SetQueueItemTitle } from '../wailsjs/go/main/App.js';
  // v2.0.0 PV-1 — resolve a link and show it before downloading (release_preview.go)
  import { PreviewRelease, EnqueuePreviewSelection } from '../wailsjs/go/main/App.js';
  import { SearchCatalog } from '../wailsjs/go/main/App.js';
  import { DiagnoseNetwork } from '../wailsjs/go/main/App.js';
  // issue #75 — deleting leftover playlist files (playlist_orphans.go). Takes an
  // opaque set id, never a path.
  import { DeletePlaylistOrphans } from '../wailsjs/go/main/App.js';
  import { parsePlaylistFile, buildImportPayload, MAX_ROWS as IMPORT_MAX_ROWS, type ImportResult } from './lib/importer';
  import { ScanFolder, AnalyzeAudio, PickAnalyzerFiles, WriteFile, GetArtistDiscography, SearchArtists, GetSlskdWebUIInfo, GetDownloadedMusicLibrary, GetDownloadedRelease, GetSupportStatus, GetAlbumAvailability, GetSpotifyLibrary, GetAppleMusicLibrary, RunAutoSync, GetTrackLyrics, GetKeyInfo, ValidateKey, StartDeviceLogin, PollDeviceLogin, SignOutDevice, RenewDeviceTokenIfNeeded, GetDeviceAccountStatus, ExtractArtworkSeed, CustomThemeSeed, CustomThemeTable } from '../wailsjs/go/main/App.js';
  import { EventsOn, BrowserOpenURL, ClipboardGetText, ClipboardSetText } from '../wailsjs/runtime/runtime.js';
  import type { main } from '../wailsjs/go/models';
  import AntraLogo from './AntraLogo.svelte';
  import { playSaved, playFinished, preview as previewSfx, type SfxName } from './lib/sfx';
  // v2.0.0 PORT-1 — command palette. The matcher is DOM-free so the test can
  // execute the SHIPPED functions under node instead of re-implementing them.
  import { searchCommands, type Command as PaletteCommand, type Scored as PaletteScored } from './lib/palette';
  import { SETTINGS_CATALOG, type SettingSurface } from './lib/settings-catalog';
  import { t, tr, locale, setLocale, LOCALES, systemLocale } from './lib/i18n';

  /* ── v2.0.0 DMX-1 — per-tag write toggles ──────────────────────────────
     The ids here MUST match TAG_FIELDS in antra/utils/tagger.py; a name that
     exists on only one side is a switch that silently does nothing, so
     test_tag_toggles.py compares the two lists rather than trusting them.

     Stored as the list of tags switched OFF (config.disabled_tags), which is
     what makes an absent key mean "everything on" for every existing user. */
  const TAG_GROUPS: { group: string; fields: { id: string; label: string; note?: string }[] }[] = [
    {
      group: 'tags.groups.core',
      fields: [
        { id: 'title', label: 'tags.fields.title.label' },
        { id: 'artist', label: 'tags.fields.artist.label' },
        { id: 'album', label: 'tags.fields.album.label' },
        { id: 'album_artist', label: 'tags.fields.album_artist.label' },
        { id: 'cover', label: 'tags.fields.cover.label', note: 'tags.fields.cover.note' },
      ],
    },
    {
      group: 'tags.groups.ordering',
      fields: [
        { id: 'track_number', label: 'tags.fields.track_number.label' },
        { id: 'track_total', label: 'tags.fields.track_total.label', note: 'tags.fields.track_total.note' },
        { id: 'disc_number', label: 'tags.fields.disc_number.label' },
      ],
    },
    {
      group: 'tags.groups.release',
      fields: [
        { id: 'date', label: 'tags.fields.date.label' },
        { id: 'year', label: 'tags.fields.year.label', note: 'tags.fields.year.note' },
        { id: 'genre', label: 'tags.fields.genre.label' },
        { id: 'label', label: 'tags.fields.label.label' },
        { id: 'composer', label: 'tags.fields.composer.label' },
      ],
    },
    {
      group: 'tags.groups.identifiers',
      fields: [
        { id: 'isrc', label: 'tags.fields.isrc.label' },
        { id: 'barcode', label: 'tags.fields.barcode.label' },
        { id: 'iswc', label: 'tags.fields.iswc.label' },
        { id: 'spotify_id', label: 'tags.fields.spotify_id.label' },
        { id: 'audio_traits', label: 'tags.fields.audio_traits.label', note: 'tags.fields.audio_traits.note' },
        { id: 'advisory', label: 'tags.fields.advisory.label', note: 'tags.fields.advisory.note' },
      ],
    },
    {
      group: 'tags.groups.lyrics',
      fields: [
        { id: 'lyrics', label: 'tags.fields.lyrics.label' },
        { id: 'synced_lyrics', label: 'tags.fields.synced_lyrics.label' },
      ],
    },
  ];

  /* ── v2.0.0 DMX-5 — when the target file already exists ────────────────
     Ids MUST match CONFLICT_BEHAVIORS in antra_shared/filename_prefs.py
     ("append_counter" is the web's existing spelling for keep-both). */
  const CONFLICT_BEHAVIORS = [
    { id: 'skip', label: 'opts.conflict.skip.label',
      note: 'opts.conflict.skip.note' },
    { id: 'tag_only', label: 'opts.conflict.tag_only.label',
      note: 'opts.conflict.tag_only.note' },
    { id: 'overwrite', label: 'opts.conflict.overwrite.label',
      note: 'opts.conflict.overwrite.note' },
    { id: 'append_counter', label: 'opts.conflict.append_counter.label',
      note: 'opts.conflict.append_counter.note' },
  ];

  /* ── v2.0.0 DMX-2 — output-only text transforms ─────────────────────────
     These ids MUST match CASING_MODES / FEATURED_MODES / MULTI_ARTIST_SEPARATORS
     in antra_shared/filename_prefs.py. They are applied at the tag and filename
     write sites only — never to the strings the resolver matches on, or BUG-1's
     wrong-audio class comes straight back. */
  const CASING_OPTIONS = [
    { id: 'nothing', label: 'opts.casing.nothing' },
    { id: 'lower', label: 'opts.casing.lower' },
    { id: 'upper', label: 'opts.casing.upper' },
    { id: 'start', label: 'opts.casing.start' },
    { id: 'sentence', label: 'opts.casing.sentence' },
  ];

  const FEATURED_OPTIONS = [
    { id: 'nothing', label: 'opts.featured.nothing' },
    { id: 'remove', label: 'opts.featured.remove' },
    { id: 'move', label: 'opts.featured.move' },
  ];

  const SEPARATOR_OPTIONS = [
    { id: 'default', label: 'opts.separator.default' },
    { id: 'comma', label: 'opts.separator.comma' },
    { id: 'semicolon', label: 'opts.separator.semicolon' },
    { id: 'slash', label: 'opts.separator.slash' },
  ];

  // v2.0.0 DMX-6. 'track' fires once per downloaded file and is
  // the default; 'batch' is what a library rescan or a beets import
  // actually wants, because firing those 200 times for one album is the
  // harmful case. Both are offered so the user picks.
  const POST_CMD_MODES = [
    { id: 'track', label: 'opts.postcmd.track' },
    { id: 'batch', label: 'opts.postcmd.batch' },
  ];

  // Tokens the hook understands, shown as clickable chips. Kept in step
  // with ALL_TOKENS in antra/utils/post_command.py.
  const POST_CMD_TOKENS = [
    '{path}', '{filename}', '{folder}', '{release_folder}', '{library}',
    '{title}', '{artist}', '{album}', '{album_artist}', '{year}',
    '{isrc}', '{track}', '{disc}', '{source}', '{format}',
    '{playlist}', '{count}', '{failed}', '{total}',
  ];

  // Mirrors split_template() in antra/utils/post_command.py closely enough
  // to show the user how their command will be split into arguments. It is
  // a PREVIEW only - the real split happens in Python, and token values are
  // never fed back through it, which is what keeps a title containing a
  // semicolon from ever becoming a second command.
  function postCmdSplit(text) {
    const out = [];
    let cur = '', quote = '', started = false;
    for (const ch of String(text || '')) {
      if (quote) {
        if (ch === quote) quote = '';
        else cur += ch;
      } else if (ch === '"' || ch === "'") {
        quote = ch; started = true;
      } else if (/\s/.test(ch)) {
        if (started || cur) { out.push(cur); cur = ''; started = false; }
      } else {
        cur += ch; started = true;
      }
    }
    if (started || cur) out.push(cur);
    return out;
  }

  $: postCmdArgv = postCmdSplit(config.post_command);

  // Editing the command REVOKES the confirmation. Confirming command A must
  // never authorise command B - that is the whole point of the gate.
  function postCmdChanged() {
    config.post_command_confirmed = false;
  }

  function postCmdConfirm() {
    config.post_command_confirmed = true;
    SaveConfig(config);
  }

  function postCmdInsertToken(tok) {
    config.post_command = ((config.post_command || '') + (config.post_command ? ' ' : '') + tok);
    postCmdChanged();
  }

  const DATE_FORMATS = [
    { id: 'iso', label: '2024-04-12', note: 'opts.datefmt.iso' },
    { id: 'year', label: '2024', note: 'opts.datefmt.year' },
    { id: 'dmy', label: '12/04/2024', note: 'opts.datefmt.dmy' },
    { id: 'mdy', label: '04/12/2024', note: 'opts.datefmt.mdy' },
  ];

  let showAdvancedTags = false;

  function tagOn(disabled: string[] | undefined, id: string): boolean {
    return !(disabled || []).includes(id);
  }

  function setTag(id: string, on: boolean) {
    const current: string[] = Array.isArray(config.disabled_tags) ? config.disabled_tags : [];
    // Rebuilt rather than mutated so Svelte sees a new array and the derived
    // "N off" count re-renders.
    config.disabled_tags = on ? current.filter((x) => x !== id) : [...new Set([...current, id])];
  }

  function allTagsOn() {
    config.disabled_tags = [];
  }

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
    playlist_renumber: true,
    playlist_prune_orphans: 'ask',
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
    language: '',
    notify_sound: 'crystal',
    notify_volume: 70,
    preview_volume: 70,
    notify_batch_only: false,
    strict_matching: false,
    strict_format: true,
    prevent_lossy_transcode: true,
    write_antra_tags: true,
    download_source: 'auto',
    download_sources: ['auto'],
    save_cover_art_sidecar: false,
    cover_image_template: 'cover',
    local_artwork_size: 0,
    local_artwork_format: 'jpg',
    jpeg_image_quality: 90,
    embedded_artwork_size: 1000,
    embedded_artwork_png: false,
    save_artist_image: false,
    artist_image_template: 'folder',
    disabled_tags: [] as string[],
    date_format: "iso",
    write_id3v1: true,
    title_casing: 'nothing',
    artist_casing: 'nothing',
    remove_album_version: false,
    remove_duplicate_artists: true,
    featured_artists_mode: 'nothing',
    multi_artist_separator: 'default',
    album_various_artists: true,
    replaygain_enabled: false,
    verify_audio_enabled: true,
    post_command_enabled: false,
    post_command_confirmed: false,
    post_command: '',
    post_command_mode: 'track',
    post_command_timeout: 30,
    sidecar_cue: false,
    sidecar_nfo: false,
    sidecar_json: false,
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
    { id: 'antra',        label: 'Antra',        cat: 'original', desc: 'themes.cards.antra',  colors: ['#081412','#102320','#37e2c2'], icon: null, preview: 'linear-gradient(145deg, #081412 0%, #102320 55%, #1e312d 100%)', tone: '#edfefa' },
    { id: 'ember',        label: 'Ember',        cat: 'original', desc: 'themes.cards.ember',           colors: ['#1a0e07','#2c1a0e','#cf7a3a'], icon: null, preview: 'linear-gradient(145deg, #1a0e07 0%, #2c1a0e 55%, #3a281b 100%)', tone: '#fff8f3' },
    { id: 'ocean',        label: 'Ocean',        cat: 'original', desc: 'themes.cards.ocean',        colors: ['#09121b','#13202e','#71a8e8'], icon: null, preview: 'linear-gradient(145deg, #09121b 0%, #13202e 55%, #202e3c 100%)', tone: '#f5faff' },
    { id: 'graphite',     label: 'Graphite',     cat: 'original', desc: 'themes.cards.graphite',    colors: ['#101114','#1d1f23','#b7bcc6'], icon: null, preview: 'linear-gradient(145deg, #101114 0%, #1d1f23 55%, #2a2c31 100%)', tone: '#f7fafe' },
    { id: 'sunset',       label: 'Sunset',       cat: 'original', desc: 'themes.cards.sunset',                  colors: ['#190d14','#2a1922','#e091a3'], icon: null, preview: 'linear-gradient(145deg, #190d14 0%, #2a1922 55%, #382630 100%)', tone: '#fff7fb' },
    { id: 'spotify',      label: 'Spotify',      cat: 'service',  desc: 'themes.cards.spotify', colors: ['#111111','#1f1f1f','#1ed760'], icon: null, preview: 'linear-gradient(145deg, #111111 0%, #1f1f1f 55%, #2c2c2c 100%)', tone: '#f9f9f9' },
    { id: 'tidal',        label: 'TIDAL',        cat: 'service',  desc: 'themes.cards.tidal',      colors: ['#0f1215','#1b2024','#8ae5ff'], icon: '/icons/tidal.webp', preview: 'linear-gradient(145deg, #0f1215 0%, #1b2024 55%, #282d32 100%)', tone: '#f5faff' },
    { id: 'qobuz',        label: 'Qobuz',        cat: 'service',  desc: 'themes.cards.qobuz',    colors: ['#0b121b','#16202d','#3d7fe3'], icon: '/icons/qobuz.png', preview: 'linear-gradient(145deg, #0b121b 0%, #16202d 55%, #222d3b 100%)', tone: '#f6faff' },
    { id: 'deezer',       label: 'Deezer',       cat: 'service',  desc: 'themes.cards.deezer', colors: ['#140e1b','#241a2e','#a953ff'], icon: '/icons/deezer.webp', preview: 'linear-gradient(145deg, #140e1b 0%, #241a2e 55%, #31273c 100%)', tone: '#fbf8ff' },
    { id: 'apple-music',  label: 'Apple Music',  cat: 'service',  desc: 'themes.cards.apple_music', colors: ['#f5f6fb','#e5e7f0','#de0347'], icon: '/icons/apple-music.png', preview: 'linear-gradient(145deg, #f5f6fb 0%, #e5e7f0 55%, #d3d5de 100%)', tone: '#23252c' },
    { id: 'amazon-music', label: 'Amazon Music', cat: 'service',  desc: 'themes.cards.amazon_music', colors: ['#0a121b','#13202e','#18a8ff'], icon: '/icons/amazon-music.jpg', preview: 'linear-gradient(145deg, #0a121b 0%, #13202e 55%, #202d3d 100%)', tone: '#f5faff' },
    // Dynamic (v2.0.0 THEME-3). colors/preview here are the FALLBACK that
    // matches the [data-theme="artwork"] block in style.css; once a cover has
    // been seen the picker renders the live seed instead. See artworkCardColors.
    // Custom (v2.0.0 THEME-4). Like artwork, colors/preview here are only the
    // FALLBACK matching the [data-theme="custom"] block; once a pair is chosen
    // the picker renders the live seed. See customCardColors.
    { id: 'custom',       label: 'Custom',       cat: 'custom',   desc: 'themes.cards.custom', colors: ['#061417','#0f2226','#52b8ae'], icon: null, preview: 'linear-gradient(145deg, #061417 0%, #0f2226 55%, #1b2f33 100%)', tone: '#eff8fb' },
    { id: 'artwork',      label: 'Artwork',      cat: 'dynamic',  desc: 'themes.cards.artwork', colors: ['#071412','#0f2320','#00bfa8'], icon: null, preview: 'linear-gradient(145deg, #071412 0%, #0f2320 55%, #1c302c 100%)', tone: '#edfefa' },
  ];
  let showThemes = false;

  // ── Light / Dark / System (v2.0.0 THEME-2) ────────────────────────────────
  // SYSTEM is the ABSENCE of data-mode, resolved by the stylesheet's
  // prefers-color-scheme query -- so an existing user sees no change at all
  // unless their OS says light. An explicit stamp outranks it in BOTH
  // directions, which is why 'dark' writes an attribute rather than removing
  // one: on a light-set OS, "Dark" has to win.
  const MODES = [
    { id: 'system', icon: '◐', label: 'themes.modes.system.label', desc: 'themes.modes.system.desc' },
    { id: 'light',  icon: '☀', label: 'themes.modes.light.label',  desc: 'themes.modes.light.desc' },
    { id: 'dark',   icon: '☽', label: 'themes.modes.dark.label',   desc: 'themes.modes.dark.desc' },
  ];

  // The pre-paint block in index.html reads these before any script runs, so
  // they must be written on EVERY change -- not just on save -- or the first
  // frame after a relaunch is painted in the previous theme.
  function rememberAppearance(theme: string, mode: string) {
    try {
      localStorage.setItem('antra:theme', theme || 'antra');
      localStorage.setItem('antra:mode', mode || 'system');
    } catch (e) { /* private mode / storage disabled: only costs a flash */ }
  }

  function setModeAttr(mode: string) {
    const root = document.documentElement;
    if (mode === 'light' || mode === 'dark') root.setAttribute('data-mode', mode);
    else root.removeAttribute('data-mode');
  }

  function applyMode(mode: string) {
    config.mode = mode === 'system' ? '' : mode;
    setModeAttr(mode);
    // A dynamic theme's ground is inline, so unlike every token it does not
    // follow the mode ramp on its own and has to be repainted here.
    if (isArtworkTheme()) paintThemeSeed(artworkSeed);
    else if (isCustomTheme()) paintThemeSeed(customSeed);
    rememberAppearance(config.theme || 'antra', mode);
    SaveConfig(config);
  }

  function applyTheme(id: string, keepOpen = false) {
    document.documentElement.setAttribute('data-theme', id || 'antra');
    config.theme = id;
    // Picking a theme card is a finished decision, so the panel closes. Picking
    // one axis of the custom pair is not -- the user still has the other axis to
    // set, and dismissing the panel underneath them would make the two-axis
    // picker unusable.
    if (!keepOpen) showThemes = false;
    // Always strip the inline paint first. Two things live there: the artwork
    // seed, whose ground would otherwise stay painted over the newly chosen
    // theme, and the pre-paint block's own inline background, which is set at
    // LAUNCH and would otherwise pin the launch theme's ground for the rest of
    // the session no matter what the user picks.
    clearThemeSeedPaint();
    if (id === ARTWORK_THEME_ID) paintThemeSeed(artworkSeed);
    else if (id === CUSTOM_THEME_ID) paintThemeSeed(customSeed);
    rememberAppearance(id || 'antra', config.mode || 'system');
    SaveConfig(config);
  }

  // ── Artwork theme (v2.0.0 THEME-3) ────────────────────────────────────────
  // The seed is the same nine slots every static theme declares, extracted from
  // the cover in Go (artwork_theme.go explains why not here) and written as
  // inline style on <html>, so every derived token follows with no extra
  // wiring. It is persisted alongside the theme id because index.html has to
  // replay it before the stylesheet loads -- a colour only known at runtime
  // cannot live in that file's static table.
  const ARTWORK_THEME_ID = 'artwork';
  const THEME_SEED_VARS = ['--n-h', '--n-cf', '--n-cf-hi', '--a1-l', '--a1-c',
                             '--a1-h', '--a2-l', '--a2-c', '--a2-h'];
  let artworkSeed: any = null;

  function isArtworkTheme() { return (config.theme || 'antra') === ARTWORK_THEME_ID; }

  // Reads the PAINTED attribute rather than config.mode, so this and the
  // stylesheet cannot disagree, and falls back to the OS when the attribute is
  // absent -- which is what 'system' is.
  function modeIsLight() {
    const m = document.documentElement.dataset.mode;
    if (m === 'light') return true;
    if (m === 'dark') return false;
    try { return window.matchMedia('(prefers-color-scheme: light)').matches; }
    catch (e) { return false; }
  }

  function loadArtworkSeed() {
    try {
      const raw = JSON.parse(localStorage.getItem('antra:artworkSeed') || 'null');
      if (raw && typeof raw.h === 'number' && typeof raw.c1 === 'number') artworkSeed = raw;
    } catch (e) { /* private mode or a malformed record: use the CSS fallback */ }
  }

  function storeArtworkSeed(seed: any) {
    artworkSeed = seed;
    try { localStorage.setItem('antra:artworkSeed', JSON.stringify(seed)); }
    catch (e) { /* only costs a flash on the next launch */ }
  }

  function paintThemeSeed(seed: any) {
    if (!seed) return false;   // no cover seen yet: leave the CSS fallback block
    try {
      const root = document.documentElement;
      root.style.setProperty('--n-h', String(seed.h));
      root.style.setProperty('--n-cf', String(seed.n_cf));
      root.style.setProperty('--n-cf-hi', String(seed.n_cf_hi));
      root.style.setProperty('--a1-l', String(seed.l1));
      root.style.setProperty('--a1-c', String(seed.c1));
      // NOT seed.h: for a custom theme the neutral hue and the accent hue are
      // different. Older stored artwork seeds have no `ah`, and for those the
      // two are the same by construction, so the fallback is exact.
      root.style.setProperty('--a1-h', String(seed.ah ?? seed.h));
      root.style.setProperty('--a2-l', String(seed.l2));
      root.style.setProperty('--a2-c', String(seed.c2));
      root.style.setProperty('--a2-h', String(seed.h2));
      root.style.background = modeIsLight() ? seed.bg_light : seed.bg_dark;
      return true;
    } catch (e) { return false; }
  }

  function clearThemeSeedPaint() {
    try {
      const root = document.documentElement;
      for (const v of THEME_SEED_VARS) root.style.removeProperty(v);
      root.style.removeProperty('background');
    } catch (e) { /* nothing to clear */ }
  }

  // Q2 from the changelog: follow the release currently SHOWN or DOWNLOADING,
  // and hold the last seed when idle. playlist_loaded fires once per pasted
  // release and the library hook once per opened release -- never per track, so
  // a 200-track album cannot strobe the theme. The url guard also stops a
  // re-render from re-fetching the same cover.
  let artworkAdoptedUrl = '';
  async function adoptArtwork(url: string) {
    const u = (url || '').trim();
    if (!u || u === artworkAdoptedUrl) return;
    artworkAdoptedUrl = u;
    try {
      const parsed = JSON.parse((await ExtractArtworkSeed(u)) || '{}');
      // A cover with no usable hue keeps the previous seed rather than
      // producing grey mud -- extraction failure is never a user-facing error.
      if (!parsed || !parsed.ok || !parsed.seed) return;
      storeArtworkSeed(parsed.seed);
      if (isArtworkTheme()) paintThemeSeed(parsed.seed);
    } catch (e) { /* keep the last seed */ }
  }

  // -- Custom theme (v2.0.0 THEME-4) ----------------------------------------
  // Two axes -- base neutral and accent -- resolved into the SAME nine-slot seed
  // the artwork theme uses, so it paints with the same painter, persists in the
  // same shape and is replayed by the same validator in index.html. That reuse
  // is the feature: there is no new CSS anywhere.
  //
  // The seed is computed in Go because the per-hue sRGB clamping, the OKLCH
  // conversions and the hex emitters all live in artwork_theme.go. A second
  // copy in JS would be free to drift from the first.
  const CUSTOM_THEME_ID = 'custom';
  let customSeed: any = null;
  let customTable: { bases: any[]; accents: any[]; default_base: string; default_accent: string } | null = null;

  function isCustomTheme() { return (config.theme || 'antra') === CUSTOM_THEME_ID; }
  function customBaseId() { return config.custom_base || customTable?.default_base || 'mist'; }
  function customAccentId() { return config.custom_accent || customTable?.default_accent || 'teal'; }
  // The markup must reference these, NOT the functions above. Svelte tracks the
  // variables an expression names; it cannot see through a call, so a template
  // test written as a function call never re-runs when config changes -- which
  // is exactly how the chips kept showing the previous pair while the theme
  // underneath them had already changed.
  $: customBaseSel = config.custom_base || customTable?.default_base || 'mist';
  $: customAccentSel = config.custom_accent || customTable?.default_accent || 'teal';

  function loadCustomSeed() {
    try {
      const raw = JSON.parse(localStorage.getItem('antra:customSeed') || 'null');
      if (raw && typeof raw.h === 'number' && typeof raw.c1 === 'number') customSeed = raw;
    } catch (e) { /* private mode or a malformed record: use the CSS fallback */ }
  }

  // Fetched once. 7 + 17 = 24 chips, not 119 cards -- the two axes are
  // independent, which is the whole reason a two-axis picker is worth having.
  async function loadCustomTable() {
    try {
      const t = JSON.parse((await CustomThemeTable()) || 'null');
      if (t && Array.isArray(t.bases) && Array.isArray(t.accents)) customTable = t;
    } catch (e) { /* the picker falls back to the static preview */ }
  }

  // Resolve a pair and paint it. Go owns the maths; a failure keeps whatever is
  // already painted rather than inventing a colour.
  async function resolveCustomSeed(base: string, accent: string, activate: boolean) {
    try {
      const parsed = JSON.parse((await CustomThemeSeed(base, accent)) || '{}');
      if (!parsed || !parsed.ok || !parsed.seed) return;
      customSeed = parsed.seed;
      try { localStorage.setItem('antra:customSeed', JSON.stringify(customSeed)); }
      catch (e) { /* only costs a flash on the next launch */ }
      if (activate || isCustomTheme()) paintThemeSeed(customSeed);
    } catch (e) { /* keep whatever is painted */ }
  }

  async function chooseCustomAxis(axis: string, id: string) {
    if (axis === 'base') config.custom_base = id;
    else config.custom_accent = id;
    config = { ...config };
    // Picking a chip is an unambiguous request for this theme, so it activates
    // it too. applyTheme() runs FIRST because it is what writes the attribute
    // and strips any leftover inline paint -- including the pre-paint block's
    // own launch background, which would otherwise sit on top of the new
    // ground. It is also the only thing that should touch showThemes.
    const wasCustom = isCustomTheme();
    if (!wasCustom) applyTheme(CUSTOM_THEME_ID, true);
    await resolveCustomSeed(customBaseId(), customAccentId(), true);
    if (wasCustom) SaveConfig(config);
  }

  // Same contract as artworkCardColors: show the LIVE pair once one exists.
  function customCardColors(seed: any, mode: string) {
    if (!seed) return null;
    const light = mode === 'light' || (mode !== 'dark' && modeIsLight());
    return light
      ? [seed.bg_light, seed.bg_elev_light, seed.swatch]
      : [seed.bg_dark, seed.bg_elev_dark, seed.swatch];
  }

  // A chip must advertise the colour the CURRENT mode paints, or a light-mode
  // user picks from a row of dark-mode swatches.
  function customChipColor(item: any, kind: string, mode: string) {
    const light = mode === 'light' || (mode !== 'dark' && modeIsLight());
    if (kind === 'base') return light ? item.bg_light : item.bg_dark;
    return light ? item.swatch_light : item.swatch;
  }

  // The picker card shows the LIVE seed once one exists, so the chip is the
  // theme the user would actually get rather than the static fallback.
  function artworkCardColors(seed: any, mode: string) {
    if (!seed) return null;
    const light = mode === 'light' || (mode !== 'dark' && modeIsLight());
    return light
      ? [seed.bg_light, seed.bg_elev_light, seed.swatch]
      : [seed.bg_dark, seed.bg_elev_dark, seed.swatch];
  }

  // Linen is retired as a THEME: measured, it was never a theme that happened
  // to be pale, it was the default theme's light ramp. Migrate rather than
  // silently reset -- a linen user must not launch into a dark app. Apple
  // Music keeps its identity but was also light, so it keeps its light ground.
  function migrateAppearance() {
    if (config.theme === 'linen') {
      config.theme = 'antra';
      if (!config.mode) config.mode = 'light';
    } else if (config.theme === 'apple-music' && !config.mode) {
      config.mode = 'light';
    }
  }

  // ── Typeface (v1.1.8 FEAT-10) ───────────────────────────────────────────────
  // Both non-system faces are bundled, so every option works offline.
  const FONTS = [
    { id: 'antra',      label: 'themes.fonts.antra.label',   desc: 'themes.fonts.antra.desc',   css: "'Fira Code','JetBrains Mono','Consolas',monospace" },
    { id: 'system',     label: 'themes.fonts.system.label',       desc: 'themes.fonts.system.desc',  css: "system-ui,-apple-system,'Segoe UI',Roboto,sans-serif" },
    { id: 'nunito',     label: 'themes.fonts.nunito.label',       desc: 'themes.fonts.nunito.desc',                css: "'Nunito',system-ui,sans-serif" },
    { id: 'dyslexic',   label: 'themes.fonts.dyslexic.label', desc: 'themes.fonts.dyslexic.desc', css: "'OpenDyslexic','Comic Sans MS',sans-serif" },
  ];
  // ── Notification sounds (v1.1.8 FEAT-13) ────────────────────────────────────
  const SOUNDS = [
    { id: 'crystal', icon: '✦', label: 'themes.sounds.crystal.label',  desc: 'themes.sounds.crystal.desc' },
    { id: 'pluck',   icon: '♪', label: 'themes.sounds.pluck.label',    desc: 'themes.sounds.pluck.desc' },
    { id: 'blip',    icon: '•', label: 'themes.sounds.blip.label',     desc: 'themes.sounds.blip.desc' },
    { id: 'off',     icon: '⏻', label: 'themes.sounds.off.label',      desc: 'themes.sounds.off.desc' },
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

  /* ── PORT-5 — resizable, persisted activity-log panel ────────────────────
     The width lives in a CSS variable, is clamped, and is persisted. Three
     deliberate departures from the obvious implementation:

       1. POINTER CAPTURE, not document-level mousemove/mouseup. The source
          binds mouseup on `document`, so a drag released outside the window
          never gets its up event and the panel sticks to the cursor. That is
          the exact bug FR-5 had to fix on the preview canvas. setPointerCapture
          routes move/up/cancel back to the handle wherever the pointer goes,
          and brings touch and pen along for free.
       2. RE-CLAMPED ON WINDOW RESIZE. The source reads `innerWidth * 0.9` only
          while dragging, so a width chosen on a wide window survives into a
          narrow one and the panel covers the whole app.
       3. PERSISTED TO config.json, not localStorage. Every other desktop
          preference lives there; the pre-paint reason that puts theme/mode in
          localStorage does not apply, because the panel starts closed. */
  const LOG_PANEL_DEFAULT = 380;
  const LOG_PANEL_MIN = 300;      // measured: the head row's min-content is ~255px
  const LOG_PANEL_MAX_VW = 0.9;   // as the source
  let logPanelWidth = LOG_PANEL_DEFAULT;
  let logResizing = false;
  let logHandleEl: HTMLElement;

  function clampLogPanelWidth(px: number): number {
    const vw = typeof window === 'undefined' ? 1280 : (window.innerWidth || 1280);
    // A window narrower than the minimum has no valid range, so the viewport
    // wins: better a cramped panel than one wider than the screen.
    const max = Math.max(Math.min(LOG_PANEL_MIN, vw), Math.floor(vw * LOG_PANEL_MAX_VW));
    return Math.round(Math.min(max, Math.max(Math.min(LOG_PANEL_MIN, vw), px)));
  }

  let logWidthSaveTimer: ReturnType<typeof setTimeout>;
  function setLogPanelWidth(px: number, persist: boolean = true) {
    logPanelWidth = clampLogPanelWidth(px);
    if (logHandleEl?.parentElement) {
      // Written straight onto the element as well as through the reactive
      // value: Svelte flushes on a microtask, and a drag wants the paint now.
      logHandleEl.parentElement.style.setProperty('--log-panel-width', logPanelWidth + 'px');
    }
    if (!persist) return;
    clearTimeout(logWidthSaveTimer);
    logWidthSaveTimer = setTimeout(() => {
      config.log_panel_width = logPanelWidth;
      SaveConfig(config);
    }, 260);
  }

  function logResizeStart(e: PointerEvent) {
    if (e.button !== 0 && e.pointerType === 'mouse') return;
    e.preventDefault();
    logResizing = true;
    try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); } catch { /* no capture: fall back to the element's own listeners */ }
    document.documentElement.style.cursor = 'ew-resize';
    document.documentElement.style.userSelect = 'none';
  }

  function logResizeMove(e: PointerEvent) {
    if (!logResizing) return;
    // The panel is pinned to the right edge, so width = viewport - pointer x.
    setLogPanelWidth(window.innerWidth - e.clientX, false);
    // Re-wrapping changes scrollHeight; keep a bottom-pinned log pinned.
    if (shouldAutoScroll && terminalContainer) {
      terminalContainer.scrollTop = terminalContainer.scrollHeight;
    }
  }

  function logResizeEnd(e: PointerEvent) {
    if (!logResizing) return;
    logResizing = false;
    try { (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId); } catch { /* already released */ }
    document.documentElement.style.cursor = '';
    document.documentElement.style.userSelect = '';
    setLogPanelWidth(logPanelWidth);
  }

  /* Drag is not the only way in: a separator is keyboard-operable per ARIA,
     and double-click is the escape hatch back to the default. */
  function logResizeKey(e: KeyboardEvent) {
    const step = e.shiftKey ? 48 : 16;
    let next: number | null = null;
    if (e.key === 'ArrowLeft')       next = logPanelWidth + step;  // wider
    else if (e.key === 'ArrowRight') next = logPanelWidth - step;  // narrower
    else if (e.key === 'Home')       next = Number.MAX_SAFE_INTEGER;
    else if (e.key === 'End')        next = 0;
    else if (e.key === 'Enter' || e.key === ' ') next = LOG_PANEL_DEFAULT;
    if (next === null) return;
    e.preventDefault();
    setLogPanelWidth(next);
  }

  function resetLogPanelWidth() { setLogPanelWidth(LOG_PANEL_DEFAULT); }

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

  // ── Language (v2.0.0 SF-3) ──────────────────────────────────────────────────
  // What the OS would give us, resolved once and shown on the "System" card so
  // the user can see which language "System" actually means before picking it.
  const systemLocaleName =
    (LOCALES.find((l) => l.id === systemLocale()) || LOCALES[0]).nativeName;

  // `setLocale` is async because a non-English catalogue is fetched on demand;
  // the config write does not wait on it, so a slow chunk cannot stall the click.
  function applyLanguage(id: string) {
    config.language = id;
    SaveConfig(config);
    setLocale(id);
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
    // v2.0.0 DMX-3
    upc: '0602547670342',
    label: 'Apple Corps',
    explicit: '',
    track_total: '17',
    disc_total: '1',
    date: '1969-09-26',
    playlist: 'Road Trip',
    position: '03',
    owner: 'antra',
    main_artists: 'The Beatles',
    feat_artists: '',
    all_artists: 'The Beatles',
    today: new Date().toISOString().slice(0, 10),
    type: 'album',
    album_id: '0ETFjACtuP2ADo6LFhL6HN',
    track_id: '2EqlS6tkEnglzr7tkKAAYD',
  };

  // Must stay 1:1 with AVAILABLE_TEMPLATE_TOKENS in antra_shared/filename_prefs.py
  // (pinned by antra/utils/test_template_tokens.py). {today} is pinned per run.
  const TEMPLATE_TOKEN_GROUPS: { label: string; tokens: string[] }[] = [
    { label: 'opts.tokengroup.track', tokens: ['title', 'artist', 'main_artists', 'feat_artists', 'all_artists', 'track', 'track_total', 'disc', 'disc_total', 'isrc', 'explicit', 'composer', 'genre'] },
    { label: 'opts.tokengroup.release', tokens: ['album', 'album_artist', 'year', 'date', 'label', 'upc', 'type'] },
    { label: 'opts.tokengroup.playlist', tokens: ['playlist', 'position', 'owner'] },
    { label: 'opts.tokengroup.ids_other', tokens: ['track_id', 'album_id', 'codec', 'bitrate', 'quality', 'today'] },
  ];

  $: tokenHint = (name: string): string => {
    const v = (TEMPLATE_DEMO as Record<string, string>)[name];
    return v ? $t('folder.token_example', { value: v }) : $t('folder.token_empty');
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
  // Issue #72 - a handful of users cannot reach ANY Antra host: sign-in fails,
  // and with a supporter key pasted instead every mirror dies with
  // WRONG_VERSION_NUMBER while a source that is NOT behind *.anandserver.cfd
  // downloads fine. A VPN fixes it outright. That is a network path being
  // filtered, not a server outage, and the old message ("Check your
  // connection") told people the opposite. This runs the real probe and says
  // what it found.
  let deviceLoginCanDiagnose = false;
  let showDiagnostic = false;
  let diagnosticRunning = false;
  let diagnosticReport: any = null;
  let diagnosticCopied = false;

  async function runDiagnostic() {
    showDiagnostic = true;
    diagnosticRunning = true;
    diagnosticCopied = false;
    diagnosticReport = null;
    try {
      const raw = await DiagnoseNetwork();
      diagnosticReport = JSON.parse(raw);
    } catch (e) {
      diagnosticReport = { verdict: 'error', summary: String(e) };
    } finally {
      diagnosticRunning = false;
    }
  }

  async function copyDiagnostic() {
    const text = diagnosticReport?.text;
    if (!text) return;
    try {
      await ClipboardSetText(text);
      diagnosticCopied = true;
      setTimeout(() => { diagnosticCopied = false; }, 2500);
    } catch (e) {
      addLog('warning', `Could not copy the report: ${e}`);
    }
  }
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

  // URLs that arrive from a server (the sign-in reply, the remote support-status
  // link) go to the OS browser, which resolves file:, smb: and custom protocol
  // handlers too. Only a plain https URL to a real host is opened; the Go side
  // validates the same values, this is the second lock.
  function openExternal(url: unknown): boolean {
    if (typeof url !== 'string' || url.length === 0) return false;
    for (let i = 0; i < url.length; i++) {
      const c = url.charCodeAt(i);
      if (c <= 32 || c === 127) return false;
    }
    try {
      const u = new URL(url);
      if (u.protocol !== 'https:' || !u.hostname || u.username || u.password) return false;
    } catch (_e) {
      return false;
    }
    BrowserOpenURL(url);
    return true;
  }

  async function beginDeviceLogin() {
    deviceLoginState = 'starting';
    deviceLoginError = '';
    deviceLoginCanDiagnose = false;
    try {
      const res: any = await StartDeviceLogin();
      if (res?.error) {
        deviceLoginState = 'error';
        deviceLoginError = res.error;
        deviceLoginCanDiagnose = !!res.can_diagnose;
        return;
      }
      deviceUserCode = res.user_code || '';
      deviceVerifyUrl = res.verification_url || '';
      deviceDeadline = Date.now() + ((res.expires_in || 600) * 1000);
      deviceLoginState = 'waiting';
      if (deviceVerifyUrl) openExternal(deviceVerifyUrl);

      // Server enforces its own minimum interval and answers 429 -> treated as
      // still pending, so polling slightly faster than `interval` is harmless.
      const intervalMs = Math.max(3, res.interval || 3) * 1000;
      stopDevicePolling();
      devicePollTimer = setInterval(async () => {
        if (Date.now() > deviceDeadline) {
          stopDevicePolling();
          deviceLoginState = 'error';
          deviceLoginError = tr('account.code_expired');
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
            deviceLoginError = t.error || tr('account.login_failed');
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
        keyError = res?.error || tr('settings.key_not_recognized');
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
  // Request counters: a reply that is no longer the latest request is dropped, so
  // clicking release A then B cannot end with A's tracks under B's highlight.
  let downloadedReleaseReqId = 0;
  let downloadedLibraryReqId = 0;
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

  // Clicking a Discover or My Library card shows the preview instead of starting
  // a download. The URL tab is where the preview and the Download now button
  // live, so the card takes the user there; the link is left in the box so it is
  // obvious what is on screen and re-previewing is one click away.
  function downloadPlaylistUrl(url: string) {
    inputUrl = url;
    activeTab = 'url';
    loadReleasePreview(url);
  }

  // ── Auto-sync ───────────────────────────────────────────────────────────────
  let autoSyncRunning = false;
  let autoSyncLastResult = '';

  async function runAutoSyncNow() {
    autoSyncRunning = true;
    autoSyncLastResult = '';
    try {
      const result = await RunAutoSync();
      autoSyncLastResult = result || tr('status.autosync_complete');
    } catch (e: any) {
      autoSyncLastResult = tr('status.autosync_error', { error: e?.message || e });
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
    mirrorStatusFailed || !mirrorStatus ? $t('mirrors.status')
    : `${mirrorStatus.up}/${mirrorStatus.total}`;

  $: mirrorStatusTitle =
    mirrorStatusFailed || !mirrorStatus
      ? $t('mirrors.unreachable')
      : mirrorStatus.avgMs
        ? $t('mirrors.operational_avg',
             { up: mirrorStatus.up, total: mirrorStatus.total, ms: mirrorStatus.avgMs })
        : $t('mirrors.operational',
             { up: mirrorStatus.up, total: mirrorStatus.total });
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
    { value: 'auto',     name: 'Auto', label: 'opts.format.auto' },
    { value: 'lossless', name: 'FLAC', label: 'opts.format.lossless' },
    { value: 'alac',     name: 'ALAC', label: 'opts.format.alac' },
    { value: 'aac',      name: 'AAC',  label: 'opts.format.aac' },
    { value: 'mp3',      name: 'MP3',  label: 'opts.format.mp3' },
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
    title: tr('sponsor.default_title'),
    message: tr('sponsor.default_message'),
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

  // Logs terminal.
  // `ts` is carried purely for the FR-4 .txt export — the panel deliberately
  // does not render it (it would halve the width available for the message),
  // but a saved log without timestamps cannot show how long a stall lasted,
  // which is the first thing anyone reading a bug report wants.
  let logs: {id: number, type: string, text: string, isRawHtml?: boolean, ts: number}[] = [];
  let logId = 0;
  let terminalContainer: HTMLDivElement;
  let terminalEnd: HTMLElement;
  let shouldAutoScroll = true;
  let logAtBottom = true;
  let showLog = false;
  let trackOrder: string[] = [];
  let trackLabels: Record<string, string> = {};
  // FR-5 — the title/artist/ISRC a preview lookup needs. Kept separate from
  // trackLabels, which holds the rendered "Artist - Title" string: the label is
  // for display and cannot be split back apart reliably.
  let trackMeta: Record<string, {title: string, artist: string, isrc: string}> = {};
  // Job-level advisory from the engine (EngineEventType.NOTICE), e.g. the
  // pasted platform cannot serve the selected format. Emitted once per job.
  let jobNotice = '';
  // v2.0.0 FR-3 - export the resolved tracklist without downloading.
  const EXPORT_FORMATS = [
    { id: 'csv',  label: 'CSV',   hint: 'opts.export.csv' },
    { id: 'txt',  label: 'Text',  hint: 'opts.export.txt' },
    { id: 'm3u8', label: 'M3U8',  hint: 'opts.export.m3u8' },
    { id: 'xspf', label: 'XSPF',  hint: 'opts.export.xspf' },
    { id: 'json', label: 'JSON',  hint: 'opts.export.json' },
  ];
  let showExportMenu = false;
  let exportBusy = false;
  let exportStatus = '';
  let exportStatusTimer: any = null;

  function setExportStatus(msg: string, holdMs = 5000) {
    exportStatus = msg;
    if (exportStatusTimer) clearTimeout(exportStatusTimer);
    exportStatusTimer = setTimeout(() => { exportStatus = ''; }, holdMs);
  }

  async function exportTracklist(fmt: string) {
    showExportMenu = false;
    const url = (inputUrl || '').trim();
    if (!url) { setExportStatus(tr('status.export_paste_link')); return; }
    exportBusy = true;
    setExportStatus(tr('status.export_preparing'), 600000);
    try {
      const raw = await ExportPlaylist(url, fmt, playlistTitle || '');
      let res: any = {};
      try { res = JSON.parse(raw); } catch { res = { error: raw }; }
      if (res.cancelled) { exportStatus = ''; if (exportStatusTimer) clearTimeout(exportStatusTimer); return; }
      if (res.error) { setExportStatus(tr('status.export_failed', { error: res.error }), 9000); return; }
      const omitted = (res.omitted_columns || []).length;
      // Say WHICH columns were dropped rather than leaving blank headers -
      // a header row of empty values reads as data loss.
      setExportStatus(omitted
      ? tr('status.export_done_partial', { count: res.track_count, path: res.path,
                                          columns: (res.omitted_columns || []).join(', ') })
      : tr('status.export_done', { count: res.track_count, path: res.path }), 12000);
    } catch (e: any) {
      setExportStatus(tr('status.export_failed', { error: e?.message || String(e) }), 9000);
    } finally {
      exportBusy = false;
    }
  }

  // v2.0.0 PORT-3 - import a playlist file (CSV / M3U / XSPF / JSPF / JSON).
  // Parsed here so the user sees what will be downloaded before anything
  // starts; nothing is looked up at import time - the resolver matches each row
  // (ISRC first) when it downloads.
  let showImport = false;
  let importBusy = false;
  let importResult: ImportResult | null = null;
  let importFileName = '';
  let importName = '';
  let importError = '';
  // An imported job has no link, so link-only actions (Export) are hidden for it.
  let currentJobIsImport = false;
  const IMPORT_PREVIEW_ROWS = 100;

  async function openImportFile() {
    if (isDownloading || importBusy) return;
    if (!hasOwnCredential) { promptSignIn(); return; }
    importBusy = true;
    try {
      const raw = await PickPlaylistFile();
      let res: any = {};
      try { res = JSON.parse(raw); } catch { res = { error: raw }; }
      if (res.cancelled) return;
      importFileName = res.name || '';
      if (res.error) {
        importResult = null;
        importError = res.error;
      } else {
        importResult = parsePlaylistFile(importFileName, res.text || '');
        importError = importResult.error || '';
        importName = importResult.playlistName;
      }
      showImport = true;
    } catch (e: any) {
      importResult = null;
      importError = e?.message || String(e);
      showImport = true;
    } finally {
      importBusy = false;
    }
  }

  function closeImport() {
    showImport = false;
    importResult = null;
    importError = '';
  }

  async function confirmImport() {
    if (!importResult || importResult.tracks.length === 0) return;
    const payload = buildImportPayload(importResult, importName, importFileName);
    closeImport();
    // SF-1 — the view reset + header lines now happen on queue_job_started, so
    // an import added while something is downloading waits instead of wiping it.
    try {
      const res = await EnqueueImport(JSON.stringify(payload), payload.name, payload.tracks.length);
      if (!res.started) addLog('info', `Added "${payload.name}" to the queue (position ${res.position})`);
    } catch (err) {
      addLog('error', `Could not queue the import: ${err}`);
    }
  }

  // ── v2.0.0 SF-1 — persisted download queue ─────────────────────────────────
  // Go owns the queue and its file; this is a render copy refreshed by
  // queue_changed events. Advancing is requested from process_ended so that
  // failed-track retries (which are not queue jobs) always finish first.
  let queueItems: main.QueueItem[] = [];
  let queuePaused = false;
  let queueRestored = false;
  let queueRunningId = '';
  $: queueWaiting = queueItems.filter(it => it.id !== queueRunningId);

  function applyQueueSnapshot(s: any) {
    if (!s) return;
    queueItems = Array.isArray(s.items) ? s.items : [];
    queuePaused = !!s.paused;
    queueRestored = !!s.restored;
    queueRunningId = s.running_id || '';
  }

  async function enqueueUrls(urls: string[], title = '') {
    try {
      const res = await EnqueueDownload(urls, title);
      if (!res.started) addLog('info', `Added to the queue (position ${res.position})`);
    } catch (err) {
      addLog('error', `Library engine error: ${err}`);
    }
  }

  async function advanceQueue() {
    try { await StartNextQueued(); } catch { /* nothing to start */ }
  }

  async function resumeQueue() {
    if (!hasOwnCredential) { promptSignIn(); return; }
    try { await ResumeQueue(); } catch (err) { addLog('error', `Could not resume the queue: ${err}`); }
  }

  async function discardQueue() {
    try { await ClearQueue(); } catch (err) { addLog('error', `Could not clear the queue: ${err}`); }
  }

  async function removeQueued(id: string) {
    try { await RemoveQueueItem(id); } catch (err) { addLog('warning', `${err}`); }
  }

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

  // ── Leftover playlist files (issue #75) ───────────────────────────────────
  // `orphanId` is the handle Go gave us; it is null for an "auto" run, which
  // already deleted the files, so that card reports rather than offers a button.
  // `names` are basenames only — no path ever reaches this component.
  interface OrphanSet {
    id: string;
    orphanId: string | null;
    playlist: string;
    count: number;
    names: string[];
    collapsed: boolean;
    busy: boolean;
  }
  let orphanSets: OrphanSet[] = [];
  let orphanSeq = 0;

  function handlePlaylistOrphans(payload: any) {
    const names: string[] = Array.isArray(payload.names) ? payload.names.map(String) : [];
    const count = Number(payload.count) || names.length;
    if (count <= 0) return;
    const playlist = String(payload.playlist || '');
    const pruned = !!payload.pruned;
    orphanSets = [...orphanSets, {
      id: `orphan-${++orphanSeq}`,
      orphanId: pruned ? null : (payload.orphan_id ? String(payload.orphan_id) : null),
      playlist,
      count,
      names,
      // Long lists start collapsed; the headline already carries the number.
      collapsed: names.length > 8,
      busy: false,
    }];
    addLog(pruned ? 'info' : 'warning',
      pruned
        ? tr('orphans.pruned', { count, playlist })
        : tr('orphans.title', { count, playlist }));
  }

  function dismissOrphanSet(id: string) {
    orphanSets = orphanSets.filter(s => s.id !== id);
  }

  async function deleteOrphanSet(id: string) {
    const set = orphanSets.find(s => s.id === id);
    if (!set || !set.orphanId || set.busy) return;
    set.busy = true;
    orphanSets = orphanSets;
    try {
      const result = await DeletePlaylistOrphans(set.orphanId);
      addLog(result.failed > 0 ? 'warning' : 'success', result.message);
    } catch (err: any) {
      addLog('error', String(err?.message || err));
      // Go consumed the set on the way in, so the button cannot work twice.
      // Drop the handle rather than leave a button that always fails.
      set.orphanId = null;
      set.busy = false;
      orphanSets = orphanSets;
      return;
    }
    dismissOrphanSet(id);
  }

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
    return titlePart || artistPart || tr('status.unknown_track');
  }

  // issue #74 — a row's key. The backend's id when it sent one, so an event
  // can address this exact row however the list is later reordered; otherwise
  // the old positional key, which keeps the index fallback above working.
  function makeRowKey(track: any, index: number) {
    const rowId = String(track?.row_id || '');
    return rowId ? `row:${rowId}` : `track:${Date.now()}:${index}`;
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
    // issue #74 — the row id first, ALWAYS before track_index. The backend
    // reorders the track list between emitting the tracklist and downloading
    // it (enrichment ends in _stamp_disc_totals, which groups by album), so
    // track_index is a position in a list this webview was never shown. It
    // stays as a fallback for the paths that mint no ids — a single-track
    // retry, a podcast — where there is nothing to be out of step with.
    const rowId = String(data?.row_id || data?.track_data?.row_id || '');
    if (rowId) return `row:${rowId}`;
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
    const existing = activeTracks[trackName] || { mode: 'status' as const, text: tr('status.resolving_source'), status: 'resolving' as const };
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

  let appVersion = '';
  onMount(async () => {
    GetAppVersion().then((v) => { appVersion = v; }).catch(() => {});
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
      // DMX-5: an absent or unrecognised value shows as Skip, which is also
      // what the backend falls back to — the UI must never claim another mode.
      if (!CONFLICT_BEHAVIORS.some(o => o.id === config.filename_conflict_behavior)) {
        config.filename_conflict_behavior = 'skip';
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
      // issue #75 — `!config.x` would be true for a deliberate `false`, so the
      // checkbox could never stay unticked. Test for the key being absent.
      if (config.playlist_renumber === undefined || config.playlist_renumber === null) {
        config.playlist_renumber = true;
      }
      if (!config.playlist_prune_orphans) {
        config.playlist_prune_orphans = 'ask';
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
      // `== null` and not a falsy check: volume 0 is a deliberate choice
      // and must survive a relaunch (v1.1.8 FEAT-11 / FEAT-13 lesson).
      if (config.preview_volume == null)    config.preview_volume    = 70;
      // v2.0.0 PORT-5 — restore the dragged log-panel width. Deliberately NOT
      // defaulted into config here: absent means "never dragged", and omitempty
      // keeps the key out of config.json until the user actually resizes. The
      // stored value is re-clamped because the window may be narrower now than
      // it was when the width was chosen.
      if (config.log_panel_width != null) {
        logPanelWidth = clampLogPanelWidth(config.log_panel_width);
      }
      if (config.notify_batch_only == null) config.notify_batch_only = false;
      selectedDownloadSources = normalizeDownloadSources();
      config = { ...config, download_sources: selectedDownloadSources };
      if (typeof config.save_cover_art_sidecar !== 'boolean') {
        config.save_cover_art_sidecar = false;
      }
      // v2.0.0 FR-2 — cover art. `== null` and not a falsy check for the
      // numbers: local_artwork_size 0 means "original size" and is a real
      // choice that must survive a relaunch, exactly like folder_structure_template.
      if (config.cover_image_template == null)  config.cover_image_template  = 'cover';
      if (config.local_artwork_size == null)    config.local_artwork_size    = 0;
      if (config.local_artwork_format == null)  config.local_artwork_format  = 'jpg';
      if (config.jpeg_image_quality == null)    config.jpeg_image_quality    = 90;
      if (config.embedded_artwork_size == null) config.embedded_artwork_size = 1000;
      if (typeof config.embedded_artwork_png !== 'boolean') config.embedded_artwork_png = false;
      if (typeof config.save_artist_image !== 'boolean')    config.save_artist_image    = false;
      if (config.artist_image_template == null) config.artist_image_template = 'folder';
      // v2.0.0 PORT-2 — sidecars. All three default off: a new file appearing
      // in an existing user's library is a behaviour change nobody asked for.
      // v2.0.0 DMX-1. An absent disabled_tags is the common case (every
      // config.json written before this release) and means "nothing off".
      if (!Array.isArray(config.disabled_tags)) config.disabled_tags = [];
      if (!DATE_FORMATS.some((d) => d.id === config.date_format)) config.date_format = 'iso';
      if (typeof config.write_id3v1 !== 'boolean') config.write_id3v1 = true;
      // v2.0.0 DMX-2 — every transform defaults to a no-op EXCEPT
      // remove_duplicate_artists, which only changes output
      // for a track whose artist list actually repeats a name. The two
      // booleans are checked with `typeof !== 'boolean'` rather than a falsy
      // test because both default to TRUE: `!config.x` would flip a
      // deliberately-off setting back on at every launch.
      if (!CASING_OPTIONS.some((c) => c.id === config.title_casing))  config.title_casing  = 'nothing';
      if (!CASING_OPTIONS.some((c) => c.id === config.artist_casing)) config.artist_casing = 'nothing';
      if (!FEATURED_OPTIONS.some((f) => f.id === config.featured_artists_mode)) config.featured_artists_mode = 'nothing';
      if (!SEPARATOR_OPTIONS.some((s) => s.id === config.multi_artist_separator)) config.multi_artist_separator = 'default';
      if (typeof config.remove_album_version !== 'boolean')     config.remove_album_version     = false;
      if (typeof config.remove_duplicate_artists !== 'boolean') config.remove_duplicate_artists = true;
      if (typeof config.album_various_artists !== 'boolean')    config.album_various_artists    = true;
      if (typeof config.replaygain_enabled !== 'boolean')     config.replaygain_enabled     = false;
      if (config.verify_audio_enabled === undefined || config.verify_audio_enabled === null) {
        config.verify_audio_enabled = true;
      }
      if (typeof config.post_command_enabled !== 'boolean')   config.post_command_enabled   = false;
      if (typeof config.post_command_confirmed !== 'boolean') config.post_command_confirmed = false;
      if (typeof config.post_command !== 'string')            config.post_command           = '';
      if (!POST_CMD_MODES.some((m) => m.id === config.post_command_mode)) config.post_command_mode = 'track';
      if (!Number.isFinite(config.post_command_timeout) || config.post_command_timeout < 1 || config.post_command_timeout > 600)
        config.post_command_timeout = 30;
      if (typeof config.sidecar_cue !== 'boolean')  config.sidecar_cue  = false;
      if (typeof config.sidecar_nfo !== 'boolean')  config.sidecar_nfo  = false;
      if (typeof config.sidecar_json !== 'boolean') config.sidecar_json = false;
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
      // Apply saved theme + mode + typeface. loadArtworkSeed() must come
      // first: applyTheme() paints the seed, so an unloaded one would leave the
      // artwork theme on its CSS fallback until the next cover arrived.
      loadArtworkSeed();
      loadCustomSeed();
      loadCustomTable();
      migrateAppearance();
      setModeAttr(config.mode || 'system');
      rememberAppearance(config.theme || 'antra', config.mode || 'system');
      applyTheme(config.theme || 'antra');
      setFontAttr(config.font || 'antra');
      // SF-3: '' means follow the OS. Not awaited — English is already in the
      // bundle, so first paint is never blocked on a catalogue fetch.
      setLocale(config.language);

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
        deviceLoginError = r.error || tr('account.session_ended');
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
    // v2.0.0 SF-1 — pick up jobs left over from the previous session. They come
    // back paused; nothing downloads until the user presses Resume.
    GetQueue().then(applyQueueSnapshot).catch(() => {});

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
            message: tr('account.tidal_open_link'),
          };
          break;
        case 'tidal_oauth_success':
          tidalOAuth = {
            phase: 'success',
            displayName: payload.display_name || '',
            countryCode: payload.country_code || '',
            sessionJson: payload.session_json || '',
            message: payload.message || tr('account.login_successful'),
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
            message: payload.message || tr('account.tidal_session_valid'),
            display_name: payload.display_name,
            country_code: payload.country_code,
          };
          break;
        case 'tidal_oauth_error':
          tidalOAuth = { phase: 'error', message: payload.message || tr('account.tidal_oauth_failed') };
          break;
        case 'tidal_oauth_done':
          // Process ended — if still in starting/waiting state, mark as error
          if (tidalOAuth.phase === 'starting' || tidalOAuth.phase === 'waiting_browser') {
            tidalOAuth = { ...tidalOAuth, phase: 'error', message: tidalOAuth.message || tr('account.tidal_oauth_ended') };
          }
          break;
      }
      tidalOAuth = { ...tidalOAuth }; // trigger reactivity
    });

    EventsOn("apple-login-event", (payload: any) => {
      if (!payload || !payload.type) return;
      switch (payload.type) {
        case 'apple_login_status':
          appleLogin = { phase: 'starting', message: payload.message || tr('account.apple_opening') };
          break;
        case 'apple_login_success':
          appleLogin = { phase: 'success', message: payload.message || tr('account.apple_connected') };
          config.apple_enabled = true;
          if (payload.authorization_token) config.apple_authorization_token = payload.authorization_token;
          if (payload.music_user_token) config.apple_music_user_token = payload.music_user_token;
          if (payload.storefront) config.apple_storefront = payload.storefront;
          SaveConfig(config);
          if (!appleLibrary && !appleLibraryLoading) loadAppleMusicLibrary();
          setTimeout(() => { appleLogin = { phase: 'idle' }; }, 4000);
          break;
        case 'apple_login_error':
          appleLogin = { phase: 'error', message: payload.message || tr('account.apple_failed') };
          break;
        case 'apple_login_done':
          if (appleLogin.phase === 'starting') {
            appleLogin = { phase: 'error', message: appleLogin.message || tr('account.apple_ended') };
          }
          break;
      }
    });

    EventsOn("amazon-login-event", (payload: any) => {
      if (!payload || !payload.type) return;
      switch (payload.type) {
        case 'amazon_login_status':
          if (payload.phase === 'waiting_for_user') {
            amazonLogin = { phase: 'waiting_for_user', message: payload.message || tr('account.amazon_sign_in_then_click') };
          } else if (payload.phase === 'capturing') {
            amazonLogin = { phase: 'capturing', message: payload.message || tr('account.amazon_reading') };
          } else {
            amazonLogin = { phase: 'starting', message: payload.message || tr('account.amazon_opening') };
          }
          break;
        case 'amazon_login_success':
          amazonLogin = {
            phase: 'success',
            message: payload.message || tr('account.amazon_connected'),
            detail: payload.has_wvd_path ? '' : tr('account.amazon_wvd_required'),
          };
          config.amazon_enabled = true;
          if (payload.direct_creds_json) config.amazon_direct_creds_json = payload.direct_creds_json;
          break;
        case 'amazon_login_error':
          amazonLogin = { phase: 'error', message: payload.message || tr('account.amazon_failed') };
          break;
        case 'amazon_login_done':
          if (amazonLogin.phase === 'starting' || amazonLogin.phase === 'waiting_for_user' || amazonLogin.phase === 'capturing') {
            amazonLogin = { phase: 'error', message: amazonLogin.message || tr('account.amazon_ended') };
          }
          break;
      }
    });

    EventsOn("sp-dc-event", (payload: any) => {
      if (!payload || !payload.type) return;
      switch (payload.type) {
        case 'sp_dc_status':
          spDcCapture = { phase: payload.status === 'waiting' ? 'waiting_for_user' : 'starting', message: payload.message || tr('account.spdc_opening') };
          break;
        case 'sp_dc_captured':
          config.spotify_sp_dc = payload.sp_dc;
          spDcCapture = { phase: 'success', message: tr('account.spdc_connected') };
          SaveConfig(config);
          if (!spotifyLibrary && !spotifyLibraryLoading) loadSpotifyLibrary();
          setTimeout(() => { spDcCapture = { phase: 'idle' }; }, 4000);
          break;
        case 'sp_dc_error':
          spDcCapture = { phase: 'error', message: payload.message || tr('account.spdc_failed') };
          break;
        case 'sp_dc_done':
          if (spDcCapture.phase === 'starting' || spDcCapture.phase === 'waiting_for_user') {
            spDcCapture = { phase: 'error', message: tr('account.spdc_no_session') };
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

    // PORT-5 — a width chosen on a wide window must not survive into a narrow
    // one. Persist is off: the stored preference is the user's choice, and a
    // temporary viewport shrink should not overwrite it.
    const handleWindowResize = () => {
      const clamped = clampLogPanelWidth(logPanelWidth);
      if (clamped !== logPanelWidth) setLogPanelWidth(clamped, false);
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
    logs = [...logs, { id: logId++, type, text, isRawHtml, ts: Date.now() }];
    scrollToBottom();
  }


  // ── FR-5 — 30-second audio previews ───────────────────────────────────────
  //
  // A SECOND <audio> element, not `audioEl`. The spec says "one element, not
  // one per row", and that is what this is -- but it deliberately is not the
  // library player's element: audioEl carries on:timeupdate / on:loadedmetadata
  // / on:ended / on:error that drive the player's progress bar, duration,
  // auto-advance and error text. Sharing it would make a finished preview skip
  // to the next library track. Two elements is also what makes the spec's own
  // "starting a preview pauses the player and vice versa" expressible at all.
  //
  // Created lazily on the first play, so a user who never previews never pays
  // for it. The audio is fetched by the webview straight from the provider CDN.
  let previewAudio: HTMLAudioElement | null = null;
  let previewKey: string | null = null;    // row/card currently sounding
  let previewLoading: string | null = null;
  let previewErrorKey: string | null = null;
  let previewSeq = 0;                      // guards a slow lookup landing late
  let previewUnavailable: Record<string, boolean> = {};

  function previewEl(): HTMLAudioElement {
    if (previewAudio) return previewAudio;
    const a = new Audio();
    a.preload = 'none';
    a.volume = (config.preview_volume ?? 70) / 100;
    a.addEventListener('ended', () => { previewKey = null; });
    a.addEventListener('error', () => { previewKey = null; });
    previewAudio = a;
    return a;
  }

  function previewStop() {
    previewSeq++;                          // orphan any in-flight lookup
    if (previewAudio) {
      try { previewAudio.pause(); } catch {}
      previewAudio.removeAttribute('src');
    }
    previewKey = null;
    previewLoading = null;
    previewErrorKey = null;
  }

  // Preview and the library player must never sound together.
  function pauseLibraryPlayer() {
    if (audioEl && !audioEl.paused) {
      try { audioEl.pause(); } catch {}
    }
  }

  async function previewToggle(key: string, lookup: () => Promise<string>) {
    if (previewKey === key) { previewStop(); return; }
    previewStop();
    pauseLibraryPlayer();

    const mine = ++previewSeq;
    previewLoading = key;
    previewErrorKey = null;

    let raw: string;
    try {
      raw = await lookup();
    } catch (e) {
      if (mine !== previewSeq) return;
      previewLoading = null;
      previewErrorKey = key;
      return;
    }
    if (mine !== previewSeq) return;

    let res: any = {};
    try { res = JSON.parse(raw); } catch {}
    if (!res.url) {
      previewLoading = null;
      previewErrorKey = key;
      // Remember only that THIS row has nothing, so the dead control can be
      // hidden. Not sticky across a new release: trackMeta/previewUnavailable
      // are both reset with the tracklist.
      previewUnavailable[key] = true;
      previewUnavailable = { ...previewUnavailable };
      return;
    }

    const a = previewEl();
    a.volume = (config.preview_volume ?? 70) / 100;
    a.src = res.url;
    try {
      await a.play();
      if (mine !== previewSeq) { try { a.pause(); } catch {} return; }
      previewLoading = null;
      previewKey = key;
    } catch (e) {
      if (mine !== previewSeq) return;
      previewLoading = null;
      previewErrorKey = key;
    }
  }

  function previewTrackRow(rowKey: string) {
    const m = trackMeta[rowKey];
    if (!m) return;
    previewToggle(rowKey, () => PreviewTrack(m.isrc || '', m.title || '', m.artist || ''));
  }

  function previewDiscoverAlbum(item: any) {
    const key = 'disc:' + (item.url || item.name);
    previewToggle(key, () => PreviewAlbum(item.url || '', item.name || '', item.artist_name || ''));
  }

  // Live volume: dragging the slider changes a sounding preview immediately,
  // rather than only affecting the next one.
  $: if (previewAudio && config.preview_volume != null) {
    previewAudio.volume = (config.preview_volume ?? 70) / 100;
  }

  // ── FR-4 — save the activity log to a .txt ────────────────────────────────

  let savingLog = false;

  // Level names as they appear in the exported file. `terminal-rundown` is the
  // end-of-run summary block and `warn`/`warning` are both emitted by existing
  // call sites, so they are folded onto one spelling for a readable column.
  const LOG_LEVEL_LABELS: Record<string, string> = {
    'terminal-rundown': 'SUMMARY',
    warn: 'WARNING',
  };

  // The only isRawHtml producer is formatAsciiRundown(), which emits <span>
  // colour wrappers joined by <br> inside a <div>. Turning that back into text
  // keeps the rundown readable instead of dumping markup into the file.
  function stripLogHtml(html: string): string {
    return html
      .replace(/<br\s*\/?>/gi, '\n')
      .replace(/<\/(div|p|tr|li)>/gi, '\n')
      .replace(/<[^>]*>/g, '')
      .replace(/&nbsp;/g, ' ')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&quot;/g, '"')
      .replace(/&#0?39;/g, "'")
      // &amp; LAST, so an encoded "&amp;lt;" decodes to "&lt;" and not "<".
      .replace(/&amp;/g, '&')
      .replace(/[ \t]+$/gm, '')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  const LOG_STAMP_WIDTH = 21; // "[HH:MM:SS] " + level padded to 9 + " "

  function buildLogExportText(): string {
    const p2 = (n: number) => String(n).padStart(2, '0');
    return logs
      .map((l) => {
        const d = new Date(l.ts || Date.now());
        const stamp = `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
        const level = (LOG_LEVEL_LABELS[l.type] || l.type || 'info').toUpperCase();
        const body = l.isRawHtml ? stripLogHtml(l.text) : l.text;
        // Indent continuation lines so the timestamp column stays scannable.
        return body
          .split('\n')
          .map((ln, i) =>
            i === 0
              ? `[${stamp}] ${level.padEnd(9)} ${ln}`
              : `${' '.repeat(LOG_STAMP_WIDTH)}${ln}`
          )
          .join('\n');
      })
      .join('\n');
  }

  async function saveActivityLog() {
    if (savingLog) return;
    if (logs.length === 0) {
      addLog('warning', 'Nothing to save yet — the activity log is empty.');
      return;
    }
    savingLog = true;
    try {
      // Snapshot at click time: a run still in flight will keep appending, and
      // the file should reflect what the user was looking at.
      const raw = await SaveActivityLog(buildLogExportText(), logs.length);
      const res = JSON.parse(raw);
      if (res.cancelled) {
        // User dismissed the dialog — say nothing.
      } else if (res.error) {
        addLog('error', `Could not save the log: ${res.error}`);
      } else {
        const n = res.redacted || 0;
        const note = n > 0 ? ` (${n} credential${n === 1 ? '' : 's'} redacted)` : '';
        addLog('success', `Log saved to ${res.path}${note}`);
      }
    } catch (e) {
      addLog('error', `Could not save the log: ${e}`);
    } finally {
      savingLog = false;
    }
  }

  function formatDuration(ms: number): string {
    const totalSec = Math.floor(ms / 1000);
    const hours = Math.floor(totalSec / 3600);
    const mins = Math.floor((totalSec % 3600) / 60);
    if (hours > 0) return tr('common.hr_min', { h: hours, m: mins });
    if (mins > 0) return `${mins} min`;
    return `${totalSec} sec`;
  }

  // The row text for a finished track. A track whose tags could not be written is
  // still added (the audio is valid), but must not read as a clean success.
  function completedText(tagsProblem: string | null | undefined): string {
    if (tagsProblem === 'unsupported') return tr('status.added_untagged_unsupported');
    if (tagsProblem === 'failed') return tr('status.added_untagged_failed');
    return tr('status.added_to_library');
  }

  function formatAsciiRundown(summary: any): string {
    // Number(): this string is rendered with {@html}, so never interpolate raw payload values.
    const downloaded = Number(summary.downloaded) || 0;
    const skipped = Number(summary.skipped) || 0;
    const failed = Number(summary.failed) || 0;
    const untagged = Number(summary.tags_incomplete) || 0;
    const total = Number(summary.total) || 0;
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
      ...(untagged > 0 ? [pad(`${tr('status.saved_untagged').padEnd(18)}: `, `<span style="color:#facc15">${untagged}</span>`)] : []),
      ...(totalMb !== null ? [pad('Total size        : ', `<span style="color:#94a3b8">${totalMb} MB</span>`)] : []),
      ...(elapsed !== null ? [pad('Time taken        : ', `<span style="color:#94a3b8">${elapsed}s</span>`)] : []),
      `<span style="color:var(--accent-color)">${sep}</span>`,
    ];

    return `<div style="font-family:var(--font-mono);font-size:13px;line-height:1.7;margin-top:8px">${lines.join('<br>')}</div>`;
  }

  function handleEvent(payload: any) {
    // v2.0.0 SF-1 — queue events ride the same channel as process output so
    // queue_job_started is guaranteed to arrive before that job's tracklist.
    if (payload.type === 'queue_changed') {
      applyQueueSnapshot(payload.queue);
      return;
    }
    if (payload.type === 'queue_job_started') {
      const it = payload.item || {};
      resetDownloadView();
      currentJobIsImport = it.kind === 'import';
      isDownloading = true;
      queueRunningId = it.id || '';
      if (it.kind === 'import') {
        addLog('info', `━━━ Importing "${it.title}" (${it.track_count || 0} tracks) ━━━`);
        addLog('info', `Each row is matched when it downloads (ISRC first, then title and artist)...`);
      } else {
        addLog('info', `━━━ Building your music library ━━━`);
        addLog('info', it.interrupted
          ? `Resuming "${it.title}" — tracks already in your library are skipped...`
          : `Searching best available source (lossless prioritized)...`);
      }
      return;
    }
    if (payload.type === 'playlist_loaded') {
      // SF-1 — give a single-link queue job its real name, so a restore prompt
      // after a restart reads "ICEMAN" rather than a URL.
      const running = queueItems.find(q => q.id === queueRunningId);
      if (running && running.kind === 'urls' && (running.urls?.length || 0) <= 1 && payload.title) {
        SetQueueItemTitle(running.id, payload.title).catch(() => {});
      }
      awaitingTracklist = false;   // PORT-4 — real header + rows replace the skeleton
      playlistTitle = payload.title || '';
      playlistArtwork = payload.artwork_url || '';
      adoptArtwork(playlistArtwork);
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
        const rowKey = makeRowKey(t, idx + 1);
        const label = makeTrackDisplayName(t.artist, t.title);
        // Still populated: it is what the fallback above resolves against, and
        // what a job whose ids were cleared mid-flight falls back to.
        currentPlaylistTrackKeysByIndex[idx + 1] = rowKey;
        currentPlaylistTrackCount = idx + 1;
        trackLabels[rowKey] = label;
        trackMeta[rowKey] = { title: t.title || '', artist: t.artist || '', isrc: t.isrc || '' };
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
        const rowKey = makeRowKey(t, absoluteIndex);
        const label = makeTrackDisplayName(t.artist, t.title);
        currentPlaylistTrackKeysByIndex[absoluteIndex] = rowKey;
        trackLabels[rowKey] = label;
        trackMeta[rowKey] = { title: t.title || '', artist: t.artist || '', isrc: t.isrc || '' };
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
      awaitingTracklist = false;   // PORT-4 — ended before any tracklist arrived
      Object.keys(activeTracks).forEach(clearTrackInterval);
      if (payload.status === 'cancelled') {
        trackOrder = [];
        trackLabels = {};
        trackMeta = {};
        previewStop();   // FR-5 — a stale entry would play the PREVIOUS release's audio
        activeTracks = {};
        currentPlaylistTrackKeysByIndex = {};
        currentPlaylistTrackCount = 0;
        dismissedFailures = new Set();
        retryQueue = [];
        retryQueueTotal = 0;
        addLog('warning', '■ Library sync stopped');
      } else if (payload.status === 'failed') {
        addLog('error', '✖ Library sync stopped with errors');
        if (retryQueue.length > 0) { processRetryQueue(); } else { advanceQueue(); }
      } else {
        addLog('success', '✔ Library updated successfully');
        // v1.1.8 FEAT-13 — batch flourish. Only when at least one track was
        // actually saved, so a fully-skipped ("already in library") run stays
        // silent rather than congratulating the user for doing nothing.
        if (config.notify_sound && config.notify_sound !== 'off' && sfxSawTrack) {
          playFinished(config.notify_volume ?? 70);
        }
        // SF-1 — retries first, then the next queued job. A Stop (cancelled)
        // deliberately does not advance; Go has paused the queue.
        if (retryQueue.length > 0) { processRetryQueue(); } else { advanceQueue(); }
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

      // Job-level advisory — has no track, so it must be handled before
      // resolveTrackEventKey() writes a junk entry into trackLabels.
      if (name === 'notice') {
        jobNotice = data?.message || '';
        if (jobNotice) addLog('warning', jobNotice);
        return;
      }

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
          text: tr('status.resolving_best'),
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
          text: tr('status.accepted_via', { source: displaySource })
        + (data.quality_label ? ` • ${data.quality_label}` : ''),
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
            text: tr('status.waiting_soulseek'),
            status: 'downloading',
            retrying: false,
            trackData: data.track_data || activeTracks[trackKey]?.trackData,
          });
          return;
        }

        let displaySource = source;
        if (displaySource === 'hifi') displaySource = 'Tidal';
        else if (displaySource === 'apple') displaySource = 'Apple';
        else if (displaySource === 'amazon') displaySource = 'Amazon';
        else displaySource = displaySource.charAt(0).toUpperCase() + displaySource.slice(1);

        updateActiveTrack(trackKey, {
          mode: 'progress',
          progress: 8,
          text: (attempt > 1
          ? tr('status.downloading_from_retry', { source: displaySource, n: attempt })
          : tr('status.downloading_from', { source: displaySource }))
        + (data.quality_label ? ` • ${data.quality_label}` : ''),
          status: 'downloading',
          retrying: false,
          trackData: data.track_data || activeTracks[trackKey]?.trackData,
        });

        // A retry / next source fires this event again for the same row: stop the
        // previous ticker first or both run and the bar advances at double speed.
        clearTrackInterval(trackKey);
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
        if (data.tags_problem) addLog('warning', `[!] ${trackLabel}: ${completedText(data.tags_problem)}`);
        else addLog('success', `[✓] Added to library: ${trackLabel}`);
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
          text: completedText(data.tags_problem),
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
          text: tr('status.download_failed'),
          error: data.error || 'Failed',
          status: 'failed',
          retrying: false,
          trackData: data.track_data || activeTracks[trackKey]?.trackData,
        });
      } else if (name === 'track_skipped') {
        // DMX-5 tag_only re-tags the existing file instead of plain skipping.
        const retagged = String(data.message || '').startsWith('Re-tagged');
        addLog(retagged ? 'info' : 'warning',
          retagged ? `[✎] Re-tagged existing file: ${trackLabel}` : `[—] Already in library: ${trackLabel}`);
        updateActiveTrack(trackKey, {
          mode: 'status',
          text: retagged ? tr('status.retagged') : tr('status.already_in_library'),
          status: 'skipped',
          retrying: false,
          trackData: data.track_data || activeTracks[trackKey]?.trackData,
        });
      } else if (name === 'playlist_started') {
        addLog('info', `Creating playlist structure and syncing tracks: ${data.message}`);
      }
    } else if (payload.type === 'playlist_orphans') {
      // issue #75 — the Go side has already replaced the paths with basenames
      // and an opaque id, so nothing here can name a file on disk.
      handlePlaylistOrphans(payload);

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
  // Issue #58 - the app could only ever search for an ARTIST, so finding one
  // song meant opening the artist, reading the whole discography and guessing
  // which release carried it. `searchKind` widens that to songs and albums.
  //
  // A song or album result is a URL, and a URL is something PV-1 already knows
  // how to resolve and download, so clicking one hands it to loadReleasePreview
  // and the whole existing preview -> Download now path takes over unchanged.
  // The artist kind still goes to the discography modal it always did.
  let searchKind: 'song' | 'album' | 'artist' = 'song';
  let catalogResults: any[] = [];
  let catalogLoading = false;
  let showCatalogResults = false;
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
    rows.push({ label: tr('analyzer.row.codec'), value: codec });

    const sr = stream.sample_rate ? `${(+stream.sample_rate / 1000).toFixed(1)} kHz` : '—';
    rows.push({ label: tr('analyzer.row.sample_rate'), value: sr });

    const bits = stream.bits_per_raw_sample || stream.bits_per_sample;
    rows.push({ label: tr('analyzer.row.bit_depth'), value: bits ? `${bits}-bit` : '—' });

    const channels = stream.channels;
    const layout = stream.channel_layout || (channels === 2 ? 'stereo' : channels === 1 ? 'mono' : '—');
    rows.push({ label: tr('analyzer.row.channels'), value: layout });

    const br = fmt.bit_rate ? `${Math.round(+fmt.bit_rate / 1000)} kbps` : '—';
    rows.push({ label: tr('analyzer.row.bit_rate'), value: br });

    const dur = fmt.duration ? `${(+fmt.duration / 60).toFixed(0)}:${String(Math.round(+fmt.duration % 60)).padStart(2,'0')}` : '—';
    rows.push({ label: tr('analyzer.row.duration'), value: dur });

    const size = fmt.size ? `${(+fmt.size / 1048576).toFixed(2)} MB` : '—';
    rows.push({ label: tr('analyzer.row.file_size'), value: size });

    // Tags
    const tags = fmt.tags || stream.tags || {};
    if (tags.title) rows.push({ label: tr('analyzer.row.title'), value: tags.title });
    if (tags.artist || tags.ARTIST) rows.push({ label: tr('analyzer.row.artist'), value: tags.artist || tags.ARTIST });
    if (tags.album || tags.ALBUM) rows.push({ label: tr('analyzer.row.album'), value: tags.album || tags.ALBUM });

    return rows;
  }

  function analyzerFormatStats(track: TrackAnalysis): { label: string; value: string }[] {
    const s = track.stats;
    if (!s) return [];
    const fmt = (v: number, suffix: string) => (v <= -999 || v === null || v === undefined) ? '—' : `${v.toFixed(1)} ${suffix}`;
    const rows: { label: string; value: string }[] = [];
    rows.push({ label: tr('analyzer.row.peak'),        value: fmt(s.peakDb,     'dBFS') });
    rows.push({ label: tr('analyzer.row.rms'),         value: fmt(s.rmsDb,      'dBFS') });
    rows.push({ label: tr('analyzer.row.true_peak'),   value: fmt(s.truePeakDb, 'dBTP') });
    rows.push({ label: tr('analyzer.row.loudness'),    value: fmt(s.lufsI,      'LUFS') });
    rows.push({ label: tr('analyzer.row.lra'),         value: fmt(s.lufsLRA,    'LU')   });
    rows.push({ label: tr('analyzer.row.cutoff'), value: s.cutoffHz > 0 ? `${(s.cutoffHz / 1000).toFixed(0)} kHz` : '—' });
    return rows;
  }

  // ── Interactive spectrogram (v1.1.8 FEAT-5 UI) ─────────────────────────────
  // Rendered from the byte matrix the Go analyser returns. Keeping the data
  // client-side means zoom, pan and hover cost no backend round-trip — the one
  // point of returning a matrix rather than shipping the raw PCM behind it.
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
      const dir = await PickDirectory();   // WriteFile only writes into a folder the user picked
      if (!dir) return;
      await WriteFile(`${dir}/${name}`.replace(/\\/g, '/'), data);
      specReadout = tr('analyzer.png_saved', { name });
    } catch (e) {
      specReadout = tr('analyzer.png_failed');
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
          return { label: tr('analyzer.badge.fake_lossless'), color: '#f87171' };
        }
        if (spectral.verdict === 'inconclusive' && spectral.cutoffConfidence < 0.55) {
          return { label: tr('analyzer.badge.lossless_unverified'), color: '#facc15' };
        }
        if (spectral.verdict === 'lossless') {
          // Real spectral evidence beats the bitrate heuristics below, which are
          // crude enough to libel a genuine quiet or mono master as fake.
          return (bits >= 24 && sr >= 88200)
            ? { label: tr('analyzer.badge.hires_lossless'), color: '#a78bfa' }
            : { label: tr('analyzer.badge.lossless'), color: '#00ffcc' };
        }
      } else if (cutoff > 0 && cutoff < 14000) {
        // Legacy coarse probe only — flag as suspect, never assert.
        return { label: tr('analyzer.badge.suspect_low_cutoff'), color: '#facc15' };
      }
      if (bits >= 24 && sr >= 88200) {
        if (br > 0 && br < 400000) return { label: tr('analyzer.badge.suspect_low_bitrate'), color: '#f87171' };
        return { label: tr('analyzer.badge.hires_lossless'), color: '#a78bfa' };
      }
      // Standard lossless: below ~250kbps almost always a transcode
      if (br > 0 && br < 250000) return { label: tr('analyzer.badge.fake_lossless'), color: '#f87171' };
      if (br > 0 && br < 400000) return { label: tr('analyzer.badge.lossless_low_br'), color: '#facc15' };
      return { label: tr('analyzer.badge.lossless'), color: '#00ffcc' };
    }
    if (codec === 'mp3' || codec === 'aac' || codec === 'vorbis' || codec === 'opus') {
      if (br >= 256000) return { label: tr('analyzer.badge.high_quality'), color: '#4ade80' };
      if (br >= 192000) return { label: tr('analyzer.badge.standard'), color: '#facc15' };
      return { label: tr('analyzer.badge.low_quality'), color: '#f87171' };
    }
    if (codec === 'pcm_s16le' || codec === 'pcm_s24le' || codec === 'pcm_f32le') {
      return { label: tr('analyzer.badge.lossless_pcm'), color: '#00ffcc' };
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
      analyzerExportStatus = tr('analyzer.exporting_n', { i: i + 1, total: done.length });
      await analyzerExportSingleTo(track, dir, i + 1);
    }
    analyzerExportStatus = tr('analyzer.exported_pngs', { count: done.length, dir });
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
    parts.push(tr('common.n_tracks_short', { count: release.track_count }));
    return parts.join(' · ');
  }

  async function openDownloadedMusic() {
    showDownloadedMusic = true;
    await refreshDownloadedMusicLibrary();
  }

  async function refreshDownloadedMusicLibrary() {
    const libMine = ++downloadedLibraryReqId;
    downloadedLibraryLoading = true;
    downloadedLibraryError = '';
    try {
      const raw = await GetDownloadedMusicLibrary();
      if (libMine !== downloadedLibraryReqId) return;   // a newer refresh superseded this one
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
      if (libMine !== downloadedLibraryReqId) return;
      downloadedLibraryError = String(e);
      downloadedLibrary = { albums: [], playlists: [] };
    } finally {
      if (libMine === downloadedLibraryReqId) downloadedLibraryLoading = false;
    }
  }

  async function openDownloadedRelease(release: LibraryReleaseSummary) {
    const mine = ++downloadedReleaseReqId;
    downloadedSelectedPath = release.relative_path;
    downloadedSelectedReleaseLoading = true;
    try {
      const raw = await GetDownloadedRelease(release.relative_path);
      if (mine !== downloadedReleaseReqId) return;   // the user already picked another release
      const parsed = JSON.parse(raw || '{}');
      if (parsed?.error) {
        downloadedLibraryError = parsed.error;
        return;
      }
      downloadedSelectedRelease = parsed;
      adoptArtwork(parsed?.artwork_url || '');
      downloadedView = release.kind === 'playlist' ? 'playlists' : 'albums';
    } catch (e: any) {
      if (mine !== downloadedReleaseReqId) return;
      downloadedLibraryError = String(e);
    } finally {
      if (mine === downloadedReleaseReqId) downloadedSelectedReleaseLoading = false;
    }
  }

  async function playDownloadedTrack(index: number) {
    if (!downloadedSelectedRelease?.tracks?.length || !audioEl) return;
    previewStop();   // FR-5 — the two must never sound together
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
    previewStop();   // FR-5
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
      alert(tr('setup.pick_library_folder'));
      return;
    }
    if (config.soulseek_enabled && (!config.soulseek_username || !config.soulseek_password)) {
      alert(tr('setup.soulseek_credentials_needed'));
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

  // v2.0.0 PORT-4 — skeleton placeholders while a release resolves.
  // A Qobuz album can take 25-35 s between Add to Library and playlist_loaded;
  // until now that stretch showed an empty tracklist. awaitingTracklist is
  // raised by resetDownloadView (every link / discography / import start goes
  // through it) and dropped by the first playlist_loaded or by process_ended.
  // showSkeleton is DERIVED rather than toggled imperatively, so no exit path
  // (error, cancel, a failed StartDownload) can leave the placeholders stuck.
  // A retry never calls resetDownloadView, so it never shows a skeleton.
  let awaitingTracklist = false;
  // PV-1 — a preview being resolved shows the same placeholders. Resolving is
  // the slow half (25-35 s for a Qobuz album), and the alternative is the
  // PREVIOUS release's header sitting there looking like the answer.
  $: showSkeleton = (awaitingTracklist && isDownloading && trackOrder.length === 0)
    || previewReleaseLoading;
  // Fixed 8 rows: the track count is unknown until the response lands, and a
  // row per track would be a guess. Widths vary so it reads as text, not a barcode.
  const SKEL_WIDTHS: [number, number][] = [
    [62, 22], [48, 18], [70, 24], [55, 20],
    [66, 22], [44, 17], [60, 21], [52, 19],
  ];

  // Clears the previous job's tracklist, header and retry state. Shared by a
  // link download, a discography download and a playlist-file import (PORT-3)
  // so they cannot drift.
  function resetDownloadView() {
    awaitingTracklist = true;
    sfxSawTrack = false;
    logs = [];
    trackOrder = [];
    trackLabels = {};
    trackMeta = {};
    previewStop();   // FR-5 — a stale entry would play the PREVIOUS release's audio
    jobNotice = '';
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
  }


  // ── v2.0.0 PV-1 — preview before download ──────────────────────────────────
  // Pasting a link used to start a download. It now RESOLVES the link and shows
  // what it found — cover, type, artists, date, full tracklist — and writes
  // nothing to the library until the user presses Download now. Every Discover
  // and My Library card goes through the same step, because they all already
  // funnelled through downloadPlaylistUrl(), which now previews instead.
  //
  // One button does both jobs: it reads "Preview" until a release is on screen
  // and "Download now" once one is. Two buttons would leave the first one
  // meaningless for the whole time the preview is up.
  //
  // previewRelease.tracks are DISPLAY rows. The full TrackMetadata stays in Go's
  // cache file, addressed only by previewReleaseId, so the webview never holds a
  // filesystem path and a 5000-track playlist does not cross the bridge twice.
  let previewRelease: any = null;
  let previewReleaseId = '';
  let previewReleaseLoading = false;
  let previewReleaseError = '';
  let previewReleaseUrl = '';
  // Chosen rows, as indices into previewRelease.tracks — the only handle the
  // cache rows have, and exactly what EnqueuePreviewSelection takes.
  let previewSelected: Set<number> = new Set();
  // Further links from the same paste. This is what "preview the first, queue the
  // rest" means: they wait here and join the queue when Download now is pressed,
  // so abandoning the preview does not leave four albums silently queued.
  let previewQueuedUrls: string[] = [];
  let previewReleaseReqId = 0;

  $: previewTotal = previewRelease ? (previewRelease.tracks || []).length : 0;
  $: previewSelectedCount = previewSelected.size;
  // A playlist row is numbered by its position in the PLAYLIST, not by where
  // the track happens to sit on its own album - album numbers restart at 1
  // every time the playlist moves to another release, which reads as scrambled.
  // It is also the number the file itself gets on disk in the default
  // playlist_folder mode (LibraryOrganizer.playlist_stem_for).
  //
  // Gated on the release KIND, not merely on the field being present: measured
  // on a live album preview, the backend fills playlist_position on ordinary
  // album tracks too (1..25 for Scorpion), so keying off the field alone would
  // flatten every album to 1..N and lose the disc prefixes.
  $: previewUsesPlaylistPos = !!previewRelease
    && previewRelease.content_type === 'PLAYLIST'
    && (previewRelease.tracks || []).some((x: any) => x.playlist_position);
  // Disc prefixes only on a multi-disc release. Derived once rather than per
  // row, which was an O(n^2) scan of the tracklist. Read only by the album
  // branch of the row number, so a playlist can never pick up the disc number
  // of whatever album its tracks happened to come from.
  $: previewHasDiscs = previewRelease
    ? (previewRelease.tracks || []).some((x: any) => (x.disc_number || 1) > 1)
    : false;
  $: previewAllSelected = previewTotal > 0 && previewSelectedCount === previewTotal;
  $: previewSelectedMs = previewRelease
    ? (previewRelease.tracks || []).reduce(
        (sum: number, t: any, i: number) => sum + (previewSelected.has(i) ? (t.duration_ms || 0) : 0),
        0,
      )
    : 0;

  function clearReleasePreview() {
    previewReleaseReqId++;          // orphans any in-flight lookup
    previewRelease = null;
    previewReleaseId = '';
    previewReleaseLoading = false;
    previewReleaseError = '';
    previewReleaseUrl = '';
    previewSelected = new Set();
    previewQueuedUrls = [];
    previewStop();                  // FR-5 — a stale row would play the last release
  }

  // Sets are reassigned rather than mutated: Svelte cannot see a Set.add().
  function togglePreviewTrack(i: number) {
    const next = new Set(previewSelected);
    if (next.has(i)) next.delete(i); else next.add(i);
    previewSelected = next;
  }

  function togglePreviewAll() {
    previewSelected = previewAllSelected
      ? new Set()
      : new Set(Array.from({ length: previewTotal }, (_, i) => i));
  }

  // A preview goes STALE the moment the box holds a different link: the button
  // would otherwise still say Download now and download the OLD release, which is
  // the one trap this two-step flow could introduce. When stale it offers Preview
  // again. A pasted link empties the box and a card click fills it with the link
  // being previewed, so neither of those is ever stale.
  $: previewStale = !!previewRelease
    && inputUrl.trim() !== ''
    && inputUrl.trim() !== previewReleaseUrl;
  // What the button will actually do, named once so the handler and the label
  // cannot disagree.
  $: urlBarDownloads = !!previewRelease && !previewStale;

  function previewReleaseRowKey(i: number) { return `pv:${previewReleaseId}:${i}`; }

  // 30-second audio preview for a row on the preview screen. Unlike the live
  // tracklist this does not go through trackMeta — the row carries its own
  // isrc/title/artist, which is all the lookup needs.
  function previewReleaseRow(i: number) {
    const t = (previewRelease?.tracks || [])[i];
    if (!t) return;
    previewToggle(previewReleaseRowKey(i),
      () => PreviewTrack(t.isrc || '', t.title || '', t.artist || ''));
  }

  // Resolve one link and show it; extraUrls are held for the queue.
  async function loadReleasePreview(url: string, extraUrls: string[] = []) {
    if (!hasOwnCredential) { promptSignIn(); return; }
    const mine = ++previewReleaseReqId;
    previewRelease = null;
    previewReleaseId = '';
    previewReleaseError = '';
    previewSelected = new Set();
    previewReleaseUrl = url;
    previewQueuedUrls = extraUrls;
    previewReleaseLoading = true;
    previewStop();

    let raw = '';
    try {
      raw = await PreviewRelease(url);
    } catch (e: any) {
      if (mine !== previewReleaseReqId) return;
      previewReleaseLoading = false;
      previewReleaseError = e?.message || String(e);
      addLog('error', `Could not read that link: ${previewReleaseError}`);
      return;
    }
    if (mine !== previewReleaseReqId) return;   // the user moved on
    previewReleaseLoading = false;

    let parsed: any = null;
    try { parsed = JSON.parse(raw); } catch { parsed = { error: raw }; }

    // Podcasts have no preview. Queue the link the way they always were rather
    // than leaving the user at a dead end.
    if (parsed && parsed.unsupported) {
      const all = [url, ...extraUrls];
      clearReleasePreview();
      await enqueueUrls(all);   // queue_job_started resets the view, as for any link
      return;
    }
    if (!parsed || parsed.error) {
      previewReleaseError = parsed?.error || tr('preview.could_not_read');
      addLog('error', `Could not read that link: ${previewReleaseError}`);
      return;
    }

    previewRelease = parsed;
    previewReleaseId = parsed.preview_id || '';
    // Everything ticked to begin with: the common answer is "yes, all of it",
    // and the checkboxes are there to take songs out.
    previewSelected = new Set((parsed.tracks || []).map((_: any, i: number) => i));
  }

  // Download now — queue the ticked tracks, then any links held from the paste.
  async function downloadPreviewSelection() {
    if (!previewRelease || !previewReleaseId) return;
    if (!hasOwnCredential) { promptSignIn(); return; }
    if (previewSelectedCount === 0) return;

    const indices = Array.from(previewSelected).sort((a, b) => a - b);
    const title = previewRelease.title || '';
    const extra = previewQueuedUrls;
    const id = previewReleaseId;
    // Cleared before the call so the live tracklist is what the user watches.
    // The view itself is reset by queue_job_started, NOT here: while something is
    // downloading this job only joins the queue, and resetting now would wipe the
    // running job's rows. confirmImport leaves the reset to the same place.
    clearReleasePreview();
    try {
      const res = await EnqueuePreviewSelection(id, indices, title);
      if (!res.started) addLog('info', `Added "${title}" to the queue (position ${res.position})`);
    } catch (err) {
      addLog('error', `Could not start that download: ${err}`);
      return;
    }
    if (extra.length) await enqueueUrls(extra);
  }

  // The URL bar's one button. Which half it runs is the same thing its label says.
  function submitUrlBar() {
    if (urlBarDownloads) { downloadPreviewSelection(); return; }
    startDownload();
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
      inputUrl = '';
      // PV-1 — the first link is resolved and shown; the rest are held and join
      // the queue when Download now is pressed. Nothing downloads from here.
      // (SF-1's enqueueUrls still starts-or-waits, it is just called later now.)
      await loadReleasePreview(otherUrls[0], otherUrls.slice(1));
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

  /** Length of one track as m:ss. formatDuration() is for whole releases. */
  function formatTrackLength(ms: number): string {
    if (!ms || ms < 0) return '';
    const total = Math.round(ms / 1000);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
  }

  // Issue #58 - the one search entry point. Artist keeps its own modal and its
  // own long-standing flow; song and album share the results list below.
  async function startSearch() {
    if (!searchQuery.trim()) return;
    if (searchKind === 'artist') { startArtistSearch(); return; }

    // Deliberately the SAME request counter as the artist search: switching
    // kind mid-flight has to invalidate whichever request is in the air, and
    // two counters would let a slow artist search reopen its modal over these
    // results.
    const reqId = ++artistSearchReqId;
    catalogLoading = true;
    showCatalogResults = true;
    showArtistSearch = false;
    catalogResults = [];
    try {
      const raw = await SearchCatalog(searchQuery.trim(), searchKind, searchSource);
      if (reqId !== artistSearchReqId) return;
      const parsed = JSON.parse(raw);
      if (parsed?.error) {
        addLog('error', `Search error: ${parsed.error}`);
        showCatalogResults = false;
      } else {
        catalogResults = Array.isArray(parsed) ? parsed : [];
        // The backend falls back to Apple when Spotify is unreachable (its
        // public search is rate-limited on some networks). Say so rather than
        // presenting Apple results under a "Spotify" label.
        if (searchSource === 'spotify' && catalogResults.length
            && catalogResults[0]?.source && catalogResults[0].source !== 'spotify') {
          addLog('warning', 'Spotify search was unavailable — showing Apple Music results instead.');
        }
      }
    } catch (e) {
      if (reqId === artistSearchReqId) {
        addLog('error', `Search failed: ${e}`);
        showCatalogResults = false;
      }
    } finally {
      if (reqId === artistSearchReqId) catalogLoading = false;
    }
  }

  // A result is a link, so it takes exactly the path a pasted link takes. The
  // tab is NOT switched: the preview renders below the search bar with its own
  // Download now, so the box stays there for the next search.
  async function openSearchResult(row: any) {
    if (!row?.url) return;
    showCatalogResults = false;
    await loadReleasePreview(row.url);
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
    if(confirm(tr('history.confirm_clear'))) {
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

  /* ═════════════════════════════════════════════════════════════════════════
     COMMAND PALETTE — Ctrl/Cmd+K   (v2.0.0 PORT-1)

     Before this, the desktop had NO global keyboard shortcuts: the only
     window-level handler was the analyzer's, and it returns immediately unless
     the analyzer is open. Everything else was a per-input `on:keydown`.

     Three things are carried over from the Antra-Web version deliberately:

       * the matcher lives in `lib/palette.ts`, DOM-free, so it is executable
         under plain `node` in `test_palette.cjs`;
       * settings come from an INDEX (`lib/settings-catalog.ts`), never from
         scraping the settings markup, so they are searchable before any of the
         three settings surfaces has ever been opened;
       * the key handler is registered in the CAPTURE phase on `document`. The
         analyzer, the folder modal and the artist-search modal all add their
         own Escape handlers, and the palette can be opened on top of any of
         them — capturing lets it consume its own keys before they also close
         whatever is underneath. Only Ctrl/Cmd+K is bound globally; a bare-key
         shortcut would fire while the user is typing a URL.
     ═════════════════════════════════════════════════════════════════════════ */
  let paletteOpen = false;
  let paletteQuery = '';
  let paletteCursor = 0;
  let paletteAll: PaletteCommand[] = [];
  let paletteResults: PaletteScored[] = [];
  let paletteInputEl: HTMLInputElement | null = null;
  let paletteListEl: HTMLDivElement | null = null;
  let paletteRestoreFocus: HTMLElement | null = null;

  /* Catalogue KEYS, resolved with `tr()` where they are used: this table is
     lifted out of App.svelte and executed by `test_palette.cjs`, so it has to
     stay free of function calls. */
  const SETTING_SURFACE_LABEL: Record<SettingSurface, string> = {
    main: 'palette.surface.main',
    settings: 'palette.surface.settings',
    folder: 'palette.surface.folder',
    themes: 'palette.surface.themes',
  };

  /* Open the surface a setting lives on, then take the user to that exact row.
     Only ONE surface may be open at a time: leaving the folder modal up while
     the settings modal opens on top of it produces two stacked overlays that
     both want the same Escape key. */
  async function focusSetting(id: string) {
    const entry = SETTINGS_CATALOG.find(s => s.id === id);
    if (!entry) return;

    showThemes = false;
    showFolderSettings = false;
    if (entry.surface !== 'settings') showSettings = false;

    if (entry.surface === 'settings') { if (!showSettings) await openSettings(); }
    else if (entry.surface === 'folder') await openFolderSettings();
    else if (entry.surface === 'themes') showThemes = true;

    await tick();
    // One frame of slack: the modals animate in, and scrolling an element that
    // has not been laid out yet lands nowhere.
    setTimeout(() => {
      // Ten rows live inside a `{#if config.<parent>}` block and are simply not
      // in the DOM while that parent toggle is off. They stay INDEXED — hiding
      // them would make the setting undiscoverable, which is the worse failure —
      // so the jump falls back to the parent, which is the control that has to
      // be turned on first. Without this the palette silently highlights
      // nothing, which reads as a broken command.
      let el = document.querySelector<HTMLElement>(`[data-setting="${id}"]`);
      if (!el && entry.requires) {
        el = document.querySelector<HTMLElement>(`[data-setting="${entry.requires}"]`);
      }
      if (!el) return;
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      el.classList.add('setting-flash');
      setTimeout(() => el.classList.remove('setting-flash'), 1800);
      const f = el.querySelector<HTMLElement>('input, select, textarea, button');
      if (f) { try { f.focus({ preventScroll: true }); } catch { f.focus(); } }
    }, 90);
  }

  function buildPaletteCommands(): PaletteCommand[] {
    const c: PaletteCommand[] = [];
    const hasUrl = !!(inputUrl || '').trim();

    /* ── Actions ─────────────────────────────────────────────────────────── */
    c.push({
      id: 'act.focusurl', group: tr('palette.group.actions'), label: tr('palette.cmd.focusurl'),
      keywords: ['url', 'paste', 'input', 'search', 'link'],
      run: () => {
        activeTab = 'url';
        tick().then(() => { inputUrlEl?.focus(); inputUrlEl?.select(); });
      },
    });
    c.push({
      id: 'act.paste', group: tr('palette.group.actions'), label: tr('palette.cmd.paste'),
      keywords: ['clipboard', 'url', 'add', 'go'],
      when: () => !isDownloading,
      run: async () => {
        activeTab = 'url';
        try {
          const txt = ((await ClipboardGetText()) || '').trim();
          if (txt) { inputUrl = txt; await tick(); startDownload(); return; }
        } catch { /* clipboard unavailable — fall through and just focus */ }
        await tick();
        inputUrlEl?.focus();
      },
    });
    c.push({
      id: 'act.download', group: tr('palette.group.actions'), label: tr('palette.cmd.download'),
      keywords: ['go', 'fetch', 'grab', 'save'],
      when: () => (hasUrl || !!previewRelease) && !isDownloading,
      run: () => { activeTab = 'url'; submitUrlBar(); },
    });
    c.push({
      id: 'act.cancel', group: tr('palette.group.actions'), label: tr('palette.cmd.cancel'),
      keywords: ['stop', 'abort', 'kill'],
      when: () => isDownloading,
      run: () => cancelDownload(),
    });
    c.push({
      id: 'act.artistsearch', group: tr('palette.group.actions'), label: tr('palette.cmd.artistsearch'),
      keywords: ['discography', 'albums', 'find'],
      run: () => { activeTab = 'artist'; },
    });
    c.push({
      // The formats are IN the label on purpose: the matcher scores each field on
      // its own, so with them only in keywords "import csv" matched neither the
      // label nor the keyword string and scored 0 - measured, it ranked
      // "Cover file name" first. In the label, every "import <format>" query
      // ranks this first and no recorded PORT-1/PORT-2 query changes.
      id: 'act.import', group: tr('palette.group.actions'), label: tr('palette.cmd.import'),
      hint: tr('palette.cmd.import_hint'),
      keywords: ['csv', 'm3u', 'm3u8', 'xspf', 'jspf', 'json', 'exportify', 'tunemymusic', 'soundiiz', 'upload', 'open'],
      when: () => !isDownloading,
      run: () => { activeTab = 'url'; openImportFile(); },
    });

    /* ── Activity log ────────────────────────────────────────────────────── */
    c.push({
      id: 'act.toggle-log', group: tr('palette.group.activity_log'),
      label: showLog ? tr('palette.cmd.hide_log') : tr('palette.cmd.show_log'),
      keywords: ['terminal', 'console', 'output', 'panel'],
      run: () => { showLog = !showLog; },
    });
    c.push({
      id: 'act.reset-log-width', group: tr('palette.group.activity_log'),
      label: tr('palette.cmd.reset_log_width'),
      hint: `${logPanelWidth}px \u2192 ${LOG_PANEL_DEFAULT}px`,
      keywords: ['resize', 'panel', 'wide', 'narrow', 'default', 'drag'],
      when: () => logPanelWidth !== LOG_PANEL_DEFAULT,
      run: () => resetLogPanelWidth(),
    });
    c.push({
      id: 'act.save-log', group: tr('palette.group.activity_log'), label: tr('palette.cmd.save_log'),
      hint: tr('log.n_lines', { count: logs.length }),
      keywords: ['export', 'txt', 'bug report', 'diagnostics'],
      when: () => logs.length > 0 && !savingLog,
      run: () => saveActivityLog(),
    });

    /* ── Export (FR-3) ───────────────────────────────────────────────────── */
    EXPORT_FORMATS.forEach(f => {
      c.push({
        id: 'export.' + f.id, group: tr('palette.group.export'),
        label: tr('palette.cmd.export', { format: f.label }),
        hint: tr(f.hint),
        keywords: ['playlist', 'save', 'file', f.label],
        when: () => hasUrl && !exportBusy,
        run: () => exportTracklist(f.id),
      });
    });

    /* ── Screens and windows ─────────────────────────────────────────────── */
    ([
      ['library',  tr('palette.tab.library'),     ['spotify', 'apple', 'playlists', 'saved']],
      ['url',      tr('palette.tab.url'),   ['url', 'download', 'input']],
      ['artist',   tr('palette.tab.artist'),  ['discography', 'find', 'song', 'track', 'title', 'album', 'search']],
      ['discover', tr('palette.tab.discover'),       ['charts', 'browse', 'new releases']],
    ] as Array<[typeof activeTab, string, string[]]>).forEach(([id, label, kw]) => {
      c.push({
        id: 'tab.' + id, group: tr('palette.group.goto'), label: tr('palette.cmd.goto', { name: label }),
        hint: activeTab === id ? tr('palette.hint.current') : '',
        keywords: kw,
        run: () => { activeTab = id; },
      });
    });

    c.push({ id: 'win.music', group: tr('palette.group.windows'), label: tr('palette.cmd.win_music'),
      keywords: ['player', 'library', 'files', 'listen'], run: () => openDownloadedMusic() });
    c.push({ id: 'win.history', group: tr('palette.group.windows'), label: tr('palette.cmd.win_history'),
      keywords: ['past', 'previous', 'builds'], run: () => openHistory() });
    c.push({ id: 'win.analyzer', group: tr('palette.group.windows'), label: tr('palette.cmd.win_analyzer'),
      keywords: ['spectrogram', 'quality', 'fake lossless', 'fft'], run: () => { showAnalyzer = true; } });
    c.push({ id: 'win.availability', group: tr('palette.group.windows'), label: tr('palette.cmd.win_availability'),
      keywords: ['region', 'country', 'market'], run: () => { showAvailability = true; availabilityResult = null; availabilityError = ''; } });
    c.push({ id: 'win.themes', group: tr('palette.group.windows'), label: tr('palette.cmd.win_themes'),
      keywords: ['appearance', 'colour', 'color', 'font'], run: () => { showSettings = false; showFolderSettings = false; showThemes = true; } });
    c.push({ id: 'win.folder', group: tr('palette.group.windows'), label: tr('palette.cmd.win_folder'),
      keywords: ['filename', 'template', 'naming', 'path'], run: () => { showThemes = false; showSettings = false; openFolderSettings(); } });
    c.push({ id: 'win.settings', group: tr('palette.group.windows'), label: tr('palette.cmd.win_settings'),
      keywords: ['preferences', 'config', 'options'], run: () => { showThemes = false; showFolderSettings = false; openSettings(); } });

    /* ── Output format ───────────────────────────────────────────────────── */
    formatOptions.forEach(f => {
      c.push({
        id: 'fmt.' + f.value, group: tr('palette.group.output_format'),
        label: tr('palette.cmd.format', { name: f.name }),
        hint: _fmtBase === f.value ? tr('palette.hint.current') : '',
        keywords: ['quality', 'codec', f.name, tr(f.label)],
        run: () => setParentFormat(f.value),
      });
    });
    ['16', '24'].forEach(d => {
      c.push({
        id: 'fmt.depth.' + d, group: tr('palette.group.output_format'),
        label: tr('palette.cmd.bit_depth', { depth: d }),
        hint: _fmtBitDepth === d ? tr('palette.hint.current') : '',
        keywords: ['hi-res', 'hires', 'lossless', 'depth'],
        when: () => showBitDepthRow,
        run: () => setBitDepth(d),
      });
    });

    /* ── Download sources ────────────────────────────────────────────────── */
    downloadSourceOptions.forEach(s => {
      const on = selectedDownloadSources.includes(s.value);
      c.push({
        id: 'src.' + s.value, group: tr('palette.group.download_source'),
        label: on ? tr('palette.cmd.source_remove', { name: s.label })
                  : tr('palette.cmd.source_use', { name: s.label }),
        hint: on ? tr('palette.hint.selected') : '',
        keywords: ['service', 'mirror', s.label],
        run: () => toggleDownloadSource(s.value),
      });
    });

    /* ── Appearance ──────────────────────────────────────────────────────── */
    const curMode = config.mode || 'system';
    MODES.forEach(m => {
      c.push({
        id: 'mode.' + m.id, group: tr('palette.group.appearance'),
        label: tr('palette.cmd.mode', { name: tr(m.label) }),
        hint: curMode === m.id ? tr('palette.hint.current') : '',
        keywords: ['light', 'dark', 'mode', 'contrast', tr(m.desc)],
        run: () => applyMode(m.id),
      });
    });
    THEMES.forEach(t => {
      c.push({
        id: 'theme.' + t.id, group: tr('palette.group.theme'),
        label: tr('palette.cmd.theme', { name: t.label }),
        hint: (config.theme || 'antra') === t.id ? tr('palette.hint.current') : '',
        keywords: ['colour', 'color', 'appearance', tr(t.desc)],
        run: () => applyTheme(t.id),
      });
    });
    FONTS.forEach(f => {
      c.push({
        id: 'font.' + f.id, group: tr('palette.group.typeface'),
        label: tr('palette.cmd.typeface', { name: tr(f.label) }),
        hint: (config.font || 'antra') === f.id ? tr('palette.hint.current') : '',
        keywords: ['font', 'type', 'reading', tr(f.desc)],
        run: () => applyFont(f.id),
      });
    });
    SOUNDS.forEach(s => {
      c.push({
        id: 'sfx.' + s.id, group: tr('palette.group.notification_sound'),
        label: tr('palette.cmd.sound', { name: tr(s.label) }),
        hint: (config.notify_sound || 'crystal') === s.id ? tr('palette.hint.current') : '',
        keywords: ['sound', 'alert', 'chime', 'audio', tr(s.desc)],
        run: () => chooseSound(s.id),
      });
    });

    /* ── Settings — from the catalog, so this is complete whether or not any
         settings surface has ever been rendered. ──────────────────────────── */
    SETTINGS_CATALOG.forEach(s => {
      c.push({
        id: 'set.' + s.id, group: tr('palette.group.settings'),
        label: tr(s.label),
        hint: `${tr(s.group)} · ${tr(SETTING_SURFACE_LABEL[s.surface])}`,
        keywords: ['setting', 'preference', 'option', tr(s.desc), tr(s.group)],
        run: () => focusSetting(s.id),
      });
    });

    /* ── Account ─────────────────────────────────────────────────────────── */
    c.push({
      id: 'acct.signin', group: tr('palette.group.account'), label: tr('palette.cmd.signin'),
      keywords: ['login', 'account', 'supporter', 'patreon'],
      when: () => !isSignedIn,
      run: () => { openSettings(); beginDeviceLogin(); },
    });
    c.push({
      id: 'acct.signout', group: tr('palette.group.account'), label: tr('palette.cmd.signout'),
      keywords: ['log out', 'leave', 'exit'],
      when: () => isSignedIn,
      run: () => signOutDevice(),
    });

    return c;
  }

  function refreshPalette() {
    paletteResults = searchCommands(paletteQuery, paletteAll);
    paletteCursor = 0;
  }

  function openPalette() {
    if (paletteOpen) return;
    paletteRestoreFocus = document.activeElement as HTMLElement | null;
    // Snapshot on OPEN, not per keystroke: a download finishing mid-typing must
    // not silently remove "Cancel" from under the cursor and run the row that
    // slid into its place.
    paletteAll = buildPaletteCommands();
    paletteQuery = '';
    paletteOpen = true;
    refreshPalette();
    tick().then(() => paletteInputEl?.focus());
  }

  function closePalette() {
    if (!paletteOpen) return;
    paletteOpen = false;
    const back = paletteRestoreFocus;
    paletteRestoreFocus = null;
    if (back && typeof back.focus === 'function') { try { back.focus(); } catch { /* gone */ } }
  }

  function togglePalette() { paletteOpen ? closePalette() : openPalette(); }

  function movePaletteCursor(delta: number) {
    if (!paletteResults.length) return;
    const n = paletteResults.length;
    paletteCursor = ((paletteCursor + delta) % n + n) % n;
    tick().then(() => {
      paletteListEl
        ?.querySelector<HTMLElement>('.pal-row.pal-active')
        ?.scrollIntoView({ block: 'nearest' });
    });
  }

  function runPaletteRow(i: number) {
    const r = paletteResults[i];
    if (!r) return;
    closePalette();
    // Let the overlay close before the command opens another surface or moves
    // focus, otherwise the surface being opened steals focus back from itself.
    setTimeout(() => {
      try { r.cmd.run(); } catch (e) { console.warn('[palette]', r.cmd.id, e); }
    }, 0);
  }

  function paletteKeyHandler(e: KeyboardEvent) {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault(); e.stopPropagation();
      togglePalette();
      return;
    }
    if (!paletteOpen) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closePalette(); return; }
    if (e.key === 'ArrowDown' || (e.ctrlKey && e.key === 'n')) {
      e.preventDefault(); e.stopPropagation(); movePaletteCursor(1); return;
    }
    if (e.key === 'ArrowUp' || (e.ctrlKey && e.key === 'p')) {
      e.preventDefault(); e.stopPropagation(); movePaletteCursor(-1); return;
    }
    if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); runPaletteRow(paletteCursor); return; }
  }

  onMount(() => {
    document.addEventListener('keydown', paletteKeyHandler, true);
    return () => document.removeEventListener('keydown', paletteKeyHandler, true);
  });

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
          tidalValidationStatus = { ok: false, message: tr('account.tidal_invalid_session_json', { error: String(parseErr) }) };
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
    tidalOAuth = { phase: 'starting', message: tr('account.tidal_connecting') };
    tidalValidationStatus = null;
    try {
      await StartTidalOAuthLogin();
    } catch (e) {
      tidalOAuth = { phase: 'error', message: `Failed to start OAuth: ${e}` };
    }
  }

  async function startAppleLogin() {
    appleLogin = { phase: 'starting', message: tr('account.apple_opening') };
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
        expiresNote = tr('account.amazon_session_expired');
      } else {
        const hLeft = Math.floor(msLeft / 3600000);
        const mLeft = Math.floor((msLeft % 3600000) / 60000);
        expiresNote = hLeft > 0
          ? tr('account.expires_in_hm', { h: hLeft, m: mLeft })
          : tr('account.expires_in_m', { m: mLeft });
      }
      return { capturedAt, expiresNote };
    } catch {
      return null;
    }
  }

  async function startAmazonLogin() {
    amazonLogin = { phase: 'starting', message: tr('account.amazon_opening') };
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
      text: tr('status.retrying_track'),
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
        text: state.text || tr('status.retry_failed'),
        error: state.error || tr('status.retry_failed'),
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
     <h2>{$t('loading.initializing_library_engine')}</h2>
     <p style="color: #94a3b8; font-size: 13px; margin-top: 8px; opacity: 0.7;">{$t('loading.optimized_for_navidrome_jellyfin')}</p>
  </main>
{:else if setupMode}
  <main class="setup">
    <div class="logo">
      <AntraLogo />
      <p>{$t('setup.your_music_offline_lossless')}</p>
      <p style="font-size: 12px; opacity: 0.5; margin-top: -8px;">{$t('setup.paste_a_spotify_apple_music_or_amazon')}</p>
      <p style="font-size: 11px; opacity: 0.35; margin-top: -4px; letter-spacing: 0.04em;">{$t('setup.optimized_for_navidrome_jellyfin')}</p>
    </div>

    <div class="setup-box">
      <h3>{$t('setup.setup_your_music_library')}</h3>
      <div class="field">
        <label for="outDir">{$t('common.music_library_folder')}</label>
        <p style="font-size: 11px; color: #555; margin: 0 0 8px;">{$t('folder.navidrome_jellyfin_compatible_point_this')}</p>
        <div style="display: flex; gap: 8px;">
          <input id="outDir" readonly type="text" value={config.download_path} placeholder="{$t('setup.select_your_music_library_location')}" />
          <button on:click={pickDir}>{$t('common.browse')}</button>
        </div>
      </div>
      <!-- v1.1.8 FEAT-8 Phase B — account step at first run. Deliberately not a
           hard wall: a supporter with a key does not need an account, and
           someone who skips it is prompted again on the main screen rather than
           being stuck on setup with no way forward. -->
      <div class="field" style="margin-top: 18px;">
        <label>{$t('setup.antra_account')}</label>
        <p style="font-size: 11px; color: #555; margin: 0 0 10px; line-height: 1.5;">
          {$t('setup.downloads_go_through_your_own_antra')}
        </p>
        {#if isSignedIn}
          <p style="font-size: 12px; color: #86efac; margin: 0;">
            {accountStatus?.username ? $t('setup.signed_in_as', { name: accountStatus.username }) : $t('setup.signed_in')}
          </p>
        {:else if deviceLoginState === 'waiting'}
          <p style="font-size: 11px; color: #666; margin: 0 0 8px;">{$t('setup.enter_this_code_in_your_browser')}</p>
          <p style="font-family: monospace; font-size: 20px; letter-spacing: 3px; color: var(--accent-color); margin: 0 0 6px;">{deviceUserCode}</p>
          <p style="font-size: 11px; color: #888; margin: 0;">{$t('common.waiting_for_approval')}</p>
        {:else}
          <button on:click={beginDeviceLogin} disabled={deviceLoginState === 'starting'} style="width: 100%;">
            {deviceLoginState === 'starting' ? $t('setup.opening_browser') : $t('setup.log_in_with_antra')}
          </button>
          {#if deviceLoginError}
            <p style="font-size: 11px; color: #fca5a5; margin: 8px 0 0;">{deviceLoginError}</p>
            {#if deviceLoginCanDiagnose}
              <button class="diag-link" on:click={runDiagnostic}>{$t('diagnostic.run')}</button>
            {/if}
          {/if}
        {/if}
      </div>

      <button style="margin-top: 24px; width: 100%;" on:click={saveSetup}>{$t('setup.build_my_library')}</button>
      {#if !hasOwnCredential}
        <p style="font-size: 10.5px; color: #555; text-align: center; margin: 10px 0 0;">
          {$t('setup.you_can_sign_in_later_from_settings')}
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
            <h3 style="margin:0; color:var(--accent-color);">{$t('app.antra')}</h3>
            <p style="margin:0; font-size: 11px; color: #555; letter-spacing: 0.05em;">{$t('app.music_library_engine')}</p>
          </div>
        </div>
        <div style="display: flex; gap: 8px; align-items: center;">
          <!-- A shortcut nobody knows about is a shortcut nobody uses, and there
               is no menu bar here to advertise it. -->
          <button class="pal-chip" on:click={openPalette} title="{$t('app.command_palette_ctrl_k')}"><span>{$t('app.ctrl')}</span><span>K</span></button>
          <button on:click={openDownloadedMusic} title="{$t('app.downloaded_music')}" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">🎵</button>
          <button on:click={openHistory} title="{$t('app.library_history')}" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">🕒</button>
          <button on:click={() => { showAnalyzer = true; }} title="{$t('app.audio_analyzer')}" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">🔬</button>
          <button on:click={() => { showAvailability = true; availabilityResult = null; availabilityError = ''; }} title="{$t('app.album_availability_studio')}" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">🌍</button>
          <button on:click={() => showThemes = true} title="{$t('app.themes')}" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">🎨</button>
          <button on:click={openFolderSettings} title="{$t('app.library_folder_settings')}" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">📁</button>
          <button bind:this={settingsButtonEl} on:click={openSettings} title="{$t('app.settings')}" style="background: rgba(255,255,255,0.05); padding: 6px 10px; font-size: 16px; border-color: rgba(255,255,255,0.1); line-height:1;">⚙️</button>
          <div style="width: 1px; height: 20px; background: rgba(255,255,255,0.1); margin: 0 2px;"></div>
          <div class="kofi-wrap">
            <button title="{$t('app.support_antra_on_patreon')}" on:click={() => kofiTooltipVisible = !kofiTooltipVisible} style="background: transparent; border: none; padding: 4px 6px; cursor: pointer; display: flex; align-items: center; opacity: 0.7; transition: opacity 0.15s;" on:mouseenter={(e) => e.currentTarget.style.opacity='1'} on:mouseleave={(e) => e.currentTarget.style.opacity='0.7'}>
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <circle cx="15.1" cy="10.3" r="6.4" fill="#FF424D"/>
                <rect x="2.6" y="3.3" width="4" height="17.4" fill="#ffffff"/>
              </svg>
            </button>
            {#if kofiTooltipVisible && supportStatus.enabled}
              <div class="kofi-tooltip">
                <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px;">
                  <p class="kofi-tooltip-title">{supportStatus.title}</p>
                  <button class="support-close-btn" on:click={() => kofiTooltipVisible = false} title="{$t('common.close')}">×</button>
                </div>
                <p class="kofi-tooltip-body">{supportStatus.message}</p>
                {#if supportStatusLoading}
                  <p class="kofi-tooltip-refreshing">{$t('app.refreshing')}</p>
                {/if}
                <button class="kofi-tooltip-btn" on:click={() => openExternal(supportStatus.link)}>
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><circle cx="15.1" cy="10.3" r="6.4" fill="#FF424D"/><rect x="2.6" y="3.3" width="4" height="17.4" fill="#ffffff"/></svg>
                  {$t('common.support_on_patreon')}
                </button>
              </div>
            {/if}
          </div>
          <button title="{$t('app.join_our_telegram_community')}" on:click={() => BrowserOpenURL('https://t.me/antraaverse')} style="background: transparent; border: none; padding: 4px 6px; cursor: pointer; display: flex; align-items: center; opacity: 0.7; transition: opacity 0.15s;" on:mouseenter={(e) => e.currentTarget.style.opacity='1'} on:mouseleave={(e) => e.currentTarget.style.opacity='0.7'}>
            <img src="/icons/telegram.png" alt="" width="22" height="22" style="display: block; width: 22px; height: 22px;" />
          </button>
          <button title="{$t('app.join_our_discord_community')}" on:click={() => BrowserOpenURL('https://discord.gg/gAJ3QymUta')} style="background: transparent; border: none; padding: 4px 6px; cursor: pointer; display: flex; align-items: center; opacity: 0.7; transition: opacity 0.15s;" on:mouseenter={(e) => e.currentTarget.style.opacity='1'} on:mouseleave={(e) => e.currentTarget.style.opacity='0.7'}>
            <img src="/icons/discord.png" alt="" width="22" height="22" style="display: block; width: 22px; height: 22px;" />
          </button>
        </div>
      </div>
      <!-- Mode toggle -->
      <div style="margin-top: 16px; display: flex; gap: 6px; margin-bottom: 8px;">
        <button
          on:click={() => { activeTab = 'library'; inputUrl = ''; if (!spotifyLibrary && !spotifyLibraryLoading && config.spotify_sp_dc) loadSpotifyLibrary(); }}
          style="font-size: 12px; padding: 4px 12px; opacity: {activeTab === 'library' ? 1 : 0.45}; border-color: {activeTab === 'library' ? 'var(--accent-color)' : 'rgba(255,255,255,0.1)'};">
          {$t('app.my_library')}
        </button>
        <button
          on:click={() => { activeTab = 'url'; searchQuery = ''; }}
          style="font-size: 12px; padding: 4px 12px; opacity: {activeTab === 'url' ? 1 : 0.45}; border-color: {activeTab === 'url' ? 'var(--accent-color)' : 'rgba(255,255,255,0.1)'};">
          {$t('app.url')}
        </button>
        <button
          on:click={() => { activeTab = 'artist'; inputUrl = ''; }}
          style="font-size: 12px; padding: 4px 12px; opacity: {activeTab === 'artist' ? 1 : 0.45}; border-color: {activeTab === 'artist' ? 'var(--accent-color)' : 'rgba(255,255,255,0.1)'};">
          {$t('app.search')}
        </button>
        <button
          on:click={() => { activeTab = 'discover'; inputUrl = ''; if (!discoveryGenres.length) loadDiscoveryGenres(); if (!discoveryData) loadDiscoveryData(); }}
          style="font-size: 12px; padding: 4px 12px; opacity: {activeTab === 'discover' ? 1 : 0.45}; border-color: {activeTab === 'discover' ? 'var(--accent-color)' : 'rgba(255,255,255,0.1)'};">
          {$t('common.discover')}
        </button>
      </div>

      {#if activeTab === 'library'}
        <!-- My Library tab content rendered below health chips -->
      {:else if activeTab === 'artist'}
        <!-- Artist search input -->
        <div class="input-bar" style="display: flex; gap: 8px; align-items: center;">
          <input
            bind:value={searchQuery}
            placeholder="{searchKind === 'artist' ? $t('app.artist_name') : searchKind === 'album' ? $t('search.placeholder_album') : $t('search.placeholder_song')}"
            on:keydown={(e) => e.key === 'Enter' && startSearch()}
            style="flex: 1; min-width: 0; font-family: inherit; font-size: 13px; height: 38px;"
          />
          <!-- Issue #58 - Song is the default: it is the thing that could not be
               searched for at all, and the reason the issue was filed. -->
          <select bind:value={searchKind} class="discover-select"
                  style="height: 38px; width: auto; flex: 0 0 auto; min-width: 104px;"
                  title="{$t('search.what_to_search_for')}">
            <option value="song">{$t('search.kind_song')}</option>
            <option value="album">{$t('search.kind_album')}</option>
            <option value="artist">{$t('search.kind_artist')}</option>
          </select>
          <select bind:value={searchSource} class="discover-select"
                  style="height: 38px; width: auto; flex: 0 0 auto; min-width: 148px;"
                  title="{$t('app.which_catalogue_to_search')}">
            <option value="apple">{$t('common.apple_music')}</option>
            <option value="spotify" disabled={!config.spotify_sp_dc}>{config.spotify_sp_dc ? $t('discover.source_spotify') : $t('discover.source_spotify_connect')}</option>
          </select>
          <button on:click={startSearch} disabled={!searchQuery.trim() || artistSearchLoading || catalogLoading} style="height: 38px; white-space: nowrap;">
            {(artistSearchLoading || catalogLoading) ? $t('search.searching') : $t('search.search')}
          </button>
        </div>
      {:else if activeTab === 'discover'}
      {:else}
        <!-- v1.1.8 FEAT-8 Phase B — shown BEFORE the user pastes anything, so
             they are never surprised by a refusal after queueing an album. -->
        {#if !hasOwnCredential}
          <div class="signin-banner">
            <div style="flex: 1; min-width: 0;">
              <p style="margin: 0 0 4px; font-size: 12.5px; font-weight: 600; color: var(--accent-color);">{$t('app.sign_in_to_start_downloading')}</p>
              <p style="margin: 0; font-size: 11px; color: #888; line-height: 1.5;">
                {$t('app.antra_now_downloads_through_your_own')}
              </p>
            </div>
            <button on:click={promptSignIn} style="flex-shrink: 0; background: var(--accent-color); color: #000; font-weight: 600; font-size: 12px; padding: 8px 16px;">
              {$t('app.log_in_with_antra')}
            </button>
          </div>
        {/if}
        <!-- URL input -->
        <div class="input-bar" style="display: flex; gap: 8px; align-items: flex-start;">
          <textarea
            bind:this={inputUrlEl}
            bind:value={inputUrl}
            placeholder="{$t('app.paste_one_or_more_spotify_apple_music')}"
            rows="4"
            on:keydown={(e) => e.key === 'Enter' && e.ctrlKey && submitUrlBar()}
            on:contextmenu={pasteClipboardIntoUrlBox}
            style="flex: 1; min-width: 0; resize: vertical; min-height: 64px; max-height: 200px; font-family: inherit; font-size: 13px;"
          ></textarea>
          <!-- v2.0.0 SF-1 — the input stays usable while downloading; a new
               link or import joins the queue instead of replacing the job. -->
          <div class="input-actions">
            <!-- v2.0.0 PV-1 — one button, and its label is exactly what it does:
                 Preview until a release is on screen, Download now once one is.
                 It stays enabled with an empty box while a preview is up, because
                 a card click fills the preview without touching the box. -->
            <button on:click={submitUrlBar}
                    disabled={urlBarDownloads ? previewSelectedCount === 0 : (!inputUrl || previewReleaseLoading)}
                    class="input-actions-main"
                    class:input-actions-go={urlBarDownloads}
                    title={urlBarDownloads
                      ? (isDownloading ? $t('app.tooltip_download_after_current') : '')
                      : $t('preview.tooltip_resolve_first')}>
              {#if previewReleaseLoading}{$t('preview.resolving')}
              {:else if urlBarDownloads}{#if isDownloading}{$t('preview.add_to_queue_n', { count: previewSelectedCount })}{:else}{$t('preview.download_now')}{/if}
              {:else}{$t('preview.preview')}{/if}
            </button>
            <!-- v2.0.0 PORT-3 -->
            <button
              class="import-file-btn"
              title="{$t('app.import_a_playlist_file_csv_m3u_xspf_jspf')}"
              disabled={importBusy}
              on:click={openImportFile}
            >{importBusy ? 'Opening…' : $t('app.tooltip_import_file')}</button>
          </div>
          {#if isDownloading}
            <button on:click={cancelDownload} style="background: var(--error-color); color: white; border-color: var(--error-color); align-self: stretch;">{$t('app.stop')}</button>
          {/if}
        </div>
      {/if}

      <!-- v2.0.0 SF-1 — download queue. Restored jobs from a previous session
           wait here, paused, until the user resumes or discards them. -->
      {#if queueWaiting.length > 0}
        <div class="queue-panel" class:queue-panel-attn={queuePaused} role="region" aria-label="{$t('app.download_queue')}">
          <div class="queue-head">
            <span class="queue-heading">
              {#if queueRestored}
                {$t('app.queue_interrupted', { count: queueWaiting.length })}
              {:else if queuePaused}
                {$t('app.queue_paused', { count: queueWaiting.length })}
              {:else}
                {$t('app.queue_up_next', { count: queueWaiting.length })}
              {/if}
            </span>
            <div class="queue-actions">
              {#if queuePaused}
                <button class="queue-btn queue-btn-primary" on:click={resumeQueue}
                        title={isDownloading ? $t('app.tooltip_resume_after_current') : ''}>{$t('app.resume')}</button>
              {/if}
              <button class="queue-btn" on:click={discardQueue}>{queueRestored ? 'Discard' : 'Clear'}</button>
            </div>
          </div>
          <ul class="queue-list">
            {#each queueWaiting as it (it.id)}
              <li class="queue-row">
                <span class="queue-kind" aria-hidden="true">{it.kind === 'import' ? '≡' : '↓'}</span>
                <span class="queue-row-title" title={(it.urls && it.urls.length) ? it.urls.join('\n') : it.title}>{it.title}</span>
                {#if it.kind === 'import' && it.track_count}<span class="queue-meta">{$t('common.n_tracks', { count: it.track_count })}</span>{/if}
                {#if it.interrupted}<span class="queue-badge">{$t('app.interrupted')}</span>{/if}
                <button class="queue-remove" aria-label={$t('app.remove_item_aria', { title: it.title })} title="{$t('app.remove_from_queue')}" on:click={() => removeQueued(it.id)}>×</button>
              </li>
            {/each}
          </ul>
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
        <div data-setting="output_format" class="format-selector">
          <div class="format-main-row">
            {#each formatOptions as fmt}
              <button
                class="format-pill"
                class:active={_fmtBase === fmt.value}
                title={$t(fmt.label)}
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
                ? $t('app.atmos_tooltip')
                : $t('app.atmos_locked_tooltip')}
              on:click={() => isSupporter
                ? setParentFormat('atmos')
                : (settingsNotice = $t('app.atmos_requires_key'))}
            >{isSupporter ? 'Atmos' : $t('app.atmos_locked_label')}</button>
          </div>
          {#if showBitDepthRow}
            <div class="format-sub-row">
              <button
                class="format-pill format-pill--sub"
                class:active={_fmtBitDepth === '16'}
                title="{_fmtBase === 'lossless' ? $t('app.flac16_tooltip') : $t('app.alac16_tooltip')}"
                on:click={() => setBitDepth('16')}
              >{$t('app.16_bit')}</button>
              <button
                class="format-pill format-pill--sub"
                class:active={_fmtBitDepth === '24'}
                title="{_fmtBase === 'lossless' ? $t('app.flac24_tooltip') : $t('app.alac24_tooltip')}"
                on:click={() => setBitDepth('24')}
              >{$t('app.24_bit')}</button>
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
          {$t('app.atmos_note')}
        </p>
      {/if}
    </div>

    <!-- v2.0.0 PV-1 — the resolved-but-not-downloaded release. Deliberately the
         SAME .playlist-header classes as the live job below, so the screen the
         user confirms on is the screen they then watch download; only the
         right-hand actions differ. It takes this slot while it is open, and
         Download now (or Cancel) hands the slot back to the live view. -->
    {#if previewRelease}
      <div class="playlist-header preview-header">
        {#if previewRelease.artwork_url}
          <img src={previewRelease.artwork_url} alt="" class="playlist-cover" />
        {/if}
        <div class="playlist-meta">
          {#if previewRelease.content_type}
            <span class="playlist-type">{previewRelease.content_type}</span>
          {/if}
          <span class="playlist-title">{previewRelease.title || '—'}</span>
          {#if previewRelease.artists_string}
            <span class="playlist-artists">{previewRelease.artists_string}</span>
          {/if}
          <div class="playlist-info-line">
            <span>{$t('preview.n_of_m_selected', { count: previewSelectedCount, total: previewTotal })}</span>
            {#if previewSelectedMs > 0}
              <span class="playlist-info-sep">·</span>
              <span>{formatDuration(previewSelectedMs)}</span>
            {/if}
            {#if previewRelease.release_date}
              <span class="playlist-info-sep">·</span>
              <span>{previewRelease.release_date}</span>
            {/if}
          </div>
          {#if previewRelease.quality_badge}
            <span class="playlist-quality-badge">{previewRelease.quality_badge}</span>
          {/if}
        </div>
        <div class="preview-actions">
          <!-- The primary Download now lives on the URL bar with the link it came
               from; this is the same action within reach of the tracklist. -->
          <button class="preview-go-btn"
                  disabled={previewSelectedCount === 0}
                  on:click={downloadPreviewSelection}>
            {#if isDownloading}{$t('preview.add_to_queue_n', { count: previewSelectedCount })}{:else}{$t('preview.download_now')}{/if}
          </button>
          <button class="preview-cancel-btn" on:click={clearReleasePreview}>{$t('preview.cancel')}</button>
        </div>
      </div>
    {:else if !previewReleaseLoading && (playlistTitle || playlistArtwork)}
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
              <span>{$t('app.n_songs', { count: playlistTotalTracks })}</span>
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

        <!-- v2.0.0 FR-3 - Export, available as soon as a release resolves.
             Hidden for a PORT-3 import: there is no link to re-fetch, and the
             file the rows came from is already the export. -->
        {#if !currentJobIsImport}
        <div class="playlist-export">
          <button
            class="export-btn"
            title="{$t('app.export_this_tracklist_to_a_file')}"
            disabled={exportBusy}
            on:click={() => (showExportMenu = !showExportMenu)}
          >
            {exportBusy ? 'Exporting...' : 'Export'}
          </button>
          {#if showExportMenu}
            <div class="export-menu">
              {#each EXPORT_FORMATS as f}
                <button class="export-menu-item" on:click={() => exportTracklist(f.id)}>
                  <span class="export-menu-label">{f.label}</span>
                  <span class="export-menu-hint">{$t(f.hint)}</span>
                </button>
              {/each}
            </div>
          {/if}
        </div>
        {/if}
      </div>
      {#if exportStatus}
        <p class="export-status">{exportStatus}</p>
      {/if}
    {:else if showSkeleton}
      <!-- PORT-4 — same classes as the real header, so padding, gap, cover size
           and every line box are inherited, not restated. Only the bars are new. -->
      <div class="playlist-header skel-header" aria-hidden="true">
        <div class="playlist-cover skel-block"></div>
        <div class="playlist-meta">
          <span class="playlist-type"><span class="skel-block skel-line" style="width:54px"></span></span>
          <span class="playlist-title"><span class="skel-block skel-line" style="width:240px"></span></span>
          <span class="playlist-artists"><span class="skel-block skel-line" style="width:140px"></span></span>
          <div class="playlist-info-line"><span class="skel-block skel-line" style="width:170px"></span></div>
          <span class="playlist-quality-badge skel-block">{$t('app.lossless')}</span>
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
          {$t('app.spotify')}
        </button>
        <button
          class="lib-service-tab"
          class:active={libActiveService === 'apple'}
          on:click={() => { libActiveService = 'apple'; if (config.apple_music_user_token && config.apple_authorization_token && !appleLibrary && !appleLibraryLoading) loadAppleMusicLibrary(); }}
        >
          <span class="lib-tab-icon lib-tab-icon-apple">
            <img src="/icons/apple-music.png" alt="" />
          </span>
          {$t('common.apple_music')}
        </button>
      </div>

      {#if libActiveService === 'spotify'}
      {#if !config.spotify_sp_dc}
        <div class="discover-empty" style="margin-top: 24px;">
          <div style="font-size: 36px; margin-bottom: 12px; opacity: 0.3;">
            <img src="/icons/spotify.png" alt="{$t('app.spotify')}" style="width:40px;height:40px;object-fit:contain;opacity:0.3;" />
          </div>
          <p style="opacity: 0.6;">{$t('app.connect_your_spotify_account_in_settings')}</p>
          <button on:click={() => { showSettings = true; }} style="margin-top: 8px; font-size: 12px;">{$t('app.open_settings')}</button>
        </div>
      {:else if spotifyLibraryLoading}
        <div class="discover-empty" style="margin-top: 24px;">
          <div style="font-size: 28px; margin-bottom: 12px; opacity: 0.4;">⏳</div>
          <p style="opacity: 0.5;">{$t('app.loading_your_spotify_library')}</p>
        </div>
      {:else if spotifyLibraryError}
        <div class="discover-empty" style="margin-top: 24px;">
          <div style="font-size: 28px; margin-bottom: 12px; opacity: 0.4;">⚠️</div>
          <p style="opacity: 0.7; color: var(--error-color);">{spotifyLibraryError}</p>
          <button on:click={loadSpotifyLibrary} style="margin-top: 8px; font-size: 12px;">{$t('app.retry')}</button>
        </div>
      {:else if spotifyLibrary}
        <div class="discover-sections" style="margin-top: 8px;">
          <!-- Liked Songs + Algorithmic mixes -->
          <div class="discover-section">
            <div class="discover-section-header">
              <button class="lib-collapse-btn" on:click={() => libMixesCollapsed = !libMixesCollapsed} title={libMixesCollapsed ? 'Expand' : 'Collapse'}>{libMixesCollapsed ? '▸' : '▾'}</button>
              <h3 class="discover-section-title">{$t('app.your_mixes')}</h3>
              <div class="discover-section-rule"></div>
              <button class="lib-refresh-btn" on:click={loadSpotifyLibrary} disabled={spotifyLibraryLoading} title="{$t('app.refresh_library')}">↺</button>
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
                  <div class="discovery-title">{$t('app.liked_songs')}</div>
                  <div class="discovery-subtitle">{$t('common.n_tracks', { count: spotifyLibrary.liked_songs_count })}</div>
                </div>
                <button
                  class="lib-sync-pill"
                  class:active={isPlaylistSyncing('https://open.spotify.com/collection/tracks')}
                  on:click|stopPropagation={() => togglePlaylistSync({ url: 'https://open.spotify.com/collection/tracks', name: 'Liked Songs', artwork_url: '', is_algorithmic: false })}
                  title={isPlaylistSyncing('https://open.spotify.com/collection/tracks') ? $t('app.autosync_on_tooltip') : $t('app.autosync_off_tooltip')}
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
                    <div class="discovery-subtitle">{pl.track_count > 0 ? $t('common.n_tracks', { count: pl.track_count }) : $t('discover.playlist')}</div>
                  </div>
                  <button
                    class="lib-sync-pill"
                    class:active={isPlaylistSyncing(pl.url)}
                    on:click|stopPropagation={() => togglePlaylistSync(pl)}
                    title={isPlaylistSyncing(pl.url) ? $t('app.autosync_on_tooltip') : $t('app.autosync_off_tooltip')}
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
                <h3 class="discover-section-title">{$t('app.your_playlists')}</h3>
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
                      <div class="discovery-subtitle">{pl.track_count > 0 ? $t('common.n_tracks', { count: pl.track_count }) : $t('discover.playlist')}</div>
                    </div>
                    <button
                      class="lib-sync-pill"
                      class:active={isPlaylistSyncing(pl.url)}
                      on:click|stopPropagation={() => togglePlaylistSync(pl)}
                      title={isPlaylistSyncing(pl.url) ? $t('app.autosync_on_tooltip') : $t('app.autosync_off_tooltip')}
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
                <h3 class="discover-section-title">{$t('app.saved_albums')}</h3>
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
                <h3 class="discover-section-title">{$t('app.followed_artists')}</h3>
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
                      <div class="discovery-subtitle">{$t('app.artist')}</div>
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
          <p style="opacity: 0.5;">{$t('app.click_refresh_to_load_your_library')}</p>
          <button on:click={loadSpotifyLibrary} style="margin-top: 8px; font-size: 12px;">{$t('app.load_library')}</button>
        </div>
      {/if}
      {/if}<!-- /spotify -->

      <!-- Apple Music library -->
      {#if libActiveService === 'apple'}
      {#if !config.apple_music_user_token || !config.apple_authorization_token}
        <div class="discover-empty" style="margin-top: 24px;">
          <div style="font-size: 36px; margin-bottom: 12px; opacity: 0.3;">
            <img src="/icons/apple-music.png" alt="{$t('common.apple_music')}" style="width:40px;height:40px;object-fit:contain;opacity:0.3;" />
          </div>
          <p style="opacity: 0.6;">{$t('app.connect_your_apple_music_account_in')}</p>
          <button on:click={() => { showSettings = true; }} style="margin-top: 8px; font-size: 12px;">{$t('app.open_settings')}</button>
        </div>
      {:else if appleLibraryLoading}
        <div class="discover-empty" style="margin-top: 24px;">
          <div style="font-size: 28px; margin-bottom: 12px; opacity: 0.4;">⏳</div>
          <p style="opacity: 0.5;">{$t('app.loading_your_apple_music_library')}</p>
        </div>
      {:else if appleLibraryError}
        <div class="discover-empty" style="margin-top: 24px;">
          <div style="font-size: 28px; margin-bottom: 12px; opacity: 0.4;">⚠️</div>
          <p style="opacity: 0.7; color: var(--error-color);">{appleLibraryError}</p>
          <button on:click={loadAppleMusicLibrary} style="margin-top: 8px; font-size: 12px;">{$t('app.retry')}</button>
        </div>
      {:else if appleLibrary}
        <div class="discover-sections" style="margin-top: 8px;">
          <!-- Saved Songs -->
          <div class="discover-section">
            <div class="discover-section-header">
              <h3 class="discover-section-title">{$t('app.your_music')}</h3>
              <div class="discover-section-rule"></div>
              <button class="lib-refresh-btn" on:click={loadAppleMusicLibrary} disabled={appleLibraryLoading} title="{$t('app.refresh_library')}">↺</button>
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
                  <div class="discovery-title">{$t('app.saved_songs')}</div>
                  <div class="discovery-subtitle">{$t('common.n_tracks', { count: appleLibrary.saved_songs_count })}</div>
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
                    <div class="discovery-subtitle">{pl.track_count > 0 ? $t('common.n_tracks', { count: pl.track_count }) : $t('discover.playlist')}</div>
                  </div>
                </div>
              {/each}
            </div>
          </div>

          <!-- Personal playlists -->
          {#if appleLibrary.playlists.filter(p => !p.is_algorithmic).length > 0}
            <div class="discover-section">
              <div class="discover-section-header">
                <h3 class="discover-section-title">{$t('app.your_playlists')}</h3>
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
                      <div class="discovery-subtitle">{pl.track_count > 0 ? $t('common.n_tracks', { count: pl.track_count }) : $t('discover.playlist')}</div>
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
          <p style="opacity: 0.5;">{$t('app.click_refresh_to_load_your_apple_music')}</p>
          <button on:click={loadAppleMusicLibrary} style="margin-top: 8px; font-size: 12px;">{$t('app.load_library')}</button>
        </div>
      {/if}
      {/if}<!-- /apple -->

    </div><!-- /lib-scroll-area -->
    {/if}

    <!-- Tracklist: shows all tracks that have started, persists until next download -->
    {#if activeTab !== 'library'}
    <div class="tracklist-wrapper">
      {#if jobNotice}
        <div class="job-notice" role="status">
          <span class="job-notice-icon">!</span>
          <span class="job-notice-text">{jobNotice}</span>
          <button class="job-notice-close" title="{$t('common.dismiss')}" on:click={() => (jobNotice = '')}>&#215;</button>
        </div>
      {/if}
      <div class="tracklist" bind:this={tracklistEl} on:scroll={updateTracklistScroll}>
        {#if previewReleaseError}
          <!-- v2.0.0 PV-1 — a link that could not be resolved. Nothing was
               downloaded and nothing is half-open; Try again re-runs the fetch. -->
          <div class="preview-error" role="alert">
            <p class="preview-error-msg">{previewReleaseError}</p>
            <div class="preview-error-actions">
              <button on:click={() => loadReleasePreview(previewReleaseUrl, previewQueuedUrls)}
                      disabled={!previewReleaseUrl}>{$t('app.retry')}</button>
              <button on:click={clearReleasePreview}>{$t('common.dismiss')}</button>
            </div>
          </div>
        {:else if previewRelease}
          <!-- v2.0.0 PV-1 — the tracklist the user confirms. Rows carry a checkbox
               and an ALWAYS-visible 30-second preview button: on the live
               tracklist .prev-btn appears on row hover, which is fine while
               watching a download but wrong here, where listening before
               committing is the whole point of the screen. -->
          <div class="preview-select-bar">
            <label class="preview-check-label">
              <input type="checkbox" checked={previewAllSelected}
                     indeterminate={previewSelectedCount > 0 && !previewAllSelected}
                     on:change={togglePreviewAll} />
              <span>{previewAllSelected ? $t('preview.select_none') : $t('preview.select_all')}</span>
            </label>
            <span class="preview-select-count">{$t('preview.n_of_m_selected', { count: previewSelectedCount, total: previewTotal })}</span>
          </div>
          {#each previewRelease.tracks as row, i (i)}
            {@const rowKey = previewReleaseRowKey(i)}
            <div class="track-row preview-row" class:preview-row-off={!previewSelected.has(i)}>
              <div class="track-row-main">
                <div class="track-row-head">
                  <input class="preview-check" type="checkbox"
                         checked={previewSelected.has(i)}
                         on:change={() => togglePreviewTrack(i)}
                         aria-label={$t('preview.include_track_aria', { title: row.title || '' })} />
                  {#if !previewUnavailable[rowKey]}
                    <button
                      class="prev-btn prev-btn-always"
                      class:prev-playing={previewKey === rowKey}
                      class:prev-loading={previewLoading === rowKey}
                      class:prev-error={previewErrorKey === rowKey}
                      on:click|stopPropagation={() => previewReleaseRow(i)}
                      title={previewKey === rowKey ? $t('app.tooltip_stop_preview') : $t('app.tooltip_preview_30s')}
                      aria-label={previewKey === rowKey ? $t('app.tooltip_stop_preview') : $t('app.tooltip_preview_30s')}
                      aria-pressed={previewKey === rowKey}
                    >
                      {#if previewKey === rowKey}<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor" aria-hidden="true"><path d="M6 6h12v12H6z"/></svg>{:else}<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>{/if}
                    </button>
                  {/if}
                  {#if previewUsesPlaylistPos ? row.playlist_position : row.track_number}
                    <span class="preview-row-num">{#if previewUsesPlaylistPos}{row.playlist_position}{:else}{previewHasDiscs ? (row.disc_number || 1) + '-' : ''}{row.track_number}{/if}</span>
                  {/if}
                  <span class="track-row-name">{makeTrackDisplayName(row.artist, row.title)}</span>
                </div>
                <div class="track-row-side">
                  {#if row.duration_ms}
                    <span class="track-row-status">{formatDuration(row.duration_ms)}</span>
                  {/if}
                </div>
              </div>
            </div>
          {/each}
        {:else if showSkeleton}
          <!-- PORT-4 — skeleton rows built from the real row classes. The preview
               button is the real .prev-btn class (hidden), because the real row
               reserves its 18px + margin whether or not it is visible. -->
          {#each SKEL_WIDTHS as w, i (i)}
            <div class="track-row skel-row" aria-hidden="true">
              <div class="track-row-main">
                <div class="track-row-head">
                  <span class="prev-btn skel-prev"></span>
                  <span class="track-row-name"><span class="skel-block skel-line" style="width:{w[0]}%"></span></span>
                </div>
                <div class="track-row-side">
                  <span class="track-row-status"><span class="skel-block skel-line" style="width:{w[1]}%"></span></span>
                </div>
              </div>
            </div>
          {/each}
        {:else if trackOrder.length === 0}
          <div class="tracklist-empty">
            {#if !config.spotify_sp_dc && historyItems.length === 0}
              <p>{$t('app.paste_url_hint')}</p>
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
                <span class="sep-title">{sep?.title || $t('app.tooltip_next_album')}</span>
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
                      {#if trackMeta[trackKey] && !previewUnavailable[trackKey]}
                        <button
                          class="prev-btn"
                          class:prev-playing={previewKey === trackKey}
                          class:prev-loading={previewLoading === trackKey}
                          class:prev-error={previewErrorKey === trackKey}
                          on:click|stopPropagation={() => previewTrackRow(trackKey)}
                          title={previewKey === trackKey ? $t('app.tooltip_stop_preview') : $t('app.tooltip_preview_30s')}
                          aria-label={previewKey === trackKey ? $t('app.tooltip_stop_preview') : $t('app.tooltip_preview_30s')}
                          aria-pressed={previewKey === trackKey}
                        >
                          {#if previewKey === trackKey}<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor" aria-hidden="true"><path d="M6 6h12v12H6z"/></svg>{:else}<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>{/if}
                        </button>
                      {/if}
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
        <button class="tracklist-jump-btn" on:click={scrollTracklistToBottom} title="{$t('common.jump_to_bottom')}">↓</button>
      {/if}
    </div>
    {/if}<!-- /activeTab !== 'library' -->

    <!-- ── Failed Tracks Panel (ST-4) ──────────────────────────────────────── -->
    {#if activeTab !== 'library' && failedEntries.length > 0}
    <div class="failed-panel">
      <div class="failed-panel-head">
        <button class="failed-collapse-btn"
          on:click={() => failedPanelCollapsed = !failedPanelCollapsed}
          title={failedPanelCollapsed ? $t('failed.tooltip_expand') : $t('failed.tooltip_minimize')}>
          {failedPanelCollapsed ? '▸' : '▾'}
        </button>
        <span class="failed-panel-title">{$t('failed.title', { count: failedEntries.length })}</span>
        <div class="failed-panel-actions">
          {#if retryQueue.length > 0}
            <span class="failed-retry-progress">{$t('failed.retrying_progress', { done: retryQueueTotal - retryQueue.length + 1, total: retryQueueTotal })}</span>
          {:else}
            <button class="failed-action-btn"
              on:click={retryAllFailed}
              disabled={isDownloading || failedEntries.every(e => !e.trackData)}
              title="{$t('failed.retry_all_failed_tracks_one_by_one')}">{$t('failed.retry_all')}</button>
          {/if}
          <button class="failed-action-btn failed-dismiss-all"
            on:click={dismissAllFailures}
            title="{$t('failed.dismiss_all_failed_tracks_from_this_view')}">{$t('failed.dismiss_all')}</button>
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
                title="{$t('failed.retry_this_track')}">↻</button>
              <button class="failed-close-btn"
                on:click={() => dismissFailure(entry.key)}
                title="{$t('common.dismiss')}">×</button>
            </div>
          </div>
        {/each}
      </div>
      {/if}
    </div>
    {/if}<!-- /failed panel -->

    <!-- ── Leftover playlist files (issue #75) ─────────────────────────────
         One card per playlist that finished with files its playlist no longer
         contains. Reuses the failed-panel styling so it reads as the same kind
         of "something needs your attention" strip. Nothing is deleted until
         the button is pressed, and the card only ever shows basenames — the
         paths stay in Go (playlist_orphans.go). -->
    {#if activeTab !== 'library' && orphanSets.length > 0}
      {#each orphanSets as set (set.id)}
        <div class="failed-panel orphan-panel">
          <div class="failed-panel-head">
            <button class="failed-collapse-btn"
              on:click={() => (set.collapsed = !set.collapsed, orphanSets = orphanSets)}
              title={set.collapsed ? $t('failed.tooltip_expand') : $t('failed.tooltip_minimize')}>
              {set.collapsed ? '▸' : '▾'}
            </button>
            <span class="failed-panel-title">{$t('orphans.title', { count: set.count, playlist: set.playlist })}</span>
            <div class="failed-panel-actions">
              {#if set.orphanId}
                <button class="failed-action-btn"
                  on:click={() => deleteOrphanSet(set.id)}
                  disabled={set.busy}>{set.busy ? $t('orphans.deleting') : $t('orphans.delete')}</button>
              {/if}
              <button class="failed-action-btn failed-dismiss-all"
                on:click={() => dismissOrphanSet(set.id)}>{$t('orphans.dismiss')}</button>
            </div>
          </div>
          {#if !set.collapsed}
            <div class="failed-list">
              <div class="failed-entry">
                <div class="failed-entry-info">
                  <span class="failed-entry-error">{set.orphanId ? $t('orphans.body') : $t('orphans.pruned', { count: set.count, playlist: set.playlist })}</span>
                </div>
              </div>
              {#each set.names as name}
                <div class="failed-entry">
                  <div class="failed-entry-info">
                    <span class="failed-entry-label">{name}</span>
                  </div>
                </div>
              {/each}
            </div>
          {/if}
        </div>
      {/each}
    {/if}<!-- /orphan panels -->

    <!-- Floating log toggle button (not shown on Library tab) -->
    {#if activeTab !== 'library'}
    <button class="log-toggle" on:click={() => showLog = !showLog} title="{$t('common.activity_log')}">
      📋 {showLog ? $t('log.hide_log') : 'Log'}
    </button>
    {/if}
  </main>

  <!-- ── Discover full-screen overlay ──────────────────────────────────────── -->
  {#if activeTab === 'discover'}
  <div class="discover-overlay">
    <!-- Top bar: title + filters + close -->
    <div class="discover-topbar">
      <div class="discover-topbar-left">
        <span class="discover-topbar-title">{$t('common.discover')}</span>
        <select bind:value={discoverySource}
                on:change={() => { loadDiscoveryGenres(); loadDiscoveryData(); }}
                class="discover-select" title="{$t('discover.where_discover_content_comes_from')}">
          <option value="apple">{$t('common.apple_music')}</option>
          <option value="spotify" disabled={!config.spotify_sp_dc}>{config.spotify_sp_dc ? $t('discover.source_spotify') : $t('discover.source_spotify_connect')}</option>
        </select>
        <select bind:value={discoveryRegion} on:change={() => { loadDiscoveryGenres(); loadDiscoveryData(); }} class="discover-select">
          <option value="in">{$t('discover.india_in')}</option>
          <option value="us">{$t('discover.united_states_us')}</option>
          <option value="gb">{$t('discover.united_kingdom_gb')}</option>
          <option value="ca">{$t('discover.canada_ca')}</option>
          <option value="au">{$t('discover.australia_au')}</option>
          <option value="jp">{$t('discover.japan_jp')}</option>
          <option value="de">{$t('discover.germany_de')}</option>
          <option value="fr">{$t('discover.france_fr')}</option>
          <option value="br">{$t('discover.brazil_br')}</option>
          <option value="mx">{$t('discover.mexico_mx')}</option>
          <option value="kr">{$t('discover.south_korea_kr')}</option>
          <option value="sg">{$t('discover.singapore_sg')}</option>
          <option value="ae">{$t('discover.uae_ae')}</option>
          <option value="sa">{$t('discover.saudi_arabia_sa')}</option>
          <option value="ng">{$t('discover.nigeria_ng')}</option>
          <option value="za">{$t('discover.south_africa_za')}</option>
          <option value="eg">{$t('discover.egypt_eg')}</option>
          <option value="tr">{$t('discover.turkey_tr')}</option>
          <option value="it">{$t('discover.italy_it')}</option>
          <option value="es">{$t('discover.spain_es')}</option>
          <option value="nl">{$t('discover.netherlands_nl')}</option>
          <option value="se">{$t('discover.sweden_se')}</option>
          <option value="no">{$t('discover.norway_no')}</option>
          <option value="pl">{$t('discover.poland_pl')}</option>
          <option value="ru">{$t('discover.russia_ru')}</option>
          <option value="id">{$t('discover.indonesia_id')}</option>
          <option value="ph">{$t('discover.philippines_ph')}</option>
          <option value="th">{$t('discover.thailand_th')}</option>
          <option value="my">{$t('discover.malaysia_my')}</option>
          <option value="pk">{$t('discover.pakistan_pk')}</option>
          <option value="nz">{$t('discover.new_zealand_nz')}</option>
          <option value="cn">{$t('discover.china_cn')}</option>
          <option value="hk">{$t('discover.hong_kong_hk')}</option>
          <option value="tw">{$t('discover.taiwan_tw')}</option>
        </select>
        {#if discoveryGenres.length}
          <select bind:value={discoveryGenre} on:change={loadDiscoveryData} class="discover-select discover-select-genre">
            <option value="">{$t('discover.top_charts_all_genres')}</option>
            {#each discoveryGenres as genre}
              <option value={genre.id}>{genre.name}</option>
            {/each}
          </select>
        {/if}
        <button on:click={loadDiscoveryData} disabled={discoveryLoading} class="discover-refresh-btn">
          {discoveryLoading ? '↻' : $t('discover.refresh')}
        </button>
      </div>
      <button class="discover-close-btn" on:click={() => { activeTab = 'url'; }} title="{$t('discover.back_to_main')}">✕</button>
    </div>

    <!-- Scrollable content -->
    <div class="discover-body">
      {#if discoveryLoading}
        <div class="discover-loading">
          <div class="loading-spinner" style="font-size: 28px; margin-bottom: 14px;">↻</div>
          {$t('discover.loading_charts')}
        </div>
      {:else if discoveryData}
        <div class="discover-sections">
          {#if (discoveryData.top_albums && discoveryData.top_albums.length > 0) || (discoveryData.genre_albums && discoveryData.genre_albums.length > 0)}
            <div class="discover-section">
              <div class="discover-section-header">
                <h3 class="discover-section-title">{$t('discover.top_albums')}</h3>
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
                      {#if !previewUnavailable['disc:' + (item.url || item.name)]}
                        <button
                          class="disc-prev-btn"
                          class:prev-playing={previewKey === 'disc:' + (item.url || item.name)}
                          class:prev-loading={previewLoading === 'disc:' + (item.url || item.name)}
                          class:prev-error={previewErrorKey === 'disc:' + (item.url || item.name)}
                          on:click|stopPropagation={() => previewDiscoverAlbum(item)}
                          title={previewKey === 'disc:' + (item.url || item.name) ? $t('app.tooltip_stop_preview') : $t('app.tooltip_preview_30s')}
                          aria-label="{$t('discover.preview_30_seconds')}"
                          aria-pressed={previewKey === 'disc:' + (item.url || item.name)}
                        >
                          {#if previewKey === 'disc:' + (item.url || item.name)}<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor" aria-hidden="true"><path d="M6 6h12v12H6z"/></svg>{:else}<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>{/if}
                        </button>
                      {/if}
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
                <h3 class="discover-section-title">{$t('discover.recommended_playlists')}</h3>
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
          <p>{$t('discover.no_items_found_for_this_selection')}</p>
        </div>
      {/if}
    </div>
  </div>
  {/if}

  <!-- Slide-in log panel (outside main so it overlays everything) -->
  {#if showLog}
  <div class="log-panel" data-resizing={logResizing ? '1' : null}
       style="--log-panel-width: {logPanelWidth}px">
    <!-- ARIA's Window Splitter pattern: a FOCUSABLE separator is a widget and
         carries aria-valuenow/min/max (it does, below). Svelte's rule does not
         model that case and flags the tabindex, so the rule is suppressed here
         rather than dropping keyboard operability or mislabelling this a slider. -->
    <!-- svelte-ignore a11y-no-noninteractive-tabindex -->
    <div
      class="log-resize-handle"
      bind:this={logHandleEl}
      role="separator"
      aria-orientation="vertical"
      aria-label="{$t('log.resize_the_activity_log_panel')}"
      aria-valuenow={logPanelWidth}
      aria-valuemin={LOG_PANEL_MIN}
      aria-valuemax={Math.floor((typeof window !== 'undefined' ? window.innerWidth : 1280) * LOG_PANEL_MAX_VW)}
      tabindex="0"
      title="{$t('log.drag_to_resize_u00b7_arrow_keys_to_nudge')}"
      on:pointerdown={logResizeStart}
      on:pointermove={logResizeMove}
      on:pointerup={logResizeEnd}
      on:pointercancel={logResizeEnd}
      on:lostpointercapture={logResizeEnd}
      on:dblclick={resetLogPanelWidth}
      on:keydown={logResizeKey}
    ></div>
    <div class="log-panel-head">
      <span>{$t('common.activity_log')}</span>
      <div style="display:flex; gap:6px; align-items:center;">
        <button
          class="log-save-btn"
          on:click={saveActivityLog}
          disabled={savingLog || logs.length === 0}
          title="{$t('log.save_this_log_to_a_txt_file_credentials')}"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M12 3v11" /><path d="M7.5 10.5 12 15l4.5-4.5" /><path d="M4 20h16" />
          </svg>
          <span>{savingLog ? 'Saving…' : $t('log.save_log_tooltip')}</span>
        </button>
        {#if !logAtBottom}
          <button class="log-jump-btn" on:click={() => scrollToBottom(true)} title="{$t('common.jump_to_bottom')}">↓</button>
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
      <button class="sponsor-toast-btn" on:click={() => { openExternal(supportStatus.link); dismissSponsorToast(); }}>
        {$t('common.support_on_patreon')}
      </button>
    </div>
    <button class="sponsor-toast-close" on:click={dismissSponsorToast} title="{$t('common.dismiss')}">×</button>
  </div>
{/if}

<!-- ── Album Availability Studio ─────────────────────────────────────────── -->
{#if showAvailability}
<div class="modal-overlay" on:click={() => showAvailability = false}>
  <div class="modal-content" on:click|stopPropagation style="max-width: 700px; width: 100%; max-height: 88vh; display: flex; flex-direction: column;">

    <!-- Header -->
    <div style="display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid rgba(255,255,255,0.08); padding-bottom:16px; margin-bottom:16px; flex-shrink:0;">
      <div>
        <h3 style="margin:0 0 4px; color:var(--accent-color); font-size:16px;">{$t('availability.album_availability_studio')}</h3>
        <p style="margin:0; font-size:12px; color:#555; line-height:1.4;">{$t('availability.paste_a_single_spotify_or_deezer_album')}</p>
      </div>
      <button on:click={() => showAvailability = false} style="padding:4px 10px; font-size:12px; flex-shrink:0; margin-left:16px;">{$t('availability.close')}</button>
    </div>

    <!-- URL input row -->
    <div style="display:flex; gap:8px; flex-shrink:0; margin-bottom:20px;">
      <input
        type="text"
        bind:value={availabilityUrl}
        placeholder="{$t('availability.https_open_spotify_com_album_or_deezer')}"
        style="flex:1; font-size:13px; box-sizing:border-box;"
        on:keydown={(e) => e.key === 'Enter' && !availabilityLoading && inspectAlbum()}
      />
      <button
        on:click={inspectAlbum}
        disabled={availabilityLoading || !availabilityUrl.trim()}
        style="padding:7px 16px; font-size:13px; white-space:nowrap; flex-shrink:0;"
      >{availabilityLoading ? $t('availability.inspecting') : $t('availability.inspect_album')}</button>
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
              <p style="margin:0 0 2px; font-size:11px; color:#555;">{$t('availability.label_value', { label: availabilityResult.label })}</p>
            {/if}
            {#if availabilityResult.upc}
              <p style="margin:0; font-size:11px; color:#555; font-family:monospace;">{$t('availability.upc_value', { upc: availabilityResult.upc })}</p>
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
          <p style="margin:0; font-size:13px;">{$t('availability.paste_a_spotify_or_deezer_album_url')}<br>{$t('availability.country_by_country_availability')}</p>
        </div>
      {:else}
        <div style="text-align:center; padding:60px 0; color:#555; font-size:13px;">
          {$t('availability.probing_markets_in_parallel_this_takes')}
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
        <h2 class="themes-title">{$t('themes.themes')}</h2>
        <p class="themes-subtitle">{$t('themes.choose_from_antra_originals_or_streaming')}</p>
      </div>
      <button on:click={() => showThemes = false} style="padding: 6px 14px; font-size: 13px; flex-shrink: 0;">{$t('common.close')}</button>
    </div>
    <div class="themes-body">
      <!-- Appearance: Light / Dark / System (v2.0.0 THEME-2) -->
      <div data-setting="mode" class="themes-section">
        <div class="themes-section-head">
          <span class="themes-section-label">{$t('themes.appearance')}</span>
          <span class="themes-section-sub">{$t('themes.every_theme_has_a_light_and_a_dark')}</span>
        </div>
        <div class="mode-row">
          {#each MODES as m}
            <button
              class="mode-btn{(config.mode || 'system') === m.id ? ' mode-btn--active' : ''}"
              on:click={() => applyMode(m.id)}
            >
              <span class="mode-btn-icon">{m.icon}</span>
              <span class="mode-btn-name">{$t(m.label)}</span>
              <span class="mode-btn-desc">{$t(m.desc)}</span>
            </button>
          {/each}
        </div>
      </div>

      <!-- Typeface (v1.1.8 FEAT-10) -->
      <div data-setting="font" class="themes-section">
        <div class="themes-section-head">
          <span class="themes-section-label">{$t('themes.typeface')}</span>
          <span class="themes-section-sub">{$t('themes.applies_across_the_whole_app')}</span>
        </div>
        <div class="font-grid">
          {#each FONTS as f}
            <button
              class="font-card{(config.font || 'antra') === f.id ? ' font-card--active' : ''}"
              on:click={() => applyFont(f.id)}
            >
              <!-- Each card previews in its own face, so the choice is visible
                   before it is applied. -->
              <span class="font-card-sample" style="font-family:{f.css};">{$t('themes.aa')}</span>
              <span class="font-card-body">
                <span class="font-card-name" style="font-family:{f.css};">{$t(f.label)}</span>
                <span class="font-card-desc">{$t(f.desc)}</span>
              </span>
              {#if (config.font || 'antra') === f.id}<span class="font-card-badge">{$t('themes.active')}</span>{/if}
            </button>
          {/each}
        </div>
      </div>

      <!-- Language (v2.0.0 SF-3). Lives beside the typeface because it is the
           same kind of choice — how the app reads, not what it does. "System"
           is first and is the default: the webview reports the OS UI language,
           so the correct answer for most people needs no decision. -->
      <div data-setting="language" class="themes-section">
        <div class="themes-section-head">
          <span class="themes-section-label">{$t('themes.language')}</span>
          <span class="themes-section-sub">{$t('themes.language_sub')}</span>
        </div>
        <div class="font-grid">
          <button
            class="font-card{!config.language ? ' font-card--active' : ''}"
            on:click={() => applyLanguage('')}
          >
            <span class="font-card-sample">🌐</span>
            <span class="font-card-body">
              <span class="font-card-name">{$t('themes.language_system')}</span>
              <span class="font-card-desc">{$t('themes.language_system_desc', { name: systemLocaleName })}</span>
            </span>
            {#if !config.language}<span class="font-card-badge">{$t('themes.active')}</span>{/if}
          </button>
          {#each LOCALES as loc}
            <button
              class="font-card{config.language === loc.id ? ' font-card--active' : ''}"
              on:click={() => applyLanguage(loc.id)}
            >
              <span class="font-card-sample">{loc.id.slice(0, 2).toUpperCase()}</span>
              <span class="font-card-body">
                <span class="font-card-name">{loc.nativeName}</span>
                <span class="font-card-desc">{loc.englishName}</span>
              </span>
              {#if config.language === loc.id}<span class="font-card-badge">{$t('themes.active')}</span>{/if}
            </button>
          {/each}
        </div>
        <p class="themes-section-note">{$t('themes.language_help')}</p>
      </div>

      <!-- Notification sound (v1.1.8 FEAT-13) -->
      <div data-setting="notify_sound" class="themes-section">
        <div class="themes-section-head">
          <span class="themes-section-label">{$t('themes.notification_sound')}</span>
          <span class="themes-section-sub">{$t('themes.plays_when_a_track_is_saved_click_a_card')}</span>
        </div>
        <div class="font-grid">
          {#each SOUNDS as s}
            <button
              class="font-card{(config.notify_sound || 'crystal') === s.id ? ' font-card--active' : ''}"
              on:click={() => chooseSound(s.id)}
            >
              <span class="font-card-sample sfx-glyph">{s.icon}</span>
              <span class="font-card-body">
                <span class="font-card-name">{$t(s.label)}</span>
                <span class="font-card-desc">{$t(s.desc)}</span>
              </span>
              {#if (config.notify_sound || 'crystal') === s.id}<span class="font-card-badge">{$t('themes.active')}</span>{/if}
            </button>
          {/each}
        </div>

        {#if (config.notify_sound || 'crystal') !== 'off'}
          <div class="sfx-controls">
            <label data-setting="notify_volume" class="sfx-vol">
              <span class="sfx-vol-label">{$t('themes.volume')}</span>
              <input
                type="range" min="0" max="100" step="5"
                bind:value={config.notify_volume}
                on:input={onVolumeChange}
              />
              <span class="sfx-vol-num">{config.notify_volume ?? 70}%</span>
            </label>
            <label data-setting="notify_batch_only" class="sfx-check">
              <input
                type="checkbox"
                bind:checked={config.notify_batch_only}
                on:change={() => SaveConfig(config)}
              />
              <span>
                {$t('themes.only_when_whole_download_finishes')}
                <small>{$t('themes.only_when_whole_download_finishes_sub')}</small>
              </span>
            </label>
          </div>
        {/if}
      </div>

      <!-- FR-5 — preview volume. Deliberately its own control: a preview is a
           30-second snippet you audition while browsing, and wanting that quieter
           than the library player is the normal case. -->
      <div class="themes-section">
        <div class="themes-section-head">
          <span class="themes-section-label">{$t('themes.previews')}</span>
          <span class="themes-section-sub">{$t('themes.volume_for_the_30_second_previews')}</span>
        </div>
        <div class="sfx-controls">
          <label data-setting="preview_volume" class="sfx-vol">
            <span class="sfx-vol-label">{$t('themes.volume')}</span>
            <input
              type="range" min="0" max="100" step="5"
              bind:value={config.preview_volume}
              on:change={() => SaveConfig(config)}
            />
            <span class="sfx-vol-num">{config.preview_volume ?? 70}%</span>
          </label>
        </div>
      </div>

      <div class="themes-section">
        <div class="themes-section-head">
          <span class="themes-section-label">{$t('themes.dynamic')}</span>
          <span class="themes-section-sub">{$t('themes.derived_from_cover_art')}</span>
        </div>
        <div class="themes-grid">
          {#each THEMES.filter(th => th.cat === 'dynamic') as th}
            {@const live = artworkCardColors(artworkSeed, config.mode || 'system')}
            <button
              class="theme-card{config.theme === th.id ? ' theme-card--active' : ''}"
              on:click={() => applyTheme(th.id)}
            >
              <div class="theme-card-preview"
                   style="background:{live ? `linear-gradient(145deg, ${live[0]} 0%, ${live[1]} 55%, ${live[2]} 100%)` : th.preview}; color:{th.tone};">
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
                {#each (live || th.colors) as c}
                  <div class="theme-card-swatch" style="background:{c};"></div>
                {/each}
              </div>
              <div class="theme-card-name">{th.label}</div>
              <div class="theme-card-desc">{$t(th.desc)}</div>
              {#if config.theme === th.id}
                <div class="theme-card-active-badge">{$t('themes.active')}</div>
              {/if}
            </button>
          {/each}
        </div>
      </div>
      <div class="themes-section">
        <div class="themes-section-head">
          <span class="themes-section-label">{$t('themes.custom')}</span>
          <span class="themes-section-sub">{$t('themes.base_neutral_accent')}</span>
        </div>
        <div class="custom-theme-row">
          {#each THEMES.filter(th => th.cat === 'custom') as th}
            {@const live = customCardColors(customSeed, config.mode || 'system')}
            <button
              class="theme-card custom-theme-card{config.theme === th.id ? ' theme-card--active' : ''}"
              on:click={() => applyTheme(th.id)}
            >
              <div class="theme-card-preview"
                   style="background:{live ? `linear-gradient(145deg, ${live[0]} 0%, ${live[1]} 55%, ${live[2]} 100%)` : th.preview}; color:{th.tone};">
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
                {#each (live || th.colors) as c}
                  <div class="theme-card-swatch" style="background:{c};"></div>
                {/each}
              </div>
              <div class="theme-card-name">{th.label}</div>
              <div class="theme-card-desc">{$t(th.desc)}</div>
              {#if config.theme === th.id}
                <div class="theme-card-active-badge">{$t('themes.active')}</div>
              {/if}
            </button>
          {/each}

          <!-- Two axes, 7 + 17 chips. They are independent -- a base chip's
               colour depends only on the base and an accent chip's only on the
               accent -- so the 119 combinations cost 24 controls, not 119. -->
          <div class="custom-axes">
            <div class="custom-axis">
              <div class="custom-axis-head">
                <span class="custom-axis-label">{$t('themes.base_neutral')}</span>
                <span class="custom-axis-value">
                  {customTable ? (customTable.bases.find(b => b.id === customBaseSel)?.label ?? '') : ''}
                </span>
              </div>
              <div class="custom-chips">
                {#each (customTable?.bases ?? []) as b}
                  <button
                    class="custom-chip{customBaseSel === b.id ? ' custom-chip--active' : ''}"
                    title={b.label}
                    aria-label={$t('themes.base_neutral_aria', { name: b.label })}
                    style="background:{customChipColor(b, 'base', config.mode || 'system')};"
                    on:click={() => chooseCustomAxis('base', b.id)}
                  ></button>
                {/each}
              </div>
              <div class="custom-axis-hint">{$t('themes.which_grey_every_surface_border_and')}</div>
            </div>

            <div class="custom-axis">
              <div class="custom-axis-head">
                <span class="custom-axis-label">{$t('themes.accent')}</span>
                <span class="custom-axis-value">
                  {customTable ? (customTable.accents.find(a => a.id === customAccentSel)?.label ?? '') : ''}
                </span>
              </div>
              <div class="custom-chips">
                {#each (customTable?.accents ?? []) as a}
                  <button
                    class="custom-chip{customAccentSel === a.id ? ' custom-chip--active' : ''}"
                    title={a.label}
                    aria-label={$t('themes.accent_aria', { name: a.label })}
                    style="background:{customChipColor(a, 'accent', config.mode || 'system')};"
                    on:click={() => chooseCustomAxis('accent', a.id)}
                  ></button>
                {/each}
              </div>
              <div class="custom-axis-hint">{$t('themes.buttons_links_and_highlights_lightness')}</div>
            </div>
          </div>
        </div>
      </div>
      <div data-setting="theme" class="themes-section">
        <div class="themes-section-head">
          <span class="themes-section-label">{$t('themes.antra_originals')}</span>
          <span class="themes-section-sub">{$t('themes.original_themes')}</span>
        </div>
        <div class="themes-grid">
          {#each THEMES.filter(th => th.cat === 'original') as th}
            <button
              class="theme-card{config.theme === th.id || (!config.theme && th.id === 'antra') ? ' theme-card--active' : ''}"
              on:click={() => applyTheme(th.id)}
            >
              <div class="theme-card-preview" style="background:{th.preview}; color:{th.tone};">
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
                {#each th.colors as c}
                  <div class="theme-card-swatch" style="background:{c};"></div>
                {/each}
              </div>
              <div class="theme-card-name">{th.label}</div>
              <div class="theme-card-desc">{$t(th.desc)}</div>
              {#if config.theme === th.id || (!config.theme && th.id === 'antra')}
                <div class="theme-card-active-badge">{$t('themes.active')}</div>
              {/if}
            </button>
          {/each}
        </div>
      </div>
      <div class="themes-section">
        <div class="themes-section-head">
          <span class="themes-section-label">{$t('themes.streaming_services')}</span>
          <span class="themes-section-sub">{$t('themes.resolver_inspired_themes')}</span>
        </div>
        <div class="themes-grid">
          {#each THEMES.filter(th => th.cat === 'service') as th}
            <button
              class="theme-card{config.theme === th.id ? ' theme-card--active' : ''}"
              on:click={() => applyTheme(th.id)}
            >
              <div class="theme-card-preview" style="background:{th.preview}; color:{th.tone};">
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
                {#each th.colors as c}
                  <div class="theme-card-swatch" style="background:{c};"></div>
                {/each}
              </div>
              <div style="display:flex; align-items:center; gap:6px; margin-top:2px;">
                {#if th.icon}
                  <img src={th.icon} alt="" class="theme-card-icon" />
                {:else}
                  <div style="width:14px; height:14px; border-radius:50%; background:{th.colors[2]}; flex-shrink:0; opacity:0.9;"></div>
                {/if}
                <div class="theme-card-name">{th.label}</div>
              </div>
              <div class="theme-card-desc">{$t(th.desc)}</div>
              {#if config.theme === th.id}
                <div class="theme-card-active-badge">{$t('themes.active')}</div>
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
        <h3 style="margin:0; color:var(--accent-color);">{$t('player.downloaded_music')}</h3>
        <p style="margin:4px 0 0; font-size:12px; opacity:0.55;">{$t('player.browse_and_play_albums_or_playlists')}</p>
      </div>
      <div style="display:flex; gap:8px; align-items:center;">
        <button on:click={refreshDownloadedMusicLibrary} style="padding: 6px 10px; font-size: 12px;">{$t('player.refresh')}</button>
        <button on:click={() => showDownloadedMusic = false} style="padding: 6px 10px; font-size: 12px;">{$t('common.close')}</button>
      </div>
    </div>

    <div class="downloaded-tabs">
      <button class:active-tab={downloadedView === 'albums'} on:click={() => downloadedView = 'albums'}>{$t('player.albums')}</button>
      <button class:active-tab={downloadedView === 'playlists'} on:click={() => downloadedView = 'playlists'}>{$t('player.playlists')}</button>
    </div>

    <div class="downloaded-layout">
      <div class="downloaded-library-pane">
        {#if downloadedLibraryLoading}
          <div class="downloaded-empty">{$t('player.scanning_your_library')}</div>
        {:else if downloadedLibraryError}
          <div class="downloaded-empty" style="color:#fca5a5;">{downloadedLibraryError}</div>
        {:else}
          {@const items = downloadedView === 'albums' ? downloadedLibrary.albums : downloadedLibrary.playlists}
          {#if items.length === 0}
            <div class="downloaded-empty">{$t('player.nothing_found', { kind: downloadedView, path: config.download_path })}</div>
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
          <div class="downloaded-empty">{$t('player.loading_release')}</div>
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
                {currentPlayerTrack && playerReleaseTitle === downloadedSelectedRelease.title ? $t('player.play_from_start') : $t('player.play_release')}
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
                    {track.artist || downloadedSelectedRelease.artist || $t('player.unknown_artist')}
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
          <div class="downloaded-empty">{$t('player.pick_an_album_or_playlist_to_start')}</div>
        {/if}
      </div>
    </div>

    {#if showLyrics && currentPlayerTrack}
      <div class="lyrics-panel">
        {#if lyricsLoading}
          <div class="lyrics-empty">{$t('player.loading_lyrics')}</div>
        {:else if lyricsLines.length === 0}
          <div class="lyrics-empty">{$t('player.no_lyrics_embedded_in_this_track')}</div>
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
        <div class="downloaded-player-title">{currentPlayerTrack?.title || $t('player.nothing_playing')}</div>
        <div class="downloaded-player-meta">
          {#if currentPlayerTrack}
            {(currentPlayerTrack.artist || downloadedSelectedRelease?.artist || $t('player.unknown_artist'))} · {playerReleaseTitle || downloadedSelectedRelease?.title || 'Release'}
          {:else}
            {$t('player.select_a_track')}
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
          title="{$t('player.toggle_lyrics')}"
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
        <span>{$t('player.vol')}</span>
        <input type="range" min="0" max="1" step="0.01" bind:value={playerVolume} on:input={() => { if (audioEl) audioEl.volume = playerVolume; }} />
      </div>
    </div>

    {#if playerError}
      <div style="margin-top: 10px; color: #fca5a5; font-size: 12px;">{$t('player.playback_error', { error: playerError })}</div>
    {/if}

    <audio
      bind:this={audioEl}
      preload="metadata"
      on:timeupdate={handleAudioTimeUpdate}
      on:loadedmetadata={handleAudioLoadedMetadata}
      on:ended={handleAudioEnded}
      on:error={() => playerError = $t('player.cannot_play')}
    ></audio>
  </div>
</div>
{/if}

{#if showImport}
<!-- v2.0.0 PORT-3 - preview of an imported playlist file -->
<div class="modal-overlay" on:click|self={closeImport} on:keydown={(e) => e.key === 'Escape' && closeImport()} role="dialog" aria-modal="true" aria-label="{$t('importer.import_playlist_file')}" tabindex="-1">
  <div class="modal-content import-modal">
    <div class="import-head">
      <h3>{$t('importer.import_playlist_file')}</h3>
      <button on:click={closeImport} class="import-close">{$t('common.close')}</button>
    </div>
    {#if importFileName}<p class="import-file">{importFileName}</p>{/if}

    {#if importResult && importResult.tracks.length > 0}
      <label class="import-name-label" for="importNameInput">{$t('importer.playlist_name')}</label>
      <input id="importNameInput" type="text" class="import-name" bind:value={importName} maxlength="200" />
      <p class="import-stats">
        {$t('common.n_tracks', { count: importResult.tracks.length })}
        {$t('importer.n_with_isrc', { count: importResult.tracks.filter(t => t.isrc).length })}
        · {importResult.format.toUpperCase()}
        {#if importResult.skipped.length}{$t('importer.n_skipped', { count: importResult.skipped.length })}{/if}
      </p>
      {#if importResult.truncated}
        <p class="import-warn">{$t('importer.truncated_warning', { max: IMPORT_MAX_ROWS })}</p>
      {/if}
      <ol class="import-list">
        {#each importResult.tracks.slice(0, IMPORT_PREVIEW_ROWS) as th}
          <li>
            <span class="import-row-title">{th.title}</span>
            <span class="import-row-artist">{th.artists.join(', ')}</span>
            {#if th.isrc}<span class="import-row-isrc">{$t('importer.isrc')}</span>{/if}
          </li>
        {/each}
      </ol>
      {#if importResult.tracks.length > IMPORT_PREVIEW_ROWS}
        <p class="import-more">{$t('importer.and_n_more', { count: importResult.tracks.length - IMPORT_PREVIEW_ROWS })}</p>
      {/if}
    {:else if !importError}
      <p class="import-warn">{$t('importer.no_downloadable_tracks_were_found_in')}</p>
    {/if}

    {#if importError}
      <p class="import-error">{importError}</p>
    {/if}

    {#if importResult && importResult.skipped.length}
      <details class="import-skipped">
        <summary>{$t('importer.n_rows_skipped', { count: importResult.skipped.length })}</summary>
        <ul>
          {#each importResult.skipped.slice(0, 50) as s}
            <li>{$t('importer.skipped_row', { row: s.row, reason: s.reason })}{s.raw ? ` — ${s.raw}` : ''}</li>
          {/each}
        </ul>
      </details>
    {/if}

    <div class="import-foot">
      <p class="import-note">{$t('importer.nothing_is_looked_up_now_each_row_is')}</p>
      <button
        class="import-confirm"
        disabled={!importResult || importResult.tracks.length === 0 || isDownloading}
        on:click={confirmImport}
      >{$t('importer.add_n_to_library', { count: importResult ? importResult.tracks.length : 0 })}</button>
    </div>
  </div>
</div>
{/if}

{#if showHistory}
<div class="modal-overlay" on:click={() => showHistory = false}>
  <div class="modal-content" on:click|stopPropagation>
    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(0,255,204,0.2); padding-bottom: 16px; margin-bottom: 16px;">
      <h3 style="margin:0; color:var(--accent-color);">{$t('history.library_build_history')}</h3>
      <button on:click={() => showHistory = false} style="padding: 4px 8px; font-size: 12px;">{$t('common.close')}</button>
    </div>
    <div style="overflow-y: auto; max-height: 400px; display: flex; flex-direction: column; gap: 12px; padding-right: 8px;">
      {#if historyItems.length === 0}
        <p style="color: #777; font-size: 13px; text-align: center;">{$t('history.no_history_found')}</p>
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
                  <span>{$t('common.n_tracks', { count: item.total || 0 })}</span>
                  {#if item.failed > 0}<span style="color:#f87171;">{$t('history.n_failed', { count: item.failed })}</span>{/if}
                  <span>· {new Date(item.date).toLocaleDateString(undefined, {month:'short', day:'numeric', year:'numeric'})}</span>
                </div>
              </div>
              <!-- An imported file is recorded as "file:<name>" (PORT-3), which is not a
                   link - startDownload would silently drop it, so offer Re-queue only
                   for something it can actually run. -->
              {#if /^(https?:|apple-music:)/i.test(item.url || '')}
              <button
                title="{$t('history.re_queue_this_url')}"
                on:click={() => { inputUrl = (inputUrl ? inputUrl + '\n' : '') + item.url; showHistory = false; }}
                style="flex-shrink: 0; padding: 2px 8px; font-size: 11px; border-color: rgba(0,255,204,0.3); color: var(--accent-color); background: rgba(0,255,204,0.05);"
              >{$t('history.re_queue')}</button>
              {/if}
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
      <button on:click={clearHistory} style="margin-top: 16px; width: 100%; border-color: var(--error-color); color: var(--error-color); background: rgba(255,0,0,0.05);">{$t('history.clear_history')}</button>
    {/if}
  </div>
</div>
{/if}

{#if showSettings}
<div class="modal-overlay" on:click={saveSettings}>
  <div class="modal-content" on:click|stopPropagation style="max-height: 88vh; display: flex; flex-direction: column;">
    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(0,255,204,0.2); padding-bottom: 16px; margin-bottom: 16px; flex-shrink: 0;">
      <h3 style="margin:0; color:var(--accent-color);">{$t('settings.settings')}</h3>
      <button on:click={saveSettings} style="padding: 4px 8px; font-size: 12px;">{$t('settings.save_close')}</button>
    </div>
    <div style="display: flex; flex-direction: column; gap: 16px; overflow-y: auto; flex: 1; padding-right: 4px;">

      <!-- ── Antra Access Key ──────────────────────────────────────────────── -->
      <div id="access-key-section" class="field" style="display:flex; flex-direction:column; gap:12px; padding:0; background:none; border:none;">

        <!-- Patreon promo box — hidden for anyone who already has supporter
             tier by EITHER route, not merely anyone with a key in the box. -->
        {#if !isSupporter}
        <div style="background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1); border-radius: 10px; padding: 14px 16px;">
          <p style="font-size: 13px; font-weight: 700; margin: 0 0 6px; color: var(--accent-color);">{$t('settings.want_the_full_experience')}</p>
          <p style="font-size: 11.5px; color: var(--text-secondary); margin: 0 0 12px; line-height: 1.5;">
            {$t('settings.patreon_promo')}
          </p>
          <button
            on:click={() => BrowserOpenURL('https://patreon.com/AntraVerse')}
            style="font-size: 12px; padding: 7px 14px; background: var(--accent-color); color: #000; border: none; border-radius: 6px; cursor: pointer; font-weight: 600;"
          >{$t('settings.support_on_patreon')}</button>
        </div>
        {/if}

        <!-- Key input -->
        <!-- v1.1.8 FEAT-8 — account login. The app never sees a password: it
             shows a short code and the browser does the actual sign-in. -->
        <div style="background: rgba(0,255,204,0.03); border: 1px solid rgba(0,255,204,0.12); border-radius: 10px; padding: 14px 16px; margin-bottom: 14px;">
          <p style="font-size: 13px; font-weight: 600; margin: 0 0 4px; color: var(--accent-color);">{$t('settings.antra_account')}</p>
          {#if isSignedIn}
            <p style="font-size: 11px; color: #86efac; margin: 0 0 4px;">
              {(accountStatus?.username || deviceLoginUser) ? (accountIsSupporter ? $t('settings.signed_in_as_supporter', { name: accountStatus?.username || deviceLoginUser }) : $t('settings.signed_in_as', { name: accountStatus?.username || deviceLoginUser })) : (accountIsSupporter ? $t('settings.signed_in_supporter') : $t('settings.signed_in')) }
            </p>
            <!-- Tier is reported from the server's verification of the token.
                 "Could not check" is deliberately distinct from "free": an
                 outage must never look like a downgrade, and it must never
                 discard a separately-valid supporter key. -->
            {#if accountStatus && accountStatus.reachable && !accountStatus.valid}
              <p style="font-size: 11px; color: #fca5a5; margin: 0 0 10px;">
                {$t('settings.this_session_has_ended_sign_in_again')}
              </p>
            {:else if accountStatus && !accountStatus.reachable}
              <p style="font-size: 11px; color: #fcd34d; margin: 0 0 10px;">
                {keyIsSupporter ? $t('settings.tier_unknown_with_key') : $t('settings.tier_unknown')}
              </p>
            {:else if accountStatus?.valid && !accountIsSupporter}
              <p style="font-size: 11px; color: #888; margin: 0 0 10px;">
                {keyIsSupporter ? $t('settings.free_tier_with_key') : $t('settings.free_tier')}
              </p>
            {/if}
            <button on:click={signOutDevice} style="font-size: 11px; padding: 6px 12px;">{$t('settings.sign_out')}</button>
          {:else if deviceLoginState === 'waiting'}
            <p style="font-size: 11px; color: #666; margin: 0 0 8px; line-height: 1.45;">
              {$t('settings.your_browser_has_been_opened_sign_in')}
            </p>
            <p style="font-family: monospace; font-size: 22px; letter-spacing: 3px; color: var(--accent-color); margin: 0 0 8px;">{deviceUserCode}</p>
            <p style="font-size: 11px; color: #888; margin: 0 0 8px;">{$t('common.waiting_for_approval')}</p>
            <button on:click={() => { stopDevicePolling(); deviceLoginState = 'idle'; }} style="font-size: 11px; padding: 6px 12px;">{$t('settings.cancel')}</button>
          {:else}
            <p style="font-size: 11px; color: #666; margin: 0 0 12px; line-height: 1.45;">
              {$t('settings.sign_in_with_your_antra_website_account')}
            </p>
            <button
              on:click={beginDeviceLogin}
              disabled={deviceLoginState === 'starting'}
              style="font-size: 12px; padding: 8px 16px; background: var(--accent-color); color: #000; font-weight: 600;"
            >{deviceLoginState === 'starting' ? $t('setup.opening_browser') : $t('setup.log_in_with_antra')}</button>
            {#if deviceLoginState === 'error'}
              <p style="font-size: 11px; color: #fca5a5; margin: 8px 0 0;">{deviceLoginError}</p>
              {#if deviceLoginCanDiagnose}
                <button class="diag-link" on:click={runDiagnostic}>{$t('diagnostic.run')}</button>
              {/if}
            {/if}
          {/if}
        </div>

        <div style="background: rgba(0,255,204,0.03); border: 1px solid rgba(0,255,204,0.12); border-radius: 10px; padding: 14px 16px;">
          <p style="font-size: 13px; font-weight: 600; margin: 0 0 4px; color: var(--accent-color);">{$t('settings.supporter_key')}</p>
          <p style="font-size: 11px; color: #666; margin: 0 0 12px; line-height: 1.45;">
            {#if accountIsSupporter}
              {$t('settings.key_not_needed')}
            {:else}
              {$t('settings.key_how_to_get')}
            {/if}
          </p>
          <div data-setting="antra_api_key" style="display: flex; gap: 8px; align-items: center;">
            <input
              type="password"
              bind:value={config.antra_api_key}
              on:input={scheduleKeyValidation}
              placeholder="{$t('settings.paste_your_supporter_key_here')}"
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
            <p style="font-size: 11px; color: #888; margin: 8px 0 0;">{$t('settings.validating_key')}</p>
          {:else if keyState === 'valid'}
            <p style="font-size: 11px; color: #86efac; margin: 8px 0 0;">
              {keyInfo?.expires_at ? (keyInfo?.key_type === 'premium' ? $t('settings.premium_key_active_expires', { date: formatKeyExpiry(keyInfo.expires_at) }) : $t('settings.supporter_key_active_expires', { date: formatKeyExpiry(keyInfo.expires_at) })) : (keyInfo?.key_type === 'premium' ? $t('settings.premium_key_active_forever') : $t('settings.supporter_key_active_forever')) }
              {#if keyInfo && keyInfo.download_limit}
                <br />{$t('settings.downloads_count', { used: keyInfo.download_count ?? 0, limit: keyInfo.download_limit })}
              {:else if keyInfo?.is_supporter}
                <br />{$t('settings.unlimited_downloads')}
              {/if}
            </p>
          {:else if keyState === 'invalid'}
            <p style="font-size: 11px; color: #fca5a5; margin: 8px 0 0;">
              {$t('settings.key_rejected', { reason: keyError || $t('settings.key_not_recognized') })}
            </p>
          {:else if keyState === 'unverified'}
            <p style="font-size: 11px; color: #fcd34d; margin: 8px 0 0;">
              {$t('settings.could_not_reach_the_antra_servers_so')}
            </p>
          {/if}
        </div>
      </div>

      <div class="field">
        <label data-setting="prefer_explicit" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
          <input type="checkbox" bind:checked={config.prefer_explicit} style="margin-top: 2px;" />
          <div>
            <span style="font-weight: 500; font-size: 13px;">{$t('settings.prefer_explicit_versions')}</span>
            <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.avoid_radio_edits_and_clean_versions')}</p>
          </div>
        </label>

        <div style="margin-top: 14px;">
          <label data-setting="strict_matching" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
            <input type="checkbox" bind:checked={config.strict_matching} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">{$t('settings.strict_matching_mode')}</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.opt_in_safety_mode_for_niche_music_antra')}</p>
            </div>
          </label>
        </div>

        <div style="margin-top: 14px;">
          <label data-setting="strict_format" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
            <input type="checkbox" bind:checked={config.strict_format} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">{$t('settings.strict_format_never_accept_a_lower')}</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.when_you_ask_for_a_lossless_format_and')}</p>
            </div>
          </label>
        </div>

        <div style="margin-top: 14px;">
          <label data-setting="prevent_lossy_transcode" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
            <input type="checkbox" bind:checked={config.prevent_lossy_transcode} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">{$t('settings.never_re_encode_lossy_audio')}</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.converting_one_lossy_format_into_another')}</p>
            </div>
          </label>
        </div>

        <div style="margin-top: 14px;">
          <label data-setting="write_antra_tags" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
            <input type="checkbox" bind:checked={config.write_antra_tags} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">{$t('settings.record_antra_version_in_downloaded_files')}</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.writes_antra_version_antra_source_and')}</p>
            </div>
          </label>
        </div>

        <!-- ── Cover Art (v2.0.0 FR-2) ────────────────────────── -->
        <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px; margin-top: 16px;">
          <p style="font-size: 13px; font-weight: 600; margin: 0 0 6px;">{$t('settings.cover_art')}</p>
          <p style="font-size: 11px; color: #555; margin: 0 0 10px;">
            {$t('settings.artwork_saved_beside_your_music_and')}
          </p>

          <label data-setting="save_cover_art_sidecar" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
            <input type="checkbox" bind:checked={config.save_cover_art_sidecar} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">{$t('settings.save_cover_art_file')}</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.writes_a_separate_image_next_to_the')}</p>
            </div>
          </label>

          {#if config.save_cover_art_sidecar}
          <div data-setting="cover_image_template" style="margin: 12px 0 0 26px; padding-left: 12px; border-left: 2px solid rgba(255,255,255,0.06);">
            <label for="coverTemplate">{$t('settings.cover_file_name')}</label>
            <p style="font-size: 11px; color: #555; margin: 4px 0 6px;">{$t('settings.cover_name_help')}</p>
            <input
              id="coverTemplate"
              type="text"
              bind:value={config.cover_image_template}
              placeholder="{$t('settings.cover')}"
              on:focus={(e) => focusedTemplateEl = e.currentTarget}
            />
            <p style="font-size: 11px; color: var(--accent-color, #37e2c2); margin: 6px 0 0; font-family: var(--font-mono);">
              {renderPreview(config.cover_image_template || 'cover')}.{config.local_artwork_format === 'png' ? 'png' : 'jpg'}{config.local_artwork_format === 'both' ? ' + .png' : ''}
            </p>

            <div style="display: flex; gap: 12px; margin-top: 12px; flex-wrap: wrap;">
              <div data-setting="local_artwork_size" style="flex: 1 1 120px; min-width: 120px;">
                <label for="coverSize">{$t('settings.max_size_px')}</label>
                <input id="coverSize" type="number" min="0" max="4000" step="100" bind:value={config.local_artwork_size} style="width: 100%;" />
                <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.0_keep_the_original')}</p>
              </div>
              <div data-setting="local_artwork_format" style="flex: 1 1 110px; min-width: 110px;">
                <label for="coverFormat">{$t('settings.format')}</label>
                <select id="coverFormat" bind:value={config.local_artwork_format} style="width: 100%;">
                  <option value="jpg">{$t('settings.jpeg')}</option>
                  <option value="png">{$t('common.png')}</option>
                  <option value="both">{$t('settings.both')}</option>
                </select>
              </div>
              <div data-setting="jpeg_image_quality" style="flex: 1 1 110px; min-width: 110px;">
                <label for="coverQuality">{$t('settings.jpeg_quality')}</label>
                <input id="coverQuality" type="number" min="50" max="100" bind:value={config.jpeg_image_quality} style="width: 100%;" />
              </div>
            </div>
            <p style="font-size: 11px; color: #555; margin: 8px 0 0;">
              {$t('settings.at_0_px_and_quality_90_the_downloaded')}
            </p>
          </div>
          {/if}

          <div style="margin-top: 16px;">
            <label data-setting="save_artist_image" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
              <input type="checkbox" bind:checked={config.save_artist_image} style="margin-top: 2px;" />
              <div>
                <span style="font-weight: 500; font-size: 13px;">{$t('settings.save_artist_image')}</span>
                <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.writes_an_artist_photo_into_the_artist')}</p>
              </div>
            </label>
            {#if config.save_artist_image}
            <div data-setting="artist_image_template" style="margin: 12px 0 0 26px; padding-left: 12px; border-left: 2px solid rgba(255,255,255,0.06);">
              <label for="artistImageTemplate">{$t('settings.artist_image_file_name')}</label>
              <input
                id="artistImageTemplate"
                type="text"
                bind:value={config.artist_image_template}
                placeholder="{$t('settings.folder')}"
                on:focus={(e) => focusedTemplateEl = e.currentTarget}
              />
              <p style="font-size: 11px; color: var(--accent-color, #37e2c2); margin: 6px 0 0; font-family: var(--font-mono);">
                {renderPreview(config.artist_image_template || 'folder')}.jpg
              </p>
            </div>
            {/if}
          </div>

          <div style="margin-top: 16px; padding-top: 14px; border-top: 1px solid rgba(255,255,255,0.04);">
            <p style="font-size: 12px; font-weight: 600; margin: 0 0 4px;">{$t('settings.embedded_artwork')}</p>
            <p style="font-size: 11px; color: #555; margin: 0 0 10px;">
              {$t('settings.the_cover_stored_inside_every_track_one')}
            </p>
            <div style="display: flex; gap: 12px; align-items: flex-start; flex-wrap: wrap;">
              <div data-setting="embedded_artwork_size" style="flex: 1 1 140px; min-width: 140px;">
                <label for="embeddedSize">{$t('settings.max_size_px')}</label>
                <input id="embeddedSize" type="number" min="0" max="4000" step="100" bind:value={config.embedded_artwork_size} style="width: 100%;" />
                <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.0_keep_the_original_default_1000')}</p>
              </div>
              <label data-setting="embedded_artwork_png" style="flex: 1 1 180px; display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin-top: 22px;">
                <input type="checkbox" bind:checked={config.embedded_artwork_png} style="margin-top: 2px;" />
                <div>
                  <span style="font-weight: 500; font-size: 13px;">{$t('settings.embed_as_png')}</span>
                  <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.much_larger_files_jpeg_is_what_almost')}</p>
                </div>
              </label>
            </div>
          </div>
        </div>

        <!-- ── Advanced tags (v2.0.0 DMX-1) ─────────────────── -->
        <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px; margin-top: 16px;">
          <button
            type="button"
            data-setting="disabled_tags"
            on:click={() => (showAdvancedTags = !showAdvancedTags)}
            style="display: flex; align-items: center; gap: 8px; background: none; border: none; padding: 0; cursor: pointer; color: inherit; width: 100%; text-align: left;"
          >
            <span style="font-size: 11px; opacity: 0.6;">{showAdvancedTags ? '▾' : '▸'}</span>
            <span style="font-size: 13px; font-weight: 600;">{$t('settings.advanced_tags')}</span>
            <span style="font-size: 11px; color: #555; margin-left: auto;">
              {(config.disabled_tags || []).length === 0
                ? $t('settings.tags_all_on')
                : $t('settings.tags_n_off', { count: (config.disabled_tags || []).length })}
            </span>
          </button>
          <p style="font-size: 11px; color: #555; margin: 6px 0 0;">
            {$t('settings.which_tags_get_written_into_each_file')}
          </p>

          {#if showAdvancedTags}
            <div style="margin-top: 12px;">
              {#each TAG_GROUPS as grp}
                <p style="font-size: 11px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; opacity: 0.5; margin: 14px 0 6px;">{$t(grp.group)}</p>
                {#each grp.fields as f}
                  <label style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin-top: 8px;">
                    <input
                      type="checkbox"
                      checked={tagOn(config.disabled_tags, f.id)}
                      on:change={(e) => setTag(f.id, e.currentTarget.checked)}
                      style="margin-top: 2px;"
                    />
                    <div>
                      <span style="font-weight: 500; font-size: 13px;">{$t(f.label)}</span>
                      {#if f.note}
                        <p style="font-size: 11px; color: #555; margin: 3px 0 0;">{$t(f.note)}</p>
                      {/if}
                    </div>
                  </label>
                {/each}
              {/each}

              <button
                type="button"
                on:click={allTagsOn}
                disabled={(config.disabled_tags || []).length === 0}
                style="margin-top: 16px; font-size: 12px; padding: 6px 12px;"
              >{$t('settings.turn_every_tag_back_on')}</button>
            </div>
          {/if}

          <div data-setting="date_format" style="margin-top: 16px;">
            <label for="dateFormatSel" style="font-size: 13px; font-weight: 500;">{$t('settings.date_format')}</label>
            <p style="font-size: 11px; color: #555; margin: 4px 0 8px;">
              {$t('settings.how_the_date_tag_is_written_leave_this')}
            </p>
            <select id="dateFormatSel" bind:value={config.date_format} style="appearance: none; -webkit-appearance: none;">
              {#each DATE_FORMATS as d}
                <option value={d.id}>{d.label} — {$t(d.note)}</option>
              {/each}
            </select>
          </div>

          <label data-setting="write_id3v1" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin-top: 14px;">
            <input type="checkbox" bind:checked={config.write_id3v1} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">{$t('settings.write_legacy_id3v1_tag_mp3')}</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.old_car_stereos_and_hardware_players')}</p>
            </div>
          </label>

          <label data-setting="replaygain_enabled" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin-top: 14px;">
            <input type="checkbox" bind:checked={config.replaygain_enabled} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">{$t('settings.write_replaygain_tags')}</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.measures_each_tracks_loudness_replaygain')}</p>
            </div>
          </label>

          <label data-setting="verify_audio_enabled" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin-top: 14px;">
            <input type="checkbox" bind:checked={config.verify_audio_enabled} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">{$t('settings.verify_audio_matches_track')}</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.verify_audio_matches_track_desc')}</p>
            </div>
          </label>
        </div>

        <!-- ── Sidecar Files (v2.0.0 PORT-2) ─────────────────── -->
        <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px; margin-top: 16px;">
          <p style="font-size: 13px; font-weight: 600; margin: 0 0 6px;">{$t('settings.sidecar_files')}</p>
          <p style="font-size: 11px; color: #555; margin: 0 0 10px;">
            {$t('settings.sidecar_help')}
          </p>

          <label data-setting="sidecar_cue" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
            <input type="checkbox" bind:checked={config.sidecar_cue} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">{$t('settings.cue_sheet_cue')}</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.track_order_titles_and_isrcs_for')}</p>
            </div>
          </label>

          <label data-setting="sidecar_nfo" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin-top: 12px;">
            <input type="checkbox" bind:checked={config.sidecar_nfo} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">{$t('settings.info_file_nfo')}</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.a_plain_text_summary_you_can_open_in_any')}</p>
            </div>
          </label>

          <label data-setting="sidecar_json" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin-top: 12px;">
            <input type="checkbox" bind:checked={config.sidecar_json} style="margin-top: 2px;" />
            <div>
              <span style="font-weight: 500; font-size: 13px;">{$t('settings.metadata_file_json')}</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.the_same_information_in_a_machine')}</p>
            </div>
          </label>
        </div>

        <div data-setting="max_retries" style="margin-top: 14px;">
          <label for="maxRetriesModal">{$t('settings.failed_track_retries')}</label>
          <p style="font-size: 11px; color: #555; margin: 4px 0 8px;">{$t('settings.automatic_retries_for_transient_failures')}</p>
          <input
            id="maxRetriesModal"
            type="number"
            min="1"
            max="10"
            bind:value={config.max_retries}
            style="width: 96px;"
          />
        </div>

        <!-- ── Post-Download Command (v2.0.0 DMX-6) ─────────────── -->
        <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px; margin-top: 16px;">
          <p style="font-size: 13px; font-weight: 600; margin: 0 0 6px;">{$t('settings.post_download_command')}</p>
          <p style="font-size: 11px; color: #555; margin: 0 0 10px;">
            {$t('settings.post_command_help')}
          </p>

          <label data-setting="post_command_enabled" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
            <input
              type="checkbox"
              bind:checked={config.post_command_enabled}
              style="margin-top: 2px;"
            />
            <div>
              <span style="font-weight: 500; font-size: 13px;">{$t('settings.run_a_command_after_downloads')}</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.off_by_default_nothing_runs_until_you')}</p>
            </div>
          </label>

          {#if config.post_command_enabled}
            <div data-setting="post_command" style="margin-top: 12px;">
              <label for="postCmdInput">{$t('settings.command')}</label>
              <input
                id="postCmdInput"
                type="text"
                bind:value={config.post_command}
                on:input={postCmdChanged}
                placeholder="{$t('settings.curl_fss_http_localhost_4533_api_scanner')}"
                style="width: 100%; font-family: var(--font-mono, monospace); font-size: 12px;"
              />
              <div style="display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px;">
                {#each POST_CMD_TOKENS as tok}
                  <button
                    type="button"
                    on:click={() => postCmdInsertToken(tok)}
                    style="font-family: var(--font-mono, monospace); font-size: 10px; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(255,255,255,0.10); background: rgba(255,255,255,0.04); color: #888; cursor: pointer;"
                  >{tok}</button>
                {/each}
              </div>

              {#if postCmdArgv.length}
                <p style="font-size: 11px; color: #555; margin: 10px 0 4px;">{$t('settings.antra_will_run_these_arguments')}</p>
                <div style="display: flex; flex-wrap: wrap; gap: 4px;">
                  {#each postCmdArgv as arg, i}
                    <code
                      style="font-size: 11px; padding: 2px 6px; border-radius: 4px; background: rgba(255,255,255,0.05); color: {i === 0 ? 'var(--accent-color, #37e2c2)' : '#999'};"
                    >{arg}</code>
                  {/each}
                </div>
              {/if}
            </div>

            <div data-setting="post_command_mode" style="margin-top: 12px;">
              <label for="postCmdMode">{$t('settings.when_to_run_it')}</label>
              <select id="postCmdMode" bind:value={config.post_command_mode} style="width: 100%; appearance: none; -webkit-appearance: none;">
                {#each POST_CMD_MODES as m}
                  <option value={m.id}>{$t(m.label)}</option>
                {/each}
              </select>
              <p style="font-size: 11px; color: #555; margin: 6px 0 0;">
                {$t('settings.post_command_mode_help')}
              </p>
            </div>

            <div data-setting="post_command_timeout" style="margin-top: 12px;">
              <label for="postCmdTimeout">{$t('settings.timeout_seconds')}</label>
              <input
                id="postCmdTimeout"
                type="number"
                min="1"
                max="600"
                bind:value={config.post_command_timeout}
                style="width: 96px;"
              />
              <p style="font-size: 11px; color: #555; margin: 6px 0 0;">{$t('settings.the_command_is_terminated_after_this')}</p>
            </div>

            {#if config.post_command && !config.post_command_confirmed}
              <div style="margin-top: 12px; padding: 12px; border-radius: 8px; border: 1px solid var(--warning-color, #facc15); background: rgba(250, 204, 21, 0.06);">
                <p style="font-size: 12px; font-weight: 600; margin: 0 0 6px; color: var(--warning-color, #facc15);">{$t('settings.confirm_before_this_runs')}</p>
                <p style="font-size: 11px; color: #999; margin: 0 0 10px;">
                  {$t('settings.antra_will_run_the_program_shown_above')}
                </p>
                <button
                  type="button"
                  on:click={postCmdConfirm}
                  style="font-size: 12px; padding: 6px 12px; border-radius: 6px; border: 1px solid var(--warning-color, #facc15); background: transparent; color: var(--warning-color, #facc15); cursor: pointer;"
                >{$t('settings.i_understand_run_this_command')}</button>
              </div>
            {:else if config.post_command_confirmed}
              <p style="font-size: 11px; color: var(--success-color, #4ade80); margin: 10px 0 0;">{$t('settings.confirmed_this_command_will_run_after')}</p>
            {/if}
          {/if}
        </div>
      </div>

      <!-- ── Download Source ──────────────────────────────────────────────── -->
      <div data-setting="download_sources" class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 6px;">{$t('settings.download_source')}</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 10px;">{$t('settings.choose_one_or_more_services_to_use_auto')}</p>
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
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 12px;">{$t('settings.sources')}</p>

        <!-- Soulseek toggle -->
        <label data-setting="soulseek_enabled" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer;">
          <input type="checkbox" bind:checked={config.soulseek_enabled} style="margin-top: 2px;" />
          <div>
            <span style="font-weight: 500;">{$t('settings.soulseek_p2p')}</span>
            <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.find_rare_or_hi_res_versions_not_on')}</p>
          </div>
        </label>

        {#if config.soulseek_enabled}
          <div style="margin-top: 12px; padding: 12px; background: rgba(255,255,255,0.03); border-radius: 8px; border: 1px solid rgba(255,255,255,0.06);">
            <label data-setting="soulseek_username" for="slskUsernameSettings" style="font-size: 12px; opacity: 0.7;">{$t('settings.soulseek_username')}</label>
            <input id="slskUsernameSettings" type="text" bind:value={config.soulseek_username} placeholder="{$t('settings.your_soulseek_username')}" style="width: 100%; box-sizing: border-box; margin-top: 6px;" />
            <label data-setting="soulseek_password" for="slskPasswordSettings" style="font-size: 12px; opacity: 0.7; margin-top: 10px; display: block;">{$t('settings.soulseek_password')}</label>
            <input id="slskPasswordSettings" type="password" bind:value={config.soulseek_password} placeholder="{$t('settings.your_soulseek_password')}" style="width: 100%; box-sizing: border-box; margin-top: 6px;" />

            <label data-setting="soulseek_seed_after_download" style="display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin-top: 14px;">
              <input type="checkbox" bind:checked={config.soulseek_seed_after_download} style="margin-top: 2px;" />
              <div>
                <span style="font-weight: 500; font-size: 13px;">{$t('settings.seed_after_download')}</span>
                <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('settings.keep_a_hardlink_in_slskds_folder_so')}</p>
              </div>
            </label>

            {#if slskdWebUIInfo}
            <div style="margin-top: 14px; padding: 10px 12px; background: rgba(0,0,0,0.2); border-radius: 6px; border: 1px solid rgba(255,255,255,0.07);">
              <p style="font-size: 11px; opacity: 0.5; margin: 0 0 6px; text-transform: uppercase; letter-spacing: 0.05em;">{$t('settings.slskd_web_ui')}</p>
              <p style="font-size: 12px; margin: 0 0 4px; font-family: monospace; color: var(--accent-color);">{slskdWebUIInfo.url}</p>
              <p style="font-size: 11px; margin: 0; opacity: 0.65; font-family: monospace;">{$t('settings.slskd_user_pass', { user: slskdWebUIInfo.username, pass: slskdWebUIInfo.password })}</p>
            </div>
            {/if}
          </div>
        {/if}


      </div>

      <div data-setting="spotify_sp_dc" class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;" id="settings-spotify-account">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 4px;">{$t('settings.spotify_account')}</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 12px;">{$t('settings.connect_your_spotify_account_to_unlock')}</p>

        <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 10px; flex-wrap: wrap;">
          <button
            style="font-size: 11px; padding: 5px 12px; background: #1DB954; color: #000; border: none; border-radius: 4px; cursor: pointer; font-weight: 600; opacity: {spDcCapture.phase === 'starting' || spDcCapture.phase === 'waiting_for_user' ? 0.6 : 1}; pointer-events: {spDcCapture.phase === 'starting' || spDcCapture.phase === 'waiting_for_user' ? 'none' : 'auto'};"
            on:click={async () => { spDcCapture = { phase: 'starting', message: $t('settings.opening_browser') }; try { await CaptureSpDC(); } catch(e) { spDcCapture = { phase: 'error', message: String(e) }; } }}
          >{spDcCapture.phase === 'starting' ? $t('setup.opening_browser') : spDcCapture.phase === 'waiting_for_user' ? $t('settings.waiting_for_login') : $t('settings.connect_spotify')}</button>
          {#if spDcCapture.phase === 'idle' || !spDcCapture.phase}
            <span style="font-size: 11px; color: #555;">{$t('settings.auto_captures_your_session_after_login')}</span>
          {:else if spDcCapture.phase === 'success'}
            <span style="font-size: 11px; color: #00ffcc;">✓ {spDcCapture.message}</span>
          {:else if spDcCapture.phase === 'error'}
            <span style="font-size: 11px; color: #ff6b6b;">{spDcCapture.message}</span>
          {:else}
            <span style="font-size: 11px; color: #aaa;">{spDcCapture.message}</span>
          {/if}
        </div>

        <label for="spDcInput" style="font-size: 12px; opacity: 0.7;">{$t('settings.sp_dc_cookie_manual')}</label>
        <input
          id="spDcInput"
          type="password"
          bind:value={config.spotify_sp_dc}
          placeholder="{$t('settings.aq')}"
          style="width: 100%; box-sizing: border-box; margin-top: 6px; font-family: monospace; font-size: 12px;"
        />
        <p style="font-size: 11px; color: #555; margin: 6px 0 0;">
          {$t('settings.sp_dc_manual_help')}
        </p>
        {#if config.spotify_sp_dc}
          <p style="font-size: 11px; color: #00ffcc; margin: 6px 0 0;">{$t('settings.connected_library_podcasts_and_auto_sync')}</p>
        {:else}
          <p style="font-size: 11px; color: #555; margin: 6px 0 0;">{$t('settings.not_connected')}</p>
        {/if}
      </div>

      <!-- ── Apple Music Account ────────────────────────────────────────────── -->
      <div data-setting="apple_music_user_token" class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;" id="settings-apple-account">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 4px;">{$t('settings.apple_music_account')}</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 12px;">{$t('settings.connect_your_apple_music_account_to')}</p>

        <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 10px; flex-wrap: wrap;">
          <button
            style="font-size: 11px; padding: 5px 12px; background: #fc3c44; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-weight: 600; opacity: {appleLogin.phase === 'starting' || appleLogin.phase === 'waiting_for_user' ? 0.6 : 1}; pointer-events: {appleLogin.phase === 'starting' || appleLogin.phase === 'waiting_for_user' ? 'none' : 'auto'};"
            on:click={startAppleLogin}
          >{appleLogin.phase === 'starting' ? $t('setup.opening_browser') : appleLogin.phase === 'waiting_for_user' ? $t('settings.waiting_for_login') : $t('settings.connect_apple')}</button>
          {#if appleLogin.phase === 'idle' || !appleLogin.phase}
            <span style="font-size: 11px; color: #555;">{$t('settings.auto_captures_your_session_after_login')}</span>
          {:else if appleLogin.phase === 'success'}
            <span style="font-size: 11px; color: #00ffcc;">✓ {appleLogin.message}</span>
          {:else if appleLogin.phase === 'error'}
            <span style="font-size: 11px; color: #ff6b6b;">{appleLogin.message}</span>
          {:else}
            <span style="font-size: 11px; color: #aaa;">{appleLogin.message}</span>
          {/if}
        </div>

        <label for="appleMusicUserTokenInput" style="font-size: 12px; opacity: 0.7;">{$t('settings.music_user_token_manual')}</label>
        <input
          id="appleMusicUserTokenInput"
          type="password"
          bind:value={config.apple_music_user_token}
          placeholder="{$t('settings.paste_your_music_user_token_here')}"
          style="font-size: 11px; width: 100%; margin-top: 4px; margin-bottom: 6px;"
          on:change={() => { if (config.apple_music_user_token && config.apple_authorization_token) { SaveConfig(config); appleLibrary = null; loadAppleMusicLibrary(); } }}
        />
        <p style="font-size: 11px; color: #555; margin: 0 0 4px;">
          {$t('settings.apple_token_help')}
        </p>
        {#if config.apple_music_user_token && config.apple_authorization_token}
          <p style="font-size: 11px; color: #00ffcc; margin: 6px 0 0;">{$t('settings.connected_apple_music_library_enabled')}</p>
        {:else}
          <p style="font-size: 11px; color: #555; margin: 6px 0 0;">{$t('settings.not_connected')}</p>
        {/if}
      </div>

      <!-- ── Auto-Sync / Scheduled Downloads ───────────────────────────────── -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;" id="settings-auto-sync">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 4px;">{$t('settings.auto_sync_scheduled_downloads')}</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 12px;">{$t('settings.automatically_check_your_spotify_library')}</p>

        <label data-setting="auto_sync_enabled" style="display: flex; align-items: center; gap: 8px; cursor: pointer; margin-bottom: 12px;">
          <input type="checkbox" bind:checked={config.auto_sync_enabled} on:change={() => SaveConfig(config)} />
          <span style="font-size: 12px;">{$t('settings.enable_scheduled_auto_sync')}</span>
        </label>

        {#if config.auto_sync_enabled}
          <div data-setting="auto_sync_time" style="display: flex; gap: 12px; align-items: center; margin-bottom: 12px;">
            <label style="font-size: 12px;">{$t('settings.time')}</label>
            <input type="number" min="0" max="23" bind:value={config.auto_sync_hour}
              style="width: 52px; text-align: center;"
              on:change={() => SaveConfig(config)} />
            <span style="font-size: 12px; opacity: 0.6;">:</span>
            <input type="number" min="0" max="59" bind:value={config.auto_sync_minute}
              style="width: 52px; text-align: center;"
              on:change={() => SaveConfig(config)} />
          </div>

          <p data-setting="auto_sync_days" style="font-size: 12px; margin: 0 0 6px; opacity: 0.7;">{$t('settings.days')}</p>
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
        <p style="font-size: 12px; font-weight: 600; margin: 0 0 8px;">{$t('settings.playlists_to_sync')}</p>
        {#if !config.spotify_sp_dc}
          <p style="font-size: 11px; color: #555; margin: 0 0 8px;">{$t('settings.configure_your_spotify_account_in_the')}</p>
        {:else if spotifyLibraryLoading}
          <p style="font-size: 11px; color: #555; margin: 0 0 8px;">{$t('settings.loading_your_library')}</p>
        {:else if spotifyLibrary}
          <div style="display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; max-height: 260px; overflow-y: auto;">
            <!-- Liked Songs -->
            <label style="display: flex; align-items: center; gap: 8px; padding: 6px 8px; background: rgba(255,255,255,0.04); border-radius: 6px; cursor: pointer;">
              <input type="checkbox"
                checked={isPlaylistSyncing('https://open.spotify.com/collection/tracks')}
                on:change={() => togglePlaylistSync({ url: 'https://open.spotify.com/collection/tracks', name: 'Liked Songs', artwork_url: '', is_algorithmic: false })} />
              <span style="font-size: 12px; flex: 1;">{$t('settings.liked_songs')}</span>
              <span style="font-size: 11px; opacity: 0.4;">{$t('common.n_tracks', { count: spotifyLibrary.liked_songs_count })}</span>
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
          <p style="font-size: 11px; color: #555; margin: 0 0 8px;">{$t('settings.library_not_loaded_return_to_the_home')}</p>
        {/if}

        <div style="display: flex; gap: 8px; margin-bottom: 10px;">
          <button
            style="font-size: 11px; padding: 5px 10px;"
            on:click={runAutoSyncNow}
            disabled={autoSyncRunning}
          >
            {autoSyncRunning ? 'Syncing…' : $t('settings.sync_now')}
          </button>
          {#if autoSyncLastResult}
            <span style="font-size: 11px; opacity: 0.7; align-self: center;">{autoSyncLastResult}</span>
          {/if}
        </div>
      </div>

    </div>

    <div style="flex-shrink: 0; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.05); margin-top: 8px; display:flex; flex-direction:column; gap:8px; align-items:center;">
      <p style="text-align: center; font-size: 11px; color: rgba(255,255,255,0.2); margin: 0;">{$t('settings.antra_version', { version: appVersion })}</p>
    </div>
  </div>
</div>
{/if}

<!-- ── Folder & Library Settings Modal ───────────────────────────────────── -->
{#if showFolderSettings}
<div class="modal-overlay" on:click|self={closeFolderSettings} on:keydown={(e) => e.key === 'Escape' && closeFolderSettings()} role="dialog" aria-modal="true" tabindex="-1">
  <div class="modal-content" on:click|stopPropagation on:keydown|stopPropagation style="max-height: 88vh; display: flex; flex-direction: column;">
    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(0,255,204,0.2); padding-bottom: 16px; margin-bottom: 16px; flex-shrink: 0;">
      <h3 style="margin:0; color:var(--accent-color);">{$t('folder.folder_structure_2')}</h3>
      <button on:click={saveFolderSettings} disabled={folderSettingsSaving} style="padding: 4px 8px; font-size: 12px;">{folderSettingsSaving ? 'Saving…' : $t('settings.save_and_close')}</button>
    </div>
    <div style="display: flex; flex-direction: column; gap: 16px; overflow-y: auto; flex: 1; padding-right: 4px;">

      <!-- Music Folder -->
      <div data-setting="download_path" class="field">
        <label for="outDirFolder">{$t('common.music_library_folder')}</label>
        <p style="font-size: 11px; color: #555; margin: 4px 0 8px;">{$t('folder.navidrome_jellyfin_compatible_point_this')}</p>
        <div style="display: flex; gap: 8px; margin-top: 4px;">
          <input id="outDirFolder" readonly type="text" value={config.download_path} />
          <button on:click={pickDir}>{$t('common.browse')}</button>
        </div>
      </div>

      <!-- Library Mode -->
      <div data-setting="library_mode" class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 0;">{$t('folder.library_mode')}</p>
        <div style="display: flex; flex-direction: column; gap: 12px; margin-top: 8px;">
          <label style="display: flex; align-items: flex-start; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="smart_dedup" bind:group={config.library_mode} style="margin-top: 2px;" />
            <div>
              {$t('folder.smart_dedup')} <span style="font-size: 11px; opacity: 0.6; margin-left: 4px;">{$t('folder.default_skip_if_already_in_library')}</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('folder.saves_storage_if_a_track_was_downloaded')}</p>
            </div>
          </label>
          <label style="display: flex; align-items: flex-start; gap: 8px; font-weight: normal; cursor: pointer;">
            <input type="radio" value="full_albums" bind:group={config.library_mode} style="margin-top: 2px;" />
            <div>
              {$t('folder.full_albums')} <span style="font-size: 11px; opacity: 0.6; margin-left: 4px;">{$t('folder.each_album_complete_allows_cross_album')}</span>
              <p style="font-size: 11px; color: #555; margin: 4px 0 0;">{$t('folder.every_album_folder_is_always_complete_a')}</p>
            </div>
          </label>
        </div>
      </div>

      <!-- v2.0.0 DMX-5 — what happens when the file is already there -->
      <div data-setting="filename_conflict_behavior" class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <label for="conflictBehaviorSelect" style="font-size: 13px; font-weight: 600; margin: 0 0 8px; display:block;">{$t('folder.when_the_file_already_exists')}</label>
        <select id="conflictBehaviorSelect" bind:value={config.filename_conflict_behavior} style="appearance: none; -webkit-appearance: none;">
          {#each CONFLICT_BEHAVIORS as opt}
            <option value={opt.id}>{$t(opt.label)}</option>
          {/each}
        </select>
        <p style="font-size: 11px; color: #555; margin: 6px 0 0;">
          {$t(CONFLICT_BEHAVIORS.find(o => o.id === config.filename_conflict_behavior)?.note ?? 'opts.conflict.skip.note')}
        </p>
      </div>

      <!-- Filenames -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 12px;">{$t('folder.filenames')}</p>

        <div style="display:flex; flex-direction:column; gap:14px;">
          <div data-setting="single_track_filename_template">
            <label for="singleTplInput" style="font-size:12px; font-weight:500; opacity:0.8; margin-bottom:5px; display:block;">{$t('folder.single_track_filename')}</label>
            <input
              id="singleTplInput"
              type="text"
              bind:value={config.single_track_filename_template}
              placeholder={'{artist} - {title}'}
              style="width:100%; box-sizing:border-box; font-family:monospace; font-size:12px;"
              on:focus={(e) => focusedTemplateEl = e.currentTarget}
            />
            {#if config.single_track_filename_template}
              <p class="tpl-preview">{$t('folder.preview_value', { value: renderPreview(config.single_track_filename_template) + '.flac' })}</p>
            {/if}
          </div>

          <div data-setting="album_track_filename_template">
            <label for="albumTplInput" style="font-size:12px; font-weight:500; opacity:0.8; margin-bottom:5px; display:block;">{$t('folder.track_filename_inside_album_folder')}</label>
            <input
              id="albumTplInput"
              type="text"
              bind:value={config.album_track_filename_template}
              placeholder={'{track} - {title}'}
              style="width:100%; box-sizing:border-box; font-family:monospace; font-size:12px;"
              on:focus={(e) => focusedTemplateEl = e.currentTarget}
            />
            {#if config.album_track_filename_template}
              <p class="tpl-preview">{$t('folder.preview_value', { value: renderPreview(config.album_track_filename_template) + '.flac' })}</p>
            {/if}
          </div>
        </div>
      </div>

      <!-- Folder Structure -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 12px;">{$t('folder.folder_structure')}</p>

        <div data-setting="folder_structure_template">
          <label for="folderTplInput" style="font-size:12px; font-weight:500; opacity:0.8; margin-bottom:5px; display:block;">{$t('folder.folder_directory_structure')}</label>
          <input
            id="folderTplInput"
            type="text"
            bind:value={config.folder_structure_template}
            placeholder="{$t('folder.leave_empty_to_save_straight_into_the')}"
            style="width:100%; box-sizing:border-box; font-family:monospace; font-size:12px;"
            on:focus={(e) => focusedTemplateEl = e.currentTarget}
          />
          {#if config.folder_structure_template}
            <p class="tpl-preview">{$t('folder.preview_value', { value: renderPreview(config.folder_structure_template) + '/' })}</p>
          {:else}
            <p class="tpl-preview">{$t('folder.no_album_folder_tracks_are_saved')}</p>
          {/if}
        </div>

        <!-- issue #78 - where a PLAYLIST download lands. Lives in this modal,
             not Settings, because the reporter's expectation was that the
             Folder Structure above already governed it. -->
        <div data-setting="playlist_storage_mode" style="margin-top:12px;">
          <label for="playlistStorageModeSelect" style="font-size:12px; font-weight:500; opacity:0.8; margin-bottom:5px; display:block;">{$t('folder.playlist_storage_mode')}</label>
          <select id="playlistStorageModeSelect" bind:value={config.playlist_storage_mode} style="width:100%;">
            <option value="playlist_folder">{$t('folder.playlist_storage_playlist_folder')}</option>
            <option value="library">{$t('folder.playlist_storage_library')}</option>
          </select>
          {#if config.playlist_storage_mode === 'library'}
            <p style="font-size:11px; opacity:0.6; margin:4px 0 0;">{$t('folder.playlist_storage_library_help')}</p>
          {:else}
            <p style="font-size:11px; opacity:0.6; margin:4px 0 0;">{$t('folder.playlist_storage_playlist_folder_help')}</p>
          {/if}
        </div>

        <!-- issue #75 - keeping a playlist folder in step with the playlist.
             Both are hidden in library mode, where a playlist has no folder of
             its own and neither control can do anything. -->
        {#if config.playlist_storage_mode !== 'library'}
          <div data-setting="playlist_renumber" style="margin-top:12px;">
            <label style="display:flex; align-items:flex-start; gap:8px; font-size:12px; font-weight:500; opacity:0.8;">
              <input type="checkbox" bind:checked={config.playlist_renumber} style="margin-top:2px;" />
              <span>{$t('folder.playlist_renumber')}</span>
            </label>
            <p style="font-size:11px; opacity:0.6; margin:4px 0 0 24px;">{$t('folder.playlist_renumber_help')}</p>
          </div>

          <div data-setting="playlist_prune_orphans" style="margin-top:12px;">
            <label for="playlistPruneOrphansSelect" style="font-size:12px; font-weight:500; opacity:0.8; margin-bottom:5px; display:block;">{$t('folder.playlist_prune_orphans')}</label>
            <select id="playlistPruneOrphansSelect" bind:value={config.playlist_prune_orphans} style="width:100%;">
              <option value="ask">{$t('folder.playlist_prune_orphans_ask')}</option>
              <option value="auto">{$t('folder.playlist_prune_orphans_auto')}</option>
              <option value="off">{$t('folder.playlist_prune_orphans_off')}</option>
            </select>
            {#if config.playlist_prune_orphans === 'auto'}
              <p style="font-size:11px; opacity:0.6; margin:4px 0 0;">{$t('folder.playlist_prune_orphans_auto_help')}</p>
            {:else if config.playlist_prune_orphans === 'off'}
              <p style="font-size:11px; opacity:0.6; margin:4px 0 0;">{$t('folder.playlist_prune_orphans_off_help')}</p>
            {:else}
              <p style="font-size:11px; opacity:0.6; margin:4px 0 0;">{$t('folder.playlist_prune_orphans_ask_help')}</p>
            {/if}
          </div>
        {/if}
      </div>

      <!-- Formatting & Sanitization -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 12px;">{$t('folder.formatting_sanitization')}</p>

        <div style="display:flex; flex-direction:column; gap:12px;">
          <div style="display:flex; gap:12px; align-items:flex-start;">
            <div data-setting="multi_disc_handling" style="flex:1;">
              <label for="multiDiscHandlingSelect" style="font-size:12px; opacity:0.7; display:block; margin-bottom:4px;">{$t('folder.multi_disc_handling')}</label>
              <select id="multiDiscHandlingSelect" bind:value={config.multi_disc_handling} style="width:100%;">
                <option value="">{$t('folder.prefix_2_05_dash_format')}</option>
                <option value="dash">{$t('folder.2_05_disc_dash_track')}</option>
                <option value="track_only">{$t('folder.05_track_only_no_disc')}</option>
                <option value="offset">{$t('folder.101_201_disc_as_leading_digit')}</option>
                <option value="folder">{$t('folder.cd1_cd2_subfolders')}</option>
              </select>
              {#if config.multi_disc_handling === 'folder'}
                <p style="font-size:11px; opacity:0.6; margin:4px 0 0;">{$t('folder.multi_disc_folder_help')}</p>
              {/if}
            </div>
            <div data-setting="track_number_padding" style="flex-shrink:0;">
              <label for="padInput2" style="font-size:12px; opacity:0.7; display:block; margin-bottom:4px;">{$t('folder.padding_digits')}</label>
              <input id="padInput2" type="number" min="1" max="4" bind:value={config.track_number_padding} placeholder="2" style="width:64px;" />
            </div>
          </div>

          <div style="display:flex; gap:12px; align-items:flex-start;">
            <div data-setting="illegal_character_replacement" style="flex:1;">
              <label for="illegalCharReplacementInput" style="font-size:12px; opacity:0.7; display:block; margin-bottom:4px;">{$t('folder.illegal_character_replacement')}</label>
              <input id="illegalCharReplacementInput" type="text" bind:value={config.illegal_character_replacement} placeholder="_" maxlength="4" style="width:100%; box-sizing:border-box; font-family:monospace;" />
            </div>
            <div data-setting="whitespace_handling" style="flex:1;">
              <label for="whitespaceHandlingSelect" style="font-size:12px; opacity:0.7; display:block; margin-bottom:4px;">{$t('folder.whitespace_handling')}</label>
              <select id="whitespaceHandlingSelect" bind:value={config.whitespace_handling} style="width:100%;">
                <option value="keep">{$t('folder.preserve_spaces')}</option>
                <option value="underscore">{$t('folder.replace_with_2')}</option>
                <option value="dash">{$t('folder.replace_with')}</option>
              </select>
            </div>
          </div>
        </div>
      </div>

      <!-- Text Transforms (v2.0.0 DMX-2) -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 6px;">{$t('folder.text_transforms')}</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 12px;">
          {$t('folder.text_transforms_help')}
        </p>

        <div style="display:flex; flex-direction:column; gap:12px;">
          <div style="display:flex; gap:12px; align-items:flex-start;">
            <div data-setting="title_casing" style="flex:1;">
              <label for="titleCasingSelect" style="font-size:12px; opacity:0.7; display:block; margin-bottom:4px;">{$t('folder.title_casing')}</label>
              <select id="titleCasingSelect" bind:value={config.title_casing} style="width:100%; appearance:none; -webkit-appearance:none;">
                {#each CASING_OPTIONS as c}<option value={c.id}>{$t(c.label)}</option>{/each}
              </select>
            </div>
            <div data-setting="artist_casing" style="flex:1;">
              <label for="artistCasingSelect" style="font-size:12px; opacity:0.7; display:block; margin-bottom:4px;">{$t('folder.artist_casing')}</label>
              <select id="artistCasingSelect" bind:value={config.artist_casing} style="width:100%; appearance:none; -webkit-appearance:none;">
                {#each CASING_OPTIONS as c}<option value={c.id}>{$t(c.label)}</option>{/each}
              </select>
            </div>
          </div>

          <div data-setting="featured_artists_mode">
            <label for="featModeSelect" style="font-size:12px; opacity:0.7; display:block; margin-bottom:4px;">{$t('folder.featured_artists')}</label>
            <select id="featModeSelect" bind:value={config.featured_artists_mode} style="width:100%; appearance:none; -webkit-appearance:none;">
              {#each FEATURED_OPTIONS as f}<option value={f.id}>{$t(f.label)}</option>{/each}
            </select>
            <p style="font-size:11px; color:#555; margin:4px 0 0;">
              {$t('folder.move_takes_every_guest_out_of_the_artist')}
            </p>
          </div>

          <div data-setting="multi_artist_separator">
            <label for="artistSepSelect" style="font-size:12px; opacity:0.7; display:block; margin-bottom:4px;">{$t('folder.multiple_artists_joined_by')}</label>
            <select id="artistSepSelect" bind:value={config.multi_artist_separator} style="width:100%; appearance:none; -webkit-appearance:none;">
              {#each SEPARATOR_OPTIONS as s}<option value={s.id}>{$t(s.label)}</option>{/each}
            </select>
            <p style="font-size:11px; color:#555; margin:4px 0 0;">
              {$t('folder.default_writes_flac_and_m4a_a_real_multi')}
            </p>
          </div>

          <label data-setting="remove_duplicate_artists" style="display:flex; align-items:flex-start; gap:10px; cursor:pointer;">
            <input type="checkbox" bind:checked={config.remove_duplicate_artists} style="margin-top:2px;" />
            <div>
              <span style="font-weight:500; font-size:13px;">{$t('folder.remove_repeated_artists')}</span>
              <p style="font-size:11px; color:#555; margin:3px 0 0;">{$t('folder.services_routinely_list_the_main_artist')}</p>
            </div>
          </label>

          <label data-setting="remove_album_version" style="display:flex; align-items:flex-start; gap:10px; cursor:pointer;">
            <input type="checkbox" bind:checked={config.remove_album_version} style="margin-top:2px;" />
            <div>
              <span style="font-weight:500; font-size:13px;">{$t('folder.strip_album_version')}</span>
              <p style="font-size:11px; color:#555; margin:3px 0 0;">{$t('folder.only_that_phrase_live_remix_instrumental')}</p>
            </div>
          </label>

          <label data-setting="album_various_artists" style="display:flex; align-items:flex-start; gap:10px; cursor:pointer;">
            <input type="checkbox" bind:checked={config.album_various_artists} style="margin-top:2px;" />
            <div>
              <span style="font-weight:500; font-size:13px;">{$t('folder.keep_various_artists_as_the_album_artist')}</span>
              <p style="font-size:11px; color:#555; margin:3px 0 0;">{$t('folder.on_by_default_turn_it_off_to_file_each')}</p>
            </div>
          </label>
        </div>
      </div>

      <!-- Variables / token chips -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px;">
        <p style="font-size: 13px; font-weight: 600; margin: 0 0 6px;">{$t('folder.variables')}</p>
        <p style="font-size: 11px; color: #555; margin: 0 0 10px;">{$t('folder.click_a_token_to_insert_it_at_the_cursor')}</p>
        {#each TEMPLATE_TOKEN_GROUPS as group}
          <p style="font-size:10px; text-transform:uppercase; letter-spacing:0.06em; color:var(--text-secondary); margin:10px 0 5px;">{$t(group.label)}</p>
          <div style="display:flex; flex-wrap:wrap; gap:6px;">
            {#each group.tokens as name}
              <button
                on:click={() => insertToken(`{${name}}`)}
                title={tokenHint(name)}
                style="font-family:monospace; font-size:11px; padding:3px 9px; border-radius:6px; background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.12); cursor:pointer; color:var(--accent-color);"
              >{`{${name}}`}</button>
            {/each}
          </div>
        {/each}
        <p style="font-size:11px; color:var(--text-secondary); margin:10px 0 0;">{$t('folder.token_help')}</p>
      </div>

      <!-- Restore defaults -->
      <div class="field" style="border-top: 1px solid rgba(255,255,255,0.05); padding-top: 12px;">
        <button
          on:click={restoreFolderDefaults}
          disabled={folderSettingsSaving}
          style="width:100%; font-size:12px; padding:8px; border-color:rgba(255,255,255,0.15); color:#888; background:rgba(255,255,255,0.02);"
        >{$t('folder.restore_folder_defaults')}</button>
      </div>

    </div>
    <div style="flex-shrink: 0; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.05); margin-top: 8px;">
      <button style="width: 100%;" on:click={saveFolderSettings} disabled={folderSettingsSaving}>{folderSettingsSaving ? 'Saving…' : $t('folder.save_preferences')}</button>
    </div>
  </div>
</div>
{/if}

<!-- ── Artist Search Results Modal ───────────────────────────────────────── -->
{#if showArtistSearch}
<div class="modal-overlay" on:click={() => { artistSearchReqId++; showArtistSearch = false; }} on:keydown={(e) => e.key === 'Escape' && (showArtistSearch = false)} role="dialog" aria-modal="true" tabindex="-1">
  <div class="modal-content" on:click|stopPropagation on:keydown|stopPropagation style="max-width: 560px; width: 100%; max-height: 75vh; display: flex; flex-direction: column;">

    <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid rgba(0,255,204,0.2); padding-bottom:14px; margin-bottom:14px; flex-shrink:0;">
      <h3 style="margin:0; color:var(--accent-color); font-size:15px;">{$t('artist.results_for', { query: searchQuery })}</h3>
      <button on:click={() => { artistSearchReqId++; showArtistSearch = false; }} style="padding:4px 8px; font-size:12px;">✕</button>
    </div>

    {#if artistSearchLoading}
      <p style="text-align:center; color:#777; padding:32px 0;">{$t('artist.searching')}</p>
    {:else if artistSearchResults.length === 0}
      <p style="text-align:center; color:#777; padding:32px 0;">{$t('artist.no_artists_found')}</p>
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
                {$t('artist.percent_match', { percent: Math.round(artist.match_score * 100) })}
              </span>
              {#if artist.followers != null}
                <span style="font-size:10px; color:#555;">{$t('artist.n_followers', { count: artist.followers.toLocaleString() })}</span>
              {/if}
            </div>
          </div>
        {/each}
      </div>
    {/if}

  </div>
</div>
{/if}

<!-- ── Connection diagnostic (issue #72) ───────────────────────────────────── -->
{#if showDiagnostic}
<div class="modal-overlay" on:click={() => showDiagnostic = false} on:keydown={(e) => e.key === 'Escape' && (showDiagnostic = false)} role="dialog" aria-modal="true" tabindex="-1">
  <div class="modal-content" on:click|stopPropagation on:keydown|stopPropagation style="max-width: 640px; width: 100%; max-height: 80vh; display: flex; flex-direction: column;">

    <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid rgba(0,255,204,0.2); padding-bottom:14px; margin-bottom:14px; flex-shrink:0;">
      <h3 style="margin:0; color:var(--accent-color); font-size:15px;">{$t('diagnostic.title')}</h3>
      <button on:click={() => showDiagnostic = false} style="padding:4px 8px; font-size:12px;">✕</button>
    </div>

    {#if diagnosticRunning}
      <p style="text-align:center; color:#777; padding:32px 0;">{$t('diagnostic.running')}</p>
    {:else if !diagnosticReport}
      <p style="text-align:center; color:#777; padding:32px 0;">{$t('diagnostic.nothing_yet')}</p>
    {:else}
      <div style="overflow-y:auto; flex:1; padding-right:4px;">
        <p class="diag-summary" class:diag-summary-ok={diagnosticReport.verdict === 'ok'}>{diagnosticReport.summary}</p>
        {#if diagnosticReport.advice}
          <p class="diag-advice">{diagnosticReport.advice}</p>
        {/if}

        {#each [['diagnostic.antra_hosts', diagnosticReport.antra], ['diagnostic.controls', diagnosticReport.controls]] as [heading, rows]}
          {#if rows && rows.length}
            <p class="diag-heading">{$t(heading)}</p>
            <ul class="diag-list">
              {#each rows as p (p.host)}
                <li class="diag-row">
                  <span class="diag-dot" class:diag-dot-ok={p.stage === 'ok'}>{p.stage === 'ok' ? '●' : '●'}</span>
                  <span class="diag-host">{p.host}</span>
                  <span class="diag-stage">
                    {#if p.stage === 'ok'}{$t('diagnostic.stage_ok', { status: p.http_status })}
                    {:else if p.not_tls}{$t('diagnostic.stage_not_tls')}
                    {:else if p.stage === 'dns'}{$t('diagnostic.stage_dns')}
                    {:else if p.stage === 'tcp'}{$t('diagnostic.stage_tcp')}
                    {:else}{$t('diagnostic.stage_tls')}{/if}
                  </span>
                </li>
              {/each}
            </ul>
          {/if}
        {/each}
      </div>

      <div style="display:flex; gap:8px; justify-content:flex-end; margin-top:14px; flex-shrink:0;">
        <button on:click={runDiagnostic}>{$t('diagnostic.run_again')}</button>
        <button class="diag-copy" on:click={copyDiagnostic} disabled={!diagnosticReport.text}>
          {diagnosticCopied ? $t('diagnostic.copied') : $t('diagnostic.copy')}
        </button>
      </div>
    {/if}

  </div>
</div>
{/if}

<!-- ── Song / Album Search Results (issue #58) ─────────────────────────────── -->
{#if showCatalogResults}
<div class="modal-overlay" on:click={() => { artistSearchReqId++; showCatalogResults = false; }} on:keydown={(e) => e.key === 'Escape' && (showCatalogResults = false)} role="dialog" aria-modal="true" tabindex="-1">
  <div class="modal-content" on:click|stopPropagation on:keydown|stopPropagation style="max-width: 620px; width: 100%; max-height: 75vh; display: flex; flex-direction: column;">

    <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid rgba(0,255,204,0.2); padding-bottom:14px; margin-bottom:14px; flex-shrink:0;">
      <h3 style="margin:0; color:var(--accent-color); font-size:15px;">{$t('search.results_for', { query: searchQuery })}</h3>
      <button on:click={() => { artistSearchReqId++; showCatalogResults = false; }} style="padding:4px 8px; font-size:12px;">✕</button>
    </div>

    {#if catalogLoading}
      <p style="text-align:center; color:#777; padding:32px 0;">{$t('search.searching')}</p>
    {:else if catalogResults.length === 0}
      <p style="text-align:center; color:#777; padding:32px 0;">{$t('search.nothing_found')}</p>
    {:else}
      <div style="overflow-y:auto; flex:1; display:flex; flex-direction:column; gap:4px; padding-right:4px;">
        {#each catalogResults as row (row.kind + ':' + row.id)}
          <div
            on:click={() => openSearchResult(row)}
            on:keydown={(e) => e.key === 'Enter' && openSearchResult(row)}
            role="button"
            tabindex="0"
            style="display:flex; align-items:center; gap:12px; padding:8px 10px; border-radius:8px; cursor:pointer; transition:background 0.12s;"
            on:mouseenter={(e) => e.currentTarget.style.background='rgba(255,255,255,0.05)'}
            on:mouseleave={(e) => e.currentTarget.style.background='transparent'}
          >
            {#if row.artwork_url}
              <img src={row.artwork_url} alt="" style="width:44px; height:44px; border-radius:4px; object-fit:cover; flex-shrink:0;"/>
            {:else}
              <div style="width:44px; height:44px; border-radius:4px; background:rgba(255,255,255,0.07); flex-shrink:0; display:flex; align-items:center; justify-content:center; font-size:18px;">{row.kind === 'album' ? '💿' : '🎵'}</div>
            {/if}
            <div style="flex:1; min-width:0;">
              <div style="font-size:14px; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                {row.title}
                {#if row.is_explicit}<span style="font-size:9px; padding:1px 4px; border-radius:3px; background:rgba(255,255,255,0.12); color:#aaa; margin-left:6px; vertical-align:middle;">E</span>{/if}
              </div>
              <div style="font-size:11px; color:#777; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                {row.artists}{#if row.kind === 'song' && row.album} · {row.album}{/if}{#if row.kind === 'album' && row.track_count} · {$t('common.n_tracks', { count: row.track_count })}{/if}{#if row.year} · {row.year}{/if}
              </div>
            </div>
            <div style="flex-shrink:0; display:flex; flex-direction:column; align-items:flex-end; gap:4px;">
              <span style="font-size:10px; padding:2px 7px; border-radius:99px; background:rgba(0,255,204,0.12); color:var(--accent-color);">
                {$t('artist.percent_match', { percent: Math.round((row.match_score || 0) * 100) })}
              </span>
              {#if row.kind === 'song' && row.duration_ms}
                <span style="font-size:10px; color:#555;">{formatTrackLength(row.duration_ms)}</span>
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
          <p style="margin:0; font-size:11px; color:#555;">{$t('discography.select_releases_to_download')}</p>
        </div>
      </div>
      <button on:click={() => { discographyReqId++; showDiscography = false; }} style="padding:4px 8px; font-size:12px;">✕</button>
    </div>

    {#if discographyLoading}
      <p style="text-align:center; color:#777; padding:32px 0;">{$t('discography.fetching_discography')}</p>
    {:else if discographyArtist}
      <!-- Select all / deselect all -->
      <div style="display:flex; gap:8px; margin-bottom:12px; flex-shrink:0;">
        <button on:click={() => { discographySelected = new Set(discographyArtist.albums.map(a => a.url)); }} style="font-size:12px; padding:4px 10px;">{$t('discography.select_all')}</button>
        <button on:click={() => { discographySelected = new Set(); }} style="font-size:12px; padding:4px 10px;">{$t('discography.deselect_all')}</button>
        <span style="margin-left:auto; font-size:12px; color:#777; align-self:center;">{$t('discography.n_selected', { count: discographySelected.size })}</span>
      </div>

      <!-- Album list grouped by type -->
      <div style="overflow-y:auto; flex:1; display:flex; flex-direction:column; gap:4px; padding-right:4px;">
        {#each [['album','Albums'], ['single','Singles'], ['compilation',$t('discography.eps_and_compilations')]] as [type, label]}
          {#if discographyArtist.albums.filter(a => a.type === type).length > 0}
            <div style="display:flex; align-items:center; justify-content:space-between; margin-top:12px; margin-bottom:4px;">
              <span style="font-size:11px; color:#555; letter-spacing:0.08em;">{label.toUpperCase()}</span>
              <div style="display:flex; gap:4px;">
                <button
                  on:click={() => { discographyArtist.albums.filter(a => a.type === type).forEach(a => discographySelected.add(a.url)); discographySelected = discographySelected; }}
                  style="font-size:10px; padding:2px 7px; opacity:0.55; border-color:rgba(255,255,255,0.1);">{$t('discography.all')}</button>
                <button
                  on:click={() => { discographyArtist.albums.filter(a => a.type === type).forEach(a => discographySelected.delete(a.url)); discographySelected = discographySelected; }}
                  style="font-size:10px; padding:2px 7px; opacity:0.55; border-color:rgba(255,255,255,0.1);">{$t('discography.none')}</button>
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
                  <div style="font-size:11px; color:#666;">{album.year ?? '—'} · {$t('common.n_tracks', { count: album.track_count })}</div>
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
          // SF-1 — queued like any other job; the view is reset on
          // queue_job_started (the shared resetDownloadView, as PORT-4 required).
          const artistName = discographyArtist?.artist_name || '';
          const n = albumUrls.length;
          await enqueueUrls(albumUrls, artistName
              ? $t('discography.queue_title_artist', { artist: artistName, count: n })
              : $t('discography.queue_title', { count: n }));
        }}
        style="margin-top:16px; width:100%; flex-shrink:0;">
        {$t('discography.download_n_releases', { count: discographySelected.size })}
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
        <span style="color:var(--accent-color);font-weight:600;font-size:14px;">{$t('analyzer.audio_quality_analyzer')}</span>
        {#if analyzerTracks.length > 0}
          <span style="font-size:11px;color:#555;">{$t('analyzer.n_analyzed', { done: analyzerDoneCount, total: analyzerTracks.length })}</span>
        {/if}
      </div>
      <div style="display:flex;gap:6px;align-items:center;">
        {#if analyzerTracks.length > 1}
          <button class="az-btn-sm" on:click={() => analyzerViewMode = analyzerViewMode === 'gallery' ? 'single' : 'gallery'}>
            {analyzerViewMode === 'gallery' ? $t('analyzer.view_single') : $t('analyzer.view_gallery')}
          </button>
        {/if}
        {#if analyzerTracks.length > 0}
          <button class="az-btn-sm" on:click={analyzerPickFiles}>{$t('analyzer.add_files')}</button>
          <button class="az-btn-sm" on:click={analyzerExportCurrent}>{$t('analyzer.export_png')}</button>
        {/if}
        {#if analyzerShowExportAll}
          <button class="az-btn-sm az-btn-accent" on:click={analyzerExportAll}>
            {analyzerExportStatus || $t('analyzer.export_all')}
          </button>
        {/if}
        {#if analyzerTracks.length > 0}
          <button class="az-btn-sm" on:click={analyzerReset} title="{$t('analyzer.remove_all_files')}" style="color:#f87171;border-color:rgba(248,113,113,0.25);">{$t('analyzer.clear_all')}</button>
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
            <button class="az-remove-btn" on:click|stopPropagation={() => analyzerRemoveTrack(i)} title="{$t('analyzer.remove')}">✕</button>
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
          <p>{$t('analyzer.drop_audio_files_or_a_folder_here')}</p>
          <p class="az-drop-sub">{$t('analyzer.supports_flac_mp3_m4a_aac_alac_wav_aiff')}</p>
          <button class="az-btn-sm az-btn-accent" style="margin-top:12px;" on:click|stopPropagation={analyzerPickFiles}>{$t('analyzer.browse_files')}</button>
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
              <img src={track.spectrogram} class="az-spectrogram" alt="{$t('analyzer.spectrogram')}" />
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
                  <div class="az-stats-title">{$t('analyzer.audio_stats')}</div>
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
                  <div class="az-stats-title">{$t('analyzer.spectral_analysis')}</div>
                  <div class="az-stats-grid">
                    <span class="az-stat-label">{$t('analyzer.verdict')}</span>
                    <span class="az-stat-value" style="color:{track.spectral.verdict === 'lossless' ? '#86efac' : track.spectral.verdict === 'lossy' ? '#f87171' : '#facc15'};">
                      {track.spectral.verdict === 'lossless' ? $t('analyzer.consistent_with_lossless')
                        : track.spectral.verdict === 'lossy' ? (track.spectral.likelySource ? $t('analyzer.lossy_from', { source: track.spectral.likelySource }) : $t('analyzer.lossy'))
                        : 'Inconclusive'}
                    </span>
                    <span class="az-stat-label">{$t('analyzer.confidence')}</span>
                    <span class="az-stat-value">{Math.round((track.spectral.cutoffConfidence || 0) * 100)}%</span>
                    <!-- "Content to" rather than "Cutoff": this is the highest
                         frequency still carrying energy — the point where a Spek
                         plot goes black — not a filter corner. -->
                    <span class="az-stat-label">{$t('analyzer.content_to')}</span>
                    <span class="az-stat-value">{(track.spectral.cutoffHz / 1000).toFixed(2)} kHz</span>
                    <span class="az-stat-label">{$t('analyzer.nyquist')}</span>
                    <span class="az-stat-value">{(track.spectral.nyquistHz / 1000).toFixed(1)} kHz</span>
                    <span class="az-stat-label">{$t('analyzer.steepest_roll_off')}</span>
                    <span class="az-stat-value">
                      {track.spectral.cliffHz ? $t('analyzer.rolloff_at', { db: (track.spectral.shelfDropDb || 0).toFixed(1), khz: (track.spectral.cliffHz / 1000).toFixed(2) }) : $t('analyzer.rolloff', { db: (track.spectral.shelfDropDb || 0).toFixed(1) })}
                    </span>
                    <!-- The decisive number: a codec zeroes the bins above its
                         wall, so they collapse to the numerical floor. A steep
                         but natural mastering roll-off leaves noise behind. -->
                    <span class="az-stat-label">{$t('analyzer.level_above')}</span>
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
                        <span class="az-spec-hint">{$t('analyzer.drag_to_pan_wheel_to_zoom_hover_to_read')}</span>
                        <span class="az-spec-readout">{specReadout}</span>
                        <select bind:value={specPalette} on:change={() => drawSpectrogram(track)}>
                          <option value="magma">{$t('analyzer.magma')}</option>
                          <option value="viridis">{$t('analyzer.viridis')}</option>
                          <option value="grey">{$t('analyzer.greyscale')}</option>
                        </select>
                        <button on:click={() => { specZoom = 1; specPan = 0; drawSpectrogram(track); }}>{$t('analyzer.reset')}</button>
                        <button on:click={() => exportSpectrogramPng(track)}>{$t('common.png')}</button>
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
                        <span>{$t('analyzer.frequency_axis', { max: (track.spectral.nyquistHz / 1000).toFixed(1) })}</span>
                        <span>{track.spectral.timeEndSec?.toFixed(0)}s</span>
                      </div>
                    </div>
                  {/if}
                </div>
              {:else if track.spectralError}
                <div class="az-stats-panel">
                  <div class="az-stats-title">{$t('analyzer.spectral_analysis')}</div>
                  <p style="font-size: 11px; color: #888; margin: 6px 0 0;">
                    {$t('analyzer.fft_failed', { error: track.spectralError })}
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
                  <img src={track.spectrogram} class="az-spectrogram az-spectrogram-gallery" alt={$t('analyzer.spectrogram_alt', { name: track.fileName })} />
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

<!-- ── Command palette (v2.0.0 PORT-1) ─────────────────────────────────────── -->
{#if paletteOpen}
<div class="pal-overlay" on:mousedown|self={closePalette} role="dialog" aria-modal="true" aria-label="{$t('palette.command_palette')}">
  <div class="pal-card">
    <div class="pal-top">
      <span class="pal-icon">⌘</span>
      <input
        class="pal-input"
        type="text"
        bind:this={paletteInputEl}
        bind:value={paletteQuery}
        on:input={refreshPalette}
        placeholder="{$t('palette.search_commands_and_settings_2')}"
        aria-label="{$t('palette.search_commands_and_settings')}"
        autocomplete="off"
        spellcheck="false"
      />
      <kbd class="pal-esc">esc</kbd>
    </div>

    <div class="pal-list" bind:this={paletteListEl} role="listbox" aria-label="{$t('palette.results')}">
      {#each paletteResults as r, i (r.cmd.id)}
        {#if i === 0 || paletteResults[i - 1].cmd.group !== r.cmd.group}
          <div class="pal-group">{r.cmd.group || ''}</div>
        {/if}
        <!-- Keyboard interaction for the listbox is owned by the palette's own
             document-level handler (arrows / Enter / Escape), which is the ARIA
             listbox pattern; the click here is the mouse affordance only. -->
        <!-- svelte-ignore a11y-click-events-have-key-events -->
        <div
          class="pal-row{i === paletteCursor ? ' pal-active' : ''}"
          role="option"
          tabindex="-1"
          aria-selected={i === paletteCursor}
          on:mousemove={() => { if (paletteCursor !== i) paletteCursor = i; }}
          on:click={() => runPaletteRow(i)}
        >
          <span class="pal-label">{r.cmd.label}</span>
          {#if r.cmd.hint}<span class="pal-hint">{r.cmd.hint}</span>{/if}
        </div>
      {/each}
      {#if paletteResults.length === 0}
        <div class="pal-empty">{$t('palette.no_matches')}</div>
      {/if}
    </div>
  </div>
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
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.05);
    padding: 32px;
    border-radius: 8px;
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.1);
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
    box-shadow: 0 4px 16px rgba(0, 0, 0, calc(0.6 * var(--shd-k)));
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
  .playlist-export {
    position: relative;
    margin-left: auto;
    align-self: flex-start;
  }
  .export-btn {
    background: transparent;
    border: 1px solid var(--line-soft, rgba(255,255,255,0.14));
    color: var(--text-secondary, #ccc);
    border-radius: 6px;
    padding: 5px 12px;
    font-size: 11px;
    font-family: var(--font-mono);
    letter-spacing: 0.04em;
    cursor: pointer;
    transition: border-color 0.15s, color 0.15s;
  }
  .export-btn:hover:not(:disabled) {
    border-color: var(--accent-color, #37e2c2);
    color: var(--accent-color, #37e2c2);
  }
  .export-btn:disabled { opacity: 0.5; cursor: default; }
  .export-menu {
    position: absolute;
    right: 0;
    top: calc(100% + 6px);
    z-index: 40;
    min-width: 240px;
    background: var(--bg-elevated, #16151c);
    border: 1px solid var(--line-soft, rgba(255,255,255,0.12));
    border-radius: 8px;
    padding: 4px;
    box-shadow: 0 10px 28px rgba(0,0,0,0.45);
  }
  .export-menu-item {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    width: 100%;
    background: transparent;
    border: 0;
    border-radius: 6px;
    padding: 7px 10px;
    text-align: left;
    cursor: pointer;
  }
  .export-menu-item:hover { background: rgba(255,255,255,0.06); }
  .export-menu-label {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-primary, #fff);
  }
  .export-menu-hint {
    font-size: 10.5px;
    color: var(--text-faint, #666);
  }
  .export-status {
    margin: 8px 0 0;
    font-size: 11px;
    font-family: var(--font-mono);
    color: var(--text-muted, #888);
    word-break: break-all;
  }
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

  /* ── PORT-4 skeleton loaders ────────────────────────────────────────────────
     Shown only between Add to Library and the first playlist_loaded.
     1. Geometry is INHERITED: the skeleton reuses .playlist-header,
        .playlist-cover, .playlist-meta and its line classes, .track-row and its
        children, and the real .prev-btn. Only the bars are new.
     2. Each text bar is exactly one line box tall -- 1lh, NOT 1em. On the web
        build 1em left every row 7px short (line-height: normal is font-metric
        dependent), i.e. 56px of jump on 8 rows. The px value is the no-lh
        fallback and is approximate by nature.
     3. Colour is the TEXT tint, because the bars stand in for text; that token
        flips with the mode, so light and dark both get a visible bar. */
  .skel-block {
    display: block;
    border-radius: 4px;
    background-color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.08);
    background-image: linear-gradient(90deg,
      transparent 0%,
      oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.10) 50%,
      transparent 100%);
    background-size: 55% 100%;
    background-repeat: no-repeat;
    animation: skel-shimmer 1.5s linear infinite;
  }
  .skel-line { height: 14px; height: 1lh; }
  .skel-header .playlist-cover.skel-block { box-shadow: none; border-radius: 6px; }
  /* The badge keeps its real padding and its real text (LOSSLESS), painted
     transparent, so its box is the real box by construction. */
  .skel-header .playlist-quality-badge.skel-block {
    display: inline-block;
    color: transparent;
    background-color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.08);
    user-select: none;
  }
  /* The real row reserves the preview button's box while it is invisible;
     the skeleton reserves the same box and never reveals it on hover. */
  .skel-row .prev-btn.skel-prev { visibility: hidden; pointer-events: none; }
  .skel-row { pointer-events: none; }
  .skel-row:nth-child(2) .skel-block { animation-delay: 0.06s; }
  .skel-row:nth-child(3) .skel-block { animation-delay: 0.12s; }
  .skel-row:nth-child(4) .skel-block { animation-delay: 0.18s; }
  .skel-row:nth-child(5) .skel-block { animation-delay: 0.24s; }
  .skel-row:nth-child(6) .skel-block { animation-delay: 0.30s; }
  .skel-row:nth-child(7) .skel-block { animation-delay: 0.36s; }
  .skel-row:nth-child(8) .skel-block { animation-delay: 0.42s; }
  @keyframes skel-shimmer {
    0%   { background-position: -60% 0; }
    100% { background-position: 160% 0; }
  }
  @media (prefers-reduced-motion: reduce) {
    .skel-block { animation: none; background-image: none; }
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
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.06);
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
  /* v2.0.0 SF-1 — download queue */
  .queue-panel {
    margin: 10px 0 0;
    border: 1px solid var(--accent-border, rgba(255,255,255,0.12));
    border-radius: 8px;
    background: var(--surface-color, rgba(255,255,255,0.04));
    overflow: hidden;
  }
  .queue-panel-attn {
    border-color: var(--accent-color);
    background: var(--surface-accent, rgba(255,255,255,0.06));
  }
  .queue-head {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 10px;
  }
  .queue-heading {
    flex: 1;
    min-width: 0;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-primary, #fff);
  }
  .queue-actions { display: flex; gap: 6px; flex-shrink: 0; }
  .queue-btn {
    padding: 4px 10px;
    font-size: 11px;
    line-height: 1.2;
    border-radius: 4px;
    white-space: nowrap;
  }
  .queue-btn-primary {
    background: var(--accent-color);
    border-color: var(--accent-color);
    color: var(--bg-color, #000);
    font-weight: 600;
  }
  .queue-list {
    list-style: none;
    margin: 0;
    padding: 0;
    max-height: 168px;
    overflow-y: auto;
    border-top: 1px solid var(--accent-border, rgba(255,255,255,0.08));
  }
  .queue-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 10px;
    font-size: 12px;
  }
  .queue-row + .queue-row { border-top: 1px solid var(--accent-border, rgba(255,255,255,0.06)); }
  .queue-kind { width: 12px; text-align: center; color: var(--text-secondary, #aaa); flex-shrink: 0; }
  .queue-row-title {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-primary, #fff);
  }
  .queue-meta { font-size: 11px; color: var(--text-secondary, #aaa); flex-shrink: 0; }
  .queue-badge {
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 999px;
    border: 1px solid var(--warning-color, #facc15);
    color: var(--warning-color, #facc15);
    flex-shrink: 0;
  }
  .queue-remove {
    padding: 0 6px;
    min-width: 0;
    height: 20px;
    line-height: 1;
    font-size: 14px;
    background: transparent;
    border-color: transparent;
    color: var(--text-secondary, #aaa);
    flex-shrink: 0;
  }
  .queue-remove:hover { color: var(--error-color, #f87171); }

  .failed-panel {
    /* bottom margin keeps the last entry's buttons clear of the floating Log button */
    margin: 0 0 64px 0;
    border: 1px solid rgba(248, 113, 113, 0.25);
    border-radius: 8px;
    background: rgba(248, 113, 113, 0.04);
    overflow: hidden;
  }
  /* issue #75 — leftover playlist files are housekeeping, not a failure, so the
     card borrows the failed panel's shape but not its red. Amber, and stacked
     cards get their margin back (the failed panel's 64px is there to clear the
     floating Log button, which only the LAST card needs). */
  .failed-panel.orphan-panel {
    margin-bottom: 12px;
    border-color: rgba(251, 191, 36, 0.3);
    background: rgba(251, 191, 36, 0.05);
  }
  .failed-panel.orphan-panel:last-of-type { margin-bottom: 64px; }
  .failed-panel.orphan-panel .failed-panel-head {
    background: rgba(251, 191, 36, 0.1);
    border-bottom-color: rgba(251, 191, 36, 0.2);
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
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.04);
    border-color: oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.12);
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.5);
  }
  .failed-action-btn.failed-dismiss-all:hover:not(:disabled) {
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.08);
    border-color: oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.22);
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.7);
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
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.85);
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
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.04);
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.1);
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.4);
  }
  .failed-close-btn:hover {
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.1);
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.7);
  }

  .progress-bar-bg {
    width: 100%;
    height: 3px;
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.08);
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
    /* PORT-5 — the fallback is the pre-PORT-5 width, so a render before the
       inline variable lands is byte-identical to the old panel. */
    width: var(--log-panel-width, 380px);
    background: rgba(8, 8, 8, 0.97);
    border-left: 1px solid rgba(0, 255, 204, 0.18);
    display: flex;
    flex-direction: column;
    z-index: 60;
    backdrop-filter: blur(14px);
    box-shadow: -8px 0 32px rgba(0, 0, 0, calc(0.6 * var(--shd-k)));
  }

  .log-panel-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 16px;
    border-bottom: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.06);
    font-size: 13px;
    font-weight: 500;
    flex-shrink: 0;
    color: #aaa;
    letter-spacing: 0.03em;
  }

  /* PORT-5 — drag handle. Straddles the panel's left border so the grab area
     is wider than the 1px line it sits on. */
  .log-resize-handle {
    position: absolute;
    left: -3px;
    top: 0;
    bottom: 0;
    width: 9px;
    cursor: ew-resize;
    z-index: 2;
    /* Without this a touch drag scrolls the page instead of resizing. */
    touch-action: none;
    background: transparent;
    transition: background 0.15s;
  }
  /* Hover and drag: the accent at low alpha. MEASURED over all 11 themes x 2
     modes against this panel's near-black ground, this is 1.51-2.56:1 and does
     NOT improve with alpha -- the ground is too dark for the accent to reach
     SC 1.4.11's 3:1. That is acceptable here and is stated rather than dressed
     up: a splitter is identified by its ew-resize cursor, not by colour, so the
     tint is reinforcement. The FOCUS indicator is held to a real bar below. */
  .log-resize-handle:hover,
  .log-panel[data-resizing] .log-resize-handle {
    background: oklch(var(--a1-l) var(--a1-c) var(--a1-h) / 0.35);
  }
  /* Keyboard focus must be unmistakable, so it is near-white rather than the
     accent. The outline is NOT removed -- an earlier cut set `outline: none`
     and left a ~2.3:1 tint as the only focus cue.
     Deliberately a fixed white and NOT --text-tint-l: that token FLIPS with the
     mode, and .log-panel is hardcoded near-black in BOTH modes, so the themed
     token would paint a near-black bar on a near-black ground in light mode.
     Same trap FR-4 measured on .log-save-btn, which is why everything else on
     this panel also assumes a dark surface. */
  .log-resize-handle:focus-visible {
    background: rgba(255, 255, 255, 0.85);
    outline: 2px solid rgba(255, 255, 255, 0.95);
    outline-offset: -2px;
  }
  /* No width transition exists on .log-panel today, so this is defence rather
     than a fix: adding one later must not make the drag lag a frame behind. */
  .log-panel[data-resizing] { transition: none; }
  /* Dragging over the log must not select its text. */
  .log-panel[data-resizing] .log-panel-body { user-select: none; }

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
    box-shadow: 0 10px 25px rgba(0, 0, 0, calc(0.5 * var(--shd-k)));
  }
  /* ── v2.0.0 PORT-3 — playlist-file import ──────────────────────────────── */
  .input-actions {
    display: flex;
    flex-direction: column;
    gap: 6px;
    align-self: stretch;
  }
  .input-actions-main { flex: 1; }
  .import-file-btn {
    font-size: 11px;
    padding: 4px 8px;
    white-space: nowrap;
    background: transparent;
    color: var(--text-secondary);
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.12);
  }
  .import-file-btn:hover:not(:disabled) {
    color: var(--text-primary);
    border-color: var(--accent-border);
  }
  .import-modal {
    max-width: 560px;
    background: var(--bg-elevated);
    border-color: var(--accent-border);
    color: var(--text-primary);
    display: flex;
    flex-direction: column;
    max-height: 86vh;
  }
  .import-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 4px;
  }
  .import-head h3 { margin: 0; color: var(--accent-color); font-size: 16px; }
  .import-close { padding: 4px 8px; font-size: 12px; }
  .import-file {
    margin: 0 0 12px;
    font-size: 11px;
    color: var(--text-secondary);
    word-break: break-all;
  }
  .import-name-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: var(--text-secondary);
  }
  .import-name { margin: 4px 0 8px; width: 100%; box-sizing: border-box; }
  .import-stats { margin: 0 0 8px; font-size: 12px; color: var(--text-secondary); }
  .import-list {
    margin: 0;
    padding: 0 0 0 28px;
    overflow-y: auto;
    min-height: 60px;
    flex: 1;
    font-size: 12px;
    border-top: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.08);
    border-bottom: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.08);
  }
  .import-list li { padding: 4px 0; color: var(--text-secondary); }
  .import-row-title { color: var(--text-primary); }
  .import-row-artist { margin-left: 6px; }
  .import-row-artist::before { content: '— '; }
  .import-row-isrc {
    margin-left: 6px;
    font-size: 9px;
    letter-spacing: 0.06em;
    padding: 1px 4px;
    border-radius: 3px;
    color: var(--accent-color);
    border: 1px solid var(--accent-border);
  }
  .import-more { margin: 6px 0 0; font-size: 11px; color: var(--text-secondary); }
  /* TEXT roles, not the raw --state-* fills: THEME-2 caps these for light mode,
     where the fills measure 1.4-2.9:1 as text. */
  .import-warn { margin: 8px 0; font-size: 12px; color: var(--warning-color); }
  .import-error { margin: 8px 0; font-size: 12px; color: var(--error-color); word-break: break-word; }
  .import-skipped { margin-top: 8px; font-size: 11px; color: var(--text-secondary); }
  .import-skipped summary { cursor: pointer; }
  .import-skipped ul { margin: 6px 0 0; padding-left: 18px; max-height: 120px; overflow-y: auto; }
  .import-foot {
    margin-top: 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .import-note { margin: 0; font-size: 11px; color: var(--text-secondary); }
  .import-confirm { white-space: nowrap; }

  .history-card {
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.03);
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.05);
    border-radius: 6px;
    padding: 12px;
  }

  /* ── Settings modal ─────────────────────────────────────────────────────── */
  .settings-modal { max-width: 520px; }

  .settings-section {
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.06);
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
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.1);
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
    box-shadow: 0 0 60px rgba(0, 0, 0, calc(0.8 * var(--shd-k)));
  }

  .analyzer-titlebar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    border-bottom: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.06);
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.02);
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
    border-right: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.06);
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
  .az-sidebar-item:hover { background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.05); }
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
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.06);
    border-radius: 6px;
    padding: 12px 14px;
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.02);
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
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.04);
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
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.05);
    border-color: oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.1);
  }
  .az-nav-btn:disabled { opacity: 0.25; cursor: not-allowed; }

  /* Small buttons in toolbar */
  .az-btn-sm {
    padding: 4px 10px;
    font-size: 12px;
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.05);
    border-color: oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.1);
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
    box-shadow: 0 8px 28px rgba(0, 0, 0, calc(0.55 * var(--shd-k)));
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
      0 8px 32px rgba(0, 0, 0, calc(0.65 * var(--shd-k))),
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
      0 10px 36px rgba(0, 0, 0, calc(0.7 * var(--shd-k))),
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
    box-shadow: 0 6px 22px rgba(0, 0, 0, calc(0.55 * var(--shd-k)));
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

  /* ── Connection diagnostic (issue #72) ──────────────────────────────────── */
  .diag-link {
    margin-top: 8px;
    font-size: 11px;
    padding: 4px 10px;
    background: transparent;
    border-color: rgba(255, 255, 255, 0.18);
  }
  .diag-summary {
    margin: 0 0 10px;
    font-size: 13px;
    line-height: 1.5;
    color: var(--error-color);
  }
  .diag-summary-ok { color: var(--accent-color); }
  .diag-advice {
    margin: 0 0 16px;
    font-size: 12px;
    line-height: 1.55;
    color: #999;
  }
  .diag-heading {
    margin: 0 0 6px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #666;
  }
  .diag-list { list-style: none; margin: 0 0 16px; padding: 0; }
  .diag-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 0;
    font-size: 12px;
  }
  .diag-dot { color: var(--error-color); font-size: 9px; }
  .diag-dot-ok { color: var(--accent-color); }
  .diag-host { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .diag-stage { color: #888; font-size: 11px; flex-shrink: 0; }
  .diag-copy { background: var(--accent-color); color: #000; font-weight: 600; }

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
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.04);
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.1);
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.38);
    cursor: pointer;
    transition: all 0.12s;
  }
  .format-pill:hover { background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.08); color: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.7); }
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
    border-color: oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.12);
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
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.08); border-radius: 6px;
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
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.03);
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.12);
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.45);
    cursor: pointer;
    transition: border-color 0.15s, color 0.15s, background 0.15s;
  }
  .mirror-status-chip:hover { background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.07); }
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
  .mirror-status-chip.is-unknown  { border-color: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.12); color: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.35); }
  .mirror-status-label { line-height: 1; }

  /* Job-level advisory from the engine, shown above the tracklist. */
  .job-notice {
    display: flex; align-items: flex-start; gap: 8px;
    margin: 0 0 10px; padding: 9px 11px;
    border: 1px solid rgba(252, 211, 77, 0.35);
    border-left: 3px solid #fcd34d;
    border-radius: 6px;
    background: rgba(252, 211, 77, 0.07);
    font-size: 12px; line-height: 1.45; color: #fcd34d;
  }
  .job-notice-icon {
    flex: 0 0 auto; width: 15px; height: 15px; margin-top: 1px;
    border-radius: 50%; background: #fcd34d; color: #1a1a1a;
    font-size: 10px; font-weight: 700; line-height: 15px; text-align: center;
  }
  .job-notice-text { flex: 1 1 auto; }
  .job-notice-close {
    flex: 0 0 auto; background: none; border: none; cursor: pointer;
    color: #fcd34d; opacity: 0.6; font-size: 15px; line-height: 1; padding: 0 2px;
  }
  .job-notice-close:hover { opacity: 1; }
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


  /* ── FR-5 — preview buttons ──────────────────────────────────────────────
     Built from the theme seeds like the rest of v2.0.0, so they follow every
     theme and light mode. */

  /* ── v2.0.0 PV-1 — preview before download ───────────────────────────────── */

  /* The reason this class exists: .prev-btn is opacity:0 until .track-row:hover,
     which is right while watching a download but wrong on a screen whose whole
     purpose is listening before committing. Nothing else about the button
     changes, so only the reveal is overridden. */
  .prev-btn.prev-btn-always { opacity: 1; }

  .preview-header .preview-actions {
    display: flex;
    flex-direction: column;
    gap: 6px;
    align-self: flex-start;
    flex-shrink: 0;
  }
  .preview-go-btn {
    font-weight: 600;
    font-size: 12.5px;
    padding: 8px 18px;
    white-space: nowrap;
    background: var(--accent-color);
    color: var(--accent-contrast, #000);
    border: 1px solid var(--accent-color);
    cursor: pointer;
    border-radius: 6px;
  }
  .preview-go-btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .preview-cancel-btn {
    font-size: 11.5px;
    padding: 5px 18px;
    white-space: nowrap;
    cursor: pointer;
    border-radius: 6px;
  }
  /* The URL bar's own button in Download-now state, so the two read as the same
     action wherever the user's eye is. */
  .input-actions-main.input-actions-go {
    background: var(--accent-color);
    color: var(--accent-contrast, #000);
    border-color: var(--accent-color);
    font-weight: 600;
  }

  .preview-select-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 6px 4px 8px;
    border-bottom: 1px solid var(--border-soft, rgba(128,128,128,0.18));
    margin-bottom: 4px;
  }
  .preview-check-label {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-size: 12px;
    cursor: pointer;
  }
  .preview-select-count { font-size: 11.5px; opacity: 0.6; }
  .preview-check { accent-color: var(--accent-color); cursor: pointer; flex-shrink: 0; }
  .track-row.preview-row .track-row-head { display: flex; align-items: center; gap: 0; }
  .track-row.preview-row .preview-check { margin-right: 9px; }
  /* A deselected row stays readable and in place — it is still part of the
     tracklist being reviewed, not removed from it. */
  .track-row.preview-row-off { opacity: 0.4; }
  .preview-row-num {
    font-variant-numeric: tabular-nums;
    font-size: 11.5px;
    opacity: 0.55;
    min-width: 26px;
    margin-right: 8px;
    flex-shrink: 0;
  }

  .preview-error {
    margin: 18px auto;
    max-width: 420px;
    text-align: center;
  }
  .preview-error-msg {
    margin: 0 0 12px;
    font-size: 12.5px;
    color: var(--error-color);
    line-height: 1.55;
  }
  .preview-error-actions { display: flex; gap: 8px; justify-content: center; }
  .preview-error-actions button { font-size: 12px; }

  @media (prefers-reduced-motion: reduce) {
    .preview-go-btn, .preview-cancel-btn { transition: none; }
  }

  .prev-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    margin-right: 7px;
    padding: 0;
    flex-shrink: 0;
    border-radius: 50%;
    /* Mode-capped tokens here, unlike .disc-prev-btn: this one sits on the
       app background, which DOES go light in light mode, so the cap is right.
       The alphas are tuned rather than inherited — measured over 13 themes x 2
       modes, the stock 0.11 fill / 0.32 border left the outline at 1.26:1 in
       light mode; 0.09 / 0.70 lifts that to 2.12:1 with the glyph unchanged at
       3.20:1 (SC 1.4.11 asks 3:1 for a graphical control). */
    background: oklch(var(--a1-lm) var(--a1-cm) var(--a1-h) / 0.09);
    border: 1px solid oklch(var(--a1-lm) var(--a1-cm) var(--a1-h) / 0.7);
    color: var(--accent-color);
    cursor: pointer;
    opacity: 0;
    transition: opacity 0.12s, background 0.12s;
  }
  /* Revealed on hover, but ALWAYS visible while sounding or in error -- a
     control that vanished mid-playback would leave no way to stop it. */
  .track-row:hover .prev-btn,
  .prev-btn.prev-playing,
  .prev-btn.prev-loading,
  .prev-btn.prev-error,
  .prev-btn:focus-visible { opacity: 1; }
  .prev-btn:hover { background: var(--accent-soft); }
  .prev-btn.prev-playing { background: var(--accent-soft); }
  .prev-btn.prev-loading { opacity: 0.55; }
  .prev-btn.prev-error {
    color: var(--error-color);
    border-color: var(--error-color);
    background: transparent;
  }
  .prev-btn.prev-loading svg { animation: prevPulse 0.9s ease-in-out infinite; }

  @keyframes prevPulse { 0%, 100% { opacity: 0.35; } 50% { opacity: 1; } }

  .disc-prev-btn {
    position: absolute;
    right: 8px;
    bottom: 8px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    padding: 0;
    border-radius: 50%;
    /* Deliberately LIGHTER than the accent seed, with chroma pulled back.
       This pill sits on arbitrary album art with a fixed dark glyph, so the
       PILL is what has to guarantee contrast. At the raw seed lightness the
       two darkest accents measured 4.34:1 against the glyph — below AA. At
       L 0.85 / chroma x0.55 the same sweep gives 10.98-12.96 across all 13
       themes with zero gamut clipping, while keeping the theme hue. */
    background: oklch(0.85 calc(var(--a1-c) * 0.55) var(--a1-h));
    border: none;
    color: #0b0b0b;
    cursor: pointer;
    opacity: 0;
    transform: translateY(4px);
    transition: opacity 0.15s, transform 0.15s;
    box-shadow: 0 2px 8px rgba(0, 0, 0, calc(0.45 * var(--shd-k)));
    z-index: 2;
  }
  .discovery-card:hover .disc-prev-btn,
  .disc-prev-btn.prev-playing,
  .disc-prev-btn.prev-loading,
  .disc-prev-btn:focus-visible { opacity: 1; transform: translateY(0); }
  .disc-prev-btn.prev-loading svg { animation: prevPulse 0.9s ease-in-out infinite; }
  .disc-prev-btn.prev-error { background: var(--error-color); color: #fff; }

  @media (prefers-reduced-motion: reduce) {
    .prev-btn, .disc-prev-btn { transition: none; }
    .prev-btn.prev-loading svg, .disc-prev-btn.prev-loading svg { animation: none; }
  }

  /* FR-4 — save log. Built from the theme seeds rather than the hardcoded cyan
     its neighbours still use, so it follows all 11 themes.

     It uses the RAW seed (--a1-l / --a1-c), not --accent-color, on purpose.
     .log-panel is hardcoded `rgba(8,8,8,0.97)` and stays near-black in light
     mode — like the terminal it is — while --accent-color is capped to
     L 0.509 in light mode for light *pages*. Combining the two put dark text
     on a dark ground: measured 3.02-3.47 across all 11 themes, i.e. failing
     AA everywhere in light mode. The raw seed is the dark-surface rendering
     and is what every other colour on this panel already assumes. */
  .log-save-btn {
    --log-accent: oklch(var(--a1-l) var(--a1-c) var(--a1-h));
    display: flex;
    align-items: center;
    gap: 5px;
    height: 24px;
    padding: 0 9px;
    border-radius: 12px;
    background: oklch(var(--a1-l) var(--a1-c) var(--a1-h) / 0.11);
    border: 1px solid oklch(var(--a1-l) var(--a1-c) var(--a1-h) / 0.32);
    color: var(--log-accent);
    font-family: inherit;
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.02em;
    cursor: pointer;
    transition: background 0.15s, opacity 0.15s;
    flex-shrink: 0;
    white-space: nowrap;
  }
  .log-save-btn:hover:not(:disabled) {
    background: oklch(var(--a1-l) var(--a1-c) var(--a1-h) / 0.2);
  }
  .log-save-btn:disabled { opacity: 0.35; cursor: default; }
  .log-save-btn svg { flex-shrink: 0; }

  /* ── Album separator row in tracklist ────────────────────────────────────── */
  .tracklist-album-sep {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 4px 4px;
    margin-top: 6px;
    border-top: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.06);
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
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.08);
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.02);
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
    border-color: oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.08);
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.02);
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
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.06);
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.5);
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
    border-bottom: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.08);
    background: linear-gradient(180deg, rgba(0,255,204,0.05), oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.01));
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
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.02);
    border-color: oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.06);
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
    border-bottom: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.07);
    flex-shrink: 0;
    gap: 12px;
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.02);
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
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.15);
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
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.06);
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.1);
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.7);
    border-radius: 6px;
    padding: 5px 12px;
    font-size: 14px;
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.12s, color 0.12s;
  }
  .discover-close-btn:hover {
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.12);
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
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.4);
    font-family: var(--font-mono);
    letter-spacing: 0.05em;
  }

  .discover-empty {
    padding: 80px 20px;
    text-align: center;
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.3);
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
    background: linear-gradient(90deg, oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.1), transparent);
  }

  .discover-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 18px;
  }

  .discovery-card {
    cursor: pointer;
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.03);
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.05);
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
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.08);
    border-color: var(--accent-color);
    transform: translateY(-4px);
    box-shadow: 0 10px 20px rgba(0, 0, 0, calc(0.3 * var(--shd-k)));
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
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.5);
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
    border-bottom: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.06);
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
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.1);
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.03);
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.45);
    cursor: pointer;
    transition: all 0.15s;
  }

  .lib-service-tab:hover {
    border-color: oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.22);
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.8);
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.06);
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
    scrollbar-color: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.15) transparent;
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
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.9);
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
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.25);
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.05);
  }

  .lib-sync-pill {
    position: absolute;
    top: 8px;
    right: 8px;
    background: rgba(0, 0, 0, 0.6);
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.15);
    border-radius: 50%;
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.5);
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
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.15);
    border-radius: 6px;
    color: oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.5);
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
    color: var(--text-secondary, oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.6));
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
    color: var(--text-secondary, oklch(var(--text-tint-l) var(--tint-c) var(--tint-h) / 0.5));
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.08);
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
    border-bottom: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.07);
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
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.03);
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.08);
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
  .mode-row {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
  }
  .mode-btn {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    padding: 10px 12px;
    border: 1px solid var(--border-color);
    border-radius: 10px;
    background: var(--surface-color);
    color: var(--text-secondary);
    cursor: pointer;
    text-align: left;
    transition: border-color 0.15s ease, background 0.15s ease;
  }
  .mode-btn:hover { border-color: var(--border-strong); background: var(--surface-light); }
  .mode-btn--active {
    border-color: var(--accent-border);
    background: var(--surface-accent);
    color: var(--text-primary);
  }
  .mode-btn-icon { font-size: 16px; line-height: 1; }
  .mode-btn-name { font-size: 13px; font-weight: 600; }
  .mode-btn-desc { font-size: 11px; color: var(--text-muted); }

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
  /* SF-3 — one-line footnote under the language grid. Uses the capped text
     role, not a raw literal, so it follows the theme in both modes. */
  .themes-section-note {
    margin: 10px 0 0;
    font-size: 11.5px;
    line-height: 1.5;
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
      linear-gradient(180deg, oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.04), oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.02)),
      oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.025);
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.08);
    border-radius: 16px;
    cursor: pointer;
    text-align: left;
    transition: background 0.18s ease, border-color 0.18s ease, transform 0.18s ease, box-shadow 0.18s ease;
    position: relative;
    box-shadow: 0 14px 30px rgba(0, 0, 0, calc(0.16 * var(--shd-k)));
  }
  .theme-card:hover {
    background:
      linear-gradient(180deg, oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.06), oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.03)),
      oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.035);
    border-color: oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.18);
    transform: translateY(-3px);
    box-shadow: 0 18px 34px rgba(0, 0, 0, calc(0.22 * var(--shd-k)));
    color: inherit;
  }
  .theme-card--active {
    border-color: var(--accent-color) !important;
    background:
      linear-gradient(180deg, oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.07), oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.03)),
      oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.04) !important;
    box-shadow: 0 0 0 1px var(--accent-color) inset, 0 18px 34px rgba(0, 0, 0, calc(0.2 * var(--shd-k)));
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
    box-shadow: inset 0 1px 0 oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.12);
  }
  .theme-card-preview::after {
    content: '';
    position: absolute;
    inset: 0;
    background:
      radial-gradient(circle at top right, oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.18), transparent 38%),
      linear-gradient(180deg, oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.08), transparent 55%);
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
  /* Custom theme picker (v2.0.0 THEME-4). The card reuses .theme-card
     wholesale so it cannot drift from the other three sections; only the
     two-axis chip strip beside it is new. */
  .custom-theme-row {
    display: grid;
    grid-template-columns: minmax(155px, 200px) 1fr;
    gap: 16px;
    align-items: start;
  }
  .custom-theme-card {
    margin: 0;
  }
  .custom-axes {
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding: 14px 16px 15px;
    background: oklch(var(--surf-tint-l) var(--tint-c) var(--tint-h) / 0.025);
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.08);
    border-radius: 16px;
  }
  .custom-axis-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin-bottom: 9px;
  }
  .custom-axis-label {
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.14em;
    color: var(--text-muted);
  }
  .custom-axis-value {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-primary);
  }
  .custom-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
  }
  .custom-chip {
    width: 26px;
    height: 26px;
    border-radius: 8px;
    border: 1px solid oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.16);
    cursor: pointer;
    padding: 0;
    flex-shrink: 0;
    transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease;
  }
  .custom-chip:hover {
    transform: translateY(-1px);
    border-color: oklch(var(--bord-tint-l) var(--tint-c) var(--tint-h) / 0.34);
  }
  .custom-chip--active {
    border-color: var(--accent-color);
    box-shadow: 0 0 0 2px var(--accent-soft);
  }
  .custom-axis-hint {
    font-size: 10.5px;
    color: var(--text-faint);
    line-height: 1.45;
    margin-top: 8px;
  }
  @media (max-width: 720px) {
    .custom-theme-row { grid-template-columns: 1fr; }
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

  /* ── Command palette (v2.0.0 PORT-1) ─────────────────────────────────────
     Built from the three-tier tokens THEME-1 introduced, so every theme and
     both modes are covered with no per-theme block. --shd-k is the mode-aware
     shadow scalar: a dark scrim alpha on a light ground reads as mud. */
  .pal-overlay {
    position: fixed;
    inset: 0;
    z-index: 400;
    display: flex;
    justify-content: center;
    align-items: flex-start;
    padding: min(14vh, 120px) 16px 16px;
    background: oklch(from var(--bg-color) l c h / calc(0.62 * var(--shd-k)));
    backdrop-filter: blur(6px);
  }
  .pal-card {
    width: 100%;
    max-width: 620px;
    background: var(--bg-elevated);
    border: 1px solid var(--border-strong);
    border-radius: 14px;
    box-shadow: 0 24px 70px oklch(from var(--bg-color) l c h / calc(0.6 * var(--shd-k)));
    overflow: hidden;
    display: flex;
    flex-direction: column;
    max-height: min(70vh, 560px);
  }
  .pal-top {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 14px;
    border-bottom: 1px solid var(--border-color);
    flex-shrink: 0;
  }
  .pal-icon { color: var(--text-secondary); font-size: 13px; }
  .pal-input {
    flex: 1;
    min-width: 0;
    background: transparent;
    border: 0;
    outline: none;
    color: var(--text-primary);
    font-size: 15px;
    font-family: var(--font-ui);
  }
  .pal-input::placeholder { color: var(--text-secondary); }
  .pal-esc {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text-secondary);
    border: 1px solid var(--border-color);
    border-radius: 5px;
    padding: 2px 6px;
    white-space: nowrap;
  }
  .pal-list { overflow-y: auto; padding: 6px; flex: 1; }
  .pal-group {
    font-family: var(--font-mono);
    font-size: 9.5px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text-secondary);
    padding: 10px 10px 4px;
  }
  .pal-group:first-child { padding-top: 4px; }
  .pal-row {
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 8px 10px;
    border-radius: 8px;
    cursor: pointer;
    color: var(--text-secondary);
  }
  /* --bg-color, not --surface-color: the latter is a mode-flipped overlay that
     DARKENS the row in light mode while the text tokens stay dark, measured at
     2.02:1 for secondary text. --bg-color is the ground THEME-1 tuned the text
     tokens against and holds in both modes (14.14-18.03 / 6.27-10.14). The
     surface delta from the card is deliberately small, so the inset accent bar
     is what actually carries the selection. */
  .pal-row.pal-active {
    background: var(--bg-color);
    color: var(--text-primary);
    box-shadow: inset 3px 0 0 var(--accent-color);
  }
  .pal-label {
    flex: 1;
    min-width: 0;
    font-size: 13.5px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .pal-hint {
    font-size: 11px;
    color: var(--text-secondary);
    white-space: nowrap;
    flex-shrink: 0;
  }
  .pal-row.pal-active .pal-hint { color: var(--text-primary); }
  .pal-empty { padding: 26px 14px 30px; text-align: center; color: var(--text-secondary); font-size: 13px; }

  .pal-chip {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    background: transparent;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 4px 7px;
    cursor: pointer;
    line-height: 1;
  }
  .pal-chip span {
    font-family: var(--font-mono);
    font-size: 9.5px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--text-muted);
  }
  .pal-chip:hover { border-color: var(--accent-border); }
  .pal-chip:hover span { color: var(--accent-color); }

  /* The row the palette jumped to. :global because the target lives in the
     settings/folder/themes markup and the class is added imperatively, so
     Svelte's scoper would otherwise strip this rule as unused. */
  :global(.setting-flash) {
    animation: settingFlash 1.8s ease-out;
    border-radius: 8px;
  }
  @keyframes settingFlash {
    0%   { box-shadow: 0 0 0 2px var(--accent-color), 0 0 18px var(--accent-soft); }
    70%  { box-shadow: 0 0 0 2px var(--accent-color), 0 0 18px var(--accent-soft); }
    100% { box-shadow: 0 0 0 2px transparent, 0 0 18px transparent; }
  }
  @media (prefers-reduced-motion: reduce) {
    :global(.setting-flash) { animation: none; box-shadow: 0 0 0 2px var(--accent-color); }
  }
</style>
