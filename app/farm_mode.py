"""
Review-farming mode toggle (v4 раздел 9).

Глобальный флаг "режим отзыв-фарма" — приоритет на простые быстрые заказы
для набора отзывов. Переключается из Telegram командами /farm_on и /farm_off.
Хранение — JSON-файл рядом с quota.json. Первичное значение из env-переменной
REVIEW_FARMING_MODE_DEFAULT (true/false).
"""

import json
import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)

FARM_MODE_FILE = os.path.join("app", "db", "database", "farm_mode.json")


def _default_state() -> Dict:
    env_value = os.environ.get("REVIEW_FARMING_MODE_DEFAULT", "false").lower()
    return {"active": env_value in ("true", "1", "yes", "on")}


def _load() -> Dict:
    if not os.path.exists(FARM_MODE_FILE):
        os.makedirs(os.path.dirname(FARM_MODE_FILE), exist_ok=True)
        state = _default_state()
        _save(state)
        return state
    try:
        with open(FARM_MODE_FILE, encoding="utf-8") as f:
            data = json.load(f)
            if "active" not in data:
                return _default_state()
            return data
    except (json.JSONDecodeError, OSError):
        return _default_state()


def _save(state: Dict) -> None:
    os.makedirs(os.path.dirname(FARM_MODE_FILE), exist_ok=True)
    with open(FARM_MODE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_farm_mode_active() -> bool:
    return bool(_load().get("active", False))


def set_farm_mode(active: bool) -> bool:
    state = {"active": bool(active)}
    _save(state)
    logger.info("Review farming mode → %s", "ON" if active else "OFF")
    return state["active"]
