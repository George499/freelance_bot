import asyncio
import logging
import random
import re

import aiohttp
import feedparser
from aiogram import Bot, html
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from selectolax.lexbor import LexborHTMLParser as htmlp

from app.bot.keyboards import apply_button
from app.config_reader import Settings
from app.db.tables import FreelancePlatform, Project
from app.kwork_filter import (
    MONTHLY_QUOTA,
    generate_offer_claude,
    quota_status,
    score_project,
    should_respond,
)
from app.quota import get_days_until_refill, get_quota
from kwork import Kwork

logger = logging.getLogger(__name__)

# Минимальный скор чтобы заказ попал в Telegram. Ниже — только в логи.
# 5-6 — пограничные (можно глянуть, но не рекомендую).
# 7+ — рекомендую отклик.
# ≤4 — полное игнорирование, молчим.
TELEGRAM_SCORE_THRESHOLD = 5

UPWORK_TEMPLATE = (
    "☘️ <b>{title}</b>\n\n"
    "<i>{description}</i>\n\n"
    "Budget: <code>{budget}</code>\n"
    "Hourly Range: {hourly_range}\n"
    "Category: {category}\n"
    "Country: {country}"
)


def _kwork_action_keyboard(project_id: int, project_url: str) -> InlineKeyboardMarkup:
    """Клавиатура под рекомендованными заказами — с кнопками учёта квоты."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть на Kwork", url=project_url)],
            [
                InlineKeyboardButton(
                    text="✅ Отправил отклик",
                    callback_data=f"kw_sent:{project_id}",
                ),
                InlineKeyboardButton(
                    text="🚫 Пропустить",
                    callback_data=f"kw_skip:{project_id}",
                ),
            ],
        ]
    )


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
) -> str:
    if respond:
        header = f"{'🔥' if score >= 9 else '✅'} РЕКОМЕНДУЮ ОТКЛИК — скор {score}/10"
    elif score >= TELEGRAM_SCORE_THRESHOLD:
        header = f"👀 Пограничный — скор {score}/10 (ждём лучшего)"
    else:
        header = f"Пропуск — скор {score}/10"

    badges = []
    if is_ai:
        badges.append("🤖 AI")
    if no_code_required:
        badges.append(f"⛔ {no_code_required}")
    if scope_unclear:
        badges.append("⚠️ scope unclear")

    badges_line = " ".join(badges)
    preview = html.quote(desc[:300]) + ("..." if len(desc) > 300 else "")

    return (
        f"{header}  {badges_line}\n\n"
        f"<b>{html.quote(title)}</b>\n\n"
        f"💰 {budget_str}\n"
        f"{preview}\n\n"
        f"Решение: {html.quote(decision_reason)}\n"
        f"Причина скора: {html.quote(score_reason)}\n\n"
        f"📊 Квота: {quota['remaining']}/{MONTHLY_QUOTA} осталось, "
        f"{quota['days_left']} дн. до пополнения, режим: {quota['pace']}\n\n"
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


async def get_kwork_projects(bot: Bot, config: Settings):
    kwork = Kwork(
        login=config.kw_login,
        password=config.kw_password,
        phone_last=config.kw_phone_last,
    )
    token = await kwork.token
    categories_ids = config.kw_categories

    raw_projects = await kwork.api_request(
        method="post",
        api_method="projects",
        categories=categories_ids,
        page=1,
        token=token,
    )

    if not raw_projects["success"]:
        return await kwork.close()

    def get_project_data(response: list) -> list:
        result = []
        for item in response:
            result.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "description": item.get("description"),
                    "price": item.get("price"),
                    "possible_price_limit": item.get("possible_price_limit"),
                    "offers": item.get("offers", 0),
                    "time_left": item.get("time_left", 0),
                }
            )
        return result

    projects = get_project_data(raw_projects["response"])
    paging = raw_projects["paging"]
    pages = int((paging["pages"] + 1) / 2)

    for page in range(2, pages):
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
        await kw_project.save()

        title = project.get("title", "")
        price = project.get("price") or 0
        price_limit = project.get("possible_price_limit") or price
        budget_str = f"{price:,} – {price_limit:,} ₽"

        time_left = project.get("time_left") or 0
        deadline_str = f"{time_left // 86400} дней" if time_left else "не указан"

        score_result = await score_project(
            title=title,
            description=desc,
            budget=budget_str,
            deadline=deadline_str,
            responses_count=project.get("offers", 0),
            anthropic_api_key=config.anthropic_api_key,
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

        text = _format_kwork_message(
            title=title,
            desc=desc,
            budget_str=budget_str,
            score=score_result["score"],
            is_ai=score_result["is_ai"],
            respond=respond,
            decision_reason=decision_reason,
            score_reason=score_result.get("reason", ""),
            quota=quota,
            url=kw_project_url,
            no_code_required=score_result.get("no_code_required"),
            scope_unclear=score_result.get("scope_unclear", False),
        )

        if respond:
            keyboard = _kwork_action_keyboard(kw_project.id, kw_project_url)
            stats["recommended"] += 1
        else:
            btn_data = {"id": kw_project.id, "lang": "ru", "username": config.bot_username}
            keyboard = apply_button(
                text="Открыть на Kwork", url=kw_project_url, data=btn_data
            )
            stats["borderline_sent"] += 1

        await bot.send_message(
            chat_id=config.tg_group,
            text=text,
            message_thread_id=config.tg_topic_id,
            reply_markup=keyboard,
        )

        if respond and config.anthropic_api_key:
            offer = await generate_offer_claude(
                title=title,
                description=desc,
                budget=budget_str,
                anthropic_api_key=config.anthropic_api_key,
                is_ai=score_result["is_ai"],
                scope_unclear=score_result.get("scope_unclear", False),
                site_category=score_result.get("site_category", "not_site"),
            )
            if offer:
                await bot.send_message(
                    chat_id=config.tg_group,
                    text=f"📝 <b>Готовый отклик:</b>\n\n{html.quote(offer)}",
                    message_thread_id=config.tg_topic_id,
                )

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
