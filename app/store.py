"""Saved Telegram sources (channels/groups) in /config/sources.json."""
import json
import uuid

from . import config

FILE = config.CONFIG_DIR / "sources.json"


def _load() -> list[dict]:
    return json.loads(FILE.read_text()) if FILE.exists() else []


def _save(items):
    tmp = FILE.with_name(f"sources.{uuid.uuid4().hex[:6]}.tmp")  # unique, so concurrent saves can't collide
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1))
    tmp.replace(FILE)


def sources() -> list[dict]:
    return _load()


def get_source(sid):
    return next((s for s in _load() if s["id"] == sid), None)


def add_source(link, kind, note, info) -> dict:
    items = _load()
    s = {"id": uuid.uuid4().hex[:8], "link": link.strip(), "kind": kind, "note": note, **info}
    items.append(s)
    _save(items)
    return s


def update_source(src: dict):
    items = _load()
    for i, s in enumerate(items):
        if s["id"] == src["id"]:
            items[i] = {**s, **{k: src[k] for k in ("chat_id", "title", "joined", "season_sizes") if k in src}}
    _save(items)


def patch_source(sid: str, fields: dict) -> dict | None:
    """Set/clear user-facing fields: the linked series (tvdbId, series_title, poster) and numbering mode."""
    items, out = _load(), None
    for i, s in enumerate(items):
        if s["id"] == sid:
            out = items[i] = {k: v for k, v in {**s, **fields}.items() if v is not None}
    _save(items)
    return out


def remove_source(sid):
    _save([s for s in _load() if s["id"] != sid])
