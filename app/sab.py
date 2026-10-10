"""SABnzbd-compatible API, just enough for Sonarr to use tgarr as a "Usenet" download client.

Sonarr uploads the NZB it got from the indexer (addfile), polls queue/history, imports the finished folder
itself (it moves the file into the library) and then deletes the history item.
"""
import logging
import shutil
import time
from pathlib import Path

from . import config, indexer, jobs

log = logging.getLogger("tgarr.sab")
CATEGORIES = ["tv"]
QUEUE = ("queued", "starting", "downloading", "paused")
HISTORY = ("done", "error", "stopped")


def _mine() -> list[dict]:
    return sorted((j for j in jobs.jobs.values() if j.get("via") == "sonarr" and not j.get("sab_hidden")),
                  key=lambda j: j["created"])


def _by_nzo(nzo: str) -> dict | None:
    return next((j for j in jobs.jobs.values() if j.get("nzo") == nzo), None)


def _hms(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def config_response() -> dict:
    return {"config": {
        "misc": {"complete_dir": str(config.TELEGRAM_DIR), "download_dir": str(config.TELEGRAM_DIR / "incomplete"),
                 "tv_categories": [], "enable_tv_sorting": False, "movie_categories": [], "enable_movie_sorting": False,
                 "date_categories": [], "enable_date_sorting": False, "pre_check": False,
                 "history_retention": "", "history_retention_option": "all", "history_retention_number": 0},
        "categories": [{"name": "*", "order": 0, "pp": "3", "script": "None", "dir": "", "newzbin": "", "priority": 0}] +
                      [{"name": c, "order": i + 1, "pp": "", "script": "Default", "dir": c, "newzbin": "", "priority": -100}
                       for i, c in enumerate(CATEGORIES)],
        "servers": [], "sorters": []}}


def queue_response() -> dict:
    slots = []
    for i, j in enumerate(x for x in _mine() if x["status"] in QUEUE):
        size, done = j.get("size") or 0, j.get("done") or 0
        left = size - done
        slots.append({
            "status": "Paused" if j["status"] == "paused" else "Downloading" if j["status"] in ("starting", "downloading") else "Queued",
            "index": i, "timeleft": _hms(left / j["speed"]) if j.get("speed") else "0:00:00",
            "mb": f"{size / 1048576:.2f}", "mbleft": f"{left / 1048576:.2f}", "filename": j["release"],
            "priority": "Normal", "cat": j.get("cat", "tv"), "nzo_id": j["nzo"],
            "percentage": str(int(done * 100 / size)) if size else "0"})
    return {"queue": {"paused": False, "noofslots": len(slots), "slots": slots, "status": "Downloading" if slots else "Idle",
                      "speed": "0", "kbpersec": "0.00", "mb": "0", "mbleft": "0", "timeleft": "0:00:00"}}


def history_response() -> dict:
    slots = []
    for j in (x for x in _mine() if x["status"] in HISTORY):
        ok = j["status"] == "done"
        slots.append({"nzo_id": j["nzo"], "name": j["release"], "nzb_name": j["release"] + ".nzb", "category": j.get("cat", "tv"),
                      "status": "Completed" if ok else "Failed", "fail_message": "" if ok else (j.get("error") or "Stopped in tgarr"),
                      "bytes": j.get("size") or 0, "download_time": int(j.get("took") or 0),
                      "storage": str(Path(j["dest"]).parent), "completed": int(j.get("finished") or time.time())})
    return {"history": {"noofslots": len(slots), "slots": slots}}


def add(body: bytes, cat: str) -> dict:
    ref = indexer.parse_nzb(body)
    if not ref:
        return {"status": False, "error": "Not a tgarr NZB (tgarr only downloads releases from its own indexer)"}
    jid = jobs.add_sonarr(ref, cat if cat in CATEGORIES else "tv")
    return {"status": True, "nzo_ids": [jobs.jobs[jid]["nzo"]]}


def delete(nzo: str, del_files: bool, from_history: bool) -> dict:
    j = _by_nzo(nzo)
    if not j:
        return {"status": True}
    folder = Path(j["dest"]).parent
    if from_history and j["status"] == "done":
        # Sonarr imported it (moved the file out): keep the job visible in tgarr's own list as finished
        j["sab_hidden"] = True
        j["library"] = "imported by Sonarr"
        jobs.save(force=True)
    else:
        jobs.remove(j["id"])
    # only ever delete our own per-release folder under the Telegram download dir
    if del_files and config.TELEGRAM_DIR in folder.parents and folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    return {"status": True}


def handle(mode: str, p: dict, body: bytes) -> dict:
    if mode == "version":
        return {"version": "4.4.1"}
    if mode == "get_config":
        return config_response()
    if mode == "fullstatus":
        return {"status": {"completedir": str(config.TELEGRAM_DIR), "paused": False, "pause_int": "0"}}
    if mode == "get_cats":
        return {"categories": ["*"] + CATEGORIES}
    if mode in ("queue", "history") and p.get("name") == "delete":
        for nzo in (p.get("value") or "").split(","):
            delete(nzo, p.get("del_files") == "1", mode == "history")
        return {"status": True}
    if mode == "queue" and p.get("name") in ("pause", "resume"):
        if j := _by_nzo(p.get("value") or ""):
            (jobs.pause if p["name"] == "pause" else jobs.retry)(j["id"])
        return {"status": True}
    if mode == "queue":
        return queue_response()
    if mode == "history":
        return history_response()
    if mode == "addfile":
        return add(body, p.get("cat") or "tv")
    if mode in ("pause", "resume"):
        return {"status": True}
    return {"status": False, "error": f"mode '{mode}' is not supported by tgarr"}
