#!/usr/bin/env python3

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
from typing import Any, Optional

import aiohttp
import pytz
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.tl.functions.account import UpdateProfileRequest

# ── Config ───────────────────────────────────────────────────────────

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

API_ID           = int(_require("API_ID"))
API_HASH         = _require("API_HASH")
PREFIX           = _require("PREFIX")
CITY             = _require("CITY")
TZ               = pytz.timezone(_require("TZ"))
OW_KEY           = os.getenv("OW_KEY", "").strip()
VT_KEY           = os.getenv("VT_KEY", "").strip()
PROFILE_INTERVAL = int(_require("PROFILE_INTERVAL"))
WEATHER_CACHE    = int(_require("WEATHER_CACHE"))

# ── Client & State ───────────────────────────────────────────────────

client = TelegramClient("session", API_ID, API_HASH)
_http: Optional[aiohttp.ClientSession] = None
_weather_cache: tuple[float, Optional[dict[str, Any]]] = (0.0, None)
_weather_lock   = asyncio.Lock()
_last_bio       = ""
_profile_errors = 0
_shutting_down  = False
_start_time     = time.time()

# ── Helpers ──────────────────────────────────────────────────────────

def pat(cmd: str) -> str:
    return rf"^{re.escape(PREFIX)}{cmd}\b(?:\s+([\s\S]+))?$"

def clamp(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"

def _normalize(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())

def _esc(s: str) -> str:
    return s.replace("`", "｀")

async def safe_edit(e, text: str) -> None:
    try:
        await e.edit(text)
    except MessageNotModifiedError:
        pass
    except Exception as exc:
        logging.warning("safe_edit: %s", exc)

async def _reply(e, text: str, delay: float = 0.0) -> None:
    await safe_edit(e, text)
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        await e.delete()
    except Exception:
        pass

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

def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()

async def get_file(e) -> Optional[str]:
    if not e.is_reply:
        await _reply(e, "Reply to a file.")
        return None
    r = await e.get_reply_message()
    if not r.file:
        await _reply(e, "No file in reply.")
        return None
    path = await r.download_media()
    if not path:
        await _reply(e, "Download failed.")
        return None
    return path

# ── Weather & Bio ────────────────────────────────────────────────────

async def fetch_weather() -> dict[str, Any]:
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
                result: dict[str, Any] = {"_fail": True}
            else:
                c = data.get("clouds", {}).get("all", 0)
                icon = "☀️" if c < 20 else ("⛅" if c < 60 else "☁️")
                m = data["main"]
                result = {
                    "icon": icon,
                    "temp": round(float(m["temp"])),
                    "hum":  int(m["humidity"]),
                    "wind": float(data.get("wind", {}).get("speed", 0)),
                    "sr":   datetime.fromtimestamp(data["sys"]["sunrise"], TZ).strftime("%H:%M"),
                    "ss":   datetime.fromtimestamp(data["sys"]["sunset"],  TZ).strftime("%H:%M"),
                }
        except Exception as exc:
            logging.warning("Weather fetch: %s", exc)
            result = {"_fail": True}

        _weather_cache = (time.time(), result)
        return {} if result.get("_fail") else result

def build_bio(w: dict[str, Any]) -> str:
    if not w:
        return clamp(CITY, 70)
    return clamp(
        f"{CITY} {w['temp']}°C {w['icon']} • {w['hum']}% • {w['wind']:.1f}m/s • {w['sr']}–{w['ss']}",
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

        backoff = min(30 * (2 ** (_profile_errors - 1)), 1800) if _profile_errors else PROFILE_INTERVAL
        await asyncio.sleep(backoff)

# ── Notes Index ──────────────────────────────────────────────────────

NOTE_TAG         = "📌"
NOTES_INDEX_FILE = "notes_index.json"
_notes_index: dict[str, dict[str, Any]] = {}

def _load_notes_index() -> None:
    global _notes_index
    try:
        if not os.path.exists(NOTES_INDEX_FILE):
            _notes_index = {}
            return
        with open(NOTES_INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _notes_index = data if isinstance(data, dict) else {}
    except Exception:
        _notes_index = {}

def _save_notes_index() -> None:
    try:
        with open(NOTES_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(_notes_index, f, ensure_ascii=False)
    except Exception as exc:
        logging.warning("Failed to save notes index: %s", exc)

async def _rebuild_notes_index() -> None:
    _notes_index.clear()
    async for msg in client.iter_messages("me", limit=3000):
        raw = msg.raw_text or ""
        if raw.startswith(NOTE_TAG) and msg.reply_to:
            shown = raw[len(NOTE_TAG):].strip()
            norm  = _normalize(shown)
            if norm and norm not in _notes_index:
                _notes_index[norm] = {"id": msg.reply_to.reply_to_msg_id, "name": shown}
                if len(_notes_index) >= 500:
                    break
    _save_notes_index()
    logging.info("Notes index rebuilt: %d notes", len(_notes_index))

async def _ensure_index() -> None:
    if not _notes_index:
        await _rebuild_notes_index()

# ── Help ─────────────────────────────────────────────────────────────

HELP = "\n".join([
    f"**Selfbot** (`{PREFIX}`)",
    "",
    f"`{PREFIX}help`    — commands list",
    f"`{PREFIX}ping`    — latency",
    f"`{PREFIX}uptime`  — bot uptime",
    f"`{PREFIX}del`     — delete replied msg",
    f"`{PREFIX}purge <n>` — bulk delete",
    f"`{PREFIX}virus`   — VirusTotal lookup",
    f"`{PREFIX}rebio`   — force bio update",
    f"`{PREFIX}reload`  — restart bot",
    "",
    "**Save / Send:**",
    f"`{PREFIX}save <name>`          — save replied msg",
    f"`{PREFIX}saved`                — list saved notes",
    f"`{PREFIX}get <name>`           — forward saved note",
    f"`{PREFIX}delnote <name>`       — delete a saved note",
    f"`{PREFIX}rename <old> | <new>` — rename a saved note",
])

# ── Commands ─────────────────────────────────────────────────────────

@client.on(events.NewMessage(pattern=pat("help"), outgoing=True))
async def cmd_help(e):
    await _reply(e, HELP, delay=0)

@client.on(events.NewMessage(pattern=pat("ping"), outgoing=True))
async def cmd_ping(e):
    t0 = time.perf_counter()
    await e.edit("…")
    ms = int((time.perf_counter() - t0) * 1000)
    await _reply(e, f"{ms} ms")

@client.on(events.NewMessage(pattern=pat("uptime"), outgoing=True))
async def cmd_uptime(e):
    h, r = divmod(int(time.time() - _start_time), 3600)
    m, s = divmod(r, 60)
    await _reply(e, f"Uptime: `{h:02d}:{m:02d}:{s:02d}`")

@client.on(events.NewMessage(pattern=pat("del"), outgoing=True))
async def cmd_del(e):
    if not e.is_reply:
        return
    r = await e.get_reply_message()
    try:
        await r.delete()
        await e.delete()
    except Exception:
        await _reply(e, "Can't delete.")

@client.on(events.NewMessage(pattern=pat("purge"), outgoing=True))
async def cmd_purge(e):
    arg = (e.pattern_match.group(1) or "").strip()
    try:
        n = max(1, min(200, int(arg) if arg else 10))
    except ValueError:
        return await _reply(e, f"Usage: {PREFIX}purge <number>")

    ids = []
    async for msg in client.iter_messages(e.chat_id, limit=min(2000, n * 8)):
        if msg.out and msg.id != e.id:
            ids.append(msg.id)
            if len(ids) >= n:
                break

    if ids:
        try:
            await client.delete_messages(e.chat_id, ids, revoke=True)
        except Exception:
            return await _reply(e, "Purge failed.")
    await e.delete()

@client.on(events.NewMessage(pattern=pat("virus"), outgoing=True))
async def cmd_virus(e):
    if not VT_KEY:
        return await _reply(e, "VT_KEY not set.")
    path = await get_file(e)
    if not path:
        return

    try:
        await e.edit("Scanning…")
        h = sha256_file(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    await e.edit("VT lookup…")
    try:
        s = await get_http()
        async with s.get(
            f"https://www.virustotal.com/api/v3/files/{h}",
            headers={"x-apikey": VT_KEY},
        ) as resp:
            if resp.status not in (200, 404):
                return await _reply(e, f"VT HTTP {resp.status}")
            data = await resp.json(content_type=None)
    except Exception:
        return await _reply(e, "VT request failed.")

    if "data" not in data:
        return await _reply(e, f"Not found on VT\n`{h}`")

    st  = data["data"]["attributes"]["last_analysis_stats"]
    mal = st.get("malicious", 0)
    sus = st.get("suspicious", 0)
    und = st.get("undetected", 0)
    verdict = "Clean" if mal == 0 and sus == 0 else "Detected"
    await _reply(e, f"{verdict}  mal:`{mal}` sus:`{sus}` und:`{und}`\n`{h}`")

@client.on(events.NewMessage(pattern=pat("rebio"), outgoing=True))
async def cmd_rebio(e):
    global _last_bio, _profile_errors
    await e.edit("Updating…")
    try:
        b = build_bio(await fetch_weather())
        await client(UpdateProfileRequest(about=b))
        _last_bio = b
        _profile_errors = 0
        await _reply(e, b)
    except Exception as exc:
        await _reply(e, f"Error: {exc}")

@client.on(events.NewMessage(pattern=pat("reload"), outgoing=True))
async def cmd_reload(e):
    await e.edit("Restarting…")
    await close_http()
    os.execv(sys.executable, [sys.executable] + sys.argv)

# ── Save & Send Commands ─────────────────────────────────────────────

@client.on(events.NewMessage(pattern=pat("save"), outgoing=True))
async def cmd_save(e):
    arg = (e.pattern_match.group(1) or "").strip()
    if not arg:
        return await _reply(e, f"Usage: `{PREFIX}save <name>` (reply to a message)")
    if not e.is_reply:
        return await _reply(e, "Reply to a message to save it.")

    r = await e.get_reply_message()
    if not r:
        return await _reply(e, "Cannot get replied message.")

    name     = " ".join(" ".join(arg.split()).splitlines())
    norm     = _normalize(name)
    tag_line = f"{NOTE_TAG} {name}"

    await _ensure_index()

    if norm in _notes_index:
        show = _esc((_notes_index[norm].get("name") or name).replace("\n", " "))
        return await _reply(e, f"Note `{show}` already exists.\nDelete first: `{PREFIX}delnote {show}`")

    try:
        try:
            msg = await r.forward_to("me")
        except Exception:
            if r.media:
                path = await r.download_media()
                if path:
                    try:
                        msg = await client.send_file("me", path, caption=r.raw_text or "",
                                                     formatting_entities=list(r.entities or []) or None)
                    finally:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                else:
                    msg = await client.send_message("me", r.raw_text or "[media failed]")
            else:
                msg = await client.send_message("me", r.raw_text or "[empty]",
                                                formatting_entities=list(r.entities or []) or None)

        await client.send_message("me", tag_line, reply_to=msg.id)
        _notes_index[norm] = {"id": msg.id, "name": name}
        _save_notes_index()
        await _reply(e, f"Saved: `{_esc(name)}`")
    except Exception as exc:
        await _reply(e, f"Save failed: {exc}")

@client.on(events.NewMessage(pattern=pat("saved"), outgoing=True))
async def cmd_saved(e):
    try:
        if not _notes_index:
            await e.edit("Rebuilding index…")
            await _rebuild_notes_index()
        if not _notes_index:
            return await _reply(e, f"No saved notes. Use `{PREFIX}save <name>`.")

        items = sorted(_notes_index.values(), key=lambda x: _normalize(x.get("name", "")))
        lines = ["**Saved Notes:**\n"]
        for it in items[:80]:
            lines.append(f"• `{_esc(it.get('name', '') or '')}`")
        if len(items) > 80:
            lines.append(f"\nShowing 80/{len(items)}")
        lines.append(f"\nTotal: {len(items)}  —  `{PREFIX}get <name>` to send")
        await _reply(e, "\n".join(lines), delay=0)
    except Exception as exc:
        await _reply(e, f"Error: {exc}")

@client.on(events.NewMessage(pattern=pat("get"), outgoing=True))
async def cmd_get(e):
    await _ensure_index()
    arg = (e.pattern_match.group(1) or "").strip()
    if not arg:
        return await _reply(e, f"Usage: `{PREFIX}get <name>`")

    norm = _normalize(arg)
    entry = _notes_index.get(norm)
    if not entry:
        await _rebuild_notes_index()
        entry = _notes_index.get(norm)
    if not entry:
        return await _reply(e, f"Note `{_esc(arg)}` not found.")

    msg = await client.get_messages("me", ids=entry["id"])
    if not msg:
        _notes_index.pop(norm, None)
        _save_notes_index()
        return await _reply(e, "Content message was deleted.")

    try:
        await e.delete()
    except Exception:
        pass
    await msg.forward_to(e.chat_id)

@client.on(events.NewMessage(pattern=pat("rename"), outgoing=True))
async def cmd_rename(e):
    await _ensure_index()
    arg = (e.pattern_match.group(1) or "").strip()
    if "|" not in arg:
        return await _reply(e, f"Usage: `{PREFIX}rename old | new`")

    old_name, new_name = (p.strip() for p in arg.split("|", 1))
    if not old_name or not new_name:
        return await _reply(e, "Both old and new names required.")

    old_norm = _normalize(old_name)
    new_norm = _normalize(new_name)
    entry    = _notes_index.get(old_norm)

    if not entry:
        return await _reply(e, f"Note `{_esc(old_name)}` not found.")
    if new_norm in _notes_index:
        return await _reply(e, f"Note `{_esc(new_name)}` already exists.")

    _notes_index.pop(old_norm)
    _notes_index[new_norm] = {"id": entry["id"], "name": new_name}
    _save_notes_index()

    try:
        async for msg in client.iter_messages("me", limit=2000):
            raw = msg.raw_text or ""
            if raw.startswith(NOTE_TAG) and msg.reply_to and msg.reply_to.reply_to_msg_id == entry["id"]:
                await msg.edit(f"{NOTE_TAG} {new_name}")
                break
    except Exception:
        pass

    await _reply(e, f"Renamed `{_esc(old_name)}` → `{_esc(new_name)}`")

@client.on(events.NewMessage(pattern=pat("delnote"), outgoing=True))
async def cmd_delnote(e):
    await _ensure_index()
    arg = (e.pattern_match.group(1) or "").strip()
    if not arg:
        return await _reply(e, f"Usage: `{PREFIX}delnote <name>`")

    norm  = _normalize(arg)
    entry = _notes_index.pop(norm, None)
    if not entry:
        return await _reply(e, f"Note `{_esc(arg)}` not found.")

    _save_notes_index()
    try:
        async for msg in client.iter_messages("me", limit=2000):
            raw = msg.raw_text or ""
            if raw.startswith(NOTE_TAG) and msg.reply_to and msg.reply_to.reply_to_msg_id == entry["id"]:
                await msg.delete()
                break
        await client.delete_messages("me", [entry["id"]])
    except Exception:
        pass

    await _reply(e, f"Deleted: `{_esc(arg)}`")

# ── Entry ─────────────────────────────────────────────────────────────

async def _shutdown() -> None:
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    logging.info("Shutting down…")
    await close_http()
    await client.disconnect()

def _handle_signal():
    asyncio.create_task(_shutdown())

async def main() -> None:
    logging.info("Starting client…")
    try:
        await asyncio.wait_for(client.start(), timeout=30)
    except asyncio.TimeoutError:
        logging.error("client.start() timed out — check session / network")
        return

    me = await client.get_me()
    logging.info("Started - @%s (%s)", me.username or me.first_name, me.id)
    _load_notes_index()

    loop = asyncio.get_running_loop()
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