#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
import unicodedata
from datetime import datetime
from hashlib import sha256
from typing import Any, Dict, Optional, Tuple

import aiohttp
import pytz
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.tl.functions.account import UpdateProfileRequest

# ── Config ──────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

if not load_dotenv() and not os.path.exists(".env"):
    logging.error("Missing .env file. Copy .env.example to .env and fill in your values.")
    sys.exit(1)

def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        logging.error("Missing required env var: %s", key)
        sys.exit(1)
    return val

API_ID = int(_require("API_ID"))
API_HASH = _require("API_HASH")
PREFIX = _require("PREFIX")
CITY = _require("CITY")
TZ = pytz.timezone(_require("TZ"))
OW_KEY = os.getenv("OW_KEY", "").strip()
VT_KEY = os.getenv("VT_KEY", "").strip()
PROFILE_INTERVAL = int(_require("PROFILE_INTERVAL"))
WEATHER_CACHE = int(_require("WEATHER_CACHE"))

# ── Emoji constants ────────────────────────────────────────────────
E_SUN = '☀️'  # ☀️
E_CLOUD_SUN = '⛅'  # ⛅
E_CLOUD = '☁️'  # ☁️
E_WARN = '⚠️'  # ⚠️
E_CROSS = '❌'  # ❌
E_CHECK = '✅'  # ✅
E_PING = "\U0001f3d3"        # 🏓
E_SEARCH = "\U0001f50e"      # 🔎
E_GLOBE = "\U0001f310"       # 🌐
E_SHIELD = "\U0001f6e1"      # 🛡
E_GREEN = "\U0001f7e2"       # 🟢
E_RED = "\U0001f534"         # 🔴
E_YELLOW = "\U0001f7e1"      # 🟡
E_REFRESH = "\U0001f504"     # 🔄
E_RECYCLE = '♻️'  # ♻️
E_PIN = "\U0001f4cc"         # 📌
E_CLIP = "\U0001f4ce"        # 📎
E_MAILBOX = "\U0001f4ed"     # 📭
E_TRASH = "\U0001f5d1"       # 🗑
E_DOWN = '⬇️'  # ⬇️
E_ELLIPSIS = '…'  # …
E_DASH = '–'  # –

E_DEGREE = '°'          # degree
E_BULLET = '•'          # bullet

# ── Client & State ─────────────────────────────────────────────────

client = TelegramClient("session", API_ID, API_HASH)
_http: Optional[aiohttp.ClientSession] = None
_weather_cache: Tuple[float, Optional[Dict[str, Any]]] = (0.0, None)
_weather_lock = asyncio.Lock()
_last_bio = ""
_profile_errors = 0
_shutting_down = False

# ── Helpers ─────────────────────────────────────────────────────────

def pat(cmd: str) -> str:
    return rf"^{re.escape(PREFIX)}{cmd}\b(?:\s+([\s\S]+))?$"

def clamp(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + E_ELLIPSIS

def _normalize_name(name: str) -> str:
    """Normalize note name: NFKC + casefold + squeeze whitespace."""
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def _escape_md(s: str) -> str:
    """Escape markdown special chars for safe display."""
    return s.replace("`", "｀")

async def safe_edit(e, text: str) -> None:
    try:
        await e.edit(text)
    except MessageNotModifiedError:
        pass
    except Exception as exc:
        logging.warning("safe_edit: %s", exc)

async def get_http() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(sock_connect=5, sock_read=10, total=15),
            headers={"User-Agent": "Selfbot/1.0"},
        )
    return _http

async def close_http() -> None:
    global _http
    if _http and not _http.closed:
        await _http.close()
        _http = None

def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    """Hash file in chunks to avoid loading entire file into RAM."""
    h = sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()

async def download_replied_file(e) -> Optional[Tuple[Any, str]]:
    if not e.is_reply:
        await e.edit(f"{E_WARN} Reply to a file.")
        return None
    r = await e.get_reply_message()
    if not r.file:
        await e.edit(f"{E_WARN} No file in reply.")
        return None
    path = await r.download_media()
    if not path:
        await e.edit(f"{E_CROSS} Download failed.")
        return None
    return r, path

# ── Weather & Bio ───────────────────────────────────────────────────

def _cloud_icon(c: int) -> str:
    if c < 20:
        return E_SUN
    return E_CLOUD_SUN if c < 60 else E_CLOUD

async def fetch_weather() -> Dict[str, Any]:
    if not OW_KEY:
        return {}

    global _weather_cache

    async with _weather_lock:
        ts, cached = _weather_cache
        if cached is not None and (time.time() - ts) < WEATHER_CACHE:
            return {} if cached.get("_fail") else cached

        try:
            s = await get_http()
            async with s.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={"q": CITY, "appid": OW_KEY, "units": "metric", "lang": "vi"},
            ) as r:
                if r.status != 200:
                    logging.warning("Weather API HTTP %s", r.status)
                    _weather_cache = (time.time(), {"_fail": True})
                    return {}
                data = await r.json(content_type=None)

            if data.get("cod") != 200:
                logging.warning("Weather API: %s", data.get("message", data.get("cod")))
                result = {"_fail": True}
            else:
                m = data["main"]
                result = {
                    "icon": _cloud_icon(data.get("clouds", {}).get("all", 0)),
                    "temp": round(float(m["temp"])),
                    "hum": int(m["humidity"]),
                    "wind": float(data.get("wind", {}).get("speed", 0)),
                    "sr": datetime.fromtimestamp(data["sys"]["sunrise"], TZ).strftime("%H:%M"),
                    "ss": datetime.fromtimestamp(data["sys"]["sunset"], TZ).strftime("%H:%M"),
                }
        except Exception as exc:
            logging.warning("Weather fetch: %s", exc)
            result = {"_fail": True}

        _weather_cache = (time.time(), result)
        return {} if result.get("_fail") else result

def build_bio(w: Dict[str, Any]) -> str:
    if not w:
        return clamp(CITY, 70)
    return clamp(
        f"{CITY} {w['temp']}{E_DEGREE}C {w['icon']} {E_BULLET} {w['hum']}% {E_BULLET} {w['wind']:.1f}m/s {E_BULLET} {w['sr']}{E_DASH}{w['ss']}",
        70,
    )

async def profile_loop() -> None:
    global _last_bio, _profile_errors
    await asyncio.sleep(5)
    while True:
        try:
            b = build_bio(await fetch_weather())
            if b != _last_bio:
                await client(UpdateProfileRequest(about=b))
                _last_bio = b
                _profile_errors = 0
                logging.info("Bio -> %s", b)
        except Exception as exc:
            _profile_errors += 1
            logging.warning("profile_loop: %s (err #%d)", exc, _profile_errors)

        if _profile_errors > 0:
            backoff = min(30 * (2 ** (_profile_errors - 1)), 1800)
            await asyncio.sleep(backoff)
        else:
            await asyncio.sleep(PROFILE_INTERVAL)

# ── Notes Index (RAM cache + JSON persistence) ─────────────────────

NOTE_TAG = E_PIN
NOTES_INDEX_FILE = "notes_index.json"
_notes_index: Dict[str, Dict[str, Any]] = {}  # {norm: {"id": int, "name": str}}

def _load_notes_index() -> None:
    global _notes_index
    try:
        if not os.path.exists(NOTES_INDEX_FILE):
            _notes_index = {}
            return
        with open(NOTES_INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # migrate old format: {norm: id}
        if isinstance(data, dict) and data and all(isinstance(v, int) for v in data.values()):
            _notes_index = {}  # will rebuild on first use
            _save_notes_index()
        elif isinstance(data, dict):
            _notes_index = data
        else:
            _notes_index = {}
    except Exception:
        _notes_index = {}

def _save_notes_index() -> None:
    try:
        with open(NOTES_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(_notes_index, f, ensure_ascii=False)
    except Exception as exc:
        logging.warning("Failed to save notes index: %s", exc)

async def _rebuild_notes_index() -> None:
    global _notes_index
    _notes_index.clear()
    max_notes = 500
    found = 0
    async for msg in client.iter_messages("me", limit=3000):
        raw = msg.raw_text or ""
        if raw.startswith(NOTE_TAG) and msg.reply_to:
            shown = raw[len(NOTE_TAG):].strip()
            norm = _normalize_name(shown)
            if norm and norm not in _notes_index:
                _notes_index[norm] = {"id": msg.reply_to.reply_to_msg_id, "name": shown}
                found += 1
                if found >= max_notes:
                    break
    _save_notes_index()
    logging.info("Notes index rebuilt: %d notes", len(_notes_index))

# ── Help ────────────────────────────────────────────────────────────

HELP = "\n".join([
    f"**Selfbot** (`{PREFIX}`)",
    "",
    f"`{PREFIX}help` \u2014 commands list",
    f"`{PREFIX}ping` \u2014 latency",
    f"`{PREFIX}id` \u2014 chat / user ID",
    f"`{PREFIX}del` \u2014 delete replied msg",
    f"`{PREFIX}purge <n>` \u2014 bulk delete",
    f"`{PREFIX}virus` \u2014 VirusTotal lookup",
    f"`{PREFIX}rebio` \u2014 force bio update",
    f"`{PREFIX}reload` \u2014 restart bot",
    "",
    "**Save / Send:**",
    f"`{PREFIX}save <name>` \u2014 save replied msg with a note name",
    f"`{PREFIX}saved` \u2014 list all saved notes",
    f"`{PREFIX}get <name>` \u2014 forward saved note to current chat",
    f"`{PREFIX}delnote <name>` \u2014 delete a saved note",
    f"`{PREFIX}rename <old> | <new>`  rename a saved note",
])

# ── Commands ────────────────────────────────────────────────────────

@client.on(events.NewMessage(pattern=pat("help"), outgoing=True))
async def cmd_help(e):
    await e.edit(HELP)

@client.on(events.NewMessage(pattern=pat("ping"), outgoing=True))
async def cmd_ping(e):
    t0 = time.perf_counter()
    await e.edit(E_ELLIPSIS)
    await e.edit(f"{E_PING} {int((time.perf_counter() - t0) * 1000)} ms")

@client.on(events.NewMessage(pattern=pat("id"), outgoing=True))
async def cmd_id(e):
    if e.is_reply:
        r = await e.get_reply_message()
        await e.edit(f"chat: `{e.chat_id}`\nuser: `{r.sender_id}`")
    else:
        await e.edit(f"chat: `{e.chat_id}`")

@client.on(events.NewMessage(pattern=pat("del"), outgoing=True))
async def cmd_del(e):
    if not e.is_reply:
        return
    r = await e.get_reply_message()
    try:
        await r.delete()
        await e.delete()
    except Exception:
        await e.edit(f"{E_CROSS} Can't delete.")

@client.on(events.NewMessage(pattern=pat("purge"), outgoing=True))
async def cmd_purge(e):
    arg = (e.pattern_match.group(1) or "").strip()
    try:
        n = max(1, min(200, int(arg) if arg else 10))
    except ValueError:
        return await e.edit(f"{E_WARN} {PREFIX}purge <number>")

    scan_limit = min(2000, n * 8)
    ids = []
    async for msg in client.iter_messages(e.chat_id, limit=scan_limit):
        if msg.out and msg.id != e.id:
            ids.append(msg.id)
            if len(ids) >= n:
                break

    if ids:
        try:
            await client.delete_messages(e.chat_id, ids, revoke=True)
        except Exception:
            return await e.edit(f"{E_CROSS} Purge failed.")
    await e.delete()

@client.on(events.NewMessage(pattern=pat("virus"), outgoing=True))
async def cmd_virus(e):
    if not VT_KEY:
        return await e.edit(f"{E_WARN} VT_KEY not set.")
    result = await download_replied_file(e)
    if not result:
        return
    _, path = result

    try:
        await e.edit(f"{E_SEARCH} Scanning{E_ELLIPSIS}")
        h = sha256_file(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    await e.edit(f"{E_GLOBE} VT lookup{E_ELLIPSIS}")
    try:
        s = await get_http()
        async with s.get(
            f"https://www.virustotal.com/api/v3/files/{h}",
            headers={"x-apikey": VT_KEY},
        ) as resp:
            if resp.status not in (200, 404):
                return await e.edit(f"{E_CROSS} VT HTTP {resp.status}")
            data = await resp.json(content_type=None)
    except Exception:
        return await e.edit(f"{E_CROSS} VT request failed.")

    if "data" not in data:
        return await e.edit(f"{E_YELLOW} Not found on VT\n`{h}`")

    st = data["data"]["attributes"]["last_analysis_stats"]
    mal, sus, und = st.get("malicious", 0), st.get("suspicious", 0), st.get("undetected", 0)
    icon = f"{E_GREEN} Clean" if (mal == 0 and sus == 0) else f"{E_RED} Detected"
    await e.edit(f"{E_SHIELD} {icon}  mal:`{mal}` sus:`{sus}` und:`{und}`\n`{h}`")

@client.on(events.NewMessage(pattern=pat("rebio"), outgoing=True))
async def cmd_rebio(e):
    global _last_bio, _profile_errors
    await e.edit(f"{E_REFRESH} Updating{E_ELLIPSIS}")
    try:
        b = build_bio(await fetch_weather())
        await client(UpdateProfileRequest(about=b))
        _last_bio = b
        _profile_errors = 0
        await e.edit(f"{E_CHECK} {b}")
    except Exception as exc:
        await e.edit(f"{E_CROSS} {exc}")

@client.on(events.NewMessage(pattern=pat("reload"), outgoing=True))
async def cmd_reload(e):
    await e.edit(f"{E_RECYCLE} Restarting{E_ELLIPSIS}")
    await close_http()
    os.execv(sys.executable, [sys.executable] + sys.argv)

# ── Save & Send Commands ───────────────────────────────────────────

@client.on(events.NewMessage(pattern=pat("save"), outgoing=True))
async def cmd_save(e):
    """Save replied message to Saved Messages with a note name (forward-based)."""
    arg = (e.pattern_match.group(1) or "").strip()
    if not arg:
        return await safe_edit(e, f"{E_WARN} Usage: `{PREFIX}save <name>` (reply to a message)")

    if not e.is_reply:
        return await safe_edit(e, f"{E_WARN} Reply to a message to save it.")

    r = await e.get_reply_message()
    if not r:
        return await safe_edit(e, f"{E_CROSS} Cannot get replied message.")

    name = " ".join(arg.split())
    norm = _normalize_name(name)
    tag_line = f"{NOTE_TAG} {name}"

    if not _notes_index:
        await _rebuild_notes_index()

    # If note with same name exists -> refuse (no overwrite)
    if norm in _notes_index:
        old_name = " ".join(((_notes_index[norm].get("name") or name) or "").splitlines())
        show = _escape_md(old_name)
        return await safe_edit(
            e,
            f"{E_WARN} Note `{show}` already exists.\n"
            f"Delete it first: `{PREFIX}delnote {show}`\n"
            f"Then save again."
        )

    try:
        # Forward content to Saved Messages (fast, no download/upload)
        content_msg = None
        try:
            content_msg = await r.forward_to("me")
        except Exception:
            # Fallback: re-send for restricted chats
            if r.media:
                path = await r.download_media()
                if path:
                    raw = r.raw_text or ""
                    ents = list(r.entities or [])
                    try:
                        content_msg = await client.send_file(
                            "me", path, caption=raw or "",
                            formatting_entities=ents or None)
                    finally:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                else:
                    content_msg = await client.send_message(
                        "me", r.raw_text or "[media failed]")
            else:
                raw = r.raw_text or ""
                ents = list(r.entities or [])
                content_msg = await client.send_message(
                    "me", raw or "[empty]",
                    formatting_entities=ents or None)

        # Reply to content with the tag (for indexing)
        if content_msg:
            await client.send_message("me", tag_line, reply_to=content_msg.id)
            _notes_index[norm] = {"id": content_msg.id, "name": name}
            _save_notes_index()

        await safe_edit(e, f"{E_CHECK} Saved as `{_escape_md(name)}`")
    except Exception as exc:
        await safe_edit(e, f"{E_CROSS} Save failed: {exc}")

@client.on(events.NewMessage(pattern=pat("saved"), outgoing=True))
async def cmd_saved(e):
    """List all saved notes (from cached index)."""
    try:
        if not _notes_index:
            await safe_edit(e, f"{E_REFRESH} Rebuilding index{E_ELLIPSIS}")
            await _rebuild_notes_index()

        if not _notes_index:
            return await safe_edit(e, f"{E_MAILBOX} No saved notes. Use `{PREFIX}save <name>` to save.")

        items = sorted(_notes_index.values(), key=lambda x: _normalize_name(x.get("name", "")))
        max_show = 80
        shown = items[:max_show]

        lines = ["**Saved Notes:**\n"]
        for it in shown:
            nm = _escape_md(it.get("name", "") or "")
            lines.append(f"{E_BULLET} `{nm}`")

        if len(items) > max_show:
            lines.append(f"\n{E_DOWN} Showing {max_show}/{len(items)} (too many to display)")

        lines.append(f"\nTotal: {len(items)}")
        lines.append(f"Use `{PREFIX}get <name>` to send")
        await safe_edit(e, "\n".join(lines))
    except Exception as exc:
        await safe_edit(e, f"{E_CROSS} Error: {exc}")

@client.on(events.NewMessage(pattern=pat("get"), outgoing=True))
async def cmd_get(e):
    """Forward saved note to current chat (fast, server-side forward)."""
    if not _notes_index:
        await _rebuild_notes_index()

    arg = (e.pattern_match.group(1) or "").strip()
    if not arg:
        return await safe_edit(e, f"{E_WARN} Usage: `{PREFIX}get <name>`")

    norm = _normalize_name(arg)
    chat_id = e.chat_id

    try:
        entry = _notes_index.get(norm)
        msg_id = entry["id"] if entry else None

        # Cache miss -> try rebuild once
        if msg_id is None:
            await _rebuild_notes_index()
            entry = _notes_index.get(norm)
            msg_id = entry["id"] if entry else None

        if msg_id is None:
            return await safe_edit(e, f"{E_CROSS} Note `{_escape_md(arg)}` not found.")

        content_msg = await client.get_messages("me", ids=msg_id)
        if not content_msg:
            _notes_index.pop(norm, None)
            _save_notes_index()
            return await safe_edit(e, f"{E_CROSS} Content message was deleted.")

        try:
            await e.delete()
        except Exception:
            pass
        await content_msg.forward_to(chat_id)
    except Exception as exc:
        try:
            await safe_edit(e, f"{E_CROSS} Send failed: {exc}")
        except Exception:
            logging.warning("cmd_get error: %s", exc)

@client.on(events.NewMessage(pattern=pat("rename"), outgoing=True))
async def cmd_rename(e):
    """Rename a saved note: /rename old | new"""
    if not _notes_index:
        await _rebuild_notes_index()

    arg = (e.pattern_match.group(1) or "").strip()
    if "|" not in arg:
        return await safe_edit(e, f"{E_WARN} Usage: `{PREFIX}rename old name | new name`")

    parts = arg.split("|", 1)
    old_name = parts[0].strip()
    new_name = parts[1].strip()
    if not old_name or not new_name:
        return await safe_edit(e, f"{E_WARN} Both old and new names required.")

    old_norm = _normalize_name(old_name)
    new_norm = _normalize_name(new_name)

    entry = _notes_index.get(old_norm)
    if not entry:
        return await safe_edit(e, f"{E_CROSS} Note `{old_name}` not found.")

    if new_norm in _notes_index:
        return await safe_edit(e, f"{E_WARN} Note `{new_name}` already exists.")

    # Update index
    _notes_index.pop(old_norm, None)
    _notes_index[new_norm] = {"id": entry["id"], "name": new_name}
    _save_notes_index()

    # Update tag message in Saved Messages
    try:
        async for msg in client.iter_messages("me", limit=2000):
            raw = msg.raw_text or ""
            if raw.startswith(NOTE_TAG) and msg.reply_to and msg.reply_to.reply_to_msg_id == entry["id"]:
                await msg.edit(f"{NOTE_TAG} {new_name}")
                break
    except Exception:
        pass

    await safe_edit(e, f"{E_CHECK} Renamed `{_escape_md(old_name)}` -> `{_escape_md(new_name)}`")

@client.on(events.NewMessage(pattern=pat("delnote"), outgoing=True))
async def cmd_delnote(e):
    """Delete a saved note by name."""
    if not _notes_index:
        await _rebuild_notes_index()

    arg = (e.pattern_match.group(1) or "").strip()
    if not arg:
        return await safe_edit(e, f"{E_WARN} Usage: `{PREFIX}delnote <name>`")

    norm = _normalize_name(arg)
    entry = _notes_index.pop(norm, None)
    msg_id = entry["id"] if entry else None

    if msg_id is None:
        return await safe_edit(e, f"{E_CROSS} Note `{_escape_md(arg)}` not found.")

    _save_notes_index()

    # Try to delete the content + tag messages from Saved Messages
    try:
        async for msg in client.iter_messages("me", limit=2000):
            raw = msg.raw_text or ""
            if raw.startswith(NOTE_TAG) and msg.reply_to and msg.reply_to.reply_to_msg_id == msg_id:
                await msg.delete()
                break
        await client.delete_messages("me", [msg_id])
    except Exception:
        pass

    await safe_edit(e, f"{E_TRASH} Deleted note `{_escape_md(arg)}`.")

# ── Entry ───────────────────────────────────────────────────────────

async def _shutdown() -> None:
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    logging.info("Shutting down...")
    await close_http()
    await client.disconnect()

def _handle_signal():
    asyncio.create_task(_shutdown())

async def main() -> None:
    await client.start()
    me = await client.get_me()
    logging.info("Started - @%s (%s)", me.username or me.first_name, me.id)

    _load_notes_index()

    loop = asyncio.get_event_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig:
            try:
                loop.add_signal_handler(sig, _handle_signal)
            except NotImplementedError:
                pass  # Windows

    bg = asyncio.create_task(profile_loop())
    try:
        await client.run_until_disconnected()
    finally:
        bg.cancel()
        await close_http()

if __name__ == "__main__":
    asyncio.run(main())