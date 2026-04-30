---
name: freel_bot project setup
description: Freelance job notifier bot setup — venv location, installed packages, key changes
type: project
---

Bot is based on https://github.com/hoosnick/freelance-job-notifier

**Why:** User wants to monitor Kwork/Upwork for NestJS/Next.js/Node.js/TypeScript/PostgreSQL/Docker backend jobs.

## Environment
- C: drive is completely full (0 MB free)
- Python venv is at **Z:\freel_venv** — always run with `/z/freel_venv/Scripts/python.exe`
- Run command: `cd "C:\Users\Исаев\Documents\GitHub\freel_bot" && /z/freel_venv/Scripts/python.exe -m app`
- pykwork source is at **Z:\tmp_pip\pykwork** (needed git init to fix poetry VCS bug on UNC path)

## Key files changed
- `app/config_reader.py` — added `anthropic_api_key: Optional[str] = None`
- `app/gpt.py` — replaced g4f-only with `filter_project()` using claude-haiku-4-5-20251001
- `app/parser.py` — calls `filter_project()` before sending each Kwork job to Telegram
- `.env` — created from `.sample.env` with KW_CATEGORIES=38,37,41,255
- `requirements.txt` — added `anthropic`, removed `aiohttp[speedups]`

## Kwork categories selected
- 38: Доработка и настройка сайта
- 37: Создание сайта
- 41: Скрипты и боты
- 255: Сервера и хостинг
Excluded: 79 (Верстка), 39 (Мобильные), 80 (Десктоп), 40 (Игры)

## What user must fill in .env
TG_TOKEN, TG_GROUP (int), TG_TOPIC_ID (int), TG_GROUP_LINK, TG_ADMIN (int),
KW_LOGIN, KW_PASSWORD, KW_PHONE_LAST,
UP_SECURITYTOKEN, UP_USERUID, UP_ORGUID,
APP_BASE_URL (ngrok for dev),
ANTHROPIC_API_KEY (optional — skips filter if empty)

**How to apply:** When working on this project, always use /z/freel_venv/Scripts/python.exe, not system python.
