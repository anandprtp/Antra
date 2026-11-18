/* ═══════════════════════════════════════════════════════════════════════════
   ANTRA — settings catalog  (v2.0.0 PORT-1)

   WHY THIS EXISTS, AND HOW IT DIFFERS FROM THE WEB VERSION
   -------------------------------------------------------
   Antra-Web renders its settings drawer FROM a schema, so its command palette
   gets a search index for free (`searchIndex()` is a `.map()` over that schema)
   and the index cannot go stale against a section nobody has opened.

   The desktop's settings are ~540 lines of hand-written Svelte spread across
   three different surfaces (the Settings modal, the Folder Structure modal and
   the Themes panel). Converting all of that into a renderer is a large rewrite
   of the app's most heavily-used screen, and PORT-1 does not need it: what the
   palette needs is an INDEX, not a renderer.

   So the index is declared here, and the two halves are cross-checked by a test
   rather than by construction:

     * every entry below must have exactly one `data-setting="<id>"` in
       App.svelte, and
     * every `data-setting` in App.svelte must have an entry below.

   That gives the same property the web schema gives — the index is complete and
   searchable before any settings surface has ever been rendered — with the
   difference that here it is enforced by `test_palette.cjs` rather than being
   true by construction. Stated plainly because it IS a weaker guarantee: it
   holds only while the test is run.

   `surface` says which window must be opened to reach the row; `id` is both the
   catalog key and the `data-setting` anchor the palette scrolls to and flashes.
   ═══════════════════════════════════════════════════════════════════════════ */

export type SettingSurface = 'settings' | 'folder' | 'themes' | 'main';

export interface SettingEntry {
  /** Anchor id — matches `data-setting="…"` in App.svelte. Usually the config key. */
  id: string;
  label: string;
  /** Short plain-language description; searched too, at a lower weight. */
  desc: string;
  /** Section heading as shown on screen. */
  group: string;
  surface: SettingSurface;
  /**
   * Set when this row is rendered inside a `{#if config.<parent>}` block, so it
   * is NOT in the DOM while the parent toggle is off. The palette still indexes
   * it — hiding it would make the setting undiscoverable, which is worse — but
   * the jump falls back to flashing the parent, which is the control the user
   * has to change first. Measured from the markup, not guessed, and pinned by
   * `test_palette.cjs`.
   */
  requires?: string;
}

export const SETTINGS_CATALOG: SettingEntry[] = [
  /* ── Main screen ──────────────────────────────────────────────────────── */
  { id: 'output_format', label: 'settings.catalog.output_format.label', surface: 'main', group: 'settings.groups.downloads',
    desc: 'settings.catalog.output_format.desc' },

  /* ── Settings modal ───────────────────────────────────────────────────── */
  { id: 'antra_api_key', label: 'settings.catalog.antra_api_key.label', surface: 'settings', group: 'settings.groups.antra_account',
    desc: 'settings.catalog.antra_api_key.desc' },

  { id: 'prefer_explicit', label: 'settings.catalog.prefer_explicit.label', surface: 'settings', group: 'settings.groups.matching',
    desc: 'settings.catalog.prefer_explicit.desc' },
  { id: 'strict_matching', label: 'settings.catalog.strict_matching.label', surface: 'settings', group: 'settings.groups.matching',
    desc: 'settings.catalog.strict_matching.desc' },
  { id: 'strict_format', label: 'settings.catalog.strict_format.label', surface: 'settings', group: 'settings.groups.matching',
    desc: 'settings.catalog.strict_format.desc' },
  { id: 'prevent_lossy_transcode', label: 'settings.catalog.prevent_lossy_transcode.label', surface: 'settings', group: 'settings.groups.matching',
    desc: 'settings.catalog.prevent_lossy_transcode.desc' },
  { id: 'write_antra_tags', label: 'settings.catalog.write_antra_tags.label', surface: 'settings', group: 'settings.groups.matching',
    desc: 'settings.catalog.write_antra_tags.desc' },

  { id: 'save_cover_art_sidecar', label: 'settings.catalog.save_cover_art_sidecar.label', surface: 'settings', group: 'settings.groups.cover_art',
    desc: 'settings.catalog.save_cover_art_sidecar.desc' },
  { id: 'cover_image_template', label: 'settings.catalog.cover_image_template.label', surface: 'settings', group: 'settings.groups.cover_art',
    desc: 'settings.catalog.cover_image_template.desc', requires: 'save_cover_art_sidecar' },
  { id: 'local_artwork_size', label: 'settings.catalog.local_artwork_size.label', surface: 'settings', group: 'settings.groups.cover_art',
    desc: 'settings.catalog.local_artwork_size.desc', requires: 'save_cover_art_sidecar' },
  { id: 'local_artwork_format', label: 'settings.catalog.local_artwork_format.label', surface: 'settings', group: 'settings.groups.cover_art',
    desc: 'settings.catalog.local_artwork_format.desc', requires: 'save_cover_art_sidecar' },
  { id: 'jpeg_image_quality', label: 'settings.catalog.jpeg_image_quality.label', surface: 'settings', group: 'settings.groups.cover_art',
    desc: 'settings.catalog.jpeg_image_quality.desc', requires: 'save_cover_art_sidecar' },
  { id: 'save_artist_image', label: 'settings.catalog.save_artist_image.label', surface: 'settings', group: 'settings.groups.cover_art',
    desc: 'settings.catalog.save_artist_image.desc' },
  { id: 'artist_image_template', label: 'settings.catalog.artist_image_template.label', surface: 'settings', group: 'settings.groups.cover_art',
    desc: 'settings.catalog.artist_image_template.desc', requires: 'save_artist_image' },
  { id: 'embedded_artwork_size', label: 'settings.catalog.embedded_artwork_size.label', surface: 'settings', group: 'settings.groups.cover_art',
    desc: 'settings.catalog.embedded_artwork_size.desc' },
  { id: 'embedded_artwork_png', label: 'settings.catalog.embedded_artwork_png.label', surface: 'settings', group: 'settings.groups.cover_art',
    desc: 'settings.catalog.embedded_artwork_png.desc' },

  { id: 'disabled_tags', label: 'settings.catalog.disabled_tags.label', surface: 'settings', group: 'settings.groups.advanced_tags',
    desc: 'settings.catalog.disabled_tags.desc' },
  { id: 'date_format', label: 'settings.catalog.date_format.label', surface: 'settings', group: 'settings.groups.advanced_tags',
    desc: 'settings.catalog.date_format.desc' },
  { id: 'write_id3v1', label: 'settings.catalog.write_id3v1.label', surface: 'settings', group: 'settings.groups.advanced_tags',
    desc: 'settings.catalog.write_id3v1.desc' },
  { id: 'replaygain_enabled', label: 'settings.catalog.replaygain_enabled.label', surface: 'settings', group: 'settings.groups.advanced_tags',
    desc: 'settings.catalog.replaygain_enabled.desc' },
  { id: 'verify_audio_enabled', label: 'settings.catalog.verify_audio_enabled.label', surface: 'settings', group: 'settings.groups.advanced_tags',
    desc: 'settings.catalog.verify_audio_enabled.desc' },
  { id: 'sidecar_cue', label: 'settings.catalog.sidecar_cue.label', surface: 'settings', group: 'settings.groups.sidecar_files',
    desc: 'settings.catalog.sidecar_cue.desc' },
  { id: 'sidecar_nfo', label: 'settings.catalog.sidecar_nfo.label', surface: 'settings', group: 'settings.groups.sidecar_files',
    desc: 'settings.catalog.sidecar_nfo.desc' },
  { id: 'sidecar_json', label: 'settings.catalog.sidecar_json.label', surface: 'settings', group: 'settings.groups.sidecar_files',
    desc: 'settings.catalog.sidecar_json.desc' },

  { id: 'max_retries', label: 'settings.catalog.max_retries.label', surface: 'settings', group: 'settings.groups.downloads',
    desc: 'settings.catalog.max_retries.desc' },
  { id: 'download_sources', label: 'settings.catalog.download_sources.label', surface: 'settings', group: 'settings.groups.downloads',
    desc: 'settings.catalog.download_sources.desc' },

  { id: 'post_command_enabled', label: 'settings.catalog.post_command_enabled.label', surface: 'settings', group: 'settings.groups.downloads',
    desc: 'settings.catalog.post_command_enabled.desc' },
  { id: 'post_command', label: 'settings.catalog.post_command.label', surface: 'settings', group: 'settings.groups.downloads',
    desc: 'settings.catalog.post_command.desc',
    requires: 'post_command_enabled' },
  { id: 'post_command_mode', label: 'settings.catalog.post_command_mode.label', surface: 'settings', group: 'settings.groups.downloads',
    desc: 'settings.catalog.post_command_mode.desc',
    requires: 'post_command_enabled' },
  { id: 'post_command_timeout', label: 'settings.catalog.post_command_timeout.label', surface: 'settings', group: 'settings.groups.downloads',
    desc: 'settings.catalog.post_command_timeout.desc',
    requires: 'post_command_enabled' },

  { id: 'soulseek_enabled', label: 'settings.catalog.soulseek_enabled.label', surface: 'settings', group: 'settings.groups.sources',
    desc: 'settings.catalog.soulseek_enabled.desc' },
  { id: 'soulseek_username', label: 'settings.catalog.soulseek_username.label', surface: 'settings', group: 'settings.groups.sources',
    desc: 'settings.catalog.soulseek_username.desc', requires: 'soulseek_enabled' },
  { id: 'soulseek_password', label: 'settings.catalog.soulseek_password.label', surface: 'settings', group: 'settings.groups.sources',
    desc: 'settings.catalog.soulseek_password.desc', requires: 'soulseek_enabled' },
  { id: 'soulseek_seed_after_download', label: 'settings.catalog.soulseek_seed_after_download.label', surface: 'settings', group: 'settings.groups.sources',
    desc: 'settings.catalog.soulseek_seed_after_download.desc', requires: 'soulseek_enabled' },

  { id: 'spotify_sp_dc', label: 'settings.catalog.spotify_sp_dc.label', surface: 'settings', group: 'settings.groups.spotify_account',
    desc: 'settings.catalog.spotify_sp_dc.desc' },
  { id: 'apple_music_user_token', label: 'settings.catalog.apple_music_user_token.label', surface: 'settings', group: 'settings.groups.apple_music_account',
    desc: 'settings.catalog.apple_music_user_token.desc' },

  { id: 'auto_sync_enabled', label: 'settings.catalog.auto_sync_enabled.label', surface: 'settings', group: 'settings.groups.auto_sync',
    desc: 'settings.catalog.auto_sync_enabled.desc' },
  { id: 'auto_sync_time', label: 'settings.catalog.auto_sync_time.label', surface: 'settings', group: 'settings.groups.auto_sync',
    desc: 'settings.catalog.auto_sync_time.desc', requires: 'auto_sync_enabled' },
  { id: 'auto_sync_days', label: 'settings.catalog.auto_sync_days.label', surface: 'settings', group: 'settings.groups.auto_sync',
    desc: 'settings.catalog.auto_sync_days.desc', requires: 'auto_sync_enabled' },

  /* ── Folder Structure modal ───────────────────────────────────────────── */
  { id: 'download_path', label: 'settings.catalog.download_path.label', surface: 'folder', group: 'settings.groups.library',
    desc: 'settings.catalog.download_path.desc' },
  { id: 'library_mode', label: 'settings.catalog.library_mode.label', surface: 'folder', group: 'settings.groups.library',
    desc: 'settings.catalog.library_mode.desc' },
  { id: 'filename_conflict_behavior', label: 'settings.catalog.filename_conflict_behavior.label', surface: 'folder', group: 'settings.groups.library',
    desc: 'settings.catalog.filename_conflict_behavior.desc' },
  { id: 'single_track_filename_template', label: 'settings.catalog.single_track_filename_template.label', surface: 'folder', group: 'settings.groups.naming',
    desc: 'settings.catalog.single_track_filename_template.desc' },
  { id: 'album_track_filename_template', label: 'settings.catalog.album_track_filename_template.label', surface: 'folder', group: 'settings.groups.naming',
    desc: 'settings.catalog.album_track_filename_template.desc' },
  { id: 'folder_structure_template', label: 'settings.catalog.folder_structure_template.label', surface: 'folder', group: 'settings.groups.naming',
    desc: 'settings.catalog.folder_structure_template.desc' },
  { id: 'playlist_storage_mode', label: 'settings.catalog.playlist_storage_mode.label', surface: 'folder', group: 'settings.groups.naming',
    desc: 'settings.catalog.playlist_storage_mode.desc' },
  // issue #75 — both are hidden in library storage mode, where a playlist has no
  // folder of its own for them to act on, so the palette jump falls back to the
  // storage-mode control the user has to change first.
  { id: 'playlist_renumber', label: 'settings.catalog.playlist_renumber.label', surface: 'folder', group: 'settings.groups.naming',
    desc: 'settings.catalog.playlist_renumber.desc', requires: 'playlist_storage_mode' },
  { id: 'playlist_prune_orphans', label: 'settings.catalog.playlist_prune_orphans.label', surface: 'folder', group: 'settings.groups.naming',
    desc: 'settings.catalog.playlist_prune_orphans.desc', requires: 'playlist_storage_mode' },
  { id: 'multi_disc_handling', label: 'settings.catalog.multi_disc_handling.label', surface: 'folder', group: 'settings.groups.formatting',
    desc: 'settings.catalog.multi_disc_handling.desc' },
  { id: 'track_number_padding', label: 'settings.catalog.track_number_padding.label', surface: 'folder', group: 'settings.groups.formatting',
    desc: 'settings.catalog.track_number_padding.desc' },
  { id: 'illegal_character_replacement', label: 'settings.catalog.illegal_character_replacement.label', surface: 'folder', group: 'settings.groups.formatting',
    desc: 'settings.catalog.illegal_character_replacement.desc' },
  { id: 'title_casing', label: 'settings.catalog.title_casing.label', surface: 'folder', group: 'settings.groups.text',
    desc: 'settings.catalog.title_casing.desc' },
  { id: 'artist_casing', label: 'settings.catalog.artist_casing.label', surface: 'folder', group: 'settings.groups.text',
    desc: 'settings.catalog.artist_casing.desc' },
  { id: 'featured_artists_mode', label: 'settings.catalog.featured_artists_mode.label', surface: 'folder', group: 'settings.groups.text',
    desc: 'settings.catalog.featured_artists_mode.desc' },
  { id: 'multi_artist_separator', label: 'settings.catalog.multi_artist_separator.label', surface: 'folder', group: 'settings.groups.text',
    desc: 'settings.catalog.multi_artist_separator.desc' },
  { id: 'remove_duplicate_artists', label: 'settings.catalog.remove_duplicate_artists.label', surface: 'folder', group: 'settings.groups.text',
    desc: 'settings.catalog.remove_duplicate_artists.desc' },
  { id: 'remove_album_version', label: 'settings.catalog.remove_album_version.label', surface: 'folder', group: 'settings.groups.text',
    desc: 'settings.catalog.remove_album_version.desc' },
  { id: 'album_various_artists', label: 'settings.catalog.album_various_artists.label', surface: 'folder', group: 'settings.groups.text',
    desc: 'settings.catalog.album_various_artists.desc' },
  { id: 'whitespace_handling', label: 'settings.catalog.whitespace_handling.label', surface: 'folder', group: 'settings.groups.formatting',
    desc: 'settings.catalog.whitespace_handling.desc' },

  /* ── Themes panel ─────────────────────────────────────────────────────── */
  { id: 'theme', label: 'settings.catalog.theme.label', surface: 'themes', group: 'settings.groups.appearance',
    desc: 'settings.catalog.theme.desc' },
  { id: 'mode', label: 'settings.catalog.mode.label', surface: 'themes', group: 'settings.groups.appearance',
    desc: 'settings.catalog.mode.desc' },
  { id: 'font', label: 'settings.catalog.font.label', surface: 'themes', group: 'settings.groups.appearance',
    desc: 'settings.catalog.font.desc' },
  { id: 'language', label: 'settings.catalog.language.label', surface: 'themes', group: 'settings.groups.appearance',
    desc: 'settings.catalog.language.desc' },
  { id: 'notify_sound', label: 'settings.catalog.notify_sound.label', surface: 'themes', group: 'settings.groups.notification_sound',
    desc: 'settings.catalog.notify_sound.desc' },
  { id: 'notify_volume', label: 'settings.catalog.notify_volume.label', surface: 'themes', group: 'settings.groups.notification_sound',
    desc: 'settings.catalog.notify_volume.desc', requires: 'notify_sound' },
  { id: 'notify_batch_only', label: 'settings.catalog.notify_batch_only.label', surface: 'themes', group: 'settings.groups.notification_sound',
    desc: 'settings.catalog.notify_batch_only.desc', requires: 'notify_sound' },
  { id: 'preview_volume', label: 'settings.catalog.preview_volume.label', surface: 'themes', group: 'settings.groups.previews',
    desc: 'settings.catalog.preview_volume.desc' },
];
