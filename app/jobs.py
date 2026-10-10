"""Persistent download/import queue."""
import asyncio
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path, PurePath

from . import arr, config, store, tg
from .parse import safe

log = logging.getLogger("tgarr.jobs")
FILE = config.CONFIG_DIR / "jobs.json"

jobs: dict[str, dict] = {}
queue: asyncio.Queue = asyncio.Queue()
_dirty = False


def save(force=False):
    global _dirty
    _dirty = True
    if force:
        flush()


_flush_lock = threading.Lock()


def flush():
    global _dirty
    with _flush_lock:  # writers on the event loop and in threads must not share one temp file
        if _dirty:
            _dirty = False
            tmp = FILE.with_name(f"jobs.{uuid.uuid4().hex[:6]}.tmp")
            tmp.write_text(json.dumps(list(jobs.values()), ensure_ascii=False, indent=1))
            tmp.replace(FILE)


def load():
    if FILE.exists():
        for j in json.loads(FILE.read_text()):
            jobs[j["id"]] = j
            j.setdefault("batch", "b-" + j["target"]["title"])  # jobs from before batches existed
            if j["status"] in ("queued", "starting", "downloading", "extracting", "importing"):
                j["status"] = "queued"
                queue.put_nowait(j["id"])


def dest_for(j: dict) -> Path:
    t = j["target"]
    ext = PurePath(j["name"]).suffix or ".mkv"
    res = f".{j['height']}p" if j.get("height") else ""
    if j["kind"] == "tv":
        show = safe(t["title"])
        if j["file_kind"] == "archive":
            return config.TV_DIR / f"{show}.Telegram" / safe(j["name"])
        s = j["season"] if j["season"] is not None else 1
        folder = config.TV_DIR / f"{show}.S{s:02}.Telegram"
        if j["episode"] is None:
            return folder / safe(j["name"])
        return folder / f"{show}.S{s:02}E{j['episode']:02}{res}.WEB-DL.Telegram{ext}"
    base = f"{safe(t['title'])}.{t.get('year') or ''}".rstrip(".")
    if j["file_kind"] == "archive":
        return config.MOVIE_DIR / f"{base}.Telegram" / safe(j["name"])
    return config.MOVIE_DIR / f"{base}.Telegram" / f"{base}{res}.WEB-DL.Telegram{ext}"


def add(kind: str, target: dict, items: list[dict]) -> list[str]:
    ids = []
    batch = uuid.uuid4().hex[:8]
    for it in items:
        j = {"id": uuid.uuid4().hex[:10], "batch": batch, "created": time.time(), "kind": kind, "target": target,
             "source_id": it["source_id"], "msg_id": it["msg_id"], "name": it["name"],
             "file_kind": it.get("kind", "video"), "height": it.get("height"), "size": it.get("size") or 0,
             "season": it.get("season"), "episode": it.get("episode"), "replace": bool(it.get("replace")),
             "done": 0, "speed": 0, "status": "queued", "error": None, "dest": None, "library": None}
        j["dest"] = str(dest_for(j))
        # idempotent queueing: a file that already has an unfinished job isn't queued twice (double click,
        # two tabs); a stopped/failed job for the same file is superseded so the two can't share a .part
        same = [o for o in jobs.values() if o["dest"] == j["dest"]]
        if live := next((o for o in same if o["status"] not in ("done", "error", "stopped")), None):
            ids.append(live["id"])
            continue
        for o in same:
            if o["status"] in ("error", "stopped"):
                jobs.pop(o["id"], None)
        jobs[j["id"]] = j
        queue.put_nowait(j["id"])
        ids.append(j["id"])
    save(force=True)
    return ids


def add_sonarr(ref: dict, cat: str) -> str:
    """Queue a release Sonarr grabbed through the SABnzbd-compatible API. tgarr only downloads it: Sonarr
    imports the finished folder itself, so there is no library check and no import step here."""
    jid = uuid.uuid4().hex[:10]
    title = ref["title"]
    dest = config.TELEGRAM_DIR / cat / title / f"{title}{ref.get('ext') or '.mkv'}"
    if live := next((o for o in jobs.values() if o["dest"] == str(dest) and o["status"] not in ("error", "stopped")
                     and not o.get("sab_hidden")), None):
        return live["id"]  # Sonarr re-sent the same grab
    for o in [o for o in jobs.values() if o["dest"] == str(dest)]:
        jobs.pop(o["id"], None)
    jobs[jid] = {"id": jid, "batch": f"sonarr-{ref['tvdb']}", "via": "sonarr", "nzo": f"SABnzbd_nzo_{jid}",
                 "release": title, "cat": cat, "created": time.time(), "kind": "tv",
                 "target": {"title": ref["series"], "tvdbId": ref["tvdb"], "poster": ref.get("poster")},
                 "source_id": ref["source_id"], "msg_id": ref["msg_id"], "name": ref.get("name") or title,
                 "file_kind": "video", "height": ref.get("height"), "size": ref.get("size") or 0,
                 "season": ref["season"], "episode": ref["episode"], "replace": False,
                 "done": 0, "speed": 0, "status": "queued", "error": None, "dest": str(dest), "library": None}
    queue.put_nowait(jid)
    save(force=True)
    return jid


running: dict[str, asyncio.Task] = {}
ACTIVE = ("starting", "downloading", "extracting", "importing")


def retry(jid):
    """Retry a failed job or resume a paused/stopped one (download resumes from .part)."""
    j = jobs[jid]
    if j["status"] in ("queued", "done", *ACTIVE):  # finished jobs are never re-run
        return
    j.update(status="queued", error=None)
    queue.put_nowait(jid)
    save(force=True)


def pause(jid):
    j = jobs[jid]
    if j["status"] == "queued":
        j["status"] = "paused"
    elif j["status"] in ("starting", "downloading") and jid in running:
        j["status"] = "paused"
        running[jid].cancel()
    save(force=True)


def stop(jid):
    """Cancel and throw away the partial download; the job stays listed as stopped."""
    j = jobs[jid]
    if j["status"] in ("done",):
        return
    j["status"] = "stopped"
    if jid in running:
        running[jid].cancel()
    else:
        _drop_part(j)
    save(force=True)


def remove(jid):
    j = jobs.pop(jid, None)
    if j and jid in running:
        j["status"] = "removed"
        running[jid].cancel()  # worker deletes the .part after cancellation
    elif j and j["status"] != "done":
        _drop_part(j)
    save(force=True)


def _drop_part(j):
    Path(j["dest"] + ".part").unlink(missing_ok=True)


async def extract(archive: Path) -> Path:
    out = archive.parent / (archive.stem + ".extracted")
    out.mkdir(exist_ok=True)
    p = await asyncio.create_subprocess_exec("unar", "-q", "-f", "-D", "-o", str(out), str(archive),
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output, _ = await p.communicate()
    if p.returncode:
        raise RuntimeError(f"unar failed: {output.decode(errors='replace')[-300:]}")
    return out


async def precheck(j: dict) -> bool:
    """Don't spend a download on something the library already has unless the user asked to replace it.
    Returns True when the library file *is* this job's download (same hardlink): the job is simply done."""
    if j["file_kind"] != "video":
        return False
    t = j["target"]
    lib = await arr.library_files(j["kind"], t["tvdbId"] if j["kind"] == "tv" else t["tmdbId"])
    key = f"{j['season'] if j['season'] is not None else 1}-{j['episode']}" if j["kind"] == "tv" else "movie"
    f = lib["files"].get(key)
    if not f:
        return False
    try:
        if Path(j["dest"]).exists() and os.path.samefile(j["dest"], f["full"]):
            j["library"] = str(Path(f["full"]).parent.parent if j["kind"] == "tv" else Path(f["full"]).parent)
            return True
    except OSError:
        pass
    if not j.get("replace"):
        raise RuntimeError(f"Already in library ({f['quality']}, {round((f['size'] or 0) / 1e6)} MB). "
                           "Queue again with Replace on to overwrite")


async def run(j: dict):
    source = store.get_source(j["source_id"])
    if not source:
        raise RuntimeError("source was removed")
    dest = Path(j["dest"])
    via_sonarr = j.get("via") == "sonarr"
    if not via_sonarr and await precheck(j):
        j.update(status="done", done=j["size"], speed=0)
        return
    j["status"] = "downloading"
    began = time.time()
    last = {"t": time.monotonic(), "b": 0}

    def progress(done, total):
        j["done"], j["size"] = done, total
        now = time.monotonic()
        if now - last["t"] >= 2:
            j["speed"] = int((done - last["b"]) / (now - last["t"]))
            last.update(t=now, b=done)
            save()

    await tg.download(source, j["msg_id"], dest, progress)
    store.update_source(source)  # persist resolved chat_id
    j["speed"] = 0
    if via_sonarr:  # Sonarr picks the finished folder up through the SABnzbd history and imports it itself
        j.update(status="done", took=time.time() - began, finished=time.time(), library="waiting for Sonarr to import")
        return
    path = dest
    if j["file_kind"] == "archive":
        j["status"] = "extracting"
        save()
        path = await extract(dest)
    if j["file_kind"] == "subtitle":
        j["status"] = "done"
        return

    j["status"] = "importing"
    save()
    t = j["target"]
    if j["kind"] == "tv":
        series = await arr.ensure_series(t["tvdbId"])
        single = j["file_kind"] == "video"
        lib = await arr.import_episode(str(path), series, j["season"] if single else None,
                                       j["episode"] if single else None, j.get("height"), j.get("replace", False))
    else:
        movie = await arr.ensure_movie(t["tmdbId"])
        lib = await arr.import_movie(str(path), movie, j.get("height"), j.get("replace", False))
    j["library"] = lib
    try:
        await arr.plex_refresh(lib)
    except Exception as e:
        log.warning("plex refresh failed: %s", e)
    j["status"] = "done"


async def worker(n):
    while True:
        jid = await queue.get()
        j = jobs.get(jid)
        if not j or j["status"] != "queued":
            continue  # paused/stopped/removed meanwhile, or a duplicate queue entry already picked up
        if jid in running:  # resumed while its cancelled run is still unwinding: try again shortly
            asyncio.get_running_loop().call_later(1, queue.put_nowait, jid)
            continue
        # claim the job before any await, so a duplicate entry can't start a second download of the same file
        j["status"] = "starting"
        task = running[jid] = asyncio.create_task(run(j))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():  # the worker itself is shutting down
                task.cancel()
                raise
            j["speed"] = 0
            if j["status"] in ("stopped", "removed"):
                _drop_part(j)
            log.info("job %s %s", jid, j["status"])
        except Exception as e:
            log.exception("job %s failed", jid)
            j.update(status="error", error=str(e), speed=0)
        finally:
            if running.get(jid) is task:
                running.pop(jid)
        save(force=True)


async def flusher():
    while True:
        await asyncio.sleep(3)
        flush()
