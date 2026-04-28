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
	    qobuz_enabled: boolean;
	    qobuz_email?: string;
	    qobuz_password?: string;
	    qobuz_app_id?: string;
	    qobuz_app_secret?: string;
	    qobuz_user_auth_token?: string;
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
	    folder_structure?: string;
	    filename_format?: string;
	    spotify_sp_dc?: string;
	    tidal_enabled: boolean;
	    tidal_auth_mode?: string;
	    tidal_session_json?: string;
	    tidal_access_token?: string;
	    tidal_refresh_token?: string;
	    tidal_session_id?: string;
	    tidal_token_type?: string;
	    tidal_country_code?: string;
	
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
	        this.qobuz_enabled = source["qobuz_enabled"];
	        this.qobuz_email = source["qobuz_email"];
	        this.qobuz_password = source["qobuz_password"];
	        this.qobuz_app_id = source["qobuz_app_id"];
	        this.qobuz_app_secret = source["qobuz_app_secret"];
	        this.qobuz_user_auth_token = source["qobuz_user_auth_token"];
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
	        this.folder_structure = source["folder_structure"];
	        this.filename_format = source["filename_format"];
	        this.spotify_sp_dc = source["spotify_sp_dc"];
	        this.tidal_enabled = source["tidal_enabled"];
	        this.tidal_auth_mode = source["tidal_auth_mode"];
	        this.tidal_session_json = source["tidal_session_json"];
	        this.tidal_access_token = source["tidal_access_token"];
	        this.tidal_refresh_token = source["tidal_refresh_token"];
	        this.tidal_session_id = source["tidal_session_id"];
	        this.tidal_token_type = source["tidal_token_type"];
	        this.tidal_country_code = source["tidal_country_code"];
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

}

