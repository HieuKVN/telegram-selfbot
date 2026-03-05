# Telegram Selfbot

Auto bio weather update and utilities. Includes personal notes management, MIUI/HyperOS firmware tracking, and automated GKI Kernel builds via GitHub Actions.

## Setup

```bash
pip install telethon aiohttp python-dotenv tzdata
cp .env.example .env   # fill in your API keys
python main.py
```
*(Note: `tzdata` is required on Windows environments for accurate timezone handling).*

## GKI Builder Setup

To use the automated GKI Kernel builder commands, you **must fork** the following repository to your own GitHub account:
👉 [cosnefe/GKI_KernelSU_SUSFS](https://github.com/cosnefe/GKI_KernelSU_SUSFS)

*Ensure your `GH_TOKEN` has the necessary scopes (`repo` and `workflow`) to dispatch actions in your forked repository.*

## Configuration (`.env`)

See `.env.example`. Make sure to fill in all required fields:

**Core & Bio:**
* `API_ID`, `API_HASH`: Telegram API credentials.
* `PREFIX`: Command prefix (e.g., `.`).
* `CITY`: Your city for weather tracking (e.g., `Hue`).
* `TZ`: Your timezone (e.g., `Asia/Ho_Chi_Minh`).
* `PROFILE_INTERVAL`: Bio update interval in seconds.
* `WEATHER_CACHE`: Weather data cache duration in seconds.

**API Keys:**
* `OW_KEY`: OpenWeatherMap API key (for bio updates).
* `VT_KEY`: VirusTotal API key (for scanning files).
* `GH_TOKEN`: GitHub Personal Access Token (for GKI Builder).

## Commands

### System & Utilities
* `/help` — list commands
* `/ping` — latency
* `/uptime` — bot uptime
* `/del` — delete replied message
* `/purge <n>` — bulk delete
* `/virus` — VirusTotal lookup
* `/rebio` — force bio update
* `/reload` — restart bot

### Notes Management
* `/save <name>` — save replied msg
* `/saved` — list saved notes
* `/get <name>` — forward saved note
* `/rename <old> | <new>` — rename a saved note
* `/delnote <name>` — delete a saved note

### Firmware Tracker
* `/miui <model> [region]` — fetch latest MIUI/HyperOS firmware versions (e.g., `/miui houji global`)

### GKI Kernel Builder
* `/build` — start the interactive kernel build wizard
* `/list` — list recent GitHub Actions runs
* `/stop [run_id]` — cancel current or monitored build