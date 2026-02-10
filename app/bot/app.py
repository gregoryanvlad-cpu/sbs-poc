from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from app.core.config import settings
from app.bot.middlewares import CorrelationIdMiddleware

from app.bot.handlers import start, nav, referrals, yandex, kinoteka
from app.bot.admin import router as admin_router
from app.bot.admin_kick import router as admin_kick_router


def run_bot():
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()

    dp.message.middleware(CorrelationIdMiddleware())
    dp.callback_query.middleware(CorrelationIdMiddleware())

    dp.include_router(start.router)
    dp.include_router(nav.router)
    dp.include_router(referrals.router)
    dp.include_router(yandex.router)
    dp.include_router(kinoteka.router)
    dp.include_router(admin_router)
    dp.include_router(admin_kick_router)

    return bot, dp
