"""Точка входа: python -m app.main"""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from .access import UserMiddleware
from .config import Config, load_config
from .db import DB
from .handlers import admin, common, events, export, groups, persons
from .llm import Generator
from .notifier import Notifier, Scheduler


def build(cfg: Config, bot: Bot, db: DB) -> tuple[Dispatcher, Notifier]:
    gen = Generator(cfg.anthropic_key, cfg.anthropic_model, cfg.anthropic_proxy)
    notifier = Notifier(bot, db, cfg, gen)
    dp = Dispatcher(storage=MemoryStorage())
    dp["db"], dp["cfg"], dp["notifier"] = db, cfg, notifier
    mw = UserMiddleware()
    dp.message.outer_middleware(mw)
    dp.callback_query.outer_middleware(mw)
    dp.include_routers(
        common.router, admin.router, groups.router, groups.staff, events.router,
        export.router, persons.router, common.fallback,
    )
    return dp, notifier


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    if not cfg.bot_token:
        raise SystemExit("BOT_TOKEN не задан")
    if not cfg.admin_ids:
        logging.warning("ADMIN_IDS пуст — никто не сможет зайти в бота")
    os.makedirs(os.path.dirname(os.path.abspath(cfg.db_path)), exist_ok=True)
    db = DB(cfg.db_path)
    await db.connect()
    await db.ensure_admins(cfg.admin_ids)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp, notifier = build(cfg, bot, db)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="cancel", description="Отменить действие"),
        BotCommand(command="id", description="Мой Telegram ID"),
    ])
    Scheduler(notifier).start()
    logging.info("Бот запущен, генерация: %s", "Claude" if notifier.gen.enabled else "шаблоны")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
