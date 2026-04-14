export namespace main {
	
	export class Config {
	    download_path: string;
	    soulseek_enabled: boolean;
	    soulseek_username?: string;
	    soulseek_password?: string;
	    soulseek_seed_after_download: boolean;
	    sources_enabled?: string[];
	    first_run_complete: boolean;
	    output_format?: string;
	
	    static createFrom(source: any = {}) {
	        return new Config(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.download_path = source["download_path"];
	        this.soulseek_enabled = source["soulseek_enabled"];
	        this.soulseek_username = source["soulseek_username"];
	        this.soulseek_password = source["soulseek_password"];
	        this.soulseek_seed_after_download = source["soulseek_seed_after_download"];
	        this.sources_enabled = source["sources_enabled"];
	        this.first_run_complete = source["first_run_complete"];
	        this.output_format = source["output_format"];
	    }
	}
	export class HistoryItem {
	    date: string;
	    url: string;
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
	        this.total = source["total"];
	        this.downloaded = source["downloaded"];
	        this.failed = source["failed"];
	        this.skipped = source["skipped"];
	        this.error = source["error"];
	        this.sources = source["sources"];
	    }
	}

}

