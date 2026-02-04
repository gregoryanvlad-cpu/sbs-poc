import logging

from aiogram import Bot, Dispatcher

from app.core.config import settings
from app.bot.handlers.start import router as start_router
from app.bot.handlers.nav import router as nav_router
from app.bot.handlers.yandex import router as yandex_router
from app.bot.handlers.referrals import router as referrals_router
from app.bot.admin import router as admin_router
from app.bot.middlewares import CorrelationIdMiddleware, RateLimitMiddleware

log = logging.getLogger(__name__)


async def run_bot() -> None:
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.message.middleware(CorrelationIdMiddleware())
    dp.callback_query.middleware(CorrelationIdMiddleware())
    dp.callback_query.middleware(RateLimitMiddleware(min_interval_sec=0.4))

    dp.include_router(start_router)
    dp.include_router(nav_router)
    dp.include_router(yandex_router)
    dp.include_router(referrals_router)
    dp.include_router(admin_router)

    log.info("bot_start")
    await dp.start_polling(bot)
