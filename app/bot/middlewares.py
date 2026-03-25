from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, Message, CallbackQuery

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import session_scope
from app.db.models.app_setting import AppSetting
from app.db.models.message_audit import MessageAudit

log = logging.getLogger(__name__)


async def get_app_setting_int(session: AsyncSession, key: str, default: int | None = None) -> int | None:
    row = await session.get(AppSetting, key)
    if row is None or row.int_value is None:
        return default
    return int(row.int_value)


async def set_app_setting_int(session: AsyncSession, key: str, value: int | None) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, int_value=value)
        row.touch()
        session.add(row)
    else:
        row.int_value = value
        row.touch()


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


class ActivitySeenMiddleware(BaseMiddleware):
    """Marks outgoing notifications as seen when user interacts."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_id: int | None = None
        if isinstance(event, Message) and event.from_user:
            tg_id = int(event.from_user.id)
        elif isinstance(event, CallbackQuery) and event.from_user:
            tg_id = int(event.from_user.id)

        if tg_id:
            now = datetime.now(timezone.utc)
            try:
                async with session_scope() as session:
                    try:
                        ts_key = f"ua:last_interaction_ts:{tg_id}"
                        await set_app_setting_int(session, ts_key, int(now.timestamp()))

                        total_key = f"ua:actions_total:{tg_id}"
                        total = int(await get_app_setting_int(session, total_key, default=0) or 0)
                        await set_app_setting_int(session, total_key, total + 1)

                        if isinstance(event, Message):
                            k = f"ua:messages_in:{tg_id}"
                        else:
                            k = f"ua:clicks_in:{tg_id}"
                        v = int(await get_app_setting_int(session, k, default=0) or 0)
                        await set_app_setting_int(session, k, v + 1)
                    except Exception:
                        pass

                    await session.execute(
                        update(MessageAudit)
                        .where(MessageAudit.tg_id == tg_id)
                        .where(MessageAudit.seen_at.is_(None))
                        .where(MessageAudit.sent_at <= now)
                        .values(seen_at=now)
                    )
                    await session.commit()
            except Exception:
                pass

        return await handler(event, data)
