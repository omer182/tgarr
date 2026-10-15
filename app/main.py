import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import arr, catalog, config, indexer, jobs, sab, store, tg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app):
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    await tg.start()
    jobs.load()
    indexer.load_index()
    tasks = [asyncio.create_task(jobs.worker(i)) for i in range(config.WORKERS)]
    tasks.append(asyncio.create_task(jobs.flusher()))
    tasks.append(asyncio.create_task(tg.keeper()))
    tasks.append(asyncio.create_task(indexer.refresher()))
    tasks.append(asyncio.create_task(catalog.auto_loop()))
    (config.TELEGRAM_DIR / "tv").mkdir(parents=True, exist_ok=True)
    config.api_key()
    yield
    for t in tasks:
        t.cancel()
    jobs.flush()
    await tg.stop()


app = FastAPI(title="tgarr", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled(request, exc):
    """Log full tracebacks for unexpected errors and give the UI a readable message instead of a bare 500."""
    from fastapi.responses import JSONResponse
    logging.getLogger("tgarr").exception("%s %s failed", request.method, request.url.path)
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)


def fail(e: Exception):
    raise HTTPException(400, str(e))


@app.get("/api/health")
async def health():
    """Cheap liveness probe for Docker's HEALTHCHECK (no calls to Sonarr, Radarr or Plex)."""
    return {"ok": True, "telegram": tg.state["authorized"]}


@app.get("/api/status")
async def status():
    son, rad, plex = await asyncio.gather(arr.sonarr.ok(), arr.radarr.ok(), arr.plex_ok())
    return {"telegram": tg.state, "sonarr": son, "radarr": rad, "plex": plex,
            "paths": {"tv": str(config.TV_DIR), "movies": str(config.MOVIE_DIR)}}


# ---------- sources ----------

class SourceIn(BaseModel):
    link: str
    kind: str = "any"  # tv | movie | any
    note: str = ""


@app.get("/api/sources")
async def list_sources():
    return store.sources()


@app.get("/api/dialogs")
async def my_chats(q: str = ""):
    """Groups/channels the account is in, so private ones can be added without an invite link."""
    try:
        return await tg.dialogs(q)
    except Exception as e:
        fail(e)


@app.post("/api/sources")
async def add_source(s: SourceIn):
    ref = tg.chat_ref(s.link)
    if ref is not None and any(x.get("chat_id") == ref for x in store.sources()):
        fail(ValueError("That chat is already a source"))
    try:
        info = await tg.resolve(s.link)
    except Exception as e:
        fail(e)
    return store.add_source(s.link, s.kind, s.note, info)


@app.post("/api/sources/{sid}/join")
async def join_source(sid: str):
    src = store.get_source(sid) or fail(KeyError("unknown source"))
    try:
        src.update(await tg.join(src["link"]))
    except Exception as e:
        fail(e)
    store.update_source(src)
    return src


class SourcePatch(BaseModel):
    tvdbId: int | None = None
    numbering: str | None = None  # auto | posted | continuous
    unlink: bool = False


@app.patch("/api/sources/{sid}")
async def patch_source(sid: str, p: SourcePatch):
    """Link a source to its Sonarr series (what the Newznab indexer answers for) and/or set its numbering mode."""
    if not store.get_source(sid):
        fail(KeyError("unknown source"))
    fields: dict = {}  # a None value clears that field
    if p.unlink:
        fields.update(tvdbId=None, series_title=None, poster=None)
    elif p.tvdbId:
        try:
            info = await indexer.series_info(p.tvdbId)
        except Exception as e:
            fail(e)
        fields.update(tvdbId=p.tvdbId, series_title=info["title"], poster=info.get("poster"))
    if p.numbering in ("auto", "posted", "continuous"):
        fields["numbering"] = None if p.numbering == "auto" else p.numbering
    out = store.patch_source(sid, fields)
    if p.unlink:
        indexer.index.pop(sid, None)
    else:
        asyncio.create_task(indexer.refresh(sid))  # reindex this source in the background
    return out


@app.delete("/api/sources/{sid}")
async def del_source(sid: str):
    store.remove_source(sid)
    return {"ok": True}


# ---------- browse / search ----------

@app.get("/api/files")
async def files(source: str = "", q: str = "", limit: int = 0):
    srcs = [store.get_source(source)] if source else store.sources()
    srcs = [s for s in srcs if s]
    if not srcs:
        fail(KeyError("no sources"))
    per = limit or (config.SCAN_LIMIT if source else 300)

    def norm(t):
        return " ".join((t or "").lower().replace("_", " ").replace(".", " ").split())

    async def one(s):
        try:
            if source and q:
                # one channel: scan it and match captions AND file names ourselves; Telegram's own search
                # only reliably matches message text (captions), not document file names
                needle = norm(q)
                r = [f for f in await tg.list_files(s, None, per) if needle in norm(f["name"]) or needle in norm(f["caption"])]
            else:
                r = await tg.list_files(s, q, per)  # all channels: Telegram server-side search
            store.update_source(s)
            return r, None
        except Exception as e:
            return [], f"{s.get('title') or s['link']}: {e}"

    results = await asyncio.gather(*(one(s) for s in srcs))
    return {"files": [f for r, _ in results for f in r], "errors": [e for _, e in results if e],
            "season_sizes": {s["id"]: (store.get_source(s["id"]) or {}).get("season_sizes") or {} for s in srcs},
            "season_mins": {s["id"]: (store.get_source(s["id"]) or {}).get("season_mins") or {} for s in srcs}}


@app.get("/api/episodes")
async def episodes(tvdbId: int):
    try:
        return await arr.tvdb_episodes(tvdbId)
    except Exception as e:
        fail(e)


@app.get("/api/links")
async def links(chat: str):
    """t.me links posted in a channel (read-only), e.g. a directory of per-series channels."""
    try:
        return await tg.channel_links(int(chat) if chat.lstrip("-").isdigit() else chat)
    except Exception as e:
        fail(e)


@app.get("/api/avatar/{chat_id}")
async def chat_avatar(chat_id: int):
    from fastapi.responses import FileResponse, Response
    try:
        p = await tg.avatar(chat_id)
    except Exception:
        p = None
    if not p:
        return Response(status_code=404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/lookup")
async def lookup(kind: str, term: str):
    try:
        return await arr.lookup(kind, term)
    except Exception as e:
        fail(e)


@app.get("/api/library")
async def library(kind: str, id: int):
    try:
        return await arr.library_files(kind, id)
    except Exception as e:
        fail(e)


# ---------- jobs ----------

class DownloadIn(BaseModel):
    kind: str  # tv | movie
    target: dict  # {title, year, tvdbId} | {title, year, tmdbId}
    items: list[dict]


@app.post("/api/download")
async def download(d: DownloadIn):
    if d.kind == "tv" and not d.target.get("tvdbId") or d.kind == "movie" and not d.target.get("tmdbId"):
        fail(ValueError("pick a series/movie first"))
    if d.kind == "tv":
        bad = [i["name"] for i in d.items if i.get("kind") == "video" and i.get("episode") is None]
        if bad:
            fail(ValueError(f"missing episode number for: {', '.join(bad[:5])}"))
    return {"ids": jobs.add(d.kind, d.target, d.items)}


@app.get("/api/jobs")
async def list_jobs():
    return sorted(jobs.jobs.values(), key=lambda j: j["created"], reverse=True)


@app.post("/api/jobs/{jid}/{action}")
async def job_action(jid: str, action: str):
    fn = {"retry": jobs.retry, "resume": jobs.retry, "pause": jobs.pause, "stop": jobs.stop}.get(action)
    if not fn or jid not in jobs.jobs:
        fail(KeyError(f"bad job/action {jid} {action}"))
    fn(jid)
    return {"ok": True}


@app.post("/api/jobs-all/{action}")
async def all_jobs(action: str, batch: str = "", season: int | None = None):
    """pause | resume | retry-failed | stop | remove for every job, one batch, or one season of a batch."""
    for j in [j for j in jobs.jobs.values() if (not batch or j.get("batch") == batch)
              and (season is None or (j.get("season") if j.get("season") is not None else 1) == season)]:
        if action == "stop" and j["status"] in ("queued", "starting", "downloading", "paused"):
            jobs.stop(j["id"])
        elif action == "remove":
            jobs.remove(j["id"])
        elif action == "pause" and j["status"] in ("queued", "starting", "downloading"):
            jobs.pause(j["id"])
        elif action == "resume" and j["status"] in ("paused", "stopped"):
            jobs.retry(j["id"])
        elif action == "retry-failed" and j["status"] == "error":
            jobs.retry(j["id"])
    return {"ok": True}


@app.delete("/api/jobs/{jid}")
async def remove(jid: str):
    jobs.remove(jid)
    return {"ok": True}


@app.post("/api/jobs/clear")
async def clear_done():
    for jid in [j["id"] for j in jobs.jobs.values() if j["status"] == "done"]:
        jobs.jobs.pop(jid)
    jobs.save(force=True)
    return {"ok": True}


@app.get("/api/catalog")
async def get_catalog(refresh: bool = False):
    """Series channels listed by the user's directory channels, with their English titles."""
    try:
        return {"directories": catalog.directories(), "entries": await catalog.entries(refresh)}
    except Exception as e:
        fail(e)


@app.get("/api/wanted")
async def get_wanted():
    """Series Sonarr is missing episodes for that a directory channel (or a linked source) carries."""
    try:
        return await catalog.wanted()
    except Exception as e:
        fail(e)


@app.post("/api/wanted/{tvdb_id}/get")
async def wanted_get(tvdb_id: int):
    """The "Join & get it" button: join/add/link the channel, then have Sonarr search the missing episodes."""
    try:
        return await catalog.get_it(tvdb_id)
    except Exception as e:
        fail(e)


@app.post("/api/wanted/{tvdb_id}/search")
async def wanted_search(tvdb_id: int):
    w = next((x for x in await catalog.wanted() if x["tvdbId"] == tvdb_id), None) or fail(KeyError("not wanted"))
    await arr.sonarr.req("POST", "/command", json={"name": "SeriesSearch", "seriesId": w["seriesId"]})
    return {"ok": True}


class SettingsIn(BaseModel):
    auto_join: bool | None = None
    max_joins_per_day: int | None = None
    fallback_minutes: int | None = None


@app.get("/api/settings")
async def get_settings():
    return {**catalog.settings(), "joins_left_today": catalog.joins_left(), "log": catalog.state()["log"]}


@app.put("/api/settings")
async def put_settings(s: SettingsIn):
    catalog.save_settings(s.model_dump())
    return await get_settings()


class DirectoriesIn(BaseModel):
    directories: list[str]


@app.put("/api/catalog/directories")
async def put_directories(d: DirectoriesIn):
    catalog.set_directories([x.strip() for x in d.directories if x.strip()])
    return await get_catalog(True)


# ---------- Sonarr / Prowlarr integration ----------

@app.get("/api/integration")
async def integration():
    """What to paste into Prowlarr/Sonarr, and what the indexer currently knows."""
    srcs = {s["id"]: s for s in store.sources()}
    return {"apikey": config.api_key(), "newznab_url": "http://tgarr:8765/newznab", "sab_host": "tgarr", "sab_port": 8765,
            "sab_url_base": "/sab", "download_dir": str(config.TELEGRAM_DIR),
            "index": [{"source_id": sid, "source": (srcs.get(sid) or {}).get("title"), "series": v.get("series"),
                       "releases": len(v["releases"]), "at": v["at"], "error": v.get("error")} for sid, v in indexer.index.items()]}


@app.post("/api/integration/refresh")
async def integration_refresh():
    await indexer.refresh()
    return {"ok": True}


def _xml(body: str, status: int = 200) -> Response:
    return Response(body, status_code=status, media_type="application/xml")


def _num(v: str) -> int | None:
    v = v.lstrip("SsEe")
    return int(v) if v.isdigit() else None


@app.get("/newznab/api")
@app.get("/newznab")
async def newznab(request: Request, t: str = "caps", apikey: str = "", q: str = "", tvdbid: int | None = None,
                  season: str = "", ep: str = "", offset: int = 0, limit: int = 100):
    if t == "caps":
        return _xml(indexer.caps_xml())
    if apikey != config.api_key():
        return _xml(indexer.error_xml(100, "Incorrect user credentials"), 401)
    if t not in ("search", "tvsearch"):
        return _xml(indexer.error_xml(202, f"No such function ({t})"))
    rels, is_search = indexer.search(tvdbid, q.strip(), _num(season), _num(ep))
    if is_search and tvdbid:
        catalog.note_demand(tvdbid)  # Sonarr wants this series: lets hands-off mode fetch its channel
    base = str(request.base_url).rstrip("/")
    return _xml(indexer.feed_xml(rels, base, config.api_key(), max(0, offset), min(max(1, limit), 100), is_search))


@app.get("/newznab/nzb/{source_id}/{msg}.nzb")
async def newznab_nzb(source_id: str, msg: int, apikey: str = ""):
    if apikey != config.api_key():
        return _xml(indexer.error_xml(100, "Incorrect user credentials"), 401)
    r = indexer.find_release(source_id, msg)
    if not r:
        return _xml(indexer.error_xml(300, "No such item"), 404)
    return Response(indexer.nzb_xml(r), media_type="application/x-nzb",
                    headers={"Content-Disposition": f'attachment; filename="{r["title"]}.nzb"'})


@app.api_route("/sab/api", methods=["GET", "POST"])
async def sab_api(request: Request):
    p = dict(request.query_params)
    body = await request.body() if request.method == "POST" else b""
    if request.method == "POST" and "mode" not in p:  # parameters sent as a urlencoded form
        from urllib.parse import parse_qsl
        p.update(parse_qsl(body.decode("latin-1")))
    mode = p.get("mode", "")
    if mode != "version" and p.get("apikey") != config.api_key():
        return JSONResponse({"status": False, "error": "API Key Incorrect"}, status_code=403)
    return JSONResponse(sab.handle(mode, p, body))


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
