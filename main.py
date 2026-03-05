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
from typing import Any, Optional
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.tl.functions.account import UpdateProfileRequest

# ─────────────────────────────────────────────────────────────
# Logging / Env
# ─────────────────────────────────────────────────────────────

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
TZ_STR = _require("TZ")
TZ = ZoneInfo(TZ_STR)
OW_KEY = os.getenv("OW_KEY", "").strip()
VT_KEY = os.getenv("VT_KEY", "").strip()
GH_TOKEN = os.getenv("GH_TOKEN", "").strip()
PROFILE_INTERVAL = int(_require("PROFILE_INTERVAL"))
WEATHER_CACHE = int(_require("WEATHER_CACHE"))

# ─────────────────────────────────────────────────────────────
# Globals & Configs
# ─────────────────────────────────────────────────────────────

client = TelegramClient("session", API_ID, API_HASH)

_http: Optional[aiohttp.ClientSession] = None
_weather_cache: tuple[float, Optional[dict[str, Any]]] = (0.0, None)
_weather_lock = asyncio.Lock()

_last_bio = ""
_profile_errors = 0
_shutting_down = False
_start_time = time.time()

NOTE_TAG = "📌"
NOTES_INDEX_FILE = "notes_index.json"
_notes_index: dict[str, dict[str, Any]] = {}

# --- GKI Builder Configs ---
_GH_BUILD = {
    "token": GH_TOKEN,
    "owner": "HieuKVN",
    "repo":  "GKI_KernelSU_SUSFS",
    "ref":   "dev",
    "workflows": {
        "a12": "kernel-a12-5-10.yml",
        "a13": "kernel-a13-5-15.yml",
        "a14": "kernel-a14-6-1.yml",
        "a15": "kernel-a15-6-6.yml",
        "custom": "kernel-custom.yml",
    },
}

_KER  = ["a12", "a13", "a14", "a15", "custom"]
_VAR  = ["Official", "Next", "MKSU", "SukiSU", "ReSukiSU"]
_BRA  = ["Stable(标准)", "Dev(开发)", "Other(其他/指定)"]
_AV   = ["android12", "android13", "android14", "android15"]
_KV   = ["5.10", "5.15", "6.1", "6.6"]

_build_sessions: dict[int, dict] = {}
_own_msgs:        set[int]       = set()
_cancel_build_events: dict[int, asyncio.Event] = {}

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def pat(cmd: str) -> str:
    return rf"^{re.escape(PREFIX)}{cmd}\b(?:\s+([\s\S]+))?$"

def clamp(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"

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

async def _fetch_json(url: str, method: str = "GET", **kwargs) -> Optional[dict]:
    s = await get_http()
    try:
        async with s.request(method, url, **kwargs) as r:
            if r.status not in (200, 201, 202, 204, 404):
                return None
            return await r.json(content_type=None)
    except Exception as exc:
        logging.warning("_fetch_json err: %s", exc)
        return None

async def _fetch_text(url: str, **kwargs) -> Optional[str]:
    s = await get_http()
    try:
        async with s.get(url, **kwargs) as r:
            if r.status == 200:
                return await r.text()
    except Exception as exc:
        logging.warning("_fetch_text err: %s", exc)
    return None

# ─────────────────────────────────────────────────────────────
# Weather / Bio
# ─────────────────────────────────────────────────────────────

async def fetch_weather() -> dict[str, Any]:
    if not OW_KEY:
        return {}

    global _weather_cache

    async with _weather_lock:
        ts, cached = _weather_cache
        if cached is not None and (time.time() - ts) < WEATHER_CACHE:
            return {} if cached.get("_fail") else cached

        try:
            data = await _fetch_json("https://api.openweathermap.org/data/2.5/weather", params={"q": CITY, "appid": OW_KEY, "units": "metric", "lang": "vi"})
            if not data or data.get("cod") != 200:
                logging.warning("Weather API: %s", data.get("message", data.get("cod")) if data else "Failed")
                result: dict[str, Any] = {"_fail": True}
            else:
                c = data.get("clouds", {}).get("all", 0)
                icon = "☀️" if c < 20 else ("⛅" if c < 60 else "☁️")
                m = data["main"]
                result = {
                    "icon": icon,
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

# ─────────────────────────────────────────────────────────────
# Notes index
# ─────────────────────────────────────────────────────────────

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
            norm = _normalize(shown)
            if norm and norm not in _notes_index:
                _notes_index[norm] = {"id": msg.reply_to.reply_to_msg_id, "name": shown}
                if len(_notes_index) >= 500:
                    break
    _save_notes_index()
    logging.info("Notes index rebuilt: %d notes", len(_notes_index))

async def _ensure_index() -> None:
    if not _notes_index:
        await _rebuild_notes_index()

# ─────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────

HELP = "\n".join(
    [
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
        "",
        "**GKI Builder:**",
        f"`{PREFIX}gki`    — start build wizard",
        f"`{PREFIX}list`   — list GitHub runs",
        f"`{PREFIX}stop`   — cancel current/monitoring build",
    ]
)

@client.on(events.NewMessage(pattern=pat("help"), outgoing=True))
async def cmd_help(e):
    await _reply(e, HELP)

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
    await _reply(e, f"Uptime: {h:02d}:{m:02d}:{s:02d}")

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

    ids: list[int] = []
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
    if not e.is_reply:
        return await _reply(e, "Reply to a file.")

    r = await e.get_reply_message()
    if not r.file:
        return await _reply(e, "No file in reply.")

    path = await r.download_media()
    if not path:
        return await _reply(e, "Download failed.")

    try:
        await e.edit("Scanning…")
        h = sha256_file(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    await e.edit("VT lookup…")
    data = await _fetch_json(f"https://www.virustotal.com/api/v3/files/{h}", headers={"x-apikey": VT_KEY})
    if not data:
        return await _reply(e, "VT request failed or HTTP error.")

    if "data" not in data:
        return await _reply(e, f"Not found on VT\n{h}")

    st = data["data"]["attributes"]["last_analysis_stats"]
    mal = st.get("malicious", 0)
    sus = st.get("suspicious", 0)
    und = st.get("undetected", 0)
    verdict = "Clean" if mal == 0 and sus == 0 else "Detected"
    await _reply(e, f"{verdict}  mal:{mal} sus:{sus} und:{und}\n{h}")

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

@client.on(events.NewMessage(pattern=pat("save"), outgoing=True))
async def cmd_save(e):
    arg = (e.pattern_match.group(1) or "").strip()
    if not arg:
        return await _reply(e, f"Usage: {PREFIX}save <name> (reply to a message)")
    if not e.is_reply:
        return await _reply(e, "Reply to a message to save it.")

    r = await e.get_reply_message()
    if not r:
        return await _reply(e, "Cannot get replied message.")

    name = " ".join(arg.split())
    norm = _normalize(name)
    tag_line = f"{NOTE_TAG} {name}"

    await _ensure_index()

    if norm in _notes_index:
        show = _esc((_notes_index[norm].get("name") or name).replace("\n", " "))
        return await _reply(
            e,
            f"Note {show} already exists.\nDelete first: {PREFIX}delnote {show}",
        )

    try:
        try:
            msg = await r.forward_to("me")
        except Exception:
            if r.media:
                path = await r.download_media()
                if path:
                    try:
                        msg = await client.send_file(
                            "me",
                            path,
                            caption=r.raw_text or "",
                            formatting_entities=r.entities or None,
                        )
                    finally:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                else:
                    msg = await client.send_message("me", r.raw_text or "[media failed]")
            else:
                msg = await client.send_message(
                    "me",
                    r.raw_text or "[empty]",
                    formatting_entities=r.entities or None,
                )

        await client.send_message("me", tag_line, reply_to=msg.id)
        _notes_index[norm] = {"id": msg.id, "name": name}
        _save_notes_index()
        await _reply(e, f"Saved: {_esc(name)}")
    except Exception as exc:
        await _reply(e, f"Save failed: {exc}")

@client.on(events.NewMessage(pattern=pat("saved"), outgoing=True))
async def cmd_saved(e):
    try:
        if not _notes_index:
            await e.edit("Rebuilding index…")
            await _rebuild_notes_index()
        if not _notes_index:
            return await _reply(e, f"No saved notes. Use {PREFIX}save <name>.")

        items = sorted(_notes_index.values(), key=lambda x: _normalize(x.get("name", "")))
        lines = ["**Saved Notes:**\n"]
        for it in items[:80]:
            lines.append(f"• {_esc(it.get('name', '') or '')}")
        if len(items) > 80:
            lines.append(f"\nShowing 80/{len(items)}")
        lines.append(f"\nTotal: {len(items)}  —  {PREFIX}get <name> to send")
        await _reply(e, "\n".join(lines))
    except Exception as exc:
        await _reply(e, f"Error: {exc}")

@client.on(events.NewMessage(pattern=pat("get"), outgoing=True))
async def cmd_get(e):
    await _ensure_index()
    arg = (e.pattern_match.group(1) or "").strip()
    if not arg:
        return await _reply(e, f"Usage: {PREFIX}get <name>")

    norm = _normalize(arg)
    entry = _notes_index.get(norm)
    if not entry:
        await _rebuild_notes_index()
        entry = _notes_index.get(norm)
    if not entry:
        return await _reply(e, f"Note {_esc(arg)} not found.")

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
        return await _reply(e, f"Usage: {PREFIX}rename old | new")

    old_name, new_name = (p.strip() for p in arg.split("|", 1))
    if not old_name or not new_name:
        return await _reply(e, "Both old and new names required.")

    old_norm = _normalize(old_name)
    new_norm = _normalize(new_name)
    entry = _notes_index.get(old_norm)

    if not entry:
        return await _reply(e, f"Note {_esc(old_name)} not found.")
    if new_norm in _notes_index:
        return await _reply(e, f"Note {_esc(new_name)} already exists.")

    _notes_index.pop(old_norm)
    _notes_index[new_norm] = {"id": entry["id"], "name": new_name}
    _save_notes_index()

    try:
        async for msg in client.iter_messages("me", limit=2000):
            raw = msg.raw_text or ""
            if (
                raw.startswith(NOTE_TAG)
                and msg.reply_to
                and msg.reply_to.reply_to_msg_id == entry["id"]
            ):
                await msg.edit(f"{NOTE_TAG} {new_name}")
                break
    except Exception:
        pass

    await _reply(e, f"Renamed {_esc(old_name)} → {_esc(new_name)}")

@client.on(events.NewMessage(pattern=pat("delnote"), outgoing=True))
async def cmd_delnote(e):
    await _ensure_index()
    arg = (e.pattern_match.group(1) or "").strip()
    if not arg:
        return await _reply(e, f"Usage: {PREFIX}delnote <name>")

    norm = _normalize(arg)
    entry = _notes_index.pop(norm, None)
    if not entry:
        return await _reply(e, f"Note {_esc(arg)} not found.")

    _save_notes_index()
    try:
        async for msg in client.iter_messages("me", limit=2000):
            raw = msg.raw_text or ""
            if (
                raw.startswith(NOTE_TAG)
                and msg.reply_to
                and msg.reply_to.reply_to_msg_id == entry["id"]
            ):
                await msg.delete()
                break
        await client.delete_messages("me", [entry["id"]])
    except Exception:
        pass

    await _reply(e, f"Deleted: {_esc(arg)}")

# ─────────────────────────────────────────────────────────────
# GKI Kernel Builder
# ─────────────────────────────────────────────────────────────

def _gh_headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {_GH_BUILD['token']}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def _ic(v: bool) -> str:
    return "✅" if v else "❌"

def _tog_text(s: dict) -> str:
    t = s["toggles"]
    is_c = s.get("kernel") == "custom"
    lines = [
        "⚙️ **Tùy chỉnh tính năng** (gõ số để toggle, 0 để BUILD):\n",
        f"1. ZRAM:         {_ic(t['use_zram'])}",
        f"2. BBG:          {_ic(t['use_bbg'])}",
        f"3. KPM:          {_ic(t['use_kpm'])}",
        f"4. Hủy SUSFS:   {_ic(t['cancel_susfs'])}",
    ]
    if is_c:
        lines.append(f"5. OnePlus 8E:  {_ic(t['supp_op'])}")
    lines.append("\n0 → 🚀 GỬI BUILD")
    return "\n".join(lines)

async def _build_send(chat_id: int, text: str) -> None:
    s = _build_sessions.get(chat_id, {})
    mid = s.get("_mid")
    if mid:
        try:
            msg = await client.edit_message(chat_id, mid, text)
            s["_mid"] = msg.id
            _own_msgs.add(msg.id)
            return
        except Exception:
            pass
    msg = await client.send_message(chat_id, text)
    s["_mid"] = msg.id
    _own_msgs.add(msg.id)

@client.on(events.NewMessage(pattern=pat("gki"), outgoing=True))
async def cmd_build(e):
    chat_id = e.chat_id
    if chat_id in _build_sessions:
        await e.edit("⚠️ Đang có build session. Gõ .stop để hủy trước.")
        return

    msg = await client.send_message(chat_id,
        "🛠 **Bước 1: Chọn loại Build** (gõ số, q = thoát)\n\n"
        "1. A12 (5.10)\n2. A13 (5.15)\n3. A14 (6.1)\n4. A15 (6.6)\n5. Custom")
    _own_msgs.add(msg.id)
    _build_sessions[chat_id] = {
        "step": "kernel",
        "toggles": {"use_zram": True, "use_bbg": True, "use_kpm": True,
                    "cancel_susfs": False, "supp_op": False},
        "custom": {}, "_mid": msg.id,
    }

@client.on(events.NewMessage(outgoing=True,
    func=lambda e: e.chat_id in _build_sessions
        and _build_sessions[e.chat_id].get("step")
        and e.id not in _own_msgs))
async def _build_input(e):
    _own_msgs.discard(e.id)
    chat_id = e.chat_id
    s = _build_sessions[chat_id]
    step = s["step"]
    raw = (e.raw_text or "").strip()

    # Automatically delete the user's input message
    try:
        await e.delete()
    except Exception:
        pass

    if raw.lower() == "q":
        _build_sessions.pop(chat_id, None)
        await client.send_message(chat_id, "🛑 Đã hủy build.")
        return

    if step == "kernel":
        try:
            idx = int(raw) - 1
            assert 0 <= idx < len(_KER)
        except (ValueError, AssertionError):
            return
        s["kernel"] = _KER[idx]
        if _KER[idx] == "custom":
            s["step"] = "cav"
            await _build_send(chat_id,
                "🌟 **Custom – Chọn Android** (gõ số)\n\n"
                "1. Android 12\n2. Android 13\n3. Android 14\n4. Android 15")
        else:
            s["step"] = "variant"
            wf = _GH_BUILD["workflows"][_KER[idx]]
            await _build_send(chat_id,
                f"✅ Kernel: **{wf}**\n\n"
                "👉 **Chọn Variant** (gõ số)\n\n"
                + "\n".join(f"{i+1}. {v}" for i, v in enumerate(_VAR)))

    elif step == "cav":
        try:
            idx = int(raw) - 1; assert 0 <= idx < len(_AV)
        except (ValueError, AssertionError): return
        s["custom"]["android_version"] = _AV[idx]
        s["step"] = "ckv"
        await _build_send(chat_id,
            f"✅ Android: **{_AV[idx]}**\n\n"
            "**Chọn Kernel version** (gõ số)\n\n"
            + "\n".join(f"{i+1}. {v}" for i, v in enumerate(_KV)))

    elif step == "ckv":
        try:
            idx = int(raw) - 1; assert 0 <= idx < len(_KV)
        except (ValueError, AssertionError): return
        s["custom"]["kernel_version"] = _KV[idx]
        s["step"] = "csub"
        await _build_send(chat_id, f"✅ Kernel: **{_KV[idx]}**\n\n📝 Nhập sub_level (bắt buộc, vd: 66):")

    elif step == "csub":
        if not raw.isdigit() or int(raw) <= 0:
            await _build_send(chat_id, "❌ sub_level phải là số nguyên dương. Nhập lại:")
            return
        av  = s["custom"].get("android_version", "")
        kv  = s["custom"].get("kernel_version", "")
        url = (f"https://raw.githubusercontent.com/{_GH_BUILD['owner']}"
               f"/{_GH_BUILD['repo']}/dev/data/{av}/{kv}.json")
        fetch_ok = False
        dates: list[str] = []
        lts_hint = ""
        try:
            sess = await get_http()
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    fetch_ok = True
                    j = await r.json(content_type=None)
                    lts_hint = j.get("lts", "")
                    full_kernel = f"{kv}.{raw}"
                    dates = [e["date"] for e in j.get("entries", [])
                             if e.get("kernel") == full_kernel and e.get("date")]
        except Exception:
            pass

        if fetch_ok and not dates:
            hint = f"\n💡 LTS hiện tại: {lts_hint.split('.')[-1]}" if lts_hint else ""
            await _build_send(chat_id,
                f"❌ sub_level **{raw}** không tồn tại.{hint}\n\nNhập lại:")
            return

        s["custom"]["sub_level"] = raw
        if dates:
            s["custom"]["_patch_dates"] = dates
            s["step"] = "cpatch_sel"
            menu = "\n".join(f"{i+1}. {d}" for i, d in enumerate(dates))
            await _build_send(chat_id,
                f"✅ sub_level: **{raw}**\n\n"
                f"🗓 Chọn os_patch_level (gõ số):\n\n{menu}")
        else:
            s["step"] = "cpatch"
            await _build_send(chat_id,
                f"✅ sub_level: **{raw}**\n\n📝 Nhập os_patch_level (vd: 2022-01, lts):")

    elif step == "cpatch_sel":
        dates = s["custom"].pop("_patch_dates", [])
        try:
            idx = int(raw) - 1; assert 0 <= idx < len(dates)
            chosen = dates[idx]
        except (ValueError, AssertionError):
            return
        s["custom"]["os_patch_level"] = chosen
        raw = chosen
        if s["custom"].get("kernel_version") == "5.10":
            s["step"] = "crev"
            await _build_send(chat_id, f"✅ patch_level: **{chosen}**\n\n📝 Nhập revision (vd: r11):")
        else:
            s["custom"]["revision"] = ""
            s["step"] = "variant"
            await _build_send(chat_id,
                f"✅ patch_level: **{chosen}**\n\n✅ Xong cấu hình Custom!\n\n"
                "👉 **Chọn Variant** (gõ số)\n\n"
                + "\n".join(f"{i+1}. {v}" for i, v in enumerate(_VAR)))

    elif step == "cpatch":
        s["custom"]["os_patch_level"] = raw
        if s["custom"].get("kernel_version") == "5.10":
            s["step"] = "crev"
            await _build_send(chat_id, f"✅ patch_level: **{raw}**\n\n📝 Nhập revision (vd: r11):")
        else:
            s["custom"]["revision"] = ""
            s["step"] = "variant"
            await _build_send(chat_id,
                f"✅ patch_level: **{raw}**\n\n✅ Xong cấu hình Custom!\n\n"
                "👉 **Chọn Variant** (gõ số)\n\n"
                + "\n".join(f"{i+1}. {v}" for i, v in enumerate(_VAR)))

    elif step == "crev":
        s["custom"]["revision"] = "" if raw == "-" else raw
        s["step"] = "variant"
        await _build_send(chat_id,
            "✅ Xong cấu hình Custom!\n\n"
            "👉 **Chọn Variant** (gõ số)\n\n"
            + "\n".join(f"{i+1}. {v}" for i, v in enumerate(_VAR)))

    elif step == "variant":
        try:
            idx = int(raw) - 1; assert 0 <= idx < len(_VAR)
        except (ValueError, AssertionError): return
        s["variant"] = _VAR[idx]
        s["step"] = "branch"
        await _build_send(chat_id,
            f"✅ Variant: **{_VAR[idx]}**\n\n"
            "🌱 **Chọn KSU Branch** (gõ số)\n\n"
            + "\n".join(f"{i+1}. {v}" for i, v in enumerate(_BRA)))

    elif step == "branch":
        try:
            idx = int(raw) - 1; assert 0 <= idx < len(_BRA)
        except (ValueError, AssertionError): return
        s["branch"] = _BRA[idx]
        s["step"] = "version"
        await _build_send(chat_id, f"✅ Branch: **{_BRA[idx]}**\n\n📝 Nhập Version Name (- bỏ qua):")

    elif step == "version":
        s["version"] = "" if raw == "-" else raw
        s["step"] = "build_time"
        await _build_send(chat_id,
            f"✅ Version: **{raw}**\n\n"
            "🕒 Nhập Build Time:\nN = bỏ trống | hoặc nhập thiên văn (vd: 2025-01-01 00:00:00):")

    elif step == "build_time":
        s["build_time"] = "" if raw.upper() == "N" else raw
        s["step"] = "toggles"
        await _build_send(chat_id, _tog_text(s))

    elif step == "toggles":
        _TOG_KEYS = ["use_zram", "use_bbg", "use_kpm", "cancel_susfs", "supp_op"]
        is_c = s.get("kernel") == "custom"
        avail = _TOG_KEYS[:4] + (["supp_op"] if is_c else [])
        if raw == "0":
            await _execute_build(chat_id, s.get("_mid"))
        else:
            try:
                idx = int(raw) - 1; assert 0 <= idx < len(avail)
            except (ValueError, AssertionError): return
            key = avail[idx]
            s["toggles"][key] = not s["toggles"][key]
            await _build_send(chat_id, _tog_text(s))

async def _monitor_build(chat_id: int, workflow: str) -> None:
    await asyncio.sleep(15)
    url = f"https://api.github.com/repos/{_GH_BUILD['owner']}/{_GH_BUILD['repo']}/actions/workflows/{workflow}/runs?per_page=5"
    headers = _gh_headers()

    try:
        sess = await get_http()
        async with sess.get(url, headers=headers) as r:
            if r.status != 200:
                return
            data = await r.json(content_type=None)
            runs = data.get("workflow_runs", [])
            if not runs:
                return
            run_id = runs[0]["id"]
            run_url = runs[0].get("html_url", "")
            run_name = runs[0].get("name", workflow)
    except Exception as e:
        logging.warning("_monitor_build fetch init error: %s", e)
        return

    status_url = f"https://api.github.com/repos/{_GH_BUILD['owner']}/{_GH_BUILD['repo']}/actions/runs/{run_id}"

    cancel_event = asyncio.Event()
    _cancel_build_events[chat_id] = cancel_event

    try:
        while True:
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=30.0)
                break
            except asyncio.TimeoutError:
                pass

            try:
                async with sess.get(status_url, headers=headers) as r:
                    if r.status != 200:
                        continue
                    run_data = await r.json(content_type=None)
                    status = run_data.get("status")
                    conclusion = run_data.get("conclusion")

                    if status == "completed":
                        if conclusion == "success":
                            nightly = f"https://nightly.link/{_GH_BUILD['owner']}/{_GH_BUILD['repo']}/actions/runs/{run_id}"
                            msg = f"✅ **{run_name}** đã hoàn tất thành công!\n   └ [📥 Link tải Artifacts (Nightly)]({nightly})"
                        elif conclusion == "failure":
                            msg = f"❌ **{run_name}** thất bại!\n   └ [Xem chi tiết]({run_url})"
                        elif conclusion == "cancelled":
                            msg = f"🚫 **{run_name}** đã bị hủy.\n   └ [Xem chi tiết]({run_url})"
                        else:
                            msg = f"ℹ️ **{run_name}** kết thúc với trạng thái: {conclusion}\n   └ [Xem chi tiết]({run_url})"

                        await client.send_message(chat_id, msg)
                        break
            except Exception as e:
                logging.warning("_monitor_build polling error: %s", e)
                await asyncio.sleep(60)
    finally:
        _cancel_build_events.pop(chat_id, None)

async def _execute_build(chat_id: int, msg_id: int | None) -> None:
    s = _build_sessions.get(chat_id, {})
    is_custom = s.get("kernel") == "custom"
    workflow  = _GH_BUILD["workflows"].get(s.get("kernel", ""), "")
    toggles   = s.get("toggles", {})

    if msg_id:
        await client.edit_message(chat_id, msg_id, "⏳ Đang gửi lệnh build…")

    inputs: dict[str, str] = {
        "kernelsu_variant": s.get("variant", ""),
        "kernelsu_branch":  s.get("branch", ""),
        "version":          s.get("version", ""),
        "use_zram":         str(toggles.get("use_zram", True)).lower(),
        "use_bbg":          str(toggles.get("use_bbg",  True)).lower(),
        "use_kpm":          str(toggles.get("use_kpm",  True)).lower(),
        "cancel_susfs":     str(toggles.get("cancel_susfs", False)).lower(),
        "build_time":       s.get("build_time", ""),
    }
    if is_custom:
        c = s.get("custom", {})
        inputs.update({
            "android_version": c.get("android_version", ""),
            "kernel_version":  c.get("kernel_version", ""),
            "sub_level":       c.get("sub_level", ""),
            "os_patch_level":  c.get("os_patch_level", ""),
            "revision":        c.get("revision", ""),
            "supp_op":         str(toggles.get("supp_op", False)).lower(),
        })
    else:
        inputs["sub_levels"] = ""

    url = f"https://api.github.com/repos/{_GH_BUILD['owner']}/{_GH_BUILD['repo']}/actions/workflows/{workflow}/dispatches"
    headers = _gh_headers()
    payload = {"ref": _GH_BUILD["ref"], "inputs": inputs}

    is_ok = False
    try:
        sess = await get_http()
        async with sess.post(url, json=payload, headers=headers) as resp:
            is_ok = resp.status in (200, 204)
            result = (
                "✅ Đã gửi lệnh build. Bot sẽ thông báo khi hoàn tất."
                if is_ok
                else f"❌ **GitHub API lỗi {resp.status}:**\n{(await resp.text())[:300]}"
            )
    except Exception as exc:
        result = f"❌ Request failed: {exc}"
    finally:
        _build_sessions.pop(chat_id, None)

    if msg_id:
        await client.edit_message(chat_id, msg_id, result)
    else:
        await client.send_message(chat_id, result)

    if is_ok:
        asyncio.create_task(_monitor_build(chat_id, workflow))

@client.on(events.NewMessage(pattern=pat("stop"), outgoing=True))
async def cmd_build_stop(e):
    chat_id = e.chat_id
    args = (e.raw_text or "").split()
    target_id = args[1] if len(args) > 1 else None

    _build_sessions.pop(chat_id, None)
    if chat_id in _cancel_build_events:
        _cancel_build_events[chat_id].set()

    msg = await client.send_message(chat_id, "⏳ Đang tìm và hủy build trên GitHub...")

    url = f"https://api.github.com/repos/{_GH_BUILD['owner']}/{_GH_BUILD['repo']}/actions/runs?per_page=15"
    headers = _gh_headers()

    try:
        data = await _fetch_json(url, headers=headers)
        if not data:
            return await client.edit_message(chat_id, msg.id, "❌ Lỗi lấy danh sách build")

        runs = data.get("workflow_runs", [])
        in_progress = [r for r in runs if r.get("status") in ("in_progress", "queued", "waiting")]

        if not in_progress:
            return await client.edit_message(chat_id, msg.id, "⚠️ Không có build nào đang chạy trên GitHub.")

        target_run = None
        if target_id:
            for r in in_progress:
                if str(r.get("id")) == target_id:
                    target_run = r
                    break
            if not target_run:
                 return await client.edit_message(chat_id, msg.id, f"⚠️ Không tìm thấy build {target_id} đang chạy.")
        elif len(in_progress) > 1:
            msg_text = "⚠️ **Có nhiều build đang chạy.**\n\n"
            for r in in_progress:
                r_id = r["id"]
                r_name = r.get("name", "Unknown")
                msg_text += f"• {r_id} - **{r_name}**\n"
            msg_text += "\n👉 Gửi /stop ID để hủy hoặc /cancel để bỏ qua."
            return await client.edit_message(chat_id, msg.id, msg_text)
        else:
            target_run = in_progress[0]

        if not target_run:
            return

        run_id = target_run["id"]
        wf_name = target_run.get("name", "Unknown")

        cancel_url = f"https://api.github.com/repos/{_GH_BUILD['owner']}/{_GH_BUILD['repo']}/actions/runs/{run_id}/cancel"
        sess = await get_http()
        async with sess.post(cancel_url, headers=headers) as cancel_resp:
            if cancel_resp.status in (202, 200):
                await client.edit_message(chat_id, msg.id, f"🛑 Đã gửi lệnh hủy build: **{wf_name}**")
            else:
                err = (await cancel_resp.text())[:100]
                await client.edit_message(chat_id, msg.id, f"❌ Lỗi khi hủy build: {cancel_resp.status}\n{err}")
    except Exception as exc:
        await client.edit_message(chat_id, msg.id, f"❌ Lỗi: {exc}")

@client.on(events.CallbackQuery(pattern=b"^stop_run:(.*)$"))
async def _cb_stop_run(e):
    chat_id = e.chat_id
    run_id = e.pattern_match.group(1).decode("utf-8")

    if run_id == "cancel":
        await e.edit("🛑 Đã hủy thao tác.")
        return

    await e.edit(f"⏳ Đang gửi lệnh hủy build {run_id}...")
    url = f"https://api.github.com/repos/{_GH_BUILD['owner']}/{_GH_BUILD['repo']}/actions/runs/{run_id}/cancel"
    headers = _gh_headers()
    try:
        sess = await get_http()
        async with sess.post(url, headers=headers) as resp:
            if resp.status in (202, 200):
                await e.edit(f"🛑 Đã gửi lệnh hủy build: {run_id}")
            else:
                err = (await resp.text())[:100]
                await e.edit(f"❌ Lỗi khi hủy build: {resp.status}\n{err}")
    except Exception as exc:
        await e.edit(f"❌ Lỗi: {exc}")

@client.on(events.NewMessage(pattern=pat("list"), outgoing=True))
async def cmd_build_list(e):
    await e.edit("⏳ Đang lấy danh sách runs…")
    url = (f"https://api.github.com/repos/{_GH_BUILD['owner']}/{_GH_BUILD['repo']}"
           f"/actions/runs?per_page=10")
    headers = _gh_headers()

    try:
        sess = await get_http()
        async with sess.get(url, headers=headers) as resp:
            if resp.status != 200:
                return await safe_edit(e, f"❌ GitHub API lỗi {resp.status}")
            runs = (await resp.json(content_type=None)).get("workflow_runs", [])
            if not runs:
                return await safe_edit(e, "Không có runs nào.")

            lines = [f"**{_GH_BUILD['repo']} – Tình trạng Build**\n"]
            for r in runs:
                st  = r.get("status", "")
                cl  = r.get("conclusion") or ""
                wf  = r.get("name", "?")
                url_run = r.get("html_url", "")
                run_id = r.get("id", "")

                if st in ("in_progress", "queued", "waiting"):
                    lines.append(f"🔄 **{wf}** ({run_id}) — Đang build...\n   └ [Xem Progress]({url_run})")
                elif cl == "success":
                    nightly = f"https://nightly.link/{_GH_BUILD['owner']}/{_GH_BUILD['repo']}/actions/runs/{run_id}"
                    lines.append(f"✅ **{wf}** — Thành công\n   └ [📥 Link tải Artifacts]({nightly})")
                elif cl in ("failure", "cancelled"):
                    ico = "❌" if cl == "failure" else "🚫"
                    lines.append(f"{ico} **{wf}** — {cl}\n   └ [Xem chi tiết]({url_run})")
                else:
                    lines.append(f"❔ **{wf}** — {st}")

            await safe_edit(e, "\n".join(lines))
    except Exception as exc:
        await safe_edit(e, f"❌ Request failed: {exc}")

# ─────────────────────────────────────────────────────────────
# Shutdown / Main
# ─────────────────────────────────────────────────────────────

async def _shutdown() -> None:
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    logging.info("Shutting down…")
    await close_http()
    await client.disconnect()

def _handle_signal() -> None:
    asyncio.create_task(_shutdown())

async def main() -> None:
    logging.info("Starting client…")
    await client.start()
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
                pass

    bg = asyncio.create_task(profile_loop())
    try:
        await client.run_until_disconnected()
    finally:
        bg.cancel()
        await close_http()

if __name__ == "__main__":
    try:
        if sys.version_info >= (3, 11):
            with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
                runner.run(main())
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        pass