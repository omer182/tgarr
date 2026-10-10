"""Sonarr / Radarr / Plex glue: lookup, ensure library entry, ManualImport (hardlink), Plex partial scan."""
import asyncio
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from . import config


class Arr:
    def __init__(self, url, key):
        self.url, self.key = url.rstrip("/"), key

    async def req(self, method, path, **kw):
        async with httpx.AsyncClient(timeout=60) as h:
            r = await h.request(method, f"{self.url}/api/v3{path}", headers={"X-Api-Key": self.key}, **kw)
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else None

    async def ok(self):
        try:
            return (await self.req("GET", "/system/status"))["version"]
        except Exception as e:
            return f"error: {e}"

    async def quality_for(self, height):
        """WEBDL-<res> quality object, matching the nearest standard resolution."""
        if not height:
            return None
        res = min((2160, 1080, 720, 480), key=lambda r: abs(r - height))
        for d in await self.req("GET", "/qualitydefinition"):
            if d["quality"]["name"] == f"WEBDL-{res}p":
                return {"quality": d["quality"], "revision": {"version": 1, "real": 0, "isRepack": False}}
        return None

    async def run_command(self, body, timeout=600):
        cmd = await self.req("POST", "/command", json=body)
        for _ in range(timeout // 2):
            await asyncio.sleep(2)
            cmd = await self.req("GET", f"/command/{cmd['id']}")
            if cmd["status"] in ("completed", "failed", "aborted", "cancelled"):
                break
        if cmd["status"] != "completed":
            raise RuntimeError(f"{body['name']} {cmd['status']}: {cmd.get('message') or cmd.get('exception', '')}")
        return cmd


sonarr = Arr(config.SONARR_URL, config.SONARR_KEY)
radarr = Arr(config.RADARR_URL, config.RADARR_KEY)


# ---------- lookup ----------

async def lookup(kind, term):
    if kind == "tv":
        res = await sonarr.req("GET", "/series/lookup", params={"term": term})
        return [{"id": r.get("id"), "title": r["title"], "year": r.get("year"), "tvdbId": r["tvdbId"],
                 "inLibrary": bool(r.get("id")), "seasons": [s["seasonNumber"] for s in r.get("seasons", [])],
                 "poster": next((i.get("remoteUrl") for i in r.get("images", []) if i["coverType"] == "poster"), None)}
                for r in res[:12]]
    res = await radarr.req("GET", "/movie/lookup", params={"term": term})
    return [{"id": r.get("id"), "title": r["title"], "year": r.get("year"), "tmdbId": r["tmdbId"],
             "inLibrary": bool(r.get("id")),
             "poster": next((i.get("remoteUrl") for i in r.get("images", []) if i["coverType"] == "poster"), None)}
            for r in res[:12]]


# ---------- episode list for numbering ----------

_eps_cache: dict[int, tuple[float, list]] = {}
SKYHOOK = "https://skyhook.sonarr.tv/v1/tvdb/shows/en/{}"  # the TVDB proxy Sonarr itself uses


async def tvdb_episodes(tvdb_id: int) -> list[dict]:
    """Regular (non-special) episodes in Sonarr's numbering: [{s, e, abs, title}] sorted by season/episode.
    Uses Sonarr when the series is in the library, otherwise Sonarr's metadata source. Cached 30 min."""
    hit = _eps_cache.get(tvdb_id)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    found = await sonarr.req("GET", "/series", params={"tvdbId": tvdb_id})
    if found:
        raw = await sonarr.req("GET", "/episode", params={"seriesId": found[0]["id"]})
        eps = [{"s": e["seasonNumber"], "e": e["episodeNumber"], "abs": e.get("absoluteEpisodeNumber"),
                "title": e.get("title")} for e in raw]
    else:
        async with httpx.AsyncClient(timeout=30) as h:
            r = await h.get(SKYHOOK.format(tvdb_id))
            r.raise_for_status()
        eps = [{"s": e["seasonNumber"], "e": e["episodeNumber"], "abs": e.get("absoluteEpisodeNumber"),
                "title": e.get("title")} for e in r.json().get("episodes", [])]
    eps = sorted((e for e in eps if e["s"] > 0), key=lambda e: (e["s"], e["e"]))
    # no absolute numbers from TVDB: derive them from airing order
    if not all(e["abs"] for e in eps):
        for i, e in enumerate(eps, 1):
            e["abs"] = i
    _eps_cache[tvdb_id] = (time.time(), eps)
    return eps


# ---------- ensure in library ----------

_locks: dict[str, asyncio.Lock] = {}


async def ensure_series(tvdb_id: int) -> dict:
    async with _locks.setdefault(f"tv{tvdb_id}", asyncio.Lock()):
        found = await sonarr.req("GET", "/series", params={"tvdbId": tvdb_id})
        if found:
            return found[0]
        item = (await sonarr.req("GET", "/series/lookup", params={"term": f"tvdb:{tvdb_id}"}))[0]
        item.update(qualityProfileId=config.SONARR_PROFILE, rootFolderPath=config.SONARR_ROOT, monitored=True,
                    seasonFolder=True, addOptions={"monitor": "none", "searchForMissingEpisodes": False,
                                                   "searchForCutoffUnmetEpisodes": False})
        series = await sonarr.req("POST", "/series", json=item)
        for _ in range(60):  # wait for Sonarr to fetch episode list
            if await sonarr.req("GET", "/episode", params={"seriesId": series["id"]}):
                break
            await asyncio.sleep(2)
        return series


async def ensure_movie(tmdb_id: int) -> dict:
    async with _locks.setdefault(f"mv{tmdb_id}", asyncio.Lock()):
        found = await radarr.req("GET", "/movie", params={"tmdbId": tmdb_id})
        if found:
            return found[0]
        item = await radarr.req("GET", "/movie/lookup/tmdb", params={"tmdbId": tmdb_id})
        item.update(qualityProfileId=config.RADARR_PROFILE, rootFolderPath=config.RADARR_ROOT, monitored=True,
                    minimumAvailability="released", addOptions={"searchForMovie": False})
        return await radarr.req("POST", "/movie", json=item)


# ---------- import ----------

async def library_files(kind: str, ext_id: int) -> dict:
    """What the library already has for a series/movie: {"inLibrary", "files": {"S-E" | "movie": {...}}}."""
    def info(f):
        return {"quality": f.get("quality", {}).get("quality", {}).get("name"), "size": f.get("size"),
                "path": f.get("relativePath"), "full": f.get("path")}
    # Sonarr/Radarr keep file records until their next rescan, so a file deleted by hand still shows up.
    # Same /data mount here, so check the disk and rescan once if anything is stale.
    def stale(fs):
        return any(f.get("path") and not Path(f["path"]).exists() for f in fs)

    if kind == "tv":
        found = await sonarr.req("GET", "/series", params={"tvdbId": ext_id})
        if not found:
            return {"inLibrary": False, "files": {}}
        sid = found[0]["id"]
        for attempt in range(2):
            eps = await sonarr.req("GET", "/episode", params={"seriesId": sid, "includeEpisodeFile": "true"})
            have = [e for e in eps if e.get("hasFile") and e.get("episodeFile")]
            if attempt or not stale([e["episodeFile"] for e in have]):
                break
            await sonarr.run_command({"name": "RescanSeries", "seriesId": sid}, timeout=120)
        return {"inLibrary": True, "files": {f"{e['seasonNumber']}-{e['episodeNumber']}": info(e["episodeFile"])
                                             for e in have if Path(e["episodeFile"].get("path", "")).exists()}}
    found = await radarr.req("GET", "/movie", params={"tmdbId": ext_id})
    if not found:
        return {"inLibrary": False, "files": {}}
    mf = found[0].get("movieFile")
    if mf and stale([mf]):
        await radarr.run_command({"name": "RescanMovie", "movieId": found[0]["id"]}, timeout=120)
        found = await radarr.req("GET", "/movie", params={"tmdbId": ext_id})
        mf = found[0].get("movieFile")
    exists = mf and Path(mf.get("path", "")).exists()
    return {"inLibrary": True, "files": {"movie": info(mf)} if found[0].get("hasFile") and exists else {}}


async def import_episode(path: str, series: dict, season: int | None, episode: int | None, height=None,
                         replace=False):
    """Hardlink-import a file (or every parsable file under a folder) into Sonarr.
    Existing episode files are only replaced (Sonarr deletes the old one) when replace=True."""
    sid = series["id"]
    Path(series["path"]).mkdir(parents=True, exist_ok=True)  # Sonarr 500s if a new series folder is missing
    cands = await sonarr.req("GET", "/manualimport",
                             params={"folder": path, "filterExistingFiles": "false"})  # no seriesId: that scans the library folder instead
    if not cands:
        raise RuntimeError(f"Sonarr found no video in {path}")
    eps = await sonarr.req("GET", "/episode", params={"seriesId": sid})
    by_num = {(e["seasonNumber"], e["episodeNumber"]): e["id"] for e in eps}
    old_file = {e["id"]: e.get("episodeFileId") or 0 for e in eps}
    quality = await sonarr.quality_for(height)
    files = []
    for c in cands:
        if len(cands) == 1 and season is not None and episode is not None:
            eid = by_num.get((season, episode))
            if not eid:
                raise RuntimeError(f"S{season:02}E{episode:02} does not exist in Sonarr for {series['title']}")
            ids = [eid]
        else:
            ids = [e["id"] for e in c.get("episodes") or []]
            if not ids:
                continue
        files.append({"path": c["path"], "folderName": c.get("folderName", ""), "seriesId": sid, "episodeIds": ids,
                      "quality": quality or c["quality"], "languages": c.get("languages") or [],
                      "releaseGroup": c.get("releaseGroup") or "Telegram", "indexerFlags": 0,
                      "releaseType": "singleEpisode", "downloadId": ""})
    if not files:
        raise RuntimeError("Sonarr could not map any file to an episode; set S/E manually")
    existing = [i for f in files for i in f["episodeIds"] if old_file.get(i)]
    if existing and not replace:
        raise RuntimeError(f"{len(existing)} episode(s) already in the library; queue again with Replace on to overwrite")
    await sonarr.run_command({"name": "ManualImport", "files": files, "importMode": "copy"})
    imported = {e["id"]: e for e in await sonarr.req("GET", "/episode", params={"seriesId": sid})}
    missing = [i for f in files for i in f["episodeIds"]
               if not imported[i]["hasFile"] or imported[i].get("episodeFileId") == old_file.get(i)]
    if missing:
        raise RuntimeError(f"Sonarr import finished but {len(missing)} episode(s) did not get the new file (check Sonarr logs)")
    return (await sonarr.req("GET", f"/series/{sid}"))["path"]


async def import_movie(path: str, movie: dict, height=None, replace=False):
    mid = movie["id"]
    old_file = (await radarr.req("GET", f"/movie/{mid}")).get("movieFile", {}).get("id", 0)
    if old_file and not replace:
        raise RuntimeError("Movie already in the library; queue again with Replace on to overwrite")
    Path(movie["path"]).mkdir(parents=True, exist_ok=True)
    cands = await radarr.req("GET", "/manualimport",
                             params={"folder": path, "filterExistingFiles": "false"})
    if not cands:
        raise RuntimeError(f"Radarr found no video in {path}")
    c = max(cands, key=lambda c: c.get("size", 0))  # main feature, not extras/samples
    quality = await radarr.quality_for(height)
    files = [{"path": c["path"], "folderName": c.get("folderName", ""), "movieId": mid,
              "quality": quality or c["quality"], "languages": c.get("languages") or [],
              "releaseGroup": c.get("releaseGroup") or "Telegram", "indexerFlags": 0, "downloadId": ""}]
    await radarr.run_command({"name": "ManualImport", "files": files, "importMode": "copy"})
    movie = await radarr.req("GET", f"/movie/{mid}")
    if not movie.get("hasFile") or movie.get("movieFile", {}).get("id") == old_file:
        raise RuntimeError("Radarr import finished but the movie did not get the new file (check Radarr logs)")
    return movie["path"]


# ---------- plex ----------

async def plex_ok():
    if not config.PLEX_URL:
        return "not configured"
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.get(f"{config.PLEX_URL}/identity", params={"X-Plex-Token": config.PLEX_TOKEN})
            return ET.fromstring(r.text).get("version", "ok")
    except Exception as e:
        return f"error: {e}"


async def plex_refresh(arr_path: str):
    if not (config.PLEX_URL and config.PLEX_TOKEN):
        return
    src, dst = config.PLEX_PATH_MAP.split("=", 1)
    plex_path = dst + arr_path[len(src):] if arr_path.startswith(src) else arr_path
    async with httpx.AsyncClient(timeout=30) as h:
        r = await h.get(f"{config.PLEX_URL}/library/sections", params={"X-Plex-Token": config.PLEX_TOKEN})
        for d in ET.fromstring(r.text).iter("Directory"):
            if any(plex_path.startswith(loc.get("path", "\0")) for loc in d.iter("Location")):
                await h.get(f"{config.PLEX_URL}/library/sections/{d.get('key')}/refresh",
                            params={"path": plex_path, "X-Plex-Token": config.PLEX_TOKEN})
