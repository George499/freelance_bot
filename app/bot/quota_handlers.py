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
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramBadRequest

from app.quota import (
    MONTHLY_QUOTA,
    get_days_until_refill,
    get_quota,
    increment_response,
    init_quota,
    reset_today,
    set_remaining,
)

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
