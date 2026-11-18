/*
 * PORT-3 (v2.0.0) — parse a playlist file into rows Antra can download.
 *
 * Ported from Antra-Web's frontend/static/importer.js, with ONE deliberate,
 * load-bearing deviation from how a player would do this, carried over
 * unchanged:
 *
 *   A player has to resolve every row against a streaming API AT IMPORT TIME,
 *   because it needs a playable object. Antra does not. Its resolver
 *   is already ISRC-first at DOWNLOAD time, so a CSV row IS the metadata. A second
 *   matching pass here would duplicate that work and add exactly the upstream
 *   rate-limit exposure this feature exists to route around. We parse; we do not
 *   match.
 *
 * So this module is pure: text in, rows out. No network, no DOM, no storage, no
 * Wails calls. App.svelte owns the picker, the preview and the hand-off; Go reads
 * the file; Python validates the rows again and downloads them.
 *
 * Desktop deviations from the web copy, each for a measured reason:
 *   - Antra's OWN JSON export (v2.0.0 FR-3) is accepted. It writes
 *     `"playlist": "<title>"` with `tracks[].artists`, and the web parser read that
 *     as `data.playlist || data` -> a STRING -> zero tracks, so Antra could not
 *     re-import its own export.
 *   - A `.txt` file is refused with a reason. FR-3's Text export is a
 *     human-readable list whose album/duration suffixes cannot be split back out
 *     reliably; falling through to the CSV parser produced a misleading
 *     "no title column" error.
 *   - buildImportPayload() lives here rather than in App.svelte so the exact
 *     object handed to the backend is executable in a test.
 */

export const MAX_ROWS = 5000;
const MAX_FIELD = 500;
const MAX_NAME = 200;

export interface ImportedRow {
  title: string;
  artists: string[];
  album: string;
  isrc: string | null;
  duration_ms: number | null;
  track_number: number | null;
  disc_number: number | null;
  release_year: number | null;
}

export interface SkippedRow {
  row: number;
  reason: string;
  raw: string;
}

export interface ImportResult {
  format: string;
  tracks: ImportedRow[];
  skipped: SkippedRow[];
  playlistName: string;
  error: string | null;
  truncated: boolean;
}

interface ParseOut {
  tracks: ImportedRow[];
  skipped: SkippedRow[];
  playlistName: string;
  error?: string;
}

/* ── header aliases ────────────────────────────────────────────────────────
   Drawn from real exports: Exportify ("Track Name", "Artist Name(s)",
   "Track Duration (ms)"), TuneMyMusic ("Track name"), Soundiiz (lowercase), and
   Antra's own FR-3 CSV ("Duration (ms)", "Release Date"). Compared after
   normalizeHeader(), so case/underscores/BOM are already gone. */
const HEADER_MAPPINGS: Record<string, string[]> = {
  title:    ['track name', 'title', 'song', 'name', 'track', 'track title', 'song name', 'titre'],
  artist:   ['artist name(s)', 'artist name', 'artist', 'artists', 'creator',
             'artist names', 'artiste', 'album artist name(s)'],
  album:    ['album', 'album name', 'album title'],
  isrc:     ['isrc', 'isrc code'],
  duration: ['track duration (ms)', 'duration (ms)', 'duration ms', 'duration',
             'length', 'time', 'track duration'],
  trackNo:  ['track number', 'track no', 'track #', 'position', '#'],
  discNo:   ['disc number', 'disc no', 'disc', 'disc #'],
  year:     ['album release date', 'release date', 'year', 'released'],
  type:     ['type', 'category', 'kind'],
  playlist: ['playlist name', 'playlist', 'playlist title'],
};

export function normalizeHeader(h: unknown): string {
  if (!h) return '';
  return String(h)
    .replace(/^﻿/, '')
    .toLowerCase()
    .trim()
    .replace(/[_\s]+/g, ' ');
}

export function mapHeaders(rawHeaders: string[]): Record<string, number> {
  const mapped: Record<string, number> = {};
  for (let i = 0; i < rawHeaders.length; i++) {
    const n = normalizeHeader(rawHeaders[i]);
    if (!n) continue;
    for (const key of Object.keys(HEADER_MAPPINGS)) {
      // First header wins: Exportify has both "Artist Name(s)" and
      // "Album Artist Name(s)", and the track artist column comes first.
      if (mapped[key] === undefined && HEADER_MAPPINGS[key].indexOf(n) !== -1) {
        mapped[key] = i;
        break;
      }
    }
  }
  return mapped;
}

/* ── CSV ────────────────────────────────────────────────────────────────────
   A real parser, not text.split(","). Exports routinely contain commas and
   newlines inside quoted fields ("Ballad of a Thin Man - Live, 1966"). */

export function detectDelimiter(text: string): string {
  // Count candidates OUTSIDE quotes on the header line only.
  let line = '';
  let inQ = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"') { inQ = !inQ; line += c; continue; }
    if (!inQ && (c === '\n' || c === '\r')) break;
    line += c;
  }
  let best = ',';
  let bestN = -1;
  for (const d of [',', ';', '\t', '|']) {
    let n = 0;
    let q = false;
    for (let j = 0; j < line.length; j++) {
      if (line[j] === '"') q = !q;
      else if (!q && line[j] === d) n++;
    }
    if (n > bestN) { bestN = n; best = d; }
  }
  return best;
}

export function parseCSVRows(text: string, delim: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let inQ = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQ) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }  // "" -> literal quote
        else inQ = false;
      } else field += c;
      continue;
    }
    if (c === '"') { inQ = true; continue; }
    if (c === delim) { row.push(field); field = ''; continue; }
    if (c === '\r') { if (text[i + 1] === '\n') i++; rows.push(row.concat([field])); row = []; field = ''; continue; }
    if (c === '\n') { rows.push(row.concat([field])); row = []; field = ''; continue; }
    field += c;
  }
  if (field.length || row.length) rows.push(row.concat([field]));
  // Drop blank lines (a file ending in \n yields one).
  return rows.filter((r) => r.some((v) => String(v).trim() !== ''));
}

/* ── value coercion ─────────────────────────────────────────────────────── */

function clean(v: unknown): string {
  if (v === undefined || v === null) return '';
  let s = String(v).replace(/^﻿/, '').trim();
  if (s.length > MAX_FIELD) s = s.slice(0, MAX_FIELD);
  return s;
}

export function splitArtists(raw: unknown): string[] {
  const s = clean(raw);
  if (!s) return [];
  // Split only on unambiguous separators. " & " is left alone because
  // "Simon & Garfunkel" is one artist, and the resolver's matcher already
  // handles combined artist strings.
  return s.split(/\s*[;,]\s*|\s+\/\s+/)
    .map((a) => a.trim())
    .filter(Boolean)
    .slice(0, 12);
}

export function parseDurationMs(raw: unknown, headerIsMs: boolean): number | null {
  const s = clean(raw);
  if (!s) return null;
  if (/^\d+$/.test(s)) {
    const n = parseInt(s, 10);
    if (headerIsMs) return n > 0 ? n : null;
    // A bare number under a non-ms header is seconds; one that large can only
    // be ms already.
    return n > 10000 ? n : (n > 0 ? n * 1000 : null);
  }
  const m = s.match(/^(\d+):(\d{1,2})(?::(\d{1,2}))?$/);   // M:SS or H:MM:SS
  if (m) {
    const a = parseInt(m[1], 10);
    const b = parseInt(m[2], 10);
    const c = m[3] ? parseInt(m[3], 10) : null;
    const secs = c === null ? a * 60 + b : a * 3600 + b * 60 + c;
    return secs > 0 ? secs * 1000 : null;
  }
  return null;
}

function parseIntOrNull(raw: unknown): number | null {
  const s = clean(raw);
  if (!/^\d+$/.test(s)) return null;
  const n = parseInt(s, 10);
  return n > 0 ? n : null;
}

function parseYear(raw: unknown): number | null {
  const m = clean(raw).match(/(\d{4})/);
  if (!m) return null;
  const y = parseInt(m[1], 10);
  return (y >= 1900 && y <= 2200) ? y : null;
}

function numberOrNull(v: unknown): number | null {
  return (typeof v === 'number' && isFinite(v) && v > 0) ? Math.round(v) : null;
}

// ISRC: 2-letter country + 3 alnum registrant + 2-digit year + 5-digit
// designation. Validated rather than passed through: the resolver matches
// ISRC-first, so a malformed one can pin the WRONG recording.
export function normalizeIsrc(raw: unknown): string | null {
  const s = clean(raw).toUpperCase().replace(/[\s-]/g, '');
  return /^[A-Z]{2}[A-Z0-9]{3}\d{7}$/.test(s) ? s : null;
}

function isrcFromIdentifier(raw: unknown): string | null {
  return normalizeIsrc(String(raw ?? '').replace(/^.*isrc[:\/]/i, ''));
}

/* ── format parsers ─────────────────────────────────────────────────────── */

export function parseCSV(text: string): ParseOut {
  const delim = detectDelimiter(text);
  const rows = parseCSVRows(text, delim);
  if (rows.length < 2) return { tracks: [], skipped: [], playlistName: '' };

  const mapped = mapHeaders(rows[0]);
  if (mapped.title === undefined) {
    // Without a title column there is nothing to download. Say so rather than
    // silently returning zero tracks.
    return {
      tracks: [], skipped: [], playlistName: '',
      error: 'No recognisable track/title column. Found: ' + rows[0].slice(0, 8).join(', '),
    };
  }

  const durHeaderIsMs = mapped.duration !== undefined &&
    /\bms\b|\(ms\)/.test(normalizeHeader(rows[0][mapped.duration]));

  const tracks: ImportedRow[] = [];
  const skipped: SkippedRow[] = [];
  let playlistName = '';
  for (let r = 1; r < rows.length && tracks.length < MAX_ROWS; r++) {
    const v = rows[r];
    const get = (k: string) => (mapped[k] === undefined ? '' : clean(v[mapped[k]]));

    if (!playlistName && mapped.playlist !== undefined) playlistName = get('playlist');

    // TuneMyMusic mixes track/album/artist rows under a "Type" column. Only
    // track rows are downloadable.
    const type = get('type').toLowerCase();
    if (type && type !== 'track' && type !== 'song') {
      skipped.push({ row: r + 1, reason: 'not a track row (type=' + type + ')', raw: get('title') });
      continue;
    }

    const title = get('title');
    if (!title) { skipped.push({ row: r + 1, reason: 'no title', raw: '' }); continue; }

    const artists = splitArtists(get('artist'));
    if (!artists.length) { skipped.push({ row: r + 1, reason: 'no artist', raw: title }); continue; }

    tracks.push({
      title,
      artists,
      album: get('album') || '',
      isrc: normalizeIsrc(get('isrc')),
      duration_ms: parseDurationMs(get('duration'), durHeaderIsMs),
      track_number: parseIntOrNull(get('trackNo')),
      disc_number: parseIntOrNull(get('discNo')),
      release_year: parseYear(get('year')),
    });
  }
  return { tracks, skipped, playlistName };
}

export function parseM3U(text: string): ParseOut {
  const lines = String(text).replace(/^﻿/, '').split(/\r?\n/);
  const tracks: ImportedRow[] = [];
  const skipped: SkippedRow[] = [];
  let playlistName = '';
  let pending: { artists: string[]; title: string; duration_ms: number | null } | null = null;
  for (let i = 0; i < lines.length && tracks.length < MAX_ROWS; i++) {
    const line = lines[i].trim();
    if (!line) continue;
    if (line.charAt(0) === '#') {
      if (/^#PLAYLIST:/i.test(line)) { playlistName = clean(line.slice(10)); continue; }
      const m = line.match(/^#EXTINF:\s*(-?\d+)\s*,\s*(.*)$/i);
      if (m) {
        const secs = parseInt(m[1], 10);
        const label = clean(m[2]);
        // "Artist - Title" is the near-universal EXTINF convention. Split on the
        // FIRST " - " only: titles frequently contain further dashes.
        const idx = label.indexOf(' - ');
        pending = idx > 0
          ? { artists: splitArtists(label.slice(0, idx)), title: clean(label.slice(idx + 3)), duration_ms: null }
          : { artists: [], title: label, duration_ms: null };
        pending.duration_ms = secs > 0 ? secs * 1000 : null;
      }
      continue;
    }
    // A media line. Without a preceding #EXTINF there is no artist/title to
    // match on — a bare path is not enough, so it is reported, not guessed.
    if (!pending || !pending.title) {
      skipped.push({ row: i + 1, reason: 'no #EXTINF metadata for this entry', raw: line.slice(0, 80) });
      pending = null;
      continue;
    }
    if (!pending.artists.length) {
      skipped.push({ row: i + 1, reason: 'no artist in #EXTINF', raw: pending.title });
      pending = null;
      continue;
    }
    tracks.push({
      title: pending.title, artists: pending.artists, album: '', isrc: null,
      duration_ms: pending.duration_ms, track_number: null, disc_number: null, release_year: null,
    });
    pending = null;
  }
  return { tracks, skipped, playlistName };
}

/** JSPF, plus Antra's own FR-3 JSON export (see the header note). */
export function parseJSPF(text: string): ParseOut {
  const data = JSON.parse(text);
  // JSPF nests everything under an OBJECT "playlist"; Antra's export puts the
  // title STRING there and the list at the top level. Only descend into an object.
  const pl = (data && typeof data.playlist === 'object' && data.playlist !== null)
    ? data.playlist
    : (data && typeof data === 'object' ? data : {});
  let list = pl.track || pl.tracks || [];
  if (!Array.isArray(list)) list = [];
  const plTitle = clean(typeof data?.playlist === 'string' ? data.playlist : pl.title);

  const tracks: ImportedRow[] = [];
  const skipped: SkippedRow[] = [];
  for (let i = 0; i < list.length && tracks.length < MAX_ROWS; i++) {
    const t = (list[i] && typeof list[i] === 'object') ? list[i] : {};
    const title = clean(t.title);
    let artists: string[] = [];
    if (Array.isArray(t.artists)) {
      artists = t.artists.map((a: unknown) => clean(a)).filter(Boolean).slice(0, 12);
    } else {
      artists = splitArtists(t.creator ?? t.artist);
    }
    if (!title) { skipped.push({ row: i + 1, reason: 'no title', raw: '' }); continue; }
    if (!artists.length) { skipped.push({ row: i + 1, reason: 'no creator/artist', raw: title }); continue; }

    let isrc = normalizeIsrc(t.isrc);
    if (!isrc && Array.isArray(t.identifier)) {
      for (const cand of t.identifier) {
        isrc = isrcFromIdentifier(cand);
        if (isrc) break;
      }
    } else if (!isrc && t.identifier) {
      isrc = isrcFromIdentifier(t.identifier);
    }

    tracks.push({
      title,
      artists,
      album: clean(t.album),
      isrc,
      duration_ms: numberOrNull(t.duration_ms ?? t.duration),
      track_number: numberOrNull(t.track_number ?? t.trackNum),
      disc_number: numberOrNull(t.disc_number),
      release_year: parseYear(t.release_year ?? t.release_date),
    });
  }
  return { tracks, skipped, playlistName: plTitle };
}

export function parseXSPF(text: string, domParser?: { parseFromString(s: string, t: string): any }): ParseOut {
  const DP = domParser || (typeof DOMParser !== 'undefined' ? new DOMParser() : null);
  if (!DP) throw new Error('XSPF parsing needs a DOMParser');
  const doc = DP.parseFromString(text, 'application/xml');
  if (doc.getElementsByTagName('parsererror').length) throw new Error('Malformed XSPF/XML');
  const txt = (el: any, tag: string) => {
    const n = el.getElementsByTagName(tag);
    return n.length ? clean(n[0].textContent) : '';
  };
  const root = doc.documentElement;
  const nodes = doc.getElementsByTagName('track');
  const tracks: ImportedRow[] = [];
  const skipped: SkippedRow[] = [];
  for (let i = 0; i < nodes.length && tracks.length < MAX_ROWS; i++) {
    const t = nodes[i];
    const title = txt(t, 'title');
    const creator = txt(t, 'creator');
    if (!title) { skipped.push({ row: i + 1, reason: 'no title', raw: '' }); continue; }
    if (!creator) { skipped.push({ row: i + 1, reason: 'no creator/artist', raw: title }); continue; }
    const dur = parseInt(txt(t, 'duration'), 10);
    tracks.push({
      title, artists: splitArtists(creator), album: txt(t, 'album'),
      isrc: isrcFromIdentifier(txt(t, 'identifier')),
      duration_ms: dur > 0 ? dur : null,
      track_number: parseIntOrNull(txt(t, 'trackNum')), disc_number: null, release_year: null,
    });
  }
  // Only the playlist's own <title>, never a track's.
  let plTitle = '';
  for (let c = 0; c < root.childNodes.length; c++) {
    const n = root.childNodes[c];
    if (n.nodeType === 1 && String(n.nodeName).toLowerCase() === 'title') { plTitle = clean(n.textContent); break; }
  }
  return { tracks, skipped, playlistName: plTitle };
}

/* ── entry point ────────────────────────────────────────────────────────── */

export function detectFormat(filename: string, text: string): string {
  const ext = String(filename || '').toLowerCase().split('.').pop();
  if (ext === 'csv' || ext === 'tsv') return 'csv';
  if (ext === 'm3u' || ext === 'm3u8') return 'm3u';
  if (ext === 'jspf' || ext === 'json') return 'jspf';
  if (ext === 'xspf' || ext === 'xml') return 'xspf';
  if (ext === 'txt') return 'txt';
  const head = String(text || '').replace(/^﻿/, '').slice(0, 400).trim();
  if (head.indexOf('#EXTM3U') === 0 || head.indexOf('#EXTINF') !== -1) return 'm3u';
  if (head.charAt(0) === '{') return 'jspf';
  if (head.charAt(0) === '<') return 'xspf';
  return 'csv';
}

export function parsePlaylistFile(
  filename: string,
  text: string,
  opts: { format?: string; domParser?: any } = {},
): ImportResult {
  const fmt = opts.format || detectFormat(filename, text);
  const base = String(filename || '').replace(/^.*[\\/]/, '').replace(/\.[^.]+$/, '').trim();
  let out: ParseOut;
  try {
    if (fmt === 'txt') {
      out = {
        tracks: [], skipped: [], playlistName: '',
        error: 'Plain-text lists cannot be re-imported reliably. Export as CSV, M3U8, XSPF or JSON instead.',
      };
    } else if (fmt === 'm3u') out = parseM3U(text);
    else if (fmt === 'jspf') out = parseJSPF(text);
    else if (fmt === 'xspf') out = parseXSPF(text, opts.domParser);
    else out = parseCSV(text);
  } catch (e: any) {
    return {
      format: fmt, tracks: [], skipped: [], playlistName: base || 'Imported playlist',
      error: (e && e.message) ? e.message : String(e), truncated: false,
    };
  }
  const tracks = out.tracks || [];
  return {
    format: fmt,
    tracks,
    skipped: out.skipped || [],
    playlistName: out.playlistName || base || 'Imported playlist',
    error: out.error || null,
    truncated: tracks.length >= MAX_ROWS,
  };
}

export interface ImportPayload {
  name: string;
  source_file: string;
  format: string;
  tracks: ImportedRow[];
}

/**
 * The exact object App.svelte hands to StartImportDownload. The name the user
 * typed wins; an empty one falls back to the parsed playlist name, so a
 * cleared field can never produce a nameless playlist folder.
 */
export function buildImportPayload(result: ImportResult, typedName: string, filename: string): ImportPayload {
  const name = clean(typedName).slice(0, MAX_NAME) || clean(result.playlistName).slice(0, MAX_NAME) || 'Imported playlist';
  return {
    name,
    source_file: String(filename || '').replace(/^.*[\\/]/, '').slice(0, MAX_NAME),
    format: result.format,
    tracks: result.tracks.slice(0, MAX_ROWS),
  };
}
