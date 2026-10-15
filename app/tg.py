"""Telegram (MTProto user client) access: resolve/join sources, list files, download with resume."""
import logging
import re
from pathlib import Path

from telethon import TelegramClient, functions, types, utils

from . import config
from .parse import classify, explicit, guess

log = logging.getLogger("tgarr.tg")

client: TelegramClient | None = None
state = {"configured": bool(config.TG_API_ID and config.TG_API_HASH), "authorized": False, "me": None, "error": None}


async def start():
    global client
    if not state["configured"]:
        state["error"] = "TG_API_ID / TG_API_HASH not set"
        return
    client = TelegramClient(config.SESSION, config.TG_API_ID, config.TG_API_HASH)
    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            state.update(authorized=True, me=f"{me.first_name or ''} (@{me.username or me.id})", error=None)
        else:
            state["error"] = "Not logged in: run the login command, then restart the container"
            await client.disconnect()
            client = None
    except Exception as e:  # network etc.
        state["error"] = f"connect failed: {e}"
        client = None


async def stop():
    if client:
        await client.disconnect()


async def keeper():
    """Retry the initial connection forever. After a reboot the container can start before the network is up;
    start() then fails once and nothing else would ever reconnect (Telethon only auto-reconnects an
    established client)."""
    import asyncio
    while True:
        await asyncio.sleep(30)
        if state["configured"] and not state["authorized"] and str(state.get("error") or "").startswith("connect failed"):
            log.info("retrying Telegram connection")
            await start()
            if state["authorized"]:
                log.info("Telegram connected as %s", state["me"])


def need():
    if not client or not state["authorized"]:
        raise RuntimeError(state["error"] or "Telegram not connected")
    return client


def chat_ref(link: str) -> int | None:
    """Marked chat id from 'tg:-100…', a bare id, or a private message link t.me/c/<id>/<msg>."""
    link = link.strip()
    if m := re.match(r"^tg:(-?\d+)$", link) or re.match(r"^(-?\d+)$", link):
        return int(m[1])
    if m := re.search(r"t\.me/c/(\d+)", link):
        return int(f"-100{m[1]}")
    return None


async def entity_by_id(chat_id: int):
    c = need()
    try:
        return await c.get_entity(chat_id)
    except ValueError:
        await c.get_dialogs()  # fills the session's entity cache with every chat the user is in
        return await c.get_entity(chat_id)


async def dialogs(q: str = "", limit: int = 400) -> list[dict]:
    """Groups and channels the logged-in account is a member of (for picking a source without a link)."""
    c = need()
    q = q.strip().lower()
    out = []
    async for d in c.iter_dialogs(limit=limit):
        if not (d.is_group or d.is_channel) or (q and q not in (d.name or "").lower()):
            continue
        e = d.entity
        out.append({"chat_id": d.id, "title": d.name, "username": getattr(e, "username", None),
                    "kind": "channel" if d.is_channel and not d.is_group else "group",
                    "members": getattr(e, "participants_count", None), "unread": d.unread_count})
    return out


async def resolve(link: str) -> dict:
    """Return {chat_id, title, joined} for an invite link, t.me/name, @name, a numeric/tg: id or a t.me/c/ link."""
    c = need()
    link = link.strip()
    if (ref := chat_ref(link)) is not None:
        ent = await entity_by_id(ref)
        return {"chat_id": utils.get_peer_id(ent), "title": getattr(ent, "title", None) or utils.get_display_name(ent),
                "joined": not getattr(ent, "left", False)}
    target, is_invite = utils.parse_username(link)
    if is_invite:
        r = await c(functions.messages.CheckChatInviteRequest(hash=target))
        if isinstance(r, types.ChatInviteAlready):
            return {"chat_id": utils.get_peer_id(r.chat), "title": r.chat.title, "joined": True}
        if isinstance(r, types.ChatInvitePeek):
            return {"chat_id": utils.get_peer_id(r.chat), "title": r.chat.title, "joined": False}
        return {"chat_id": None, "title": r.title, "joined": False}
    ent = await c.get_entity(int(link) if link.lstrip("-").isdigit() else (target or link))
    title = getattr(ent, "title", None) or utils.get_display_name(ent)
    joined = not getattr(ent, "left", False)
    return {"chat_id": utils.get_peer_id(ent), "title": title, "joined": joined}


async def join(link: str) -> dict:
    c = need()
    target, is_invite = utils.parse_username(link.strip())
    if is_invite:
        try:
            await c(functions.messages.ImportChatInviteRequest(hash=target))
        except Exception as e:
            if "already" not in str(e).lower():  # UserAlreadyParticipantError
                raise
    else:
        await c(functions.channels.JoinChannelRequest(await c.get_entity(target)))
    return await resolve(link)


async def entity_for(source: dict):
    c = need()
    if source.get("chat_id"):
        try:
            return await entity_by_id(source["chat_id"])
        except ValueError:
            pass  # not a chat we're in anymore, fall back to the link
    info = await resolve(source["link"])
    if not info["joined"] or not info["chat_id"]:
        raise RuntimeError(f"Not a member of '{info['title']}': join it first")
    source["chat_id"] = info["chat_id"]
    return await c.get_entity(info["chat_id"])


def describe(m) -> dict | None:
    if not m.document:
        return None
    f = m.file
    name = f.name or ""
    kind = classify(name or f"x{f.ext or ''}", f.mime_type)
    if not kind:
        return None
    caption = m.message or ""
    return {
        "msg_id": m.id,
        "date": m.date.isoformat(),
        "name": name or f"telegram_{m.id}{f.ext or ''}",
        "caption": caption[:400],
        "size": f.size,
        "mime": f.mime_type,
        "kind": kind,
        "height": f.height,
        "duration": f.duration,
        **guess(name, caption),
        "grouped_id": m.grouped_id,
    }


async def list_files(source: dict, search: str | None = None, limit: int | None = None) -> list[dict]:
    c = need()
    ent = await entity_for(source)
    out, headers = [], []  # headers: (msg_id, season) from text posts like "Season 2" announcements
    async for m in c.iter_messages(ent, limit=limit or config.SCAN_LIMIT, search=search or None):
        if d := describe(m):
            d["source_id"] = source["id"]
            d["source_title"] = source.get("title")
            out.append(d)
        elif not m.document and m.message and (s := explicit(m.message)[0]) is not None:
            headers.append((m.id, s))
    apply_context(out, headers)
    if not search:  # full scan: each channel season's highest and lowest episode number (for continuous numbering)
        sizes: dict[str, int] = {}
        mins: dict[str, int] = {}
        for f in out:
            if f["kind"] == "video" and f["episode"] is not None and not f.get("extra"):
                k = str(f["season"] if f["season"] is not None else 1)
                sizes[k] = max(sizes.get(k, 0), f["episode"])
                mins[k] = min(mins.get(k, f["episode"]), f["episode"])
        source["season_sizes"] = sizes
        source["season_mins"] = mins
    return out


TME = re.compile(r"https?://t\.me/[^\s)\]>\"']+")


async def channel_links(chat: str | int, limit: int = 3000) -> list[dict]:
    """Every t.me link a channel posts (text, hidden text links, buttons) with the message text around it.
    Read-only; used to turn "directory" channels into a catalogue of per-series channels."""
    c = need()
    ent = await (entity_by_id(chat) if isinstance(chat, int) else c.get_entity(chat))
    out, seen = [], set()
    async for m in c.iter_messages(ent, limit=limit):
        urls = set(TME.findall(m.message or ""))
        for e in m.entities or []:
            if isinstance(e, types.MessageEntityTextUrl) and "t.me/" in e.url:
                urls.add(e.url)
        for row in getattr(m.reply_markup, "rows", None) or []:
            for b in row.buttons:
                if "t.me/" in (getattr(b, "url", None) or ""):
                    urls.add(b.url)
        for u in urls:
            u = u.rstrip(".,!?")
            if u in seen:
                continue
            seen.add(u)
            out.append({"url": u, "msg_id": m.id, "date": m.date.isoformat(), "text": (m.message or "")[:300],
                        "kind": "invite" if "/+" in u or "joinchat" in u else
                                "post" if re.search(r"t\.me/(c/)?[^/]+/\d+", u) else "public"})
    return out


AVATARS = config.CONFIG_DIR / "avatars"


async def avatar(chat_id: int) -> Path | None:
    """Chat profile photo, downloaded once and cached on disk. None if the chat has no photo."""
    AVATARS.mkdir(exist_ok=True)
    path = AVATARS / f"{chat_id}.jpg"
    none = AVATARS / f"{chat_id}.none"
    if path.exists():
        return path
    if none.exists():
        return None
    ent = await entity_by_id(chat_id)
    got = await need().download_profile_photo(ent, file=str(path), download_big=False)
    if not got:
        none.touch()
        return None
    return path


EXTRA_WORDS = re.compile(
    r"(?i)trailer|teaser|promo|preview|\bPV\d*\b|\bNCOP\b|\bNCED\b|\bOP\d*\b|\bED\d*\b|opening|ending|sample|making.of|"
    r"recap|clip|\bspecial\b|\bOVA\b|\bOAD\b|טריילר|טיזר|פרומו|קדימון|פתיח|סיום|קליפ|הצצה|מאחורי הקלעים|ספיישל")


def flag_extras(files: list[dict]):
    """Mark videos that are clearly not episodes: trailer/promo/opening words, or far smaller/shorter
    than the channel's typical episode. The UI hides them by default; nothing is deleted."""
    vids = [f for f in files if f["kind"] == "video"]
    sizes = sorted(f["size"] or 0 for f in vids)
    median = sizes[len(sizes) // 2] if len(sizes) >= 5 else 0
    for f in vids:
        text = f"{f['name']} {f['caption']}"
        # a word like "ending"/"סיום" also appears in "season finale" (סיום העונה): words alone never override a file
        # that clearly is an episode (explicit episode number + normal size + normal length)
        episode_like = (f.get("episode_src") in ("caption", "filename")
                        and (not median or (f["size"] or 0) >= median * 0.5)
                        and (not f.get("duration") or f["duration"] >= 600))
        if not episode_like and (m := EXTRA_WORDS.search(text)):
            f["extra"] = f"looks like a {m[0].strip().lower()}"
        elif f.get("duration") and f["duration"] < 240:
            secs = int(f["duration"])  # Telegram reports fractional seconds
            f["extra"] = f"only {secs // 60}:{secs % 60:02d} long"
        elif median and (f["size"] or 0) < median * 0.25:
            f["extra"] = f"much smaller than the other videos ({round((f['size'] or 0) / 1e6)} MB)"


def apply_context(files: list[dict], headers: list[tuple[int, int]]):
    """Fill missing seasons from the nearest earlier season marker (announcement post or file) in post order,
    and flag episode numbers that collide so the UI can ask the user to check them."""
    events = sorted([(mid, "h", s) for mid, s in headers] + [(f["msg_id"], "f", f) for f in files], key=lambda e: e[0])
    current = None
    for _, kind, v in events:
        if kind == "h":
            current = v
        elif v["season"] is not None:
            current = v["season"]
        elif v["kind"] == "video" and current is not None:
            v["season"], v["season_src"] = current, "context"
    flag_extras(files)
    seen: dict[tuple, list] = {}
    for f in files:
        if f["kind"] == "video" and f["episode"] is not None and not f.get("extra"):
            seen.setdefault((f["season"] or 1, f["episode"]), []).append(f)
    for dupes in seen.values():
        for f in dupes:
            f["duplicate"] = len(dupes) > 1


CHUNK = 512 * 1024


async def download(source: dict, msg_id: int, dest: Path, progress) -> Path:
    """Download message media to dest, resuming from dest.part if present."""
    c = need()
    ent = await entity_for(source)
    m = await c.get_messages(ent, ids=msg_id)  # fresh file reference
    if not m or not m.document:
        raise RuntimeError(f"message {msg_id} has no file anymore")
    size = m.file.size
    if dest.exists() and dest.stat().st_size == size:
        progress(size, size)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    have -= have % CHUNK
    with open(part, "r+b" if part.exists() else "wb") as fh:
        fh.truncate(have)
        fh.seek(have)
        async for chunk in c.iter_download(m.document, offset=have, request_size=CHUNK, file_size=size):
            fh.write(chunk)
            have += len(chunk)
            progress(have, size)
    if have != size:
        raise RuntimeError(f"incomplete download {have}/{size}")
    part.rename(dest)
    return dest
