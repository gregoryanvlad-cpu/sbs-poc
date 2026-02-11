from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.middlewares import CorrelationIdMiddleware
from app.core.config import settings
from app.player_bot.middlewares import SlidingWindowRateLimitMiddleware
from app.player_bot.handlers import start


def run_player_bot():
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()

    # logging correlation like in Bot1
    dp.message.middleware(CorrelationIdMiddleware())
    dp.callback_query.middleware(CorrelationIdMiddleware())

    # per-user rate limit (best-effort)
    rl = SlidingWindowRateLimitMiddleware(max_per_minute=settings.player_rate_limit_per_minute)
    dp.message.middleware(rl)
    dp.callback_query.middleware(rl)

    dp.include_router(start.router)
    return bot, dp
