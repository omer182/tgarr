"""Newznab-compatible indexer over the linked Telegram channels.

Sonarr (through Prowlarr's "Generic Newznab") searches here. Only sources linked to a series answer, from an
in-memory index that is refreshed in the background, so a Sonarr search never triggers a Telegram scan.
Releases are named in Sonarr's numbering (see map_files) and download as tiny NZB files that carry a
reference back to the Telegram message; the SABnzbd-compatible API in sab.py turns that into a download.
"""
import asyncio
import base64
import json
import logging
import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape, quoteattr

from . import arr, config, store, tg
from .parse import safe

log = logging.getLogger("tgarr.indexer")

# source_id -> {"at": ts, "releases": [...], "error": str|None, "series": str}
index: dict[str, dict] = {}
ASKED_FILE = config.CONFIG_DIR / "asked.json"
INDEX_FILE = config.CONFIG_DIR / "index.json"  # the index survives restarts: no window where searches find nothing


def load_index():
    try:
        if INDEX_FILE.exists():
            index.update(json.loads(INDEX_FILE.read_text()))
            log.info("loaded index from disk: %d sources, %d releases", len(index), len(all_releases()))
    except Exception as e:
        log.warning("could not load saved index: %s", e)


def save_index():
    tmp = INDEX_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False))
    tmp.replace(INDEX_FILE)
_asked: dict[str, float] | None = None
_series: dict[int, tuple[float, dict]] = {}


# ---------- series info ----------

async def series_info(tvdb_id: int) -> dict:
    """Title, alternate titles and poster as Sonarr knows them (the release name must parse back to this series)."""
    hit = _series.get(tvdb_id)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    found = await arr.sonarr.req("GET", "/series", params={"tvdbId": tvdb_id})
    item = found[0] if found else (await arr.sonarr.req("GET", "/series/lookup", params={"term": f"tvdb:{tvdb_id}"}))[0]
    info = {"tvdbId": tvdb_id, "title": item["title"], "year": item.get("year"), "inLibrary": bool(found),
            # [title, season]: an alternate title such as "Spy x Family S3" belongs to one season (None = whole series)
            "titles": [[item["title"], None]] + [
                [a["title"], next((n for n in (a.get("sceneSeasonNumber"), a.get("seasonNumber")) if n and n > 0), None)]
                for a in item.get("alternateTitles", []) if a.get("title")],
            "poster": next((i.get("remoteUrl") for i in item.get("images", []) if i["coverType"] == "poster"), None)}
    _series[tvdb_id] = (time.time(), info)
    return info


# ---------- channel numbering -> Sonarr numbering (same rules as the web UI) ----------

def numbering_mode(src: dict, sizes: dict, count: dict) -> str:
    mode = src.get("numbering") or "auto"
    if mode != "auto":
        return mode
    # only a channel season with MORE episodes than Sonarr's proves different season boundaries
    return "continuous" if any(v > count.get(int(k), 0) for k, v in sizes.items()) else "posted"


def map_files(src: dict, files: list[dict], eps: list[dict]) -> list[dict]:
    """One best video per Sonarr episode: [{file, s, e, abs, ep_title}]. Extras, subtitles, archives and
    files without an episode number are left out."""
    by_se = {(e["s"], e["e"]): e for e in eps}
    by_abs = {e["abs"]: e for e in eps}
    count: dict[int, int] = {}
    for e in eps:
        count[e["s"]] = count.get(e["s"], 0) + 1
    sizes = src.get("season_sizes") or {}
    mode = numbering_mode(src, sizes, count)
    best: dict[tuple, dict] = {}
    for f in files:
        if f["kind"] != "video" or f.get("extra") or f["episode"] is None:
            continue
        ps = f["season"] if f["season"] is not None else 1
        if mode == "continuous":
            off = sum(v for k, v in sizes.items() if int(k) < ps)
            ep = by_abs.get(off + f["episode"])
        else:
            ep = by_se.get((ps, f["episode"]))
        if not ep:
            continue
        key = (ep["s"], ep["e"])
        cur = best.get(key)
        rank = (f.get("height") or 0, f.get("size") or 0)
        if not cur or rank > (cur["file"].get("height") or 0, cur["file"].get("size") or 0):
            best[key] = {"file": f, "s": ep["s"], "e": ep["e"], "abs": ep["abs"], "ep_title": ep.get("title")}
    return [best[k] for k in sorted(best)]


def release_name(series_title: str, s: int, e: int, f: dict) -> str:
    h = f.get("height")
    res = min((2160, 1080, 720, 480), key=lambda r: abs(r - h)) if h else (1080 if (f.get("size") or 0) > 6e8 else 720)
    return f"{safe(series_title)}.S{s:02}E{e:02}.{res}p.WEB-DL.HebSub-TG"


# ---------- index ----------

async def refresh_source(src: dict):
    """Scan one linked channel and rebuild its releases."""
    info = await series_info(src["tvdbId"])
    files = await tg.list_files(src, None, None)  # full scan; also refreshes src["season_sizes"]
    store.update_source(src)
    eps = await arr.tvdb_episodes(src["tvdbId"])
    rel = []
    for m in map_files(src, files, eps):
        f = m["file"]
        ext = "." + f["name"].rsplit(".", 1)[-1].lower() if "." in f["name"] else ".mkv"
        rel.append({"guid": f"tgarr-{src['id']}-{f['msg_id']}", "source_id": src["id"], "msg_id": f["msg_id"],
                    "tvdb": src["tvdbId"], "series": info["title"], "poster": info.get("poster"),
                    "title": release_name(info["title"], m["s"], m["e"], f), "season": m["s"], "episode": m["e"],
                    "abs": m["abs"], "size": f["size"] or 0, "height": f.get("height"), "ext": ext, "name": f["name"],
                    "posted": datetime.fromisoformat(f["date"]).timestamp()})
    index[src["id"]] = {"at": time.time(), "releases": rel, "error": None, "series": info["title"],
                        "titles": info["titles"], "tvdb": src["tvdbId"]}


def linked() -> list[dict]:
    return [s for s in store.sources() if s.get("tvdbId") and s.get("joined", True)]


async def refresh(only: str | None = None):
    for src in linked():
        if only and src["id"] != only:
            continue
        try:
            await refresh_source(src)
        except Exception as e:
            log.warning("index refresh failed for %s: %s", src.get("title"), e)
            index.setdefault(src["id"], {"at": 0, "releases": [], "series": src.get("series_title", "")})["error"] = str(e)
        await asyncio.sleep(2)  # be gentle with Telegram
    for sid in [k for k in index if not any(s["id"] == k for s in linked())]:
        index.pop(sid)  # unlinked or removed meanwhile
    save_index()


async def refresher():
    await asyncio.sleep(5)
    while True:
        if tg.state["authorized"]:
            try:
                await refresh()
            except Exception:
                log.exception("index refresh")
        await asyncio.sleep(config.INDEX_REFRESH if index else 30)


def all_releases() -> list[dict]:
    return [r for v in index.values() for r in v["releases"]]


def find_release(source_id: str, msg_id: int) -> dict | None:
    return next((r for r in index.get(source_id, {}).get("releases", []) if r["msg_id"] == msg_id), None)


# ---------- "first asked" stamps (make Sonarr's delay profile count from the request, not the post date) ----------

def asked(key: str, stamp: bool) -> float | None:
    global _asked
    if _asked is None:
        _asked = json.loads(ASKED_FILE.read_text()) if ASKED_FILE.exists() else {}
    if stamp and key not in _asked:
        _asked[key] = time.time()
        ASKED_FILE.write_text(json.dumps(_asked))
    return _asked.get(key)


# ---------- search ----------

def _norm(t: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).split())


def _tail(rest: str) -> dict | None:
    """What follows the series title in a query: "s02e05", "s02", "13" or nothing. None = not understood."""
    if not rest:
        return {}
    if m := re.fullmatch(r"s(\d{1,2}) ?e(\d{1,4})", rest):
        return {"season": int(m[1]), "ep": int(m[2])}
    if m := re.fullmatch(r"s(\d{1,2})", rest):
        return {"season": int(m[1])}
    if rest.isdigit():
        return {"number": int(rest)}
    return None


def search(tvdbid: int | None, q: str, season: int | None, ep: int | None) -> tuple[list[dict], bool]:
    """Releases for a Newznab query, newest first. Second value: True for a real search (vs an RSS poll).

    Sonarr asks in several shapes: tvdbid+season+ep (standard series); for anime series tvdbid with q="13"
    (absolute episode), or q="<title> 13", or q="<season-specific alternate title> 12" (episode 12 of that season).
    """
    rels = all_releases()
    nq = _norm(q)
    number = None       # bare number after the title
    title_season = None  # season implied by the alternate title that matched
    if tvdbid:
        rels = [r for r in rels if r["tvdb"] == tvdbid]
        tail = _tail(nq)
        if tail is None:  # tvdbid plus a title: strip any known title of that series and read what is left
            for v in index.values():
                if v.get("tvdb") != tvdbid:
                    continue
                for t, ts in sorted(v.get("titles", []), key=lambda x: -len(_norm(x[0]))):
                    nt = _norm(t)
                    if nt and (nq == nt or nq.startswith(nt + " ")):
                        tail, title_season = _tail(nq[len(nt):].strip()), ts
                        break
                if tail is not None:
                    break
            if tail is None:
                return [], True
    elif nq:
        hit = None  # the longest series title the query starts with
        for v in index.values():
            for t, ts in v.get("titles", []):
                nt = _norm(t)
                if nt and (nq == nt or nq.startswith(nt + " ")) and (not hit or len(nt) > len(hit[1])):
                    hit = (v["tvdb"], nt, ts)
        if not hit:
            return [], True
        rels = [r for r in rels if r["tvdb"] == hit[0]]
        title_season = hit[2]
        tail = _tail(nq[len(hit[1]):].strip())
        if tail is None:
            return [], True
    else:
        tail = {}
    is_search = bool(tvdbid or nq)
    season = tail.get("season", season)
    ep = tail.get("ep", ep)
    number = tail.get("number")
    if number is not None:
        if title_season:  # "Spy x Family S3 12" -> season 3, episode 12
            season, ep = title_season, number
        elif season is not None and ep is None:
            ep = number
        else:  # "Title 13" / tvdbid + "13" -> absolute episode 13
            rels = [r for r in rels if r["abs"] == number]
    elif title_season and season is None:
        season = title_season
    if season is not None:
        rels = [r for r in rels if r["season"] == season]
    if ep is not None:
        rels = [r for r in rels if r["episode"] == ep]
    return sorted(rels, key=lambda r: r["posted"], reverse=True), is_search


# ---------- XML ----------

NS = 'xmlns:atom="http://www.w3.org/2005/Atom" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"'


def caps_xml() -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<caps>'
            '<server version="1.0" title="tgarr" strapline="Telegram channels as a Newznab indexer"/>'
            '<limits max="100" default="100"/><registration available="no" open="no"/>'
            '<searching><search available="yes" supportedParams="q"/>'
            '<tv-search available="yes" supportedParams="q,season,ep,tvdbid"/>'
            '<movie-search available="no" supportedParams="q"/></searching>'
            '<categories><category id="5000" name="TV"><subcat id="5040" name="HD"/>'
            '<subcat id="5070" name="Anime"/></category></categories></caps>')


def error_xml(code: int, text: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<error code="{code}" description={quoteattr(text)}/>'


def feed_xml(rels: list[dict], base: str, key: str, offset: int, limit: int, is_search: bool) -> str:
    items = []
    for r in rels[offset:offset + limit]:
        # A search stamps "first asked"; the release then looks that fresh to Sonarr, so its Usenet delay
        # gives torrents a head start from the moment of the request rather than from the Telegram post date.
        first = asked(f"{r['tvdb']}-{r['season']}-{r['episode']}", is_search)
        pub = format_datetime(datetime.fromtimestamp(max(r["posted"], first or 0), timezone.utc))
        url = f"{base}/newznab/nzb/{r['source_id']}/{r['msg_id']}.nzb?apikey={key}"
        items.append(
            f"<item><title>{escape(r['title'])}</title><guid isPermaLink=\"false\">{r['guid']}</guid>"
            f"<link>{escape(url)}</link><pubDate>{pub}</pubDate><category>TV &gt; Anime</category>"
            f"<description>{escape(r['name'])}</description>"
            f"<enclosure url={quoteattr(url)} length=\"{r['size']}\" type=\"application/x-nzb\"/>"
            f"<newznab:attr name=\"category\" value=\"5000\"/><newznab:attr name=\"category\" value=\"5070\"/>"
            f"<newznab:attr name=\"size\" value=\"{r['size']}\"/><newznab:attr name=\"tvdbid\" value=\"{r['tvdb']}\"/>"
            f"<newznab:attr name=\"season\" value=\"{r['season']}\"/><newznab:attr name=\"episode\" value=\"{r['episode']}\"/>"
            f"</item>")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" {NS}><channel><title>tgarr</title>'
            f'<description>Telegram channels</description><link>{escape(base)}</link>'
            f'<newznab:response offset="{offset}" total="{len(rels)}"/>{"".join(items)}</channel></rss>')


def nzb_xml(r: dict) -> str:
    """A syntactically valid NZB whose only job is to carry the Telegram reference to the download client."""
    ref = base64.b64encode(json.dumps({k: r[k] for k in (
        "source_id", "msg_id", "tvdb", "series", "poster", "title", "season", "episode", "size", "height", "ext",
        "name")}).encode()).decode()
    subj = escape(f'{r["title"]}{r["ext"]} (1/1)')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">\n'
            '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
            f'<head><meta type="title">{escape(r["title"])}</meta><meta type="tgarr">{ref}</meta></head>'
            f'<file poster="tgarr" date="{int(r["posted"])}" subject="{subj}"><groups><group>alt.binaries.tgarr</group></groups>'
            f'<segments><segment bytes="{r["size"]}" number="1">{r["guid"]}@tgarr</segment></segments></file></nzb>')


def parse_nzb(body: bytes) -> dict | None:
    m = re.search(rb'<meta type="tgarr">([A-Za-z0-9+/=]+)</meta>', body)
    return json.loads(base64.b64decode(m[1])) if m else None
