import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import config
import ctx
import handlers_admin
import handlers_events
import handlers_user
from db import DB
from middleware import GateMiddleware
from services import scheduler


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN غير موجود. ضعه في متغيرات البيئة.")
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    ctx.db = DB(config.DB_PATH)
    ctx.mods = {r["user_id"] for r in await ctx.db.all("SELECT user_id FROM admins")}
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    ctx.bot_username = (await bot.get_me()).username
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(GateMiddleware())
    dp.callback_query.outer_middleware(GateMiddleware())
    # الترتيب مهم: الأوامر (المشرف ثم العامة) قبل خطوات المعالج
    dp.include_router(handlers_admin.router)
    dp.include_router(handlers_user.router)
    dp.include_router(handlers_events.router)
    task = asyncio.create_task(scheduler(bot, ctx.db))  # noqa: F841
    await bot.delete_webhook(drop_pending_updates=False)
    logging.info("Bot @%s started, DB=%s", ctx.bot_username, config.DB_PATH)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
