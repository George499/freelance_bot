"""
Обработчики для учёта квоты Kwork.

Добавь импорт и include_router в __main__.py или в существующий handlers.py:

    from app.bot.quota_handlers import quota_router
    dp.include_router(quota_router)

Команды:
- /quota — показать статус квоты
- /setrefill YYYY-MM-DD [used] — задать дату пополнения и (опционально) уже потраченные отклики
- /setremaining N — синхронизировать с Kwork, указав сколько осталось из 30
- /resettoday — сбросить дневной счётчик

Callback:
- kw_sent:{project_id} — нажатие "Отправил отклик" инкрементирует квоту
- kw_skip:{project_id} — нажатие "Пропустить" просто меняет клавиатуру
"""

import logging
import re
from datetime import date, datetime

from aiogram import F, Router, html
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.exceptions import TelegramBadRequest

from app.config_reader import Settings
from app.db.tables import Project
from app.farm_mode import is_farm_mode_active, set_farm_mode
from app.kwork_filter import (
    categorize_by_budget,
    generate_offer_claude,
    recommend_dump_price,
)
from app.pause_mode import is_bot_paused, set_bot_paused
from app.quota import (
    MONTHLY_QUOTA,
    get_days_until_refill,
    get_quota,
    increment_response,
    init_quota,
    reset_today,
    set_remaining,
)
from kwork import Kwork

logger = logging.getLogger(__name__)
quota_router = Router()


@quota_router.message(Command("quota"))
async def cmd_quota(message: Message):
    """Статус квоты."""
    state = get_quota()
    days_left = get_days_until_refill()
    used = state["responses_used"]
    remaining = MONTHLY_QUOTA - used
    today_used = state["responses_used_today"]

    text = (
        f"📊 <b>Статус квоты Kwork</b>\n\n"
        f"Осталось: <b>{remaining}/{MONTHLY_QUOTA}</b>\n"
        f"Использовано сегодня: <b>{today_used}</b>\n"
        f"Дней до пополнения: <b>{days_left}</b>\n"
        f"Дата пополнения: <code>{state['next_refill_date']}</code>\n\n"
        f"<i>Команды:\n"
        f"/setrefill YYYY-MM-DD [used] — задать дату пополнения\n"
        f"/setremaining N — синхронизировать с Kwork\n"
        f"/resettoday — сбросить дневной счётчик</i>"
    )
    await message.answer(text)


@quota_router.message(Command("setrefill"))
async def cmd_setrefill(message: Message, command: CommandObject):
    """
    /setrefill 2026-05-12           — установить дату пополнения, used=0
    /setrefill 2026-05-12 4         — установить дату пополнения и 4 уже использованных
    """
    if not command.args:
        await message.answer(
            "Использование: <code>/setrefill YYYY-MM-DD [used]</code>\n"
            "Например: <code>/setrefill 2026-05-12 4</code>"
        )
        return

    parts = command.args.split()
    try:
        refill_date = date.fromisoformat(parts[0])
    except ValueError:
        await message.answer("❌ Неверный формат даты. Нужно YYYY-MM-DD.")
        return

    used = 0
    if len(parts) > 1:
        try:
            used = int(parts[1])
        except ValueError:
            await message.answer("❌ Второй аргумент должен быть числом.")
            return
        if used < 0 or used > MONTHLY_QUOTA:
            await message.answer(f"❌ used должно быть от 0 до {MONTHLY_QUOTA}.")
            return

    state = init_quota(refill_date, responses_used=used)
    await message.answer(
        f"✅ Квота обновлена.\n"
        f"Дата пополнения: <code>{state['next_refill_date']}</code>\n"
        f"Использовано: <b>{state['responses_used']}/{MONTHLY_QUOTA}</b>"
    )


@quota_router.message(Command("setremaining"))
async def cmd_setremaining(message: Message, command: CommandObject):
    """/setremaining 26 — установить что осталось 26 из 30"""
    if not command.args:
        await message.answer("Использование: <code>/setremaining N</code>")
        return
    try:
        remaining = int(command.args.strip())
    except ValueError:
        await message.answer("❌ N должно быть числом.")
        return
    if remaining < 0 or remaining > MONTHLY_QUOTA:
        await message.answer(f"❌ N должно быть от 0 до {MONTHLY_QUOTA}.")
        return

    state = set_remaining(remaining)
    await message.answer(
        f"✅ Синхронизировано.\n"
        f"Осталось: <b>{MONTHLY_QUOTA - state['responses_used']}/{MONTHLY_QUOTA}</b>"
    )


@quota_router.message(Command("resettoday"))
async def cmd_resettoday(message: Message):
    state = reset_today()
    await message.answer(
        f"✅ Дневной счётчик сброшен. Использовано сегодня: "
        f"<b>{state['responses_used_today']}</b>"
    )


@quota_router.message(Command("farm_on"))
async def cmd_farm_on(message: Message):
    """Включить режим Отзыв-фарм — приоритет простых заказов для набора отзывов."""
    set_farm_mode(True)
    await message.answer(
        "⚡ <b>Режим Отзыв-фарм включён</b>\n\n"
        "Активные изменения:\n"
        "• Минимальный бюджет в FAST снижен (любая копейка идёт в скоринг)\n"
        "• Бонус +2 за признаки гарантированной приёмки\n"
        "• FAST-уведомления получают метку [⚡ ОТЗЫВ-ФАРМ]\n"
        "• В BIG-уведомлениях напоминание о приоритете FAST\n\n"
        "Выключить: /farm_off"
    )


@quota_router.message(Command("farm_off"))
async def cmd_farm_off(message: Message):
    """Выключить режим Отзыв-фарм."""
    set_farm_mode(False)
    await message.answer(
        "✅ <b>Режим Отзыв-фарм выключен</b>\n\n"
        "Возврат к стандартному скорингу. Включить обратно: /farm_on"
    )


@quota_router.message(Command("farm_status"))
async def cmd_farm_status(message: Message):
    active = is_farm_mode_active()
    if active:
        await message.answer("⚡ Отзыв-фарм: <b>ВКЛЮЧЁН</b>. Выключить: /farm_off")
    else:
        await message.answer("Отзыв-фарм: выключен. Включить: /farm_on")


def _pause_panel_text(quota_state: dict) -> str:
    paused = is_bot_paused()
    farm = is_farm_mode_active()
    used = quota_state["responses_used"]
    remaining = MONTHLY_QUOTA - used
    today_used = quota_state["responses_used_today"]
    days_left = get_days_until_refill()
    pause_line = (
        "⏸ <b>На паузе</b> — Kwork-цикл не выполняется"
        if paused
        else "▶️ <b>Активен</b> — Kwork-цикл идёт каждые 10 мин"
    )
    farm_line = "⚡ Отзыв-фарм: ВКЛ" if farm else "Отзыв-фарм: выкл"
    return (
        f"{pause_line}\n"
        f"{farm_line}\n\n"
        f"📊 Квота: <b>{remaining}/{MONTHLY_QUOTA}</b>, "
        f"сегодня {today_used}, {days_left} дн. до пополнения"
    )


def _pause_panel_keyboard() -> InlineKeyboardMarkup:
    paused = is_bot_paused()
    toggle_text = "▶️ Включить" if paused else "⏸ На паузу"
    toggle_data = "bot_resume" if paused else "bot_pause"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=toggle_text, callback_data=toggle_data)]]
    )


@quota_router.message(Command("status"))
async def cmd_status(message: Message):
    """Панель управления ботом: активен / на паузе + квота + кнопка-тогл."""
    await message.answer(
        _pause_panel_text(get_quota()),
        reply_markup=_pause_panel_keyboard(),
    )


@quota_router.message(Command("pause"))
async def cmd_pause(message: Message):
    """Поставить бота на паузу (Kwork-цикл пропускается)."""
    set_bot_paused(True)
    await message.answer(
        _pause_panel_text(get_quota()),
        reply_markup=_pause_panel_keyboard(),
    )


@quota_router.message(Command("resume"))
async def cmd_resume(message: Message):
    """Снять паузу — Kwork-цикл снова идёт."""
    set_bot_paused(False)
    await message.answer(
        _pause_panel_text(get_quota()),
        reply_markup=_pause_panel_keyboard(),
    )


@quota_router.callback_query(F.data == "bot_pause")
async def cb_bot_pause(callback: CallbackQuery):
    set_bot_paused(True)
    try:
        await callback.message.edit_text(
            _pause_panel_text(get_quota()),
            reply_markup=_pause_panel_keyboard(),
        )
    except TelegramBadRequest:
        pass
    await callback.answer("⏸ На паузе. Kwork-цикл не запускается.", show_alert=False)


@quota_router.callback_query(F.data == "bot_resume")
async def cb_bot_resume(callback: CallbackQuery):
    set_bot_paused(False)
    try:
        await callback.message.edit_text(
            _pause_panel_text(get_quota()),
            reply_markup=_pause_panel_keyboard(),
        )
    except TelegramBadRequest:
        pass
    await callback.answer("▶️ Активен. Следующий цикл — в ближайшие 10 мин.", show_alert=False)


def _kwork_id_from_url(url: str) -> str | None:
    m = re.search(r"/projects/(\d+)", url or "")
    return m.group(1) if m else None


@quota_router.callback_query(F.data.startswith("kw_recheck:"))
async def cb_kwork_recheck(callback: CallbackQuery, config: Settings):
    """Волна 5 (1.1): перепроверить число откликов + показать прирост с находки."""
    internal_id = callback.data.split(":", 1)[1]
    project = await Project.objects().where(Project.id == int(internal_id)).first()
    if not project:
        await callback.answer("Заказ не найден в базе", show_alert=True)
        return

    kw_id = _kwork_id_from_url(project.url)
    if not kw_id:
        await callback.answer("Не удалось определить ID заказа", show_alert=True)
        return

    await callback.answer("Проверяю актуальные отклики…", show_alert=False)
    try:
        kwork = Kwork(
            login=config.kw_login,
            password=config.kw_password,
            phone_last=config.kw_phone_last,
        )
        token = await kwork.token
        resp = await kwork.api_request(
            method="post", api_method="project", id=kw_id, token=token,
        )
        await kwork.close()
        data = resp.get("response") if isinstance(resp, dict) else None
        current_offers = int(data.get("offers", 0)) if data else None
    except Exception as exc:
        logger.warning("Recheck error [%s]: %s", kw_id, exc)
        await callback.message.answer("⚠️ Не удалось перепроверить (ошибка Kwork API).")
        return

    if current_offers is None:
        await callback.message.answer("⚠️ Заказ недоступен (возможно снят).")
        return

    n0 = int(project.offers_at_first or 0)
    delta = current_offers - n0
    # Δt в минутах с момента находки
    try:
        elapsed_min = max(1, int((datetime.now() - project.first_seen_at).total_seconds() // 60))
    except Exception:
        elapsed_min = None

    project.offers_rechecked = current_offers
    await project.save()

    speed_note = ""
    if elapsed_min:
        per_15 = delta / elapsed_min * 15
        if per_15 > 15:
            speed_note = "🌊 быстрый рост — массовый, вероятно скип"
        elif per_15 < 4:
            speed_note = "🎯 медленный рост — узкий, наш кандидат"
        else:
            speed_note = "🟡 средний рост"

    elapsed_txt = f"за {elapsed_min} мин" if elapsed_min else ""
    await callback.message.answer(
        f"🔄 Отклики: было {n0} → стало {current_offers} (+{delta}) {elapsed_txt}\n"
        f"{speed_note}"
    )


@quota_router.callback_query(F.data.startswith("kw_genoffer:"))
async def cb_kwork_genoffer(callback: CallbackQuery, config: Settings):
    """Волна 5 (1.1): сгенерировать черновик отклика по правилам George."""
    internal_id = callback.data.split(":", 1)[1]
    project = await Project.objects().where(Project.id == int(internal_id)).first()
    if not project:
        await callback.answer("Заказ не найден в базе", show_alert=True)
        return
    if not config.anthropic_api_key:
        await callback.answer("ANTHROPIC_API_KEY не задан", show_alert=True)
        return

    await callback.answer("Генерирую черновик…", show_alert=False)

    price = int(project.kwork_price or 0)
    category = categorize_by_budget(price, price)
    is_fast = category == "FAST"
    budget_str = f"{price:,} ₽" if price else "не указан"

    offer = await generate_offer_claude(
        title=project.title,
        description=project.description or "",
        budget=budget_str,
        anthropic_api_key=config.anthropic_api_key,
        is_fast=is_fast,
    )
    if not offer:
        await callback.message.answer("⚠️ Не удалось сгенерировать отклик, попробуй ещё раз.")
        return

    price_line = ""
    rec = recommend_dump_price(price, is_fast)
    if rec:
        price_line = f"\n\n💰 Рекомендую цену: <b>{rec:,} ₽</b> (ниже вилки, набор отзывов)"

    await callback.message.answer(
        f"✍️ <b>Черновик отклика</b> (проверь и поправь перед отправкой):\n\n"
        f"{html.quote(offer)}{price_line}"
    )


@quota_router.callback_query(F.data.startswith("kw_sent:"))
async def cb_kwork_sent(callback: CallbackQuery):
    """Нажатие ✅ Отправил отклик — инкрементирует счётчик."""
    project_id = callback.data.split(":", 1)[1]
    state = increment_response()
    remaining = MONTHLY_QUOTA - state["responses_used"]

    try:
        # Редактируем клавиатуру, убирая кнопки действия
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass  # Сообщение может быть старым

    await callback.answer(
        f"✅ Отклик учтён. Осталось {remaining}/{MONTHLY_QUOTA}",
        show_alert=False,
    )
    logger.info("Response recorded for project %s, remaining=%d", project_id, remaining)


@quota_router.callback_query(F.data.startswith("kw_skip:"))
async def cb_kwork_skip(callback: CallbackQuery):
    """Нажатие 🚫 Пропустить — просто скрывает кнопки, счётчик не трогает."""
    project_id = callback.data.split(":", 1)[1]
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer("Пропущено", show_alert=False)
    logger.info("Project %s skipped", project_id)
