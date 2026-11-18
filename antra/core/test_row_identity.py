"""issue #74 - the UI reported a download failure against the wrong song.

Two independent fixes are pinned here.

FIX 1 - row identity. Every track carries a `row_id`, stamped once before the
tracklist is emitted to the webview, and the event envelope carries it back. The
webview keys rows by that id instead of by `track_index`, which is a position in
a list it was never shown.

FIX 2 - a playlist is downloaded in the order it was fetched in.
`_stamp_disc_totals` groups by album, which sequences an ALBUM correctly and
silently permutes a PLAYLIST.

The assertions below are about the mechanism and the observed effect - which
track a status lands on - never about a comment or a string this change could
itself contain. Several carry a control that has to come out the other way:
a fix that quietly stopped reordering albums, or a row-id scheme that only
looked right because nothing was permuted, would fail those.

Run: python antra/core/test_row_identity.py
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout
from dataclasses import asdict, replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core.events import EngineEvent, EngineEventType          # noqa: E402
from antra.core.models import TrackMetadata                          # noqa: E402
from antra.core.service import AntraService                          # noqa: E402
import antra.json_cli as jc                                          # noqa: E402

FAILS = []
COUNT = 0


def check(label, cond):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(label)


def mk(title, album, album_id, number, kind, disc=1, year=None):
    track = TrackMetadata(title=title, artists=["Artist"], album=album)
    track.album_id = album_id
    track.track_number = number
    track.disc_number = disc
    track.request_kind = kind
    track.source_service = "spotify"
    track.release_year = year
    return track


def titles(tracks):
    return [t.title for t in tracks]


# The reported shape: a playlist whose tracks come from several albums, not
# grouped together. Two tracks of "Album A" sit either side of a track from
# "Album B", which is what makes the grouping permute the list.
def interleaved_playlist():
    return [
        mk("A1", "Album A", "AAA", 1, "playlist"),
        mk("B1", "Album B", "BBB", 1, "playlist"),
        mk("A2", "Album A", "AAA", 2, "playlist"),
        mk("C1", "Album C", "CCC", 1, "playlist"),
        mk("B2", "Album B", "BBB", 2, "playlist"),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# FIX 2 - ordering
# ─────────────────────────────────────────────────────────────────────────────

def test_playlist_order_is_preserved():
    tracks = interleaved_playlist()
    before = titles(tracks)
    after = titles(AntraService._stamp_disc_totals(tracks))
    check("playlist keeps the order it was fetched in", after == before)
    check("no track is lost or duplicated by the restore", sorted(after) == sorted(before))

    # The reported permutation, spelled out: before the fix index 2 of the
    # download list was "A2" while the webview's row 2 said "B1".
    check("row 2 and download 2 are the same song", after[1] == before[1] == "B1")
    check("row 3 and download 3 are the same song", after[2] == before[2] == "A2")


def test_playlist_order_survives_a_long_run_of_one_album():
    # A B A B C A - the shape that moved five of six rows before the fix.
    tracks = [mk(t, al, al, 1, "playlist")
              for t, al in [("t1", "A"), ("t2", "B"), ("t3", "A"),
                            ("t4", "B"), ("t5", "C"), ("t6", "A")]]
    before = titles(tracks)
    check("repeated albums do not pull their tracks together",
          titles(AntraService._stamp_disc_totals(tracks)) == before)


def test_album_is_still_sequenced():
    # CONTROL, and it must come out the OTHER way. An album request is
    # deliberately reordered - the filename numbering is derived from that
    # sequence - so a fix that simply stopped reordering everything would be
    # wrong, and this is what catches it.
    tracks = [mk("T3", "A", "AAA", 3, "album"),
              mk("T1", "A", "AAA", 1, "album"),
              mk("T2", "A", "AAA", 2, "album")]
    after = titles(AntraService._stamp_disc_totals(tracks))
    check("an out-of-order album is still sequenced by track number",
          after == ["T1", "T2", "T3"])
    check("the album control really was permuted (so it is not a no-op test)",
          after != ["T3", "T1", "T2"])


def test_playlist_still_gets_its_normalisation():
    # Only the SEQUENCE is restored. The per-group work - year normalisation,
    # disc totals - must still be applied, or the fix has thrown away the
    # reason _stamp_disc_totals exists.
    tracks = [
        mk("A1", "Album A", "AAA", 1, "playlist", disc=1, year=2001),
        mk("B1", "Album B", "BBB", 1, "playlist", disc=1, year=1999),
        mk("A2", "Album A", "AAA", 2, "playlist", disc=2, year=2004),
    ]
    out = AntraService._stamp_disc_totals(tracks)
    by_title = {t.title: t for t in out}
    check("year is normalised across an album group even in playlist order",
          by_title["A1"].release_year == by_title["A2"].release_year)
    check("total_discs is stamped on a multi-disc group",
          by_title["A1"].total_discs == 2 and by_title["A2"].total_discs == 2)
    check("normalisation did not reorder the playlist", titles(out) == ["A1", "B1", "A2"])


def test_mixed_kinds_do_not_lose_tracks():
    # Not a shape json_cli produces (one URL per call), but the ordering branch
    # keys off "any track is a playlist", so pin that it is total.
    tracks = [mk("P1", "A", "AAA", 2, "playlist"),
              mk("P2", "A", "AAA", 1, "playlist"),
              mk("X", "B", "BBB", 1, "album")]
    out = AntraService._stamp_disc_totals(tracks)
    check("mixed input returns every track exactly once",
          sorted(titles(out)) == ["P1", "P2", "X"])
    check("mixed input containing a playlist keeps the input order",
          titles(out) == ["P1", "P2", "X"])


# ─────────────────────────────────────────────────────────────────────────────
# FIX 1 - row identity
# ─────────────────────────────────────────────────────────────────────────────

def test_stamping_is_unique_and_idempotent():
    tracks = interleaved_playlist()
    jc._stamp_row_ids(tracks)
    ids = [t.row_id for t in tracks]
    check("every track is stamped", all(ids))
    check("ids are distinct", len(set(ids)) == len(ids))

    first = list(ids)
    extra = mk("D1", "Album D", "DDD", 1, "playlist")
    jc._stamp_row_ids(tracks + [extra])
    check("re-stamping leaves existing ids alone", [t.row_id for t in tracks] == first)
    check("a track added later is stamped", bool(extra.row_id))
    check("the later id collides with none of the first", extra.row_id not in first)


def test_stamping_tolerates_a_non_track():
    # The progressive page callback hands this whatever the fetcher produced.
    # It must not take a download down.
    jc._stamp_row_ids([object()])
    jc._stamp_row_ids([])
    jc._stamp_row_ids(None)
    check("stamping a non-track does not raise", True)


def test_identity_survives_the_reorder_that_breaks_indices():
    # THE test. An ALBUM request, because that is the case fix 2 deliberately
    # does not cover: the list still gets permuted, so only the row id can hold
    # the correlation together.
    tracks = [mk("T3", "A", "AAA", 3, "album"),
              mk("T1", "A", "AAA", 1, "album"),
              mk("T2", "A", "AAA", 2, "album")]
    jc._stamp_row_ids(tracks)

    # What the webview is given, in the order it is given it.
    rows = {t.row_id: t.title for t in tracks}
    by_index = {i + 1: t.title for i, t in enumerate(tracks)}

    ordered = AntraService._stamp_disc_totals(tracks)
    check("the list really was permuted (else this test proves nothing)",
          titles(ordered) != ["T3", "T1", "T2"])

    id_hits = sum(1 for t in ordered if rows.get(t.row_id) == t.title)
    index_hits = sum(1 for i, t in enumerate(ordered, start=1)
                     if by_index.get(i) == t.title)
    check("every event addressed by row id lands on its own row",
          id_hits == len(ordered))
    check("CONTROL: addressing by index does NOT land on its own row",
          index_hits < len(ordered))


def test_identity_survives_preserve_track_identity():
    # The Amazon / Apple paths hand _preserve_track_identity objects the
    # enricher rebuilt. Anything not copied there is lost.
    original = mk("T1", "A", "AAA", 1, "album")
    original.row_id = "abc-1"
    rebuilt = mk("T1", "A", "AAA", 1, "album")
    check("the rebuilt object starts with no id", rebuilt.row_id is None)
    out = AntraService._preserve_track_identity([original], [rebuilt])
    check("_preserve_track_identity carries row_id across", out[0].row_id == "abc-1")


def test_identity_survives_a_dataclass_copy():
    # _hydrate_track_from_spotify_search merges via dataclasses.replace().
    original = mk("T1", "A", "AAA", 1, "album")
    original.row_id = "abc-9"
    check("replace() copies row_id", replace(original).row_id == "abc-9")


def test_identity_round_trips_through_the_preview_cache():
    # PV-1 writes asdict(track) to disk in one process and rebuilds it with
    # _track_from_payload in another. The id has to make that trip or the
    # selection job addresses rows the preview screen never created.
    original = mk("T1", "A", "AAA", 1, "album")
    jc._stamp_row_ids([original])
    restored = jc._track_from_payload(json.loads(json.dumps(asdict(original))))
    check("row_id survives the preview cache round trip",
          restored.row_id == original.row_id and bool(restored.row_id))


def test_verify_clears_everything_when_identity_is_damaged():
    good = interleaved_playlist()
    jc._stamp_row_ids(good)
    buf = io.StringIO()
    with redirect_stdout(buf):
        jc._verify_row_ids(good)
    check("intact ids are left alone", all(t.row_id for t in good))
    check("intact ids log nothing", buf.getvalue().strip() == "")

    # One id lost. The failure mode being guarded against is a PARTIAL loss:
    # some rows keyed by id, the rest by index, i.e. a half-correct display.
    partial = interleaved_playlist()
    jc._stamp_row_ids(partial)
    partial[2].row_id = None
    buf = io.StringIO()
    with redirect_stdout(buf):
        jc._verify_row_ids(partial)
    check("one missing id clears them ALL, not just the gap",
          all(t.row_id is None for t in partial))
    lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    check("the fallback is reported to the user",
          any(l.get("type") == "log" and l.get("level") == "warning" for l in lines))

    dupes = interleaved_playlist()
    jc._stamp_row_ids(dupes)
    dupes[1].row_id = dupes[0].row_id
    with redirect_stdout(io.StringIO()):
        jc._verify_row_ids(dupes)
    check("duplicated ids also fall back", all(t.row_id is None for t in dupes))

    with redirect_stdout(io.StringIO()):
        jc._verify_row_ids([])
    check("an empty list is not an error", True)


def test_event_envelope_carries_the_identity():
    track = mk("T1", "A", "AAA", 1, "album")
    jc._stamp_row_ids([track])
    buf = io.StringIO()
    with redirect_stdout(buf):
        jc.emit_event(EngineEvent(
            type=EngineEventType.TRACK_FAILED,
            track=track,
            track_index=1,
            track_total=1,
            error="nope",
        ))
    payload = json.loads(buf.getvalue().strip())["payload"]
    check("the event carries row_id at the top level",
          payload.get("row_id") == track.row_id)
    check("the event also carries it inside track_data",
          (payload.get("track_data") or {}).get("row_id") == track.row_id)
    check("track_index is still sent (the fallback needs it)",
          payload.get("track_index") == 1)

    buf = io.StringIO()
    with redirect_stdout(buf):
        jc.emit_event(EngineEvent(type=EngineEventType.NOTICE, message="hi"))
    check("a trackless event has a null row_id rather than raising",
          json.loads(buf.getvalue().strip())["payload"]["row_id"] is None)


def test_end_to_end_a_failure_lands_on_its_own_row():
    """The user-visible claim, over the real ordering code and the real emitter.

    Build a release, hand the webview its tracklist, run the reorder, then fail
    exactly one track and see which row the webview would light up.
    """
    tracks = [mk("T3", "A", "AAA", 3, "album"),
              mk("T1", "A", "AAA", 1, "album"),
              mk("T2", "A", "AAA", 2, "album")]
    jc._stamp_row_ids(tracks)
    # What the webview prepopulates, exactly as json_cli emits it.
    ui_rows = {t.row_id: t.title for t in tracks}
    ui_by_index = {i + 1: t.title for i, t in enumerate(tracks)}

    ordered = AntraService._stamp_disc_totals(tracks)
    victim_position = 2                       # second track of the DOWNLOAD list
    victim = ordered[victim_position - 1]

    buf = io.StringIO()
    with redirect_stdout(buf):
        jc.emit_event(EngineEvent(
            type=EngineEventType.TRACK_FAILED, track=victim,
            track_index=victim_position, track_total=len(ordered),
            error="All sources failed",
        ))
    payload = json.loads(buf.getvalue().strip())["payload"]

    check("the failure lights up the row holding the song that failed",
          ui_rows[payload["row_id"]] == victim.title)
    check("the log line names that same song", payload["track"] == victim.title)
    check("CONTROL: the old index route would have named a different song",
          ui_by_index[payload["track_index"]] != victim.title)


def test_a_retry_reports_against_its_own_row():
    """The trigger that needs no reordering at all, and fires on every retry.

    A single-track retry is its own job: the engine numbers it 1 of 1. The
    webview's index map still holds the PREVIOUS job's rows - nothing clears it
    on a normal finish - so index 1 resolved to the first row of the last
    playlist. Every retry therefore reported its result against a song that had
    downloaded perfectly, while the track actually being retried sat on
    "Retrying..." for ever. Measured against the real CLI: retrying track 2 of a
    release emits track_index=1.
    """
    release = [mk("First", "A", "AAA", 1, "album"),
               mk("Second", "A", "AAA", 2, "album"),
               mk("Third", "A", "AAA", 3, "album")]
    jc._stamp_row_ids(release)
    ui_rows = {t.row_id: t.title for t in release}
    ui_by_index = {i + 1: t.title for i, t in enumerate(release)}

    # The webview hands the backend the track_data it received for that row.
    retried = jc._track_from_payload(json.loads(json.dumps(asdict(release[1]))))
    check("the retry payload carries the row it came from",
          retried.row_id == release[1].row_id)

    buf = io.StringIO()
    with redirect_stdout(buf):
        jc.emit_event(EngineEvent(
            type=EngineEventType.TRACK_FAILED, track=retried,
            track_index=1, track_total=1, error="still failing"))
    payload = json.loads(buf.getvalue().strip())["payload"]

    check("a retry really does renumber itself to 1 of 1",
          payload["track_index"] == 1)
    check("the retry lands on the row it was started from",
          ui_rows[payload["row_id"]] == "Second")
    check("CONTROL: the old index route lands on the previous job's first row",
          ui_by_index[payload["track_index"]] == "First")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"{COUNT - len(FAILS)}/{COUNT} checks passed")
    for f in FAILS:
        print(f"  FAIL: {f}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
