"""
Quota tracking for Kwork responses.

Kwork использует rolling-период: квота пополняется на дате N (например, 12 мая),
не 1 числа каждого месяца. Этот модуль отслеживает:
- Использованные отклики в текущем периоде
- Дату следующего пополнения
- Автоматический сброс при наступлении даты пополнения
"""

import json
import os
from datetime import date, datetime, timedelta
from typing import Dict, Optional

QUOTA_FILE = os.path.join("app", "db", "database", "quota.json")

# Kwork даёт 30 откликов на период
MONTHLY_QUOTA = 30
# Период длится 30 дней (rolling от даты пополнения)
QUOTA_PERIOD_DAYS = 30


def _default_state() -> dict:
    """Initial state. User must call init_quota() to set correct refill date."""
    today = date.today()
    # Предположим, что следующее пополнение через 30 дней (пересчитается при init)
    next_refill = today + timedelta(days=QUOTA_PERIOD_DAYS)
    return {
        "responses_used": 0,
        "responses_used_today": 0,
        "borderline_sent_today": 0,
        "next_refill_date": next_refill.isoformat(),
        "last_reset_date": today.isoformat(),
    }


def load_quota() -> dict:
    if not os.path.exists(QUOTA_FILE):
        os.makedirs(os.path.dirname(QUOTA_FILE), exist_ok=True)
        state = _default_state()
        save_quota(state)
        return state
    try:
        with open(QUOTA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_state()


def save_quota(state: dict) -> None:
    os.makedirs(os.path.dirname(QUOTA_FILE), exist_ok=True)
    with open(QUOTA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def init_quota(next_refill_date: date, responses_used: int = 0) -> dict:
    """
    Установить параметры квоты вручную.
    Используй когда бот только поставил или когда Kwork изменил тарифную дату.

    Args:
        next_refill_date: дата следующего пополнения (из Kwork UI)
        responses_used: сколько уже потрачено в текущем периоде

    Example:
        init_quota(date(2026, 5, 12), responses_used=4)
    """
    today = date.today()
    state = {
        "responses_used": responses_used,
        "responses_used_today": 0,
        "next_refill_date": next_refill_date.isoformat(),
        "last_reset_date": today.isoformat(),
    }
    save_quota(state)
    return state


def get_quota() -> dict:
    """
    Загрузить квоту и автоматически:
    - Сбросить дневной счётчик если новый день
    - Сбросить месячный счётчик + перенести дату пополнения на +30 дней
      если дата пополнения прошла
    """
    state = load_quota()
    today = date.today()
    today_str = today.isoformat()
    changed = False

    # Ежедневный сброс
    if state.get("last_reset_date") != today_str:
        state["responses_used_today"] = 0
        state["borderline_sent_today"] = 0
        state["last_reset_date"] = today_str
        changed = True
    elif "borderline_sent_today" not in state:
        state["borderline_sent_today"] = 0
        changed = True

    # Сброс квоты при наступлении даты пополнения
    next_refill_str = state.get("next_refill_date")
    if next_refill_str:
        try:
            next_refill = date.fromisoformat(next_refill_str)
            if today >= next_refill:
                # Период закончился — сбрасываем счётчик и двигаем дату
                state["responses_used"] = 0
                # Новая дата пополнения = предыдущая + 30 дней (или от сегодня если сильно отстали)
                new_refill = max(next_refill + timedelta(days=QUOTA_PERIOD_DAYS),
                                 today + timedelta(days=QUOTA_PERIOD_DAYS))
                state["next_refill_date"] = new_refill.isoformat()
                changed = True
        except ValueError:
            pass

    if changed:
        save_quota(state)
    return state


def increment_response() -> dict:
    """Записать что отклик реально был отправлен."""
    state = get_quota()
    state["responses_used"] += 1
    state["responses_used_today"] += 1
    save_quota(state)
    return state


def reset_today() -> dict:
    state = get_quota()
    state["responses_used_today"] = 0
    state["borderline_sent_today"] = 0
    save_quota(state)
    return state


# v4 волна 1.5 рег.3: дневной лимит пограничных уведомлений.
# Поднят 2→4 (после волны 5 воронка стала жёстче, пробуем шире).
BORDERLINE_DAILY_LIMIT = 4


def borderline_sent_today() -> int:
    return int(get_quota().get("borderline_sent_today", 0))


def can_send_borderline() -> bool:
    return borderline_sent_today() < BORDERLINE_DAILY_LIMIT


def increment_borderline() -> dict:
    state = get_quota()
    state["borderline_sent_today"] = int(state.get("borderline_sent_today", 0)) + 1
    save_quota(state)
    return state


# === Волна 5 правка 1 (P0): автоматический учёт коннектов через Kwork API ===
# Ручное подтверждение (кнопка «Отправил отклик») давало завышенный остаток —
# кнопку жмут не всегда, а на этой цифре висит адаптивная фильтрация. Теперь
# источник правды — сам Kwork: блок `connects` приходит В ТОМ ЖЕ ответе на
# api_method="projects", который цикл и так запрашивает, поэтому
# дополнительных запросов к API не делаем (get_connects() не нужен).
#
# Синхронизируем именно `responses_used`, а не заводим параллельный счётчик —
# так все существующие места (дайджест, панель паузы, quota_status) работают
# без изменений. Ручной инкремент остаётся fallback'ом между синками.

# Через сколько часов цифра из API считается протухшей (API молчит/упал).
QUOTA_API_STALE_HOURS = 2


def sync_from_api(
    all_connects: Optional[int],
    active_connects: Optional[int],
    update_time: Optional[int] = None,
) -> dict:
    """Записать остаток коннектов из ответа Kwork API.

    Args:
        all_connects: всего в квоте (обычно 30)
        active_connects: осталось
        update_time: unix ts пополнения (дата приходит с сервера — считать
            её самостоятельно больше не нужно)

    Если API вернул None — состояние не трогаем, остаётся последнее
    известное значение (см. is_quota_from_api для пометки о неточности).
    """
    if active_connects is None or all_connects is None:
        return get_quota()

    state = get_quota()  # сначала штатные сбросы, потом перезапись из API
    state["responses_used"] = max(0, int(all_connects) - int(active_connects))
    state["api_all_connects"] = int(all_connects)
    state["api_active_connects"] = int(active_connects)
    state["api_synced_at"] = datetime.now().isoformat(timespec="seconds")
    if update_time:
        state["api_update_time"] = int(update_time)
        state["next_refill_date"] = (
            datetime.fromtimestamp(int(update_time)).date().isoformat()
        )
    save_quota(state)
    return state


def is_quota_from_api() -> bool:
    """True если цифра квоты пришла из API и ещё не протухла.

    False → показываем цифру с пометкой «неточно» (работает ручной счётчик).
    """
    synced_at = get_quota().get("api_synced_at")
    if not synced_at:
        return False
    try:
        synced = datetime.fromisoformat(synced_at)
    except (ValueError, TypeError):
        return False
    return (datetime.now() - synced) < timedelta(hours=QUOTA_API_STALE_HOURS)


def set_remaining(remaining: int) -> dict:
    """
    Синхронизировать счётчик с реальным значением из Kwork.
    Используй если бот ошибся в счёте и ты видишь в Kwork другое число.

    Example:
        set_remaining(26)  # в Kwork написано "Осталось 26 из 30"
    """
    state = get_quota()
    state["responses_used"] = max(0, MONTHLY_QUOTA - remaining)
    save_quota(state)
    return state


def get_days_until_refill() -> int:
    """Сколько дней до пополнения квоты."""
    state = get_quota()
    next_refill_str = state.get("next_refill_date")
    if not next_refill_str:
        return QUOTA_PERIOD_DAYS
    try:
        next_refill = date.fromisoformat(next_refill_str)
        diff = (next_refill - date.today()).days
        return max(0, diff)
    except ValueError:
        return QUOTA_PERIOD_DAYS


def get_period_progress() -> float:
    """
    Прогресс текущего периода 0.0-1.0.
    0.0 = только что пополнилось, 1.0 = сегодня день пополнения.
    """
    days_left = get_days_until_refill()
    if days_left >= QUOTA_PERIOD_DAYS:
        return 0.0
    return (QUOTA_PERIOD_DAYS - days_left) / QUOTA_PERIOD_DAYS
