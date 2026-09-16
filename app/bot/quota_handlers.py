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
- /live — актуальные заказы без скоринга (работает без Anthropic-кредитов)
- /auto_on, /auto_off, /auto_status — автоотправка откликов

Callback:
- kw_sent:{project_id} — нажатие "Отправил отклик" инкрементирует квоту
- kw_skip:{project_id} — нажатие "Пропустить" просто меняет клавиатуру
"""

import asyncio
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
    classify_offer_dynamics,
    generate_offer_claude,
    recommend_dump_price,
    score_project,
)
from app.auto_offer import (
    DAILY_LIMIT as AUTO_DAILY_LIMIT,
    MIN_SCORE as AUTO_MIN_SCORE,
    get_state as get_auto_state,
    set_auto,
)
from app.pause_mode import is_bot_paused, set_bot_paused
from app.quota import (
    MONTHLY_QUOTA,
    get_days_until_refill,
    get_quota,
    is_quota_from_api,
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

    source = (
        f"✅ из API Kwork (синк {state.get('api_synced_at', '?')})"
        if is_quota_from_api()
        else "⚠️ ручной счёт — неточно, API не отвечал"
    )
    text = (
        f"📊 <b>Статус квоты Kwork</b>\n\n"
        f"Осталось: <b>{remaining}/{MONTHLY_QUOTA}</b>\n"
        f"Использовано сегодня: <b>{today_used}</b>\n"
        f"Дней до пополнения: <b>{days_left}</b>\n"
        f"Дата пополнения: <code>{state['next_refill_date']}</code>\n"
        f"Источник: {source}\n\n"
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
    # Волна 5 правка 1: помечаем цифру когда API молчит и работает
    # ручной счётчик (кнопка «Отправил отклик») — он занижает расход.
    accuracy = "" if is_quota_from_api() else " ⚠️ неточно (нет данных API)"
    return (
        f"{pause_line}\n"
        f"{farm_line}\n\n"
        f"📊 Квота: <b>{remaining}/{MONTHLY_QUOTA}</b>, "
        f"сегодня {today_used}, {days_left} дн. до пополнения{accuracy}"
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
    await _safe_answer(callback, "⏸ На паузе. Kwork-цикл не запускается.", show_alert=False)


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
    await _safe_answer(callback, "▶️ Активен. Следующий цикл — в ближайшие 10 мин.", show_alert=False)


def _kwork_id_from_url(url: str) -> str | None:
    m = re.search(r"/projects/(\d+)", url or "")
    return m.group(1) if m else None


async def _safe_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False) -> None:
    """Волна 5.1 (баг-фикс): callback.answer не должен ронять хендлер.

    Через CF-прокси апдейты доходят с задержкой → query успевает устареть
    ("query is too old"). Раньше это падало ПЕРВОЙ строкой и срывало всю
    перепроверку. Теперь глотаем — работа (edit карточки) выполнится всё равно.
    """
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest:
        pass


@quota_router.callback_query(F.data.startswith("kw_recheck:"))
async def cb_kwork_recheck(callback: CallbackQuery, config: Settings):
    """Волна 5 (1.1): перепроверить число откликов + показать прирост с находки."""
    internal_id = callback.data.split(":", 1)[1]
    project = await Project.objects().where(Project.id == int(internal_id)).first()
    if not project:
        await _safe_answer(callback, "Заказ не найден в базе", show_alert=True)
        return

    kw_id = _kwork_id_from_url(project.url)
    if not kw_id:
        await _safe_answer(callback, "Не удалось определить ID заказа", show_alert=True)
        return

    await _safe_answer(callback, "Проверяю актуальные отклики…", show_alert=False)
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
        await _safe_answer(callback, "⚠️ Не удалось перепроверить (ошибка Kwork API).", show_alert=True)
        return

    if current_offers is None:
        await _safe_answer(callback, "⚠️ Заказ недоступен (возможно снят).", show_alert=True)
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

    # Та же формула, что и в авто-замерах — чтобы вердикты не расходились.
    _verdict, speed_note = classify_offer_dynamics(n0, current_offers, elapsed_min or 0)

    # Волна 5.1 (1.6/1.1-bis): редактируем САМУ карточку (callback.message).
    # Метка времени замера делает контент уникальным — иначе Telegram отклоняет
    # edit с "message is not modified" когда отклики не изменились.
    elapsed_txt = f"за {elapsed_min} мин" if elapsed_min else ""
    stamp = datetime.now().strftime("%H:%M")
    dyn_line = (
        f"🔄 [{stamp}] Отклики: было {n0} → стало {current_offers} (+{delta}) {elapsed_txt}\n"
        f"{speed_note}"
    )
    try:
        base = callback.message.html_text
        new_text = f"{base}\n\n{dyn_line}"
        await callback.message.edit_text(
            new_text[:4096],
            reply_markup=callback.message.reply_markup,
        )
    except Exception as exc:
        # edit не прошёл (not modified / текст переполнен / прочее) — шлём
        # результат отдельным сообщением-ответом, чтобы George гарантированно
        # увидел перепроверку (alert ненадёжен: query устаревает через прокси).
        logger.info("Recheck edit failed [%s]: %s — шлю сообщением", kw_id, exc)
        try:
            await callback.message.answer(dyn_line)
        except Exception as exc2:
            logger.warning("Recheck answer also failed [%s]: %s", kw_id, exc2)


@quota_router.callback_query(F.data.startswith("kw_genoffer:"))
async def cb_kwork_genoffer(callback: CallbackQuery, config: Settings):
    """Волна 5 (1.1): сгенерировать черновик отклика по правилам George."""
    internal_id = callback.data.split(":", 1)[1]
    project = await Project.objects().where(Project.id == int(internal_id)).first()
    if not project:
        await _safe_answer(callback, "Заказ не найден в базе", show_alert=True)
        return
    if not config.anthropic_api_key:
        await _safe_answer(callback, "ANTHROPIC_API_KEY не задан", show_alert=True)
        return

    await _safe_answer(callback, "Генерирую черновик…", show_alert=False)

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

    await _safe_answer(
        callback,
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
    await _safe_answer(callback, "Пропущено", show_alert=False)
    logger.info("Project %s skipped", project_id)


# === Сентябрь 2026: /live — заказы без скоринга Haiku ===
# Нужна когда Anthropic недоступен (кончились кредиты): в этом состоянии
# score_project падает на каждом заказе, parser глушит его через
# ScoringErrorSilenced, и до Telegram не доходит НИЧЕГО (в дайджесте нули).
# Здесь скоринг сознательно пропускается, но все hard-reject фильтры бота
# отрабатывают полностью — они выполняются ДО обращения к Haiku.
LIVE_MAX_AGE_H = 48      # старше — уже разобрали
LIVE_MAX_OFFERS = 20     # выше — мясорубка, новичку не пробиться
LIVE_SHOW_LIMIT = 12     # чтобы уложиться в лимит сообщения Telegram


@quota_router.message(Command("live"))
async def cmd_live(message: Message, config: Settings):
    """Актуальные заказы: не сняты (status=active), свежие, без мясорубки."""
    await message.answer("🔍 Собираю актуальные заказы (без скоринга)…")
    try:
        kwork = Kwork(
            login=config.kw_login,
            password=config.kw_password,
            phone_last=config.kw_phone_last,
        )
        token = await kwork.token
        raw = await kwork.api_request(
            method="post", api_method="projects",
            categories=config.kw_categories, page=1, token=token,
        )
        items = list(raw["response"])
        for page in range(2, int(raw["paging"].get("pages", 1)) + 1):
            more = await kwork.api_request(
                method="post", api_method="projects",
                categories=config.kw_categories, page=page, token=token,
            )
            items.extend(more["response"])
        await kwork.close()
    except Exception as exc:
        logger.warning("LiveCmd: ошибка Kwork API: %s", exc)
        await message.answer("⚠️ Не удалось получить ленту Kwork.")
        return

    now = int(datetime.now().timestamp())
    pool = [
        i for i in items
        if i.get("status") == "active"
        and i.get("date_confirm")
        and (now - i["date_confirm"]) <= LIVE_MAX_AGE_H * 3600
        and (i.get("offers") or 0) <= LIVE_MAX_OFFERS
    ]

    async def _keep(item: dict):
        """None если заказ отбит hard-reject'ом. Haiku внутри упадёт — ожидаемо."""
        try:
            res = await score_project(
                title=item.get("title") or "",
                description=item.get("description") or "",
                budget=f"{item.get('price') or 0} ₽",
                deadline=f"{(item.get('time_left') or 0) // 86400} дней",
                responses_count=item.get("offers") or 0,
                anthropic_api_key=config.anthropic_api_key,
                hired_percent=item.get("user_hired_percent"),
                buyer_achievements=len(item.get("achievements_list") or []),
                farm_mode_active=is_farm_mode_active(),
                user_projects_count=item.get("user_projects_count") or 0,
                profile_reviews_count=config.profile_reviews_count,
            )
        except Exception:
            return item  # скоринг недоступен — заказ не виноват, показываем
        return None if res.get("hard_reject") else item

    checked = await asyncio.gather(*[_keep(i) for i in pool])
    good = sorted(
        [i for i in checked if i], key=lambda x: x.get("offers") or 0
    )
    rich = [i for i in good if (i.get("price") or 0) >= 5000]
    cheap = [i for i in good if (i.get("price") or 0) < 5000]

    def _row(i: dict) -> str:
        age_h = (now - i["date_confirm"]) / 3600
        hired = i.get("user_hired_percent")
        hired_s = f"наём {hired}%" if hired is not None else "наём н/д"
        return (
            f"<b>{i.get('offers') or 0} откл</b> · {i.get('price') or 0} ₽ · "
            f"{age_h:.0f}ч · {hired_s}\n"
            f"{html.quote((i.get('title') or '')[:60])}\n"
            f"https://kwork.ru/projects/{i.get('id')}/view"
        )

    parts = [
        f"📋 <b>Актуальные заказы</b> (active, до {LIVE_MAX_AGE_H}ч, "
        f"до {LIVE_MAX_OFFERS} откликов)\n"
        f"Скоринг пропущен — hard-reject фильтры отработали.\n"
    ]
    if rich:
        parts.append(f"\n💰 <b>От 5000 ₽</b> ({len(rich)}):\n\n" +
                     "\n\n".join(_row(i) for i in rich[:LIVE_SHOW_LIMIT]))
    if cheap:
        parts.append(f"\n\n🌱 <b>Дешёвые, на отзыв</b> ({len(cheap)}):\n\n" +
                     "\n\n".join(_row(i) for i in cheap[:LIVE_SHOW_LIMIT]))
    if not good:
        parts.append("\nНичего не прошло фильтры.")

    text = "".join(parts)
    for chunk_start in range(0, len(text), 3800):
        await message.answer(text[chunk_start:chunk_start + 3800],
                             disable_web_page_preview=True)


# === Сентябрь 2026: управление автоотправкой откликов ===


@quota_router.message(Command("auto_on"))
async def cmd_auto_on(message: Message):
    """Включить автоотправку откликов."""
    set_auto(True)
    st = get_auto_state()
    await message.answer(
        "🤖 <b>Автоотклик ВКЛЮЧЁН</b>\n\n"
        f"Условия отправки (все должны совпасть):\n"
        f"• скор ≥ <b>{AUTO_MIN_SCORE}</b>\n"
        f"• заказ в моём стеке (Python/Node/боты/API/парсинг)\n"
        f"• не больше <b>{AUTO_DAILY_LIMIT}</b> в день (сегодня {st.get('sent_today', 0)})\n"
        f"• цена = нижняя граница бюджета заказчика\n\n"
        "После каждой отправки пришлю заказ, цену и полный текст.\n"
        "Выключить: /auto_off · история: /auto_status"
    )


@quota_router.message(Command("auto_off"))
async def cmd_auto_off(message: Message):
    """Стоп-кран."""
    set_auto(False)
    await message.answer(
        "🛑 <b>Автоотклик ВЫКЛЮЧЕН</b>. Ничего не отправляется.\n"
        "Включить обратно: /auto_on"
    )


@quota_router.message(Command("auto_status"))
async def cmd_auto_status(message: Message):
    """Состояние автоотклика и что уже ушло."""
    st = get_auto_state()
    on = st.get("enabled")
    lines = [
        f"🤖 Автоотклик: <b>{'ВКЛЮЧЁН' if on else 'выключен'}</b>",
        f"Сегодня отправлено: <b>{st.get('sent_today', 0)}/{AUTO_DAILY_LIMIT}</b>",
        f"Порог скора: {AUTO_MIN_SCORE}",
    ]
    log = st.get("log") or []
    if log:
        lines.append("\n<b>Последние отправленные:</b>")
        for rec in log[:10]:
            lines.append(
                f"• {rec.get('at')} · {rec.get('price')} ₽ · "
                f"{html.quote(str(rec.get('title'))[:55])}"
            )
    else:
        lines.append("\nПока ничего не отправлялось.")
    await message.answer("\n".join(lines))


# === Команды должны работать и в канале ===
# TG_GROUP это channel (get_chat -> type=channel), а не группа. Telegram шлёт
# туда channel_post, а не message, поэтому @quota_router.message(Command(...))
# их не ловит: команда молча уходила в "Update is not handled", бот не отвечал
# и выглядел сломанным. Регистрируем те же функции на channel_post - Message
# там такой же, message.answer() отвечает в канал.
for _cmd, _handler in (
    ("quota", cmd_quota),
    ("live", cmd_live),
    ("auto_on", cmd_auto_on),
    ("auto_off", cmd_auto_off),
    ("auto_status", cmd_auto_status),
    ("farm_on", cmd_farm_on),
    ("farm_off", cmd_farm_off),
    ("farm_status", cmd_farm_status),
    ("resettoday", cmd_resettoday),
):
    quota_router.channel_post.register(_handler, Command(_cmd))
