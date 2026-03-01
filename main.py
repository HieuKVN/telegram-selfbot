#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import asyncio
import logging
from datetime import datetime
from hashlib import sha256
from typing import Dict, Any

import pytz
import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, events
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

# ── Client & State ─────────────────────────────────────────────────

client = TelegramClient("session", API_ID, API_HASH)
_http: aiohttp.ClientSession | None = None
_weather_cache: tuple[float, Dict[str, Any]] = (0.0, {})
_last_bio = ""

# ── Helpers ─────────────────────────────────────────────────────────

def pat(cmd: str) -> str:
    return rf"^{re.escape(PREFIX)}{cmd}\b(?:\s+([\s\S]+))?$"

def clamp(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"

async def get_http() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12),
            headers={"User-Agent": "Selfbot/1.0"},
        )
    return _http

async def close_http() -> None:
    global _http
    if _http and not _http.closed:
        await _http.close()
        _http = None

async def download_replied_file(e) -> tuple | None:
    if not e.is_reply:
        await e.edit("⚠️ Reply to a file.")
        return None
    r = await e.get_reply_message()
    if not r.file:
        await e.edit("⚠️ No file in reply.")
        return None
    path = await r.download_media()
    if not path:
        await e.edit("❌ Download failed.")
        return None
    return r, path

# ── Weather & Bio ───────────────────────────────────────────────────

def _cloud_icon(c: int) -> str:
    if c < 20:
        return "☀️"
    return "⛅" if c < 60 else "☁️"

async def fetch_weather() -> Dict[str, Any]:
    if not OW_KEY:
        return {}

    global _weather_cache
    ts, cached = _weather_cache
    if cached and (time.time() - ts) < WEATHER_CACHE:
        return cached

    try:
        s = await get_http()
        async with s.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"q": CITY, "appid": OW_KEY, "units": "metric", "lang": "vi"},
        ) as r:
            data = await r.json(content_type=None)

        if data.get("cod") != 200:
            logging.warning("Weather API: %s", data.get("message", data.get("cod")))
            result = {}
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
        result = {}

    _weather_cache = (time.time(), result)
    return result

def build_bio(w: Dict[str, Any]) -> str:
    if not w:
        return clamp(CITY, 70)
    return clamp(
        f"{CITY} {w['temp']}°C {w['icon']} · {w['hum']}% · {w['wind']:.1f}m/s · {w['sr']}–{w['ss']}",
        70,
    )

async def profile_loop() -> None:
    global _last_bio
    await asyncio.sleep(5)
    while True:
        try:
            b = build_bio(await fetch_weather())
            if b != _last_bio:
                await client(UpdateProfileRequest(about=b))
                _last_bio = b
                logging.info("Bio → %s", b)
        except Exception as exc:
            logging.warning("profile_loop: %s", exc)
        await asyncio.sleep(PROFILE_INTERVAL)

# ── Help ────────────────────────────────────────────────────────────

HELP = "\n".join([
    f"**Selfbot** (`{PREFIX}`)",
    "",
    f"`{PREFIX}help` — commands list",
    f"`{PREFIX}ping` — latency",
    f"`{PREFIX}id` — chat / user ID",
    f"`{PREFIX}del` — delete replied msg",
    f"`{PREFIX}purge <n>` — bulk delete",
    f"`{PREFIX}virus` — VirusTotal lookup",
    f"`{PREFIX}rebio` — force bio update",
    f"`{PREFIX}reload` — restart bot",
])

# ── Commands ────────────────────────────────────────────────────────

@client.on(events.NewMessage(pattern=pat("help"), outgoing=True))
async def cmd_help(e):
    await e.edit(HELP)

@client.on(events.NewMessage(pattern=pat("ping"), outgoing=True))
async def cmd_ping(e):
    t0 = time.perf_counter()
    await e.edit("…")
    await e.edit(f"🏓 {int((time.perf_counter() - t0) * 1000)} ms")

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
        await e.edit("❌ Can't delete.")

@client.on(events.NewMessage(pattern=pat("purge"), outgoing=True))
async def cmd_purge(e):
    arg = (e.pattern_match.group(1) or "").strip()
    try:
        n = max(1, min(200, int(arg) if arg else 10))
    except ValueError:
        return await e.edit("⚠️ /purge <number>")

    ids = []
    async for msg in client.iter_messages(e.chat_id, limit=800):
        if msg.out and msg.id != e.id:
            ids.append(msg.id)
            if len(ids) >= n:
                break

    if ids:
        try:
            await client.delete_messages(e.chat_id, ids, revoke=True)
        except Exception:
            return await e.edit("❌ Purge failed.")
    await e.delete()

@client.on(events.NewMessage(pattern=pat("virus"), outgoing=True))
async def cmd_virus(e):
    if not VT_KEY:
        return await e.edit("⚠️ VT_KEY not set.")
    result = await download_replied_file(e)
    if not result:
        return
    _, path = result

    try:
        await e.edit("🔎 Scanning…")
        with open(path, "rb") as f:
            h = sha256(f.read()).hexdigest()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    await e.edit("🌐 VT lookup…")
    try:
        s = await get_http()
        async with s.get(
            f"https://www.virustotal.com/api/v3/files/{h}",
            headers={"x-apikey": VT_KEY},
        ) as resp:
            data = await resp.json(content_type=None)
    except Exception:
        return await e.edit("❌ VT request failed.")

    if "data" not in data:
        return await e.edit(f"🟡 Not found on VT\n`{h}`")

    st = data["data"]["attributes"]["last_analysis_stats"]
    mal, sus, und = st.get("malicious", 0), st.get("suspicious", 0), st.get("undetected", 0)
    icon = "🟢 Clean" if (mal == 0 and sus == 0) else "🔴 Detected"
    await e.edit(f"🛡 {icon}  mal:`{mal}` sus:`{sus}` und:`{und}`\n`{h}`")

@client.on(events.NewMessage(pattern=pat("rebio"), outgoing=True))
async def cmd_rebio(e):
    global _last_bio
    await e.edit("🔄 Updating…")
    try:
        b = build_bio(await fetch_weather())
        await client(UpdateProfileRequest(about=b))
        _last_bio = b
        await e.edit(f"✅ {b}")
    except Exception as exc:
        await e.edit(f"❌ {exc}")

@client.on(events.NewMessage(pattern=pat("reload"), outgoing=True))
async def cmd_reload(e):
    await e.edit("♻️ Restarting…")
    await close_http()
    os.execv(sys.executable, [sys.executable] + sys.argv)

# ── Entry ───────────────────────────────────────────────────────────

async def main() -> None:
    await client.start()
    me = await client.get_me()
    logging.info("Started — @%s (%s)", me.username or me.first_name, me.id)
    bg = asyncio.create_task(profile_loop())
    try:
        await client.run_until_disconnected()
    finally:
        bg.cancel()
        await close_http()

if __name__ == "__main__":
    asyncio.run(main())