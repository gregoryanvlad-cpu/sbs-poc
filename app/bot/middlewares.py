from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

log = logging.getLogger(__name__)


class CorrelationIdMiddleware(BaseMiddleware):
    """Adds corr_id to logger records via extra in handler calls."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        update: Update | None = data.get("event_update")
        if update:
            data["corr_id"] = f"u{update.update_id}"
            data["update_id"] = update.update_id
        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, min_interval_sec: float = 0.4):
        self.min_interval_sec = min_interval_sec
        self._last: dict[tuple[int, str], float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # only for callback queries
        cb = getattr(event, "data", None)
        from_user = getattr(event, "from_user", None)
        if cb and from_user:
            key = (from_user.id, cb)
            now = time.monotonic()
            last = self._last.get(key)
            if last and (now - last) < self.min_interval_sec:
                return None
            self._last[key] = now
        return await handler(event, data)
