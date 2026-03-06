from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from app.db.models.message_audit import MessageAudit
from app.db.session import session_scope


def _preview(text: str, limit: int = 700) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[: limit - 1] + "…"


async def audit_send_message(
    bot: Bot,
    tg_id: int,
    text: str,
    *,
    kind: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Send a message and store it to message_audit (best-effort)."""

    msg = await bot.send_message(int(tg_id), text, reply_markup=reply_markup)

    try:
        async with session_scope() as session:
            session.add(
                MessageAudit(
                    tg_id=int(tg_id),
                    kind=str(kind)[:64],
                    chat_id=int(msg.chat.id) if msg and msg.chat else None,
                    message_id=int(msg.message_id) if msg else None,
                    text_preview=_preview(text),
                    sent_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()
    except Exception:
        # Never fail bot flow due to audit logging.
        pass
