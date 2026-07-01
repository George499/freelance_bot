import asyncio
import logging
import random
import re
from datetime import datetime

import aiohttp
import feedparser
from aiogram import Bot, html
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from selectolax.lexbor import LexborHTMLParser as htmlp

from app.bot.keyboards import apply_button
from app.config_reader import Settings
from app.db.tables import FreelancePlatform, Project
from app.farm_mode import is_farm_mode_active
from app.pause_mode import is_bot_paused
from app.kwork_filter import (
    MONTHLY_QUOTA,
    RECHECK_SCHEDULE_MIN,
    classify_offer_dynamics,
    generate_offer_claude,
    next_recheck_delay_min,
    quota_status,
    score_project,
    should_respond,
)
from app.quota import (
    can_send_borderline,
    get_days_until_refill,
    get_quota,
    increment_borderline,
)
from kwork import Kwork

logger = logging.getLogger(__name__)

# Минимальный скор чтобы заказ попал в Telegram. Ниже — только в логи.
# 5-6 — пограничные (можно глянуть, но не рекомендую).
# 7+ — рекомендую отклик.
# ≤4 — полное игнорирование, молчим.
TELEGRAM_SCORE_THRESHOLD = 5

# P2.3 (вариант A): freshness-бейдж через прокси time_left + offers
# (Kwork API не отдаёт published_at). Чем больше откликов / меньше времени до
# закрытия приёма — тем ниже шанс что заказчик вообще прочитает наш отклик.
FRESH_OFFERS_MAX = 10

# Жёсткий cut-off по возрасту заказа (date_confirm).
# Заказы старше — НЕ скорим (бессмысленно, перегружены откликами).
MAX_ORDER_AGE_HOURS = 48
FRESH_TIME_LEFT_MIN_SEC = 2 * 86400      # ≥ 2 дня — заказ скорее всего свежий
LATE_OFFERS_MIN = 30
LATE_TIME_LEFT_MAX_SEC = 12 * 3600       # < 12 ч — приём почти закрыт
LOW_QUOTA_THRESHOLD = 5                  # при ≤5 оставшихся квот не тратимся на 🔴


def _freshness_badge(time_left_sec: int, offers_count: int) -> tuple[str, str]:
    """Возвращает (эмодзи, читаемый лейбл) для freshness."""
    is_late = offers_count >= LATE_OFFERS_MIN or (
        0 < time_left_sec < LATE_TIME_LEFT_MAX_SEC
    )
    if is_late:
        return "🔴", "поздно (много откликов / скоро закроется приём)"
    is_fresh = (
        offers_count < FRESH_OFFERS_MAX
        and time_left_sec >= FRESH_TIME_LEFT_MIN_SEC
    )
    if is_fresh:
        return "🟢", "свежий"
    return "🟡", "средний"

UPWORK_TEMPLATE = (
    "☘️ <b>{title}</b>\n\n"
    "<i>{description}</i>\n\n"
    "Budget: <code>{budget}</code>\n"
    "Hourly Range: {hourly_range}\n"
    "Category: {category}\n"
    "Country: {country}"
)


def _kwork_action_keyboard(project_id: int, project_url: str) -> InlineKeyboardMarkup:
    """Клавиатура под рекомендованными заказами (волна 5).

    Кнопки действий вместо бесполезных "Открыть/Пропустить":
    - Перепроверить отклики (повторный замер + прирост)
    - Сгенерировать отклик (черновик по правилам George)
    - Отправил отклик (учёт квоты) — оставлена, реально нужна.
    Ссылка на заказ остаётся в тексте карточки.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Перепроверить отклики",
                    callback_data=f"kw_recheck:{project_id}",
                ),
                InlineKeyboardButton(
                    text="✍️ Сгенерировать отклик",
                    callback_data=f"kw_genoffer:{project_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✅ Отправил отклик",
                    callback_data=f"kw_sent:{project_id}",
                ),
            ],
        ]
    )


QUALI_UNKNOWNS_THRESHOLD = 2

# P3.1: тип заказа для воронки short / long
ORDER_TYPE_BADGES = {
    "short": ("🐜", "короткий"),
    "medium": ("🦌", "средний"),
    "long": ("🦣", "крупный"),
}

# v3: бейдж категории заказа
CATEGORY_BADGES = {
    "BIG": "💼 BIG",
    "FAST": "🚀 FAST",
    "DUAL": "🔄 DUAL",
}


def _format_kwork_message(
    title: str,
    desc: str,
    budget_str: str,
    score: int,
    is_ai: bool,
    respond: bool,
    decision_reason: str,
    score_reason: str,
    quota: dict,
    url: str,
    no_code_required: str | None,
    scope_unclear: bool,
    critical_unknowns: list[str] | None = None,
    is_quick_cash: bool = False,
    freshness_emoji: str = "",
    freshness_label: str = "",
    order_type: str | None = None,
    category: str | None = None,
    score_big: int | None = None,
    score_fast: int | None = None,
    offers_count: int = 0,
    buyer_achievement_names: list[str] | None = None,
    farm_mode_active: bool = False,
    competition_tier: str | None = None,
) -> str:
    unknowns = critical_unknowns or []
    is_quali = len(unknowns) >= QUALI_UNKNOWNS_THRESHOLD

    score_suffix = ""
    if category == "DUAL" and score_big is not None and score_fast is not None:
        score_suffix = f" (BIG={score_big}, FAST={score_fast})"

    # v4 6.1: метки конкуренции после скора
    competition_marks = []
    if offers_count > 0:
        competition_marks.append(f"📨 {offers_count}")
    if buyer_achievement_names:
        # Берём первую (самую заметную) ачивку
        competition_marks.append(f"🏅 {buyer_achievement_names[0].lower()}")
    competition_suffix = " " + " ".join(f"[{m}]" for m in competition_marks) if competition_marks else ""

    # P2.4: GO / QUALI / Пограничный / Пропуск роутинг
    if respond:
        if is_quali:
            status = f"🟡 QUALI — скор {score}/10{score_suffix}{competition_suffix} (ответить, но сначала уточнить)"
        elif score >= 9:
            status = f"🔥 GO — скор {score}/10{score_suffix}{competition_suffix}"
        else:
            status = f"🟢 GO — скор {score}/10{score_suffix}{competition_suffix}"
    elif score >= TELEGRAM_SCORE_THRESHOLD:
        if is_quali:
            status = f"🟡 QUALI — скор {score}/10{score_suffix}{competition_suffix} (пограничный + 2+ неизвестных)"
        else:
            status = f"👀 Пограничный — скор {score}/10{score_suffix}{competition_suffix} (ждём лучшего)"
    else:
        status = f"🔴 Пропуск — скор {score}/10{score_suffix}{competition_suffix}"

    cat_badge = CATEGORY_BADGES.get(category) if category else None
    # v4 раздел 9: метка отзыв-фарма для FAST
    if cat_badge and farm_mode_active and category == "FAST":
        cat_badge = f"{cat_badge} [⚡ ОТЗЫВ-ФАРМ]"
    header = f"{cat_badge}  {status}" if cat_badge else status

    badges = []
    # Волна 5 (2.2): пометка узкая ниша / широкое мясо
    if competition_tier == "narrow":
        badges.append("🎯 узкая ниша")
    elif competition_tier == "wide":
        badges.append("🌊 широкое мясо")
    type_badge = ORDER_TYPE_BADGES.get(order_type) if order_type else None
    if type_badge:
        badges.append(f"{type_badge[0]} {type_badge[1]}")
    if freshness_emoji:
        badges.append(f"{freshness_emoji} {freshness_label}")
    if is_ai:
        badges.append("🤖 AI")
    if no_code_required:
        badges.append(f"⛔ {no_code_required}")
    if scope_unclear:
        badges.append("⚠️ scope unclear")
    if is_quick_cash:
        badges.append("⚡ Быстрые деньги")

    badges_line = " ".join(badges)
    # Telegram message limit 4096; оставляем запас под header/badges/решение/квота/ссылку.
    preview = html.quote(desc[:2500]) + ("..." if len(desc) > 2500 else "")

    unknowns_block = ""
    if unknowns:
        unknowns_block = "❓ Неизвестные: " + html.quote("; ".join(unknowns)) + "\n\n"

    # v4 раздел 9: подвал для BIG/DUAL когда farm-mode активен
    farm_footer = ""
    if farm_mode_active and category in ("BIG", "DUAL"):
        farm_footer = (
            "\n\n💡 Сейчас приоритет — FAST для отзывов. "
            "BIG только если идеальный матч."
        )

    return (
        f"{header}  {badges_line}\n\n"
        f"<b>{html.quote(title)}</b>\n\n"
        f"💰 {budget_str}\n"
        f"{preview}\n\n"
        f"{unknowns_block}"
        f"Решение: {html.quote(decision_reason)}\n"
        f"Причина скора: {html.quote(score_reason)}\n\n"
        f"📊 Квота: {quota['remaining']}/{MONTHLY_QUOTA} осталось, "
        f"{quota['days_left']} дн. до пополнения, режим: {quota['pace']}"
        f"{farm_footer}\n\n"
        f"🔗 {url}"
    )


async def get_upwork_projects(bot: Bot, config: Settings):
    url = "https://www.upwork.com/ab/feed/jobs/rss"
    params = {
        "q": config.up_question,
        "subcategory2_uid": config.up_subcategories,
        "sort": "recency",
        "paging": "0;50",
        "api_params": "1",
        "securityToken": config.up_securitytoken,
        "userUid": config.up_useruid,
        "orgUid": config.up_orguid,
    }

    def parse_metadata(html_text: str) -> dict:
        parsed_data = {}
        patterns = {
            "hourly_range": r"Hourly Range:(.*?)(?:Posted On:|$)",
            "budget": r"Budget:(.*?)(?:Posted On:|$)",
            "posted": r"Posted On:(.*?)(?:Category:|$)",
            "category": r"Category:(.*?)(?:Skills:|$)",
            "skills": r"Skills:(.*?)(?:Country:|$)",
            "country": r"Country:(.*?)(?:click|$)",
        }
        earliest_position = len(html_text)
        for pattern in patterns.values():
            match = re.search(pattern, html_text, re.DOTALL)
            if match and match.start() < earliest_position:
                earliest_position = match.start()
        parsed_data["desc"] = html_text[:earliest_position].strip()
        for key, pattern in patterns.items():
            match = re.search(pattern, html_text, re.DOTALL)
            parsed_data[key] = match.group(1).strip() if match else None
        return parsed_data

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as response:
            if response.status != 200:
                return
            content = await response.text()

        feeds = feedparser.parse(content)
        for feed in feeds.entries:
            project = await Project.objects().get_or_create(
                Project.url == feed.link, {Project.title: feed.title}
            )

            if not project._was_created:
                continue

            data = parse_metadata(
                htmlp(feed.description).text(separator="", strip=False)
            )

            project.description = data["desc"]
            project.freelance_platform = FreelancePlatform.UPWORK
            await project.save()

            text = UPWORK_TEMPLATE.format(
                title=html.quote(feed.title),
                description=html.quote(data["desc"][:3000]),
                budget=data["budget"],
                hourly_range=data["hourly_range"],
                category=data["category"],
                country=data["country"],
            )

            btn_data = {"id": project.id, "lang": "en", "username": config.bot_username}

            await bot.send_message(
                chat_id=config.tg_group,
                text=text,
                message_thread_id=config.tg_topic_id,
                reply_markup=apply_button(
                    text="Click to apply", url=feed.link, data=btn_data
                ),
            )
            await asyncio.sleep(random.choice([1, 2, 3]))


async def _recheck_edit_card(
    bot: Bot, config: Settings, proj, body: str, collapse: bool = False
) -> None:
    """Волна 5.1 (1.6): дописывает динамику в ИСХОДНУЮ карточку через edit,
    а не шлёт новое сообщение — лента не раздувается.

    - обычный замер: card_text + строка динамики (ссылка на заказ сохраняется,
      она уже внутри card_text — 1.1-bis);
    - collapse=True (мясорубка / заказ снят / закрыт): карточка сворачивается
      до заголовка + пометки + ссылки, чтобы не занимать место.
    Если edit невозможен (нет message_id / сообщение удалено) — только лог,
    нового сообщения НЕ шлём (цель 1.6 — короткая лента).
    """
    if not proj.tg_message_id:
        logger.info("RecheckEdit skip [%s]: нет message_id", (proj.title or "")[:40])
        return

    if collapse:
        new_text = (
            f"<b>{html.quote(proj.title or '')[:80]}</b>\n"
            f"{body}\n🔗 {proj.url}"
        )
    else:
        base = proj.card_text or (
            f"<b>{html.quote(proj.title or '')[:80]}</b>\n🔗 {proj.url}"
        )
        new_text = f"{base}\n\n{body}"

    # Кнопки действий сохраняем (recheck/genoffer/sent), ссылка — в тексте.
    keyboard = _kwork_action_keyboard(proj.id, proj.url)
    try:
        await bot.edit_message_text(
            chat_id=config.tg_group,
            message_id=proj.tg_message_id,
            text=new_text[:4096],
            reply_markup=keyboard,
        )
    except Exception as exc:
        # "message is not modified" / "message to edit not found" — не критично.
        logger.info("RecheckEdit [%s]: edit не прошёл (%s)", (proj.title or "")[:40], exc)


async def _process_pending_rechecks(bot: Bot, config: Settings, kwork, token) -> None:
    """Волна 5c: авто-замеры динамики откликов по расписанию 15/45/90/360 мин.

    Перепроверяет только заказы которые были показаны (next_recheck_at > 0).
    На каждом замере запрашивает актуальные offers, классифицирует скорость
    роста и шлёт уведомление при значимом вердикте (мясорубка / узкая ниша).
    """
    now_ts = int(datetime.now().timestamp())
    pending = (
        await Project.objects()
        .where(
            (Project.freelance_platform == FreelancePlatform.KWORK)
            & (Project.next_recheck_at > 0)
            & (Project.next_recheck_at <= now_ts)
            & (Project.recheck_done < len(RECHECK_SCHEDULE_MIN))
        )
        .limit(10)  # safety: не больше 10 перепроверок за цикл (нагрузка на Kwork)
    )
    for proj in pending:
        kw_id = None
        m = re.search(r"/projects/(\d+)", proj.url or "")
        if m:
            kw_id = m.group(1)
        if not kw_id:
            proj.next_recheck_at = 0  # битый url — снять с расписания
            await proj.save()
            continue

        time_left = None
        order_gone = False
        try:
            resp = await kwork.api_request(
                method="post", api_method="project", id=kw_id, token=token,
            )
            data = resp.get("response") if isinstance(resp, dict) else None
            if data:
                current = int(data.get("offers", 0))
                tl = data.get("time_left")
                time_left = tl if isinstance(tl, (int, float)) else None
            else:
                # API ответил, но заказа нет → снят/удалён (не сетевой сбой).
                current = None
                order_gone = isinstance(resp, dict)
        except Exception as exc:
            logger.warning("RecheckCycle error [%s]: %s", kw_id, exc)
            current = None

        if current is None:
            # Заказ недоступен — снимаем с расписания. Если это явное удаление
            # (API ответил пустым), сообщаем под карточкой; сетевой сбой — молча.
            proj.next_recheck_at = 0
            await proj.save()
            if order_gone:
                await _recheck_edit_card(
                    bot, config, proj,
                    "❌ заказ снят или удалён — отклик уже не отправить",
                    collapse=True,
                )
            continue

        # Приём откликов завершён (дедлайн вышел / заказчик закрыл досрочно).
        if time_left is not None and time_left <= 0:
            proj.next_recheck_at = 0
            await proj.save()
            await _recheck_edit_card(
                bot, config, proj,
                "🔒 приём откликов завершён — заказ закрыт",
                collapse=True,
            )
            continue

        n0 = int(proj.offers_at_first or 0)
        try:
            elapsed_min = max(1, int((datetime.now() - proj.first_seen_at).total_seconds() // 60))
        except Exception:
            elapsed_min = (proj.recheck_done + 1) * 15

        verdict, note = classify_offer_dynamics(n0, current, elapsed_min)
        stage = int(proj.recheck_done) + 1
        proj.offers_rechecked = current
        proj.recheck_done = stage

        # Планируем следующий замер или завершаем.
        delay = next_recheck_delay_min(stage)
        is_final = delay is None
        # Мясорубка — прекращаем замеры досрочно (динамика ясна).
        if verdict == "fast":
            proj.next_recheck_at = 0
        elif is_final:
            proj.next_recheck_at = 0
        else:
            proj.next_recheck_at = int(proj.first_seen_at.timestamp()) + delay * 60
        await proj.save()

        # Обновляем карточку через edit (1.6) только при значимом сигнале:
        #  - быстрый рост (мясорубка) → сворачиваем карточку (collapse);
        #  - финальный замер с медленным ростом (узкий, актуален) → дописываем
        #    строку динамики в карточку.
        if verdict == "fast":
            await _recheck_edit_card(
                bot, config, proj,
                f"🌊 МЯСОРУБКА: {note} — коннект утонет, скип",
                collapse=True,
            )
        elif is_final and verdict == "slow":
            await _recheck_edit_card(bot, config, proj, f"📈 {note}")
        logger.info(
            "RecheckCycle [%s] stage=%d n0=%d→n1=%d verdict=%s",
            (proj.title or "")[:50], stage, n0, current, verdict,
        )
        await asyncio.sleep(random.choice([1, 2]))


async def get_kwork_projects(bot: Bot, config: Settings):
    # Soft-pause: цикл пропускается пока флаг активен (управление через /pause).
    if is_bot_paused():
        logger.info("BotPaused: Kwork-цикл пропущен")
        return

    kwork = Kwork(
        login=config.kw_login,
        password=config.kw_password,
        phone_last=config.kw_phone_last,
    )
    token = await kwork.token
    categories_ids = config.kw_categories

    # Волна 5c: сначала отрабатываем отложенные авто-замеры динамики откликов.
    try:
        await _process_pending_rechecks(bot, config, kwork, token)
    except Exception as exc:
        logger.warning("pending rechecks failed: %s", exc)

    raw_projects = await kwork.api_request(
        method="post",
        api_method="projects",
        categories=categories_ids,
        page=1,
        token=token,
    )

    if not raw_projects["success"]:
        return await kwork.close()

    def _extract_hired_percent(item: dict) -> int | None:
        """Пытается вытащить процент найма заказчика из разных возможных полей Kwork API."""
        user = item.get("user") or {}
        candidates = [
            item.get("user_hired_percent"),
            item.get("hired_percent"),
            item.get("customer_hired_percent"),
            user.get("hired_percent") if isinstance(user, dict) else None,
            user.get("hire_percent") if isinstance(user, dict) else None,
        ]
        for v in candidates:
            if v is None:
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
        return None

    first_project_logged = False

    def get_project_data(response: list) -> list:
        nonlocal first_project_logged
        result = []
        for item in response:
            if not first_project_logged:
                logger.info("DEBUG project sample: %s", item)
                first_project_logged = True
            achievements = item.get("achievements_list") or []
            achievement_names = [
                a.get("name", "")
                for a in achievements
                if isinstance(a, dict) and a.get("name")
            ]
            result.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "description": item.get("description"),
                    "price": item.get("price"),
                    "possible_price_limit": item.get("possible_price_limit"),
                    "offers": item.get("offers", 0),
                    "time_left": item.get("time_left", 0),
                    "date_confirm": item.get("date_confirm", 0),
                    "hired_percent": _extract_hired_percent(item),
                    "buyer_achievements_count": len(achievements),
                    "buyer_achievements_names": achievement_names,
                }
            )
        return result

    projects = get_project_data(raw_projects["response"])
    paging = raw_projects["paging"]
    # Kwork сортирует страницы не строго по дате — свежие заказы могут быть
    # на любой странице (на page 1 есть и 5-часовые, и 280-дневные). Поэтому
    # обходим ВСЕ страницы, иначе пропускаем свежие на page 6+.
    total_pages = int(paging.get("pages", 1))

    for page in range(2, total_pages + 1):
        other_projects = await kwork.api_request(
            method="post",
            api_method="projects",
            categories=categories_ids,
            page=page,
            token=token,
        )
        projects.extend(get_project_data(other_projects["response"]))

    # Квота: rolling-период Kwork
    quota_state = get_quota()
    days_until_refill = get_days_until_refill()
    quota = quota_status(
        used_this_month=quota_state["responses_used"],
        used_today=quota_state["responses_used_today"],
        days_until_refill=days_until_refill,
    )

    # Фильтр свежести: режем заказы старше MAX_ORDER_AGE_HOURS до скоринга.
    # Kwork сортирует страницы непредсказуемо — после получения всех страниц
    # отсекаем старьё чтобы не тратить Haiku-токены и БД-место.
    now_ts = int(datetime.now().timestamp())
    age_cutoff_ts = now_ts - MAX_ORDER_AGE_HOURS * 3600
    fresh_projects = [
        p for p in projects
        if p.get("date_confirm") and p["date_confirm"] >= age_cutoff_ts
    ]
    stats_too_old = len(projects) - len(fresh_projects)
    if stats_too_old:
        logger.info(
            "FreshnessCutoff: отсеяно %d/%d заказов старше %dч",
            stats_too_old, len(projects), MAX_ORDER_AGE_HOURS,
        )
    projects = fresh_projects

    stats = {
        "total": len(projects),
        "seen": 0,
        "hard_reject": 0,
        "low_score_silenced": 0,
        "borderline_sent": 0,
        "recommended": 0,
    }

    for project in projects:
        kw_project_url = "https://kwork.ru/projects/" + str(project.get("id"))

        kw_project = await Project.objects().get_or_create(
            Project.url == kw_project_url, {Project.title: project.get("title")}
        )

        if not kw_project._was_created:
            stats["seen"] += 1
            continue

        desc = htmlp(project.get("description")).text(separator="\n", strip=True)

        kw_project.description = desc
        kw_project.freelance_platform = FreelancePlatform.KWORK
        # Волна 5: сохраняем реальную цену (нижняя граница) и N0 откликов
        # для кнопок (генерация отклика, перепроверка) и динамики (5c).
        kw_project.kwork_price = project.get("price") or 0
        kw_project.offers_at_first = project.get("offers", 0) or 0
        await kw_project.save()

        title = project.get("title", "")
        price = project.get("price") or 0
        # Волна 5 (1.2/1.2-bis): верхняя граница Kwork — артефакт (x3 при галочке
        # "можно больше"), не бюджет. Показываем и скорим ТОЛЬКО нижнюю границу.
        # Потолок не показываем совсем — он только засоряет карточку.
        budget_scoring = f"{price:,} ₽"
        budget_str = f"{price:,} ₽"

        # Волна 5 (1.4-bis): нижняя рамка бюджета 5000₽. Заказы дешевле системно
        # дают мусор (размытые ТЗ, случайные заказчики), а коннект стоит столько
        # же. Демпинг ради отзыва работает на нормальных задачах от ~5к, не на
        # дешёвке за 500-1500₽. price=0 (бюджет не указан) — пропускаем в скоринг.
        if 0 < price < 5000:
            stats["low_score_silenced"] += 1
            logger.info("LowBudgetSkip [%s]: бюджет %d ₽ < 5000 — тишина", title[:60], price)
            await asyncio.sleep(random.choice([1, 2, 3]))
            continue

        time_left = project.get("time_left") or 0
        deadline_str = f"{time_left // 86400} дней" if time_left else "не указан"

        hired_percent = project.get("hired_percent")
        buyer_achievements_count = project.get("buyer_achievements_count", 0)
        buyer_achievements_names = project.get("buyer_achievements_names", [])
        user_projects_count = project.get("user_projects_count", 0)
        offers_count = project.get("offers", 0) or 0

        # Волна 3 идея 48 (доп. модификатор): 50+ откликов — коннект потерян,
        # не отправляем уведомление вообще, даже если скор высокий. Исключение —
        # большой бюджет (>=100k) + медалька покупателя + hire_rate > 50%.
        if offers_count >= 50:
            big_budget = price and price >= 100_000  # волна 5: по нижней границе
            strong_buyer = buyer_achievements_count >= 1 and (
                hired_percent is not None and hired_percent > 50
            )
            if not (big_budget and strong_buyer):
                stats["low_score_silenced"] += 1
                logger.info(
                    "HighCompetitionSkip [%s]: %d откликов — коннект потерян, тишина",
                    title[:60], offers_count,
                )
                await asyncio.sleep(random.choice([1, 2, 3]))
                continue

        farm_active = is_farm_mode_active()
        score_result = await score_project(
            title=title,
            description=desc,
            budget=budget_scoring,
            deadline=deadline_str,
            responses_count=offers_count,
            anthropic_api_key=config.anthropic_api_key,
            hired_percent=hired_percent,
            buyer_achievements=buyer_achievements_count,
            farm_mode_active=farm_active,
            user_projects_count=user_projects_count,
        )

        # Hard reject — тишина, только в логи
        if score_result["hard_reject"]:
            stats["hard_reject"] += 1
            logger.info(
                "HardReject [%s]: %s",
                title[:60], score_result.get("reason", ""),
            )
            await asyncio.sleep(random.choice([1, 2, 3]))
            continue

        # Anthropic упал / другая ошибка скоринга — молчим, не шумим в группу.
        if score_result.get("scoring_error"):
            stats["low_score_silenced"] += 1
            logger.warning(
                "ScoringErrorSilenced [%s]: %s",
                title[:60], score_result.get("reason", ""),
            )
            await asyncio.sleep(random.choice([1, 2, 3]))
            continue

        # Низкий скор (≤4) — тоже молчим
        if score_result["score"] < TELEGRAM_SCORE_THRESHOLD:
            stats["low_score_silenced"] += 1
            logger.info(
                "LowScore [%s] score=%d: %s",
                title[:60],
                score_result["score"],
                score_result.get("reason", ""),
            )
            await asyncio.sleep(random.choice([1, 2, 3]))
            continue

        respond, decision_reason = should_respond(
            score=score_result["score"],
            quota=quota,
            is_ai=score_result["is_ai"],
            no_code_required=score_result.get("no_code_required"),
        )

        # === Волна 30.06 A1: velocity-гейт по абсолюту (флипает вердикт) ===
        # Заголовок GO не должен стоять над "мясорубкой". Абсолют откликов на
        # момент показа = размер очереди куда заходишь.
        #  - FAST: абсолют >= 20 → SKIP (заказы взаимозаменяемы, забитая очередь =
        #    мёртвый коннект);
        #  - BIG/DUAL: абсолют >= 25 → флип в QUALI ("очередь забита, нужен козырь").
        _cat = score_result.get("category")
        if _cat == "FAST" and offers_count >= 20:
            stats["low_score_silenced"] += 1
            logger.info(
                "VelocityGateSkip [FAST %s]: %d откликов ≥20 — очередь забита, SKIP",
                title[:60], offers_count,
            )
            await asyncio.sleep(random.choice([1, 2, 3]))
            continue
        velocity_quali = False
        if _cat in ("BIG", "DUAL") and offers_count >= 25 and respond:
            respond = False
            velocity_quali = True
            decision_reason = (
                f"velocity-гейт: очередь забита ({offers_count} откликов), "
                f"нужен козырь против массы — QUALI, не GO"
            )
            logger.info(
                "VelocityGateQuali [BIG %s]: %d откликов ≥25 — флип GO→QUALI",
                title[:60], offers_count,
            )

        # Волна 3 идея 48 (доп. модификатор): при 30-50 откликах присылаем
        # ТОЛЬКО рекомендации (respond=True), пограничные глушим.
        # Исключение: velocity-QUALI (BIG с забитой очередью) — показываем с
        # пометкой, не глушим (BIG терпит очередь чуть больше, Selected Proposals).
        if not respond and not velocity_quali and 30 <= offers_count < 50:
            stats["low_score_silenced"] += 1
            logger.info(
                "MidCompetitionBorderlineSkip [%s]: %d откликов — пограничный глушим",
                title[:60], offers_count,
            )
            await asyncio.sleep(random.choice([1, 2, 3]))
            continue

        freshness_emoji, freshness_label = _freshness_badge(time_left, offers_count)

        # P2.3: при низкой квоте не тратим патрон на 🔴 заказы (downgrade в borderline)
        if (
            respond
            and freshness_emoji == "🔴"
            and quota["remaining"] <= LOW_QUOTA_THRESHOLD
        ):
            logger.info(
                "LowQuotaSkip [%s]: квота=%d, freshness=🔴 — downgrade в borderline",
                title[:60], quota["remaining"],
            )
            respond = False
            decision_reason = (
                f"низкая квота ({quota['remaining']}) + поздний заказ — не тратим патрон"
            )

        # ⚡ Быстрые деньги: реальный бюджет (нижняя граница) 25-60k, срок ≤2 дней,
        # скор ≥7, откликов <30 и это НЕ AI-задача. (Волна 5: по нижней, не по потолку.)
        is_quick_cash = (
            25000 <= price <= 60000
            and 0 < time_left <= 2 * 86400
            and score_result["score"] >= 7
            and offers_count < 30
            and not score_result["is_ai"]
        )
        if is_quick_cash:
            logger.info(
                "QuickCash [%s]: price=%d, time_left=%dh, offers=%d, score=%d",
                title[:60], price, time_left // 3600,
                offers_count, score_result["score"],
            )

        # Волна 3 идея 47: borderline присылаем только при ключевых факторах
        # (медалька покупателя, низкая конкуренция, или высокий скор 7+).
        # Иначе пограничные глушим — почти никогда не превращаются в реальный отклик.
        if not respond:
            has_badges = bool(buyer_achievements_names)
            low_competition = offers_count < 10
            score_high_for_borderline = score_result["score"] >= 7
            has_key_factors = has_badges or low_competition or score_high_for_borderline
            if not has_key_factors:
                stats["borderline_sent"] += 1
                logger.info(
                    "BorderlineNoKeyFactors [%s] score=%d offers=%d badges=%s: тишина",
                    title[:60], score_result["score"], offers_count, has_badges,
                )
                await asyncio.sleep(random.choice([1, 2, 3]))
                continue

        # v4 волна 1.5 рег.3: дневной лимит пограничных уведомлений.
        # Применяется ТОЛЬКО когда respond=False (рекомендации к отклику — без лимита).
        if not respond and not can_send_borderline():
            stats["borderline_sent"] += 1  # учётно
            logger.info(
                "BorderlineDailyLimit [%s]: лимит пограничных исчерпан — тишина",
                title[:60],
            )
            await asyncio.sleep(random.choice([1, 2, 3]))
            continue

        # Волна 3 идея 47: для borderline сжимаем обоснование до первой части
        # (классификация Haiku), без длинного хвоста бонусов/штрафов.
        raw_reason = score_result.get("reason", "")
        if not respond and raw_reason:
            # Берём первый фрагмент до ';' — это вывод Haiku, остальное модификаторы.
            short_reason = raw_reason.split(";", 1)[0].strip()
            if len(short_reason) > 220:
                short_reason = short_reason[:217] + "..."
            display_reason = short_reason
        else:
            display_reason = raw_reason

        text = _format_kwork_message(
            title=title,
            desc=desc,
            budget_str=budget_str,
            score=score_result["score"],
            is_ai=score_result["is_ai"],
            respond=respond,
            decision_reason=decision_reason,
            score_reason=display_reason,
            quota=quota,
            url=kw_project_url,
            no_code_required=score_result.get("no_code_required"),
            scope_unclear=score_result.get("scope_unclear", False),
            critical_unknowns=score_result.get("critical_unknowns") or [],
            is_quick_cash=is_quick_cash,
            freshness_emoji=freshness_emoji,
            freshness_label=freshness_label,
            order_type=score_result.get("order_type"),
            category=score_result.get("category"),
            score_big=score_result.get("score_big"),
            score_fast=score_result.get("score_fast"),
            offers_count=offers_count,
            buyer_achievement_names=buyer_achievements_names,
            farm_mode_active=farm_active,
            competition_tier=score_result.get("competition_tier"),
        )

        # Волна 5.1 (баг-фикс): кнопки действий (перепроверить/сгенерировать/
        # отправил) на ВСЕХ показанных карточках — и GO, и borderline. Раньше
        # borderline получали бесполезную "Открыть на Kwork" без recheck/genoffer.
        keyboard = _kwork_action_keyboard(kw_project.id, kw_project_url)
        if respond:
            stats["recommended"] += 1
        else:
            stats["borderline_sent"] += 1
            # v4 волна 1.5 рег.3: инкремент дневного счётчика borderline.
            increment_borderline()

        sent = await bot.send_message(
            chat_id=config.tg_group,
            text=text,
            message_thread_id=config.tg_topic_id,
            reply_markup=keyboard,
        )

        # Волна 5c: заказ показан → ставим на авто-замер динамики откликов.
        # Первый замер через RECHECK_SCHEDULE_MIN[0] минут от находки.
        # message_id + текст карточки — чтобы дописывать динамику через edit (1.6).
        kw_project.tg_message_id = sent.message_id
        kw_project.card_text = text
        kw_project.recheck_done = 0
        kw_project.next_recheck_at = int(
            kw_project.first_seen_at.timestamp()
        ) + RECHECK_SCHEDULE_MIN[0] * 60
        await kw_project.save()

        # Генерация черновика отклика временно отключена — user разбирает
        # вручную в Claude-чате. Раскомментировать когда промпт доведём.
        # if respond and config.anthropic_api_key:
        #     offer = await generate_offer_claude(
        #         title=title,
        #         description=desc,
        #         budget=budget_str,
        #         anthropic_api_key=config.anthropic_api_key,
        #         is_ai=score_result["is_ai"],
        #         scope_unclear=score_result.get("scope_unclear", False),
        #         site_category=score_result.get("site_category", "not_site"),
        #     )
        #     if offer:
        #         await bot.send_message(
        #             chat_id=config.tg_group,
        #             text=(
        #                 f"📝 <b>Черновик отклика</b> (проверь и поправь перед "
        #                 f"отправкой):\n\n{html.quote(offer)}"
        #             ),
        #             message_thread_id=config.tg_topic_id,
        #         )

        await asyncio.sleep(random.choice([1, 2, 3]))

    logger.info(
        "Kwork cycle: total=%d seen=%d new=%d hard_reject=%d low_score=%d borderline=%d recommended=%d",
        stats["total"],
        stats["seen"],
        stats["total"] - stats["seen"],
        stats["hard_reject"],
        stats["low_score_silenced"],
        stats["borderline_sent"],
        stats["recommended"],
    )

    await kwork.close()
