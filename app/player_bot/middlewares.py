from __future__ import annotations

import time
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, Tuple

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


class SlidingWindowRateLimitMiddleware(BaseMiddleware):
    """Simple per-user sliding window rate limit.

    Default: allow N events per 60 seconds.
    """

    def __init__(self, max_per_minute: int = 15):
        self.max_per_minute = max(1, int(max_per_minute))
        self.window_sec = 60.0
        self._events: Dict[int, Deque[float]] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        if not from_user:
            return await handler(event, data)

        uid = from_user.id
        now = time.monotonic()
        q = self._events.get(uid)
        if q is None:
            q = deque()
            self._events[uid] = q

        # drop old
        while q and (now - q[0]) > self.window_sec:
            q.popleft()

        if len(q) >= self.max_per_minute:
            # silently drop to reduce spam; handlers can still reply if needed
            return None

        q.append(now)
        return await handler(event, data)
