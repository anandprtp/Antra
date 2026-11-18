"""Word-level lyrics handling + the lrc.red / LyricsPlus / boidu providers (offline, stubbed HTTP)."""
import json
from types import SimpleNamespace

from antra.utils import lyrics as L

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)
        print("FAIL", name)


# --- word tags are flattened, line stamps kept
enh = "[00:27.40]<00:27.40>I <00:27.55>been <00:27.74>tryna <00:28.08>call <00:28.96>\n[00:30.19]<00:30.19>Hey"
flat = L.strip_word_tags(enh)
check("tags stripped", "<" not in flat)
check("line text intact", flat.splitlines()[0] == "[00:27.40]I been tryna call")
check("plain lrc untouched", L.strip_word_tags("[00:01.00]a") == "[00:01.00]a")

# --- TTML -> LRC
ttml = ('<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
        '<p begin="27.395" end="28.9"><span begin="27.395">I</span> <span begin="27.5">been</span></p>'
        '<p begin="1:02.5" end="1:04"><span>Two</span></p><p begin="3.0" end="4"></p></div></body></tt>')
plain, lrc = L._lines_to_lrc(L.ttml_to_lines(ttml))
check("ttml first line", lrc.splitlines()[0] == "[00:27.40]I been")
check("ttml mm:ss.f", "[01:02.50]Two" in lrc)
check("ttml empty p dropped", lrc.count("\n") == 1 and plain == "I been\nTwo")


class R:
    def __init__(self, status, body):
        self.status_code, self._b = status, body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return self._b


def fetcher(routes):
    f = L.LyricsFetcher(use_cache=False)
    f.session = SimpleNamespace(get=lambda url, **kw: routes(url, kw))
    return f


trk = SimpleNamespace(title="Blinding Lights", primary_artist="The Weeknd", isrc="USUG11904206",
                      duration_seconds=200, duration_ms=200000, album="", spotify_id="x")

# lrc.red
lrcred = "[ti:Blinding Lights]\n[00:27.40]<00:27.40>I <00:27.55>been\n[00:30.19]<00:30.19>Hey"
f = fetcher(lambda u, k: R(200, lrcred))
plain, synced = f._fetch_lrcred(trk)
check("lrcred synced flat", "<" not in synced)
check("lrcred plain has no header/stamps", plain == "I been\nHey")
check("lrcred 404 -> None", fetcher(lambda u, k: R(404, "nope"))._fetch_lrcred(trk) is None)
check("lrcred SPA html -> None", fetcher(lambda u, k: R(200, "<!doctype html>"))._fetch_lrcred(trk) is None)

# LyricsPlus
lp = {"metadata": {"title": "Blinding Lights"},
      "lyrics": [{"time": 27395, "text": "I been tryna call"}, {"time": 30189, "text": ""},
                 {"time": 31000, "text": "Hey"}]}
plain, synced = fetcher(lambda u, k: R(200, lp))._fetch_lyricsplus(trk)
check("lyricsplus lines", synced == "[00:27.40]I been tryna call\n[00:31.00]Hey")
lp_wrong = dict(lp, metadata={"title": "Completely Different Song"})
check("lyricsplus wrong song rejected", fetcher(lambda u, k: R(200, lp_wrong))._fetch_lyricsplus(trk) is None)
hosts = []
f = fetcher(lambda u, k: (hosts.append(u), R(404, {}))[1])
check("lyricsplus miss -> None", f._fetch_lyricsplus(trk) is None and len(hosts) == 2)

# boidu
plain, synced = fetcher(lambda u, k: R(200, {"ttml": ttml}))._fetch_boidu(trk)
check("boidu ttml", synced.startswith("[00:27.40]I been"))
check("boidu 401 -> None", fetcher(lambda u, k: R(401, {}))._fetch_boidu(trk) is None)

# chain: paxsenix gone, new providers present, nothing hits the dead host
names = [n for n, _ in L.LyricsFetcher(use_cache=False)._providers(trk)]
check("paxsenix removed", "paxsenix" not in names)
check("new providers in chain", {"lrcred", "lyricsplus", "boidu"} <= set(names))
check("no isrc -> no lrcred", "lrcred" not in [n for n, _ in L.LyricsFetcher(use_cache=False)._providers(
    SimpleNamespace(**dict(vars(trk), isrc=None)))])

# end to end: a word-level hit leaves fetch() line-level
f = fetcher(lambda u, k: R(200, lrcred + "\n[00:40.00]<00:40.00>One\n") if "lrc.red" in u else R(404, {}))
f._providers = lambda t: [("lrcred", lambda: f._fetch_lrcred(t))]
plain, synced = f.fetch(trk)
check("fetch() returns line-level", synced and "<" not in synced)

print("FAILED:" if fails else "ok", fails or "")
raise SystemExit(1 if fails else 0)
