"""Guess season/episode from Telegram file names and captions (English + Hebrew)."""
import re
from pathlib import PurePath

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm", ".wmv"}
SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
ARCHIVE_EXT = {".zip", ".rar", ".7z"}

NOISE = re.compile(
    r"(?i)\b(?:2160|1080|720|576|480|360)[pi]\b|\b[xh]\.?26[45]\b|\bhevc\b|\b10.?bit\b"
    r"|\b(?:19|20)\d{2}\b|\b\d\.\d\b|\baac\d?\b|\bddp?\d?\b|\bv\d\b|\b\d{3,4}x\d{3,4}\b"
)
SEASON_WORD = r"(?:season|עונה)"
EPISODE_WORD = r"(?:episode|\bep\.?|פרק|\bE)"


def classify(name: str, mime: str | None) -> str | None:
    ext = PurePath(name or "").suffix.lower()
    mime = mime or ""
    if ext in VIDEO_EXT or mime.startswith("video/"):
        return "video"
    if ext in ARCHIVE_EXT or mime in ("application/zip", "application/x-rar-compressed",
                                      "application/vnd.rar", "application/x-7z-compressed"):
        return "archive"
    if ext in SUB_EXT:
        return "subtitle"
    return None


GAP = r"[\s:#,.\-–|/]{0,6}"  # what may sit between a season and an episode marker of the same pair


def paired(text: str | None) -> tuple[int, int] | None:
    """Season and episode stated together: S01E02, 1x02, "עונה 1 פרק 12", "פרק 12 עונה 1", "Season 2 Episode 3"."""
    t = (text or "").replace("_", " ")
    if m := re.search(r"(?i)\bS(\d{1,2})[ ._-]?E(\d{1,4})\b", t):
        return int(m[1]), int(m[2])
    if m := re.search(r"\b(\d{1,2})x(\d{2,3})\b", t):
        return int(m[1]), int(m[2])
    if m := re.search(rf"(?i){SEASON_WORD}\s*(\d{{1,2}}){GAP}{EPISODE_WORD}\s*(\d{{1,4}})\b", t):
        return int(m[1]), int(m[2])
    if m := re.search(rf"(?i){EPISODE_WORD}\s*(\d{{1,4}}){GAP}{SEASON_WORD}\s*(\d{{1,2}})\b", t):
        return int(m[2]), int(m[1])
    return None


def explicit(text: str | None) -> tuple[int | None, int | None]:
    """Season/episode only from unambiguous markers (S01E02, 1x02, season/עונה N, episode/ep/פרק/E N)."""
    if p := paired(text):
        return p
    t = (text or "").replace("_", " ")
    season = episode = None
    if m := re.search(rf"(?i){SEASON_WORD}\s*[:#-]?\s*(\d{{1,2}})", t) or re.search(r"(?i)\bS(\d{1,2})\b", t):
        season = int(m[1])
    if m := re.search(rf"(?i){EPISODE_WORD}\s*[:#-]?\s*(\d{{1,4}})\b", t):
        episode = int(m[1])
    return season, episode


def loose_episode(name: str | None) -> int | None:
    """Best-effort episode from a file name without markers: 'Show - 03', '[03]', else the FIRST number,
    so release suffixes like 'Show_01_1.m4v' or '_03_1080P_hash' don't win."""
    t = (name or "").replace("_", " ")
    t = PurePath(t).stem if PurePath(t).suffix.lower() in VIDEO_EXT | ARCHIVE_EXT | SUB_EXT else t
    t = NOISE.sub(" ", t)
    t = re.sub(rf"(?i){SEASON_WORD}\s*\d{{1,2}}|\bS\d{{1,2}}\b", " ", t)
    t = re.sub(r"\[[^\]]*[A-Za-z][^\]]*\]|\([^)]*[A-Za-z][^)]*\)", " ", t)  # [Group] / (1080p) / [HASH] tags
    if m := re.search(r"\s[-–]\s*(\d{1,4})\b", t):
        return int(m[1])
    if m := re.search(r"\[(\d{1,4})\]", t):
        return int(m[1])
    nums = re.findall(r"(?<![\w.])(\d{1,3})(?![\w.])", t)
    return int(nums[0]) if nums else None


def guess(name: str, caption: str) -> dict:
    """Combine caption and file name. Captions are written by the channel for this release, so their explicit
    markers beat the file name (often an upstream release name). Also reports where each value came from."""
    # Season and episode are decided separately, from real cases:
    # - season: a season+episode PAIR beats a lone mention. Caption "פרק 12 - אחרון לעונה! ... עונה 2" only teases
    #   season 2; the file name's "עונה 1 פרק 12" is right.
    # - episode: the caption's number beats the file name, even a file-name pair. Uploaded names carry artifacts
    #   ("עונה_1_פרק_72" for episode 7, "Show_05_1"), while the caption says "פרק 7".
    cp, fp = paired(caption), paired(name)
    cs, ce = explicit(caption)
    ns, ne = explicit(name)
    season, s_src = ((cp[0], "caption") if cp else (fp[0], "filename") if fp else
                     (cs, "caption") if cs is not None else (ns, "filename") if ns is not None else (None, None))
    episode, e_src = ((ce, "caption") if ce is not None else (ne, "filename") if ne is not None else (None, None))
    if episode is None and (episode := loose_episode(name)) is not None:
        e_src = "guess"
    return {"season": season, "season_src": s_src, "episode": episode, "episode_src": e_src}


def safe(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|']", "", name)
    return re.sub(r"\s+", ".", name.strip()).strip(".")
