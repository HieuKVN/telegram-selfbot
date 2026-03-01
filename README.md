# Telegram Selfbot

Auto bio weather update and utilities.

## Setup

```bash
pip install telethon aiohttp pytz python-dotenv
cp .env.example .env   # fill in your API keys
python main.py
```

## .env

See `.env.example`. Required: `API_ID`, `API_HASH`, `PREFIX`, `CITY`, `TZ`, `PROFILE_INTERVAL`, `WEATHER_CACHE`.

## Commands

- `/help` — list commands
- `/ping` — latency
- `/id` — chat/user ID
- `/del` — delete replied message
- `/purge <n>` — bulk delete
- `/virus` — VirusTotal lookup
- `/rebio` — force bio update
- `/reload` — restart bot
