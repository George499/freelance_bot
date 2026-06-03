"""
Soft-pause бота из Telegram.

Бот остаётся живым (slушает команды через polling), но Kwork-цикл
пропускается пока флаг включён. Это позволяет временно усыпить бота
(например, когда кончились коннекты) без перезапуска сервиса и без sudo.

Хранение — JSON-файл рядом с quota.json. Первичное значение false.
"""

import json
import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)

PAUSE_FILE = os.path.join("app", "db", "database", "pause_mode.json")


def _default_state() -> Dict:
    return {"paused": False}


def _load() -> Dict:
    if not os.path.exists(PAUSE_FILE):
        os.makedirs(os.path.dirname(PAUSE_FILE), exist_ok=True)
        state = _default_state()
        _save(state)
        return state
    try:
        with open(PAUSE_FILE, encoding="utf-8") as f:
            data = json.load(f)
            if "paused" not in data:
                return _default_state()
            return data
    except (json.JSONDecodeError, OSError):
        return _default_state()


def _save(state: Dict) -> None:
    os.makedirs(os.path.dirname(PAUSE_FILE), exist_ok=True)
    with open(PAUSE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_bot_paused() -> bool:
    return bool(_load().get("paused", False))


def set_bot_paused(paused: bool) -> bool:
    state = {"paused": bool(paused)}
    _save(state)
    logger.info("Bot soft-pause → %s", "ON (Kwork-цикл пропускается)" if paused else "OFF (Kwork-цикл активен)")
    return state["paused"]
