import asyncio
import logging
import subprocess
import sys
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.bot import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp.web import run_app
from aiohttp.web_app import Application
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from piccolo.engine import engine_finder

from app.bot.handlers import router
from app.bot.middlewares import RetryRequestMiddleware
from app.config_reader import Settings
from app.db.tables import PROJECT_TABLES
from app.parser import get_kwork_projects, get_upwork_projects
from app.web.routes import create_offer, get_offers, home
from app.bot.quota_handlers import quota_router

def _migrate_sqlite_columns() -> None:
    """
    Idempotent миграция SQLite: добавляет колонки введённые после первичной схемы.
    Запускается на каждом старте; для уже существующих колонок — no-op.
    """
    import os
    import sqlite3

    db_file = os.path.join("app", "db", "database", "projects.db")
    if not os.path.exists(db_file):
        return

    conn = sqlite3.connect(db_file)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(project)")
        cols = {row[1] for row in cur.fetchall()}
        if "first_seen_at" not in cols:
            cur.execute("ALTER TABLE project ADD COLUMN first_seen_at TIMESTAMP")
            conn.commit()
            logging.getLogger(__name__).info(
                "DB migration: added project.first_seen_at"
            )
    finally:
        conn.close()


async def database_connection(config: Settings, close=False, persist=True) -> None:
    db = engine_finder(config.piccolo_conf)

    if db.engine_type == "sqlite":
        if close:
            return

        if not persist:
            for table in reversed(PROJECT_TABLES):
                await table.alter().drop_table(if_exists=True)

        for table in PROJECT_TABLES:
            await table.create_table(if_not_exists=True)

        _migrate_sqlite_columns()
    else:
        if close:
            return await db.close_connection_pool()

        await db.start_connection_pool()


async def on_startup(
    bot: Bot, base_url: str, scheduler: AsyncIOScheduler, config: Settings
):
    await bot.me()

    use_polling = not bool(base_url)
    webhook_url = "" if use_polling else "{url}/webhook".format(url=base_url)

    if use_polling:
        # Polling: убираем webhook чтобы TG не пытался слать апдейты на старый URL.
        await bot.delete_webhook(drop_pending_updates=True)
    else:
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url != webhook_url:
            await bot.delete_webhook(drop_pending_updates=True)

    config.bot_username = bot._me.username

    await database_connection(config, persist=True)

    if not use_polling:
        await bot.set_webhook(webhook_url)

    jobs = [
        {"func": get_upwork_projects, "minutes": 15},
        {"func": get_kwork_projects, "minutes": 10},
    ]

    for job in jobs:
        scheduler.add_job(
            func=job.get("func"),
            trigger="interval",
            minutes=job.get("minutes"),
            kwargs={"bot": bot, "config": config},
        )

    scheduler.start()

    # Notify admin that the bot is up and which code version is loaded
    try:
        git_sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        git_sha = "unknown"
    startup_msg = (
        "✅ <b>Бот запущен</b>\n"
        f"Время: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
        f"Версия: <code>{git_sha}</code>\n"
        f"Режим: <code>{'long polling' if use_polling else 'webhook ' + webhook_url}</code>\n"
        f"Фильтр: {'Claude haiku' if config.anthropic_api_key else 'выключен'}"
    )
    try:
        await bot.send_message(chat_id=config.tg_admin, text=startup_msg)
    except Exception as exc:
        logging.warning("Failed to send startup notification: %s", exc)


async def on_shutdown(config: Settings, scheduler: AsyncIOScheduler):
    await database_connection(config, close=True)

    scheduler.shutdown(wait=False)


def create_bot(config: Settings) -> Bot:
    if config.tg_api_url:
        api_server = TelegramAPIServer.from_base(config.tg_api_url.rstrip("/"))
        session = AiohttpSession(api=api_server)
        logging.getLogger(__name__).info(
            "Bot API через прокси: %s", config.tg_api_url,
        )
    else:
        session = AiohttpSession()
    session.middleware(RetryRequestMiddleware())

    return Bot(
        token=config.tg_token,
        session=session,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )


def _build_dispatcher(config: Settings, bot: Bot) -> tuple[Dispatcher, AsyncIOScheduler]:
    dp = Dispatcher()
    dp["base_url"] = config.app_base_url
    dp["config"] = config

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    dp.include_router(router)
    dp.include_router(quota_router)

    scheduler = AsyncIOScheduler()
    dp["scheduler"] = scheduler
    return dp, scheduler


async def _run_polling(config: Settings) -> None:
    bot = create_bot(config)
    dp, _ = _build_dispatcher(config, bot)
    logging.getLogger(__name__).info("Стартую в режиме long polling")
    await dp.start_polling(bot)


def _run_webhook(config: Settings) -> None:
    bot = create_bot(config)
    dp, _ = _build_dispatcher(config, bot)

    app = Application()
    app["bot"] = bot

    app.router.add_get("/", home)
    app.router.add_post("/generate", create_offer)
    app.router.add_get("/offers", get_offers)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    run_app(app, host=config.app_host, port=config.app_port)


def main():
    config = Settings()
    if config.app_base_url:
        _run_webhook(config)
    else:
        asyncio.run(_run_polling(config))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    main()
