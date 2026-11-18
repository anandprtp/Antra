export namespace main {
	
	export class Config {
	    download_path: string;
	    apple_enabled: boolean;
	    apple_authorization_token?: string;
	    apple_music_user_token?: string;
	    apple_storefront?: string;
	    apple_wvd_path?: string;
	    amazon_enabled: boolean;
	    amazon_direct_creds_json?: string;
	    amazon_wvd_path?: string;
	    amazon_region?: string;
	    qobuz_enabled: boolean;
	    qobuz_email?: string;
	    qobuz_password?: string;
	    qobuz_app_id?: string;
	    qobuz_app_secret?: string;
	    qobuz_user_auth_token?: string;
	    deezer_arl_token?: string;
	    deezer_bf_secret?: string;
	    soulseek_enabled: boolean;
	    soulseek_username?: string;
	    soulseek_password?: string;
	    soulseek_seed_after_download: boolean;
	    sources_enabled?: string[];
	    first_run_complete: boolean;
	    output_format?: string;
	    max_retries?: number;
	    library_mode?: string;
	    prefer_explicit?: boolean;
	    strict_matching: boolean;
	    strict_format?: boolean;
	    prevent_lossy_transcode?: boolean;
	    write_antra_tags?: boolean;
	    folder_structure?: string;
	    album_folder_structure?: string;
	    playlist_folder_structure?: string;
	    playlist_storage_mode?: string;
	    playlist_renumber?: boolean;
	    playlist_prune_orphans?: string;
	    single_track_structure?: string;
	    filename_format?: string;
	    single_track_filename_template?: string;
	    album_zip_name_template?: string;
	    album_track_filename_template?: string;
	    folder_structure_template?: string;
	    multi_disc_handling?: string;
	    track_number_padding?: number;
	    illegal_character_replacement?: string;
	    whitespace_handling?: string;
	    filename_conflict_behavior?: string;
	    fetch_lyrics: boolean;
	    spotify_sp_dc?: string;
	    tidal_enabled: boolean;
	    tidal_auth_mode?: string;
	    tidal_session_json?: string;
	    tidal_access_token?: string;
	    tidal_refresh_token?: string;
	    tidal_session_id?: string;
	    tidal_token_type?: string;
	    tidal_country_code?: string;
	    antra_api_key?: string;
	    antra_device_token?: string;
	    theme?: string;
	    mode?: string;
	    font?: string;
	    language?: string;
	    log_panel_width?: number;
	    custom_base?: string;
	    custom_accent?: string;
	    notify_sound?: string;
	    notify_volume?: number;
	    preview_volume?: number;
	    notify_batch_only?: boolean;
	    download_source?: string;
	    download_sources?: string[];
	    save_cover_art_sidecar: boolean;
	    cover_image_template?: string;
	    local_artwork_size?: number;
	    local_artwork_format?: string;
	    jpeg_image_quality?: number;
	    embedded_artwork_size?: number;
	    embedded_artwork_png: boolean;
	    save_artist_image: boolean;
	    artist_image_template?: string;
	    disabled_tags?: string[];
	    date_format?: string;
	    write_id3v1?: boolean;
	    title_casing?: string;
	    artist_casing?: string;
	    remove_album_version?: boolean;
	    remove_duplicate_artists?: boolean;
	    featured_artists_mode?: string;
	    multi_artist_separator?: string;
	    album_various_artists?: boolean;
	    post_command_enabled?: boolean;
	    post_command_confirmed?: boolean;
	    post_command?: string;
	    post_command_mode?: string;
	    post_command_timeout?: number;
	    replaygain_enabled?: boolean;
	    verify_audio_enabled?: boolean;
	    sidecar_cue: boolean;
	    sidecar_nfo: boolean;
	    sidecar_json: boolean;
	    auto_sync_enabled: boolean;
	    auto_sync_hour: number;
	    auto_sync_minute: number;
	    auto_sync_days: number;
	    tracked_playlists?: any[];
	
	    static createFrom(source: any = {}) {
	        return new Config(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.download_path = source["download_path"];
	        this.apple_enabled = source["apple_enabled"];
	        this.apple_authorization_token = source["apple_authorization_token"];
	        this.apple_music_user_token = source["apple_music_user_token"];
	        this.apple_storefront = source["apple_storefront"];
	        this.apple_wvd_path = source["apple_wvd_path"];
	        this.amazon_enabled = source["amazon_enabled"];
	        this.amazon_direct_creds_json = source["amazon_direct_creds_json"];
	        this.amazon_wvd_path = source["amazon_wvd_path"];
	        this.amazon_region = source["amazon_region"];
	        this.qobuz_enabled = source["qobuz_enabled"];
	        this.qobuz_email = source["qobuz_email"];
	        this.qobuz_password = source["qobuz_password"];
	        this.qobuz_app_id = source["qobuz_app_id"];
	        this.qobuz_app_secret = source["qobuz_app_secret"];
	        this.qobuz_user_auth_token = source["qobuz_user_auth_token"];
	        this.deezer_arl_token = source["deezer_arl_token"];
	        this.deezer_bf_secret = source["deezer_bf_secret"];
	        this.soulseek_enabled = source["soulseek_enabled"];
	        this.soulseek_username = source["soulseek_username"];
	        this.soulseek_password = source["soulseek_password"];
	        this.soulseek_seed_after_download = source["soulseek_seed_after_download"];
	        this.sources_enabled = source["sources_enabled"];
	        this.first_run_complete = source["first_run_complete"];
	        this.output_format = source["output_format"];
	        this.max_retries = source["max_retries"];
	        this.library_mode = source["library_mode"];
	        this.prefer_explicit = source["prefer_explicit"];
	        this.strict_matching = source["strict_matching"];
	        this.strict_format = source["strict_format"];
	        this.prevent_lossy_transcode = source["prevent_lossy_transcode"];
	        this.write_antra_tags = source["write_antra_tags"];
	        this.folder_structure = source["folder_structure"];
	        this.album_folder_structure = source["album_folder_structure"];
	        this.playlist_folder_structure = source["playlist_folder_structure"];
	        this.playlist_storage_mode = source["playlist_storage_mode"];
	        this.playlist_renumber = source["playlist_renumber"];
	        this.playlist_prune_orphans = source["playlist_prune_orphans"];
	        this.single_track_structure = source["single_track_structure"];
	        this.filename_format = source["filename_format"];
	        this.single_track_filename_template = source["single_track_filename_template"];
	        this.album_zip_name_template = source["album_zip_name_template"];
	        this.album_track_filename_template = source["album_track_filename_template"];
	        this.folder_structure_template = source["folder_structure_template"];
	        this.multi_disc_handling = source["multi_disc_handling"];
	        this.track_number_padding = source["track_number_padding"];
	        this.illegal_character_replacement = source["illegal_character_replacement"];
	        this.whitespace_handling = source["whitespace_handling"];
	        this.filename_conflict_behavior = source["filename_conflict_behavior"];
	        this.fetch_lyrics = source["fetch_lyrics"];
	        this.spotify_sp_dc = source["spotify_sp_dc"];
	        this.tidal_enabled = source["tidal_enabled"];
	        this.tidal_auth_mode = source["tidal_auth_mode"];
	        this.tidal_session_json = source["tidal_session_json"];
	        this.tidal_access_token = source["tidal_access_token"];
	        this.tidal_refresh_token = source["tidal_refresh_token"];
	        this.tidal_session_id = source["tidal_session_id"];
	        this.tidal_token_type = source["tidal_token_type"];
	        this.tidal_country_code = source["tidal_country_code"];
	        this.antra_api_key = source["antra_api_key"];
	        this.antra_device_token = source["antra_device_token"];
	        this.theme = source["theme"];
	        this.mode = source["mode"];
	        this.font = source["font"];
	        this.language = source["language"];
	        this.log_panel_width = source["log_panel_width"];
	        this.custom_base = source["custom_base"];
	        this.custom_accent = source["custom_accent"];
	        this.notify_sound = source["notify_sound"];
	        this.notify_volume = source["notify_volume"];
	        this.preview_volume = source["preview_volume"];
	        this.notify_batch_only = source["notify_batch_only"];
	        this.download_source = source["download_source"];
	        this.download_sources = source["download_sources"];
	        this.save_cover_art_sidecar = source["save_cover_art_sidecar"];
	        this.cover_image_template = source["cover_image_template"];
	        this.local_artwork_size = source["local_artwork_size"];
	        this.local_artwork_format = source["local_artwork_format"];
	        this.jpeg_image_quality = source["jpeg_image_quality"];
	        this.embedded_artwork_size = source["embedded_artwork_size"];
	        this.embedded_artwork_png = source["embedded_artwork_png"];
	        this.save_artist_image = source["save_artist_image"];
	        this.artist_image_template = source["artist_image_template"];
	        this.disabled_tags = source["disabled_tags"];
	        this.date_format = source["date_format"];
	        this.write_id3v1 = source["write_id3v1"];
	        this.title_casing = source["title_casing"];
	        this.artist_casing = source["artist_casing"];
	        this.remove_album_version = source["remove_album_version"];
	        this.remove_duplicate_artists = source["remove_duplicate_artists"];
	        this.featured_artists_mode = source["featured_artists_mode"];
	        this.multi_artist_separator = source["multi_artist_separator"];
	        this.album_various_artists = source["album_various_artists"];
	        this.post_command_enabled = source["post_command_enabled"];
	        this.post_command_confirmed = source["post_command_confirmed"];
	        this.post_command = source["post_command"];
	        this.post_command_mode = source["post_command_mode"];
	        this.post_command_timeout = source["post_command_timeout"];
	        this.replaygain_enabled = source["replaygain_enabled"];
	        this.verify_audio_enabled = source["verify_audio_enabled"];
	        this.sidecar_cue = source["sidecar_cue"];
	        this.sidecar_nfo = source["sidecar_nfo"];
	        this.sidecar_json = source["sidecar_json"];
	        this.auto_sync_enabled = source["auto_sync_enabled"];
	        this.auto_sync_hour = source["auto_sync_hour"];
	        this.auto_sync_minute = source["auto_sync_minute"];
	        this.auto_sync_days = source["auto_sync_days"];
	        this.tracked_playlists = source["tracked_playlists"];
	    }
	}
	export class DeletePlaylistOrphansResult {
	    deleted: number;
	    failed: number;
	    message: string;
	
	    static createFrom(source: any = {}) {
	        return new DeletePlaylistOrphansResult(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.deleted = source["deleted"];
	        this.failed = source["failed"];
	        this.message = source["message"];
	    }
	}
	export class DeviceAccountStatus {
	    signed_in: boolean;
	    valid: boolean;
	    reachable: boolean;
	    is_supporter: boolean;
	    tier?: string;
	    username?: string;
	    expires_at?: number;
	    should_renew?: boolean;
	    error?: string;
	
	    static createFrom(source: any = {}) {
	        return new DeviceAccountStatus(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.signed_in = source["signed_in"];
	        this.valid = source["valid"];
	        this.reachable = source["reachable"];
	        this.is_supporter = source["is_supporter"];
	        this.tier = source["tier"];
	        this.username = source["username"];
	        this.expires_at = source["expires_at"];
	        this.should_renew = source["should_renew"];
	        this.error = source["error"];
	    }
	}
	export class DeviceCodeResult {
	    device_code: string;
	    user_code: string;
	    verification_url: string;
	    expires_in: number;
	    interval: number;
	    error?: string;
	    can_diagnose?: boolean;
	
	    static createFrom(source: any = {}) {
	        return new DeviceCodeResult(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.device_code = source["device_code"];
	        this.user_code = source["user_code"];
	        this.verification_url = source["verification_url"];
	        this.expires_in = source["expires_in"];
	        this.interval = source["interval"];
	        this.error = source["error"];
	        this.can_diagnose = source["can_diagnose"];
	    }
	}
	export class DeviceRenewResult {
	    status: string;
	    token?: string;
	    expires_at?: number;
	    tier?: string;
	    username?: string;
	    days_left?: number;
	    error?: string;
	
	    static createFrom(source: any = {}) {
	        return new DeviceRenewResult(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.status = source["status"];
	        this.token = source["token"];
	        this.expires_at = source["expires_at"];
	        this.tier = source["tier"];
	        this.username = source["username"];
	        this.days_left = source["days_left"];
	        this.error = source["error"];
	    }
	}
	export class DeviceTokenResult {
	    status: string;
	    token?: string;
	    username?: string;
	    tier?: string;
	    error?: string;
	
	    static createFrom(source: any = {}) {
	        return new DeviceTokenResult(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.status = source["status"];
	        this.token = source["token"];
	        this.username = source["username"];
	        this.tier = source["tier"];
	        this.error = source["error"];
	    }
	}
	export class HistoryItem {
	    date: string;
	    url: string;
	    title?: string;
	    artwork_url?: string;
	    total: number;
	    downloaded: number;
	    failed: number;
	    skipped: number;
	    error?: string;
	    sources: Record<string, number>;
	
	    static createFrom(source: any = {}) {
	        return new HistoryItem(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.date = source["date"];
	        this.url = source["url"];
	        this.title = source["title"];
	        this.artwork_url = source["artwork_url"];
	        this.total = source["total"];
	        this.downloaded = source["downloaded"];
	        this.failed = source["failed"];
	        this.skipped = source["skipped"];
	        this.error = source["error"];
	        this.sources = source["sources"];
	    }
	}
	export class KeyInfoResult {
	    valid: boolean;
	    is_supporter: boolean;
	    key_type?: string;
	    expires_at?: string;
	    download_limit?: number;
	    download_count?: number;
	    usage_today?: Record<string, any>;
	    error?: string;
	    reachable: boolean;
	
	    static createFrom(source: any = {}) {
	        return new KeyInfoResult(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.valid = source["valid"];
	        this.is_supporter = source["is_supporter"];
	        this.key_type = source["key_type"];
	        this.expires_at = source["expires_at"];
	        this.download_limit = source["download_limit"];
	        this.download_count = source["download_count"];
	        this.usage_today = source["usage_today"];
	        this.error = source["error"];
	        this.reachable = source["reachable"];
	    }
	}
	export class QueueEnqueueResult {
	    id: string;
	    started: boolean;
	    position: number;
	
	    static createFrom(source: any = {}) {
	        return new QueueEnqueueResult(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.id = source["id"];
	        this.started = source["started"];
	        this.position = source["position"];
	    }
	}
	export class QueueItem {
	    id: string;
	    kind: string;
	    urls?: string[];
	    title: string;
	    track_count?: number;
	    state: string;
	    added_at: string;
	    interrupted?: boolean;
	
	    static createFrom(source: any = {}) {
	        return new QueueItem(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.id = source["id"];
	        this.kind = source["kind"];
	        this.urls = source["urls"];
	        this.title = source["title"];
	        this.track_count = source["track_count"];
	        this.state = source["state"];
	        this.added_at = source["added_at"];
	        this.interrupted = source["interrupted"];
	    }
	}
	export class QueueSnapshot {
	    items: QueueItem[];
	    paused: boolean;
	    running_id: string;
	    restored: boolean;
	
	    static createFrom(source: any = {}) {
	        return new QueueSnapshot(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.items = this.convertValues(source["items"], QueueItem);
	        this.paused = source["paused"];
	        this.running_id = source["running_id"];
	        this.restored = source["restored"];
	    }
	
		convertValues(a: any, classs: any, asMap: boolean = false): any {
		    if (!a) {
		        return a;
		    }
		    if (a.slice && a.map) {
		        return (a as any[]).map(elem => this.convertValues(elem, classs));
		    } else if ("object" === typeof a) {
		        if (asMap) {
		            for (const key of Object.keys(a)) {
		                a[key] = new classs(a[key]);
		            }
		            return a;
		        }
		        return new classs(a);
		    }
		    return a;
		}
	}

}

