"""Catalogue built from "directory" channels: channels that post one invite link per series, usually captioned
with the show's name in two languages ("Spy X Family - משפחה חשאית"). It gives reliable English titles for
channels whose own names are not in English, which is what series matching needs.
"""
import json
import logging
import re
import time

from . import config, tg

log = logging.getLogger("tgarr.catalog")
DIRS_FILE = config.CONFIG_DIR / "directories.json"
_cache = {"at": 0.0, "entries": []}
LATIN = re.compile(r"[A-Za-z]{3}")
HEBREW = re.compile(r"[֐-׿]{2}")


def directories() -> list[str]:
    return json.loads(DIRS_FILE.read_text()) if DIRS_FILE.exists() else []


def set_directories(items: list[str]):
    DIRS_FILE.write_text(json.dumps(items, ensure_ascii=False))
    _cache["at"] = 0


def split_title(text: str) -> tuple[str | None, list[str]]:
    """("Spy X Family", ["משפחה חשאית"]) from the first caption line that isn't a link."""
    line = next((l.strip() for l in (text or "").splitlines() if l.strip() and "t.me/" not in l), "")
    parts = [p.strip(" -–|:") for p in re.split(r"\s*[/\\|]\s*|\s+[-–]\s+", line)]
    latin = [p for p in parts if LATIN.search(p) and not HEBREW.search(p)]
    return (max(latin, key=len) if latin else None), [p for p in parts if HEBREW.search(p) and len(p) >= 3]


async def entries(force: bool = False) -> list[dict]:
    if not force and _cache["entries"] and time.time() - _cache["at"] < 3600:
        return _cache["entries"]
    out = []
    for d in directories():
        try:
            for l in await tg.channel_links(int(d) if d.lstrip("-").isdigit() else d):
                if l["kind"] == "post":
                    continue
                en, he = split_title(l["text"])
                if en or he:
                    out.append({"url": l["url"], "en": en, "he": he, "directory": d, "date": l["date"]})
        except Exception as e:
            log.warning("directory %s: %s", d, e)
    if out or not _cache["entries"]:
        _cache.update(at=time.time(), entries=out)
    return _cache["entries"]


# ---------- hands-off mode: settings, demand, join budget ----------

SETTINGS_FILE = config.CONFIG_DIR / "settings.json"
STATE_FILE = config.CONFIG_DIR / "auto_state.json"
DEFAULTS = {"auto_join": True, "max_joins_per_day": 5, "fallback_minutes": 30}
_state: dict | None = None


def settings() -> dict:
    saved = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save_settings(new: dict) -> dict:
    cur = {**settings(), **{k: v for k, v in new.items() if k in DEFAULTS and v is not None}}
    SETTINGS_FILE.write_text(json.dumps(cur))
    return cur


def state() -> dict:
    """{"demand": {tvdb: first time Sonarr searched for it}, "joins": [timestamps], "log": [recent actions]}"""
    global _state
    if _state is None:
        _state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        for k, v in (("demand", {}), ("joins", []), ("log", [])):
            _state.setdefault(k, v)
    return _state


def _save_state():
    STATE_FILE.write_text(json.dumps(state(), ensure_ascii=False))


def note_demand(tvdb_id: int):
    """Called for every real Sonarr search: the series it asked about is one the user wants."""
    d = state()["demand"]
    if str(tvdb_id) not in d:
        d[str(tvdb_id)] = time.time()
        _save_state()


def _log(text: str):
    st = state()
    st["log"] = ([{"at": time.time(), "text": text}] + st["log"])[:30]
    _save_state()
    log.info(text)


def joins_left() -> int:
    st = state()
    st["joins"] = [t for t in st["joins"] if time.time() - t < 86400]
    return max(0, settings()["max_joins_per_day"] - len(st["joins"]))


# ---------- requested series <-> catalogue ----------

async def get_it(tvdb_id: int, search: bool = True) -> dict:
    """Make a wanted series available to Sonarr from its directory channel: add the channel as a source,
    join it if needed (only ever called from the user's "Join & get it" button), link it, index it and,
    when its episodes map onto Sonarr's, ask Sonarr to search the missing ones."""
    from . import arr, indexer, store
    w = next((x for x in await wanted() if x["tvdbId"] == tvdb_id), None)
    if not w:
        raise RuntimeError("That series isn't waiting for episodes, or no directory channel carries it")
    src = store.get_source(w["source_id"]) if w["source_id"] else None
    if not src:
        info = await tg.resolve(w["entry"]["url"])
        src = store.add_source(w["entry"]["url"], "tv", "", info)
    if not src.get("joined"):
        src.update(await tg.join(src["link"]))
        store.update_source(src)
        state()["joins"].append(time.time())
        _save_state()
        if not src.get("joined"):
            raise RuntimeError("Telegram didn't let the account in yet (the channel may need an admin to approve the request)")
    if src.get("tvdbId") != tvdb_id:
        s_info = await indexer.series_info(tvdb_id)
        store.patch_source(src["id"], {"tvdbId": tvdb_id, "series_title": s_info["title"], "poster": s_info.get("poster"),
                                       "linked_at": time.time()})
    await indexer.refresh(src["id"])
    n = len(indexer.index.get(src["id"], {}).get("releases", []))
    if not n:
        store.patch_source(src["id"], {"tvdbId": None, "series_title": None, "poster": None, "linked_at": None})
        indexer.index.pop(src["id"], None)
        raise RuntimeError("Joined, but its files couldn't be matched to Sonarr's episodes. Open the channel in Find to check the numbering.")
    if search:
        await arr.sonarr.req("POST", "/command", json={"name": "SeriesSearch", "seriesId": w["seriesId"]})
        store.patch_source(src["id"], {"searched_at": time.time()})
    return {"source_id": src["id"], "releases": n, "missing": w["missing"], "searching": search, "title": w["title"]}


_last_join = 0.0
_failed: dict[int, float] = {}  # tvdb -> when the last automatic attempt failed (retry after an hour)


async def _sonarr_busy(series_id: int) -> bool:
    """Is Sonarr already downloading (or holding a delayed release for) this series? Then a tracker had it."""
    from . import arr
    q = await arr.sonarr.req("GET", "/queue", params={"seriesIds": series_id, "pageSize": 1})
    return (q.get("totalRecords") or 0) > 0


async def _sonarr_busy(series_id: int) -> bool:
    """Is Sonarr already downloading (or holding a delayed release for) this series? Then a tracker had it."""
    from . import arr
    q = await arr.sonarr.req("GET", "/queue", params={"seriesIds": series_id, "pageSize": 1})
    return (q.get("totalRecords") or 0) > 0


async def auto_process():
    """The hands-off pass, run every minute. Nothing here waits for the user.
    1. A wanted series whose directory channel is already joined links itself.
    2. Fallback: once the fallback delay since the request has passed, a series that is STILL missing episodes and
       has nothing in Sonarr's queue (the trackers came up empty) gets its directory channel joined and linked,
       within the daily join budget and never more than one join a minute. A series the trackers covered is left
       alone and no channel is joined for it.
    3. A channel that became available after Sonarr's own search is followed by one Sonarr search, also only
       after the fallback delay (an API-triggered search skips Sonarr's delay profile, so tgarr keeps the
       torrent head start itself)."""
    global _last_join
    from . import arr, store
    cfg = settings()
    for w in await wanted():
        tv = w["tvdbId"]
        demand = w["demand"]
        ready = bool(demand) and time.time() >= demand + cfg["fallback_minutes"] * 60
        if time.time() - _failed.get(tv, 0) < 3600:
            continue
        try:
            if not w["linked"] and w["joined"]:
                r = await get_it(tv, search=False)
                _log(f"Linked {w['title']} to its channel ({r['releases']} episodes)")
            elif not w["linked"] and cfg["auto_join"] and ready and w["entry"]:
                if joins_left() <= 0 or time.time() - _last_join < 60:
                    continue
                if await _sonarr_busy(w["seriesId"]):
                    continue  # a tracker release is downloading: look again next minute, join only if it stays missing
                _last_join = time.time()
                r = await get_it(tv, search=False)
                _log(f"Trackers had nothing for {w['title']}: joined its channel and linked it ({r['releases']} episodes)")
        except Exception as e:
            _failed[tv] = time.time()
            _log(f"Could not set up {w['title']}: {e}")
            continue
        # follow-up search, once, for channels Sonarr could not see when it searched
        src = next((x for x in store.sources() if x.get("tvdbId") == tv), None)
        if not src or not demand or src.get("searched_at") or (src.get("linked_at") or 0) <= demand:
            continue
        if ready:
            await arr.sonarr.req("POST", "/command", json={"name": "SeriesSearch", "seriesId": w["seriesId"]})
            store.patch_source(src["id"], {"searched_at": time.time()})
            _log(f"Asked Sonarr to search {w['title']} again now that its channel is available")


async def auto_loop():
    import asyncio
    await asyncio.sleep(20)
    while True:
        if tg.state["authorized"]:
            try:
                await auto_process()
            except Exception:
                log.exception("auto_process")
        await asyncio.sleep(60)

def _norm(t: str) -> str:
    t = re.sub(r"\(\d{4}\)", " ", t or "")  # "Hunter x Hunter (2011)"
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t.lower()).split())


def _demand(series: dict) -> float | None:
    """When the user asked for this series: the first Sonarr search tgarr saw for it, or, failing that, the
    moment it was added to Sonarr if that was within the last week (a fresh Overseerr request is demand even if
    its search happened while tgarr was restarting or was sent by title only)."""
    seen = state()["demand"].get(str(series["tvdbId"]))
    added = None
    try:
        from datetime import datetime
        added = datetime.fromisoformat(series["added"].replace("Z", "+00:00")).timestamp()
    except Exception:
        pass
    if added and time.time() - added < 7 * 86400:
        return min(seen, added) if seen else added
    return seen


async def wanted() -> list[dict]:
    """Series Sonarr is missing episodes for (Overseerr requests land here), each with the directory channel
    that carries it, if any, and whether that channel is already a joined/linked source."""
    from . import arr, store
    series = await arr.sonarr.req("GET", "/series")
    cat = await entries()
    srcs = store.sources()
    by_title: dict[str, dict] = {}
    for e in cat:
        if e["en"]:
            by_title.setdefault(_norm(e["en"]), e)
    out = []
    for s in series:
        st = s.get("statistics") or {}
        missing = (st.get("episodeCount") or 0) - (st.get("episodeFileCount") or 0)
        if not s.get("monitored") or missing <= 0:
            continue
        names = [s["title"]] + [a["title"] for a in s.get("alternateTitles", []) if a.get("title")]
        entry = next((by_title[n] for n in map(_norm, names) if n in by_title), None)
        linked = next((x for x in srcs if x.get("tvdbId") == s["tvdbId"]), None)
        src = linked or (next((x for x in srcs if entry and x.get("link") == entry["url"]), None))
        if not entry and not linked:
            continue  # nothing on Telegram for it (as far as the directories know)
        out.append({"tvdbId": s["tvdbId"], "seriesId": s["id"], "title": s["title"], "year": s.get("year"),
                    "missing": missing, "added": s.get("added"),
                    "poster": next((i.get("remoteUrl") for i in s.get("images", []) if i["coverType"] == "poster"), None),
                    "entry": entry, "source_id": src and src["id"], "joined": bool(src and src.get("joined")),
                    "linked": bool(linked), "demand": _demand(s),
                    "linked_at": linked and linked.get("linked_at"), "searched_at": linked and linked.get("searched_at")})
    return sorted(out, key=lambda x: x.get("added") or "", reverse=True)
