from __future__ import annotations

import re
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
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
    parse_mode: str | None = None,
) -> bool:
    """Send a message and store it to message_audit (best-effort).

    Notes:
    - We log BOTH successful sends and failed attempts.
    - Telegram bots can't reliably know if a user *read* a message.
      We approximate "read" as: the user interacted with the bot after sent_at.

    Returns:
        True if message was sent, False otherwise.
    """

    ok = False
    msg = None
    err_tag = ""
    err_text = ""

    try:
        msg = await bot.send_message(int(tg_id), text, reply_markup=reply_markup, parse_mode=parse_mode)
        ok = True
    except TelegramForbiddenError as e:
        ok = False
        err_tag = "FORBIDDEN"
        err_text = str(e)
    except TelegramBadRequest as e:
        ok = False
        err_tag = "BAD_REQUEST"
        err_text = str(e)
    except Exception as e:
        ok = False
        err_tag = "ERROR"
        err_text = str(e)

    if err_text:
        err_text = re.sub(r"\s+", " ", err_text).strip()
        if len(err_text) > 240:
            err_text = err_text[:239] + "…"

    preview = _preview(text)
    if not ok:
        preview = f"[SEND_FAILED:{err_tag}] {err_text}\n{preview}".strip()

    try:
        async with session_scope() as session:
            session.add(
                MessageAudit(
                    tg_id=int(tg_id),
                    kind=str(kind)[:64],
                    chat_id=int(msg.chat.id) if msg and msg.chat else None,
                    message_id=int(msg.message_id) if msg else None,
                    text_preview=preview,
                    sent_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()
    except Exception:
        # Never fail bot flow due to audit logging.
        pass

    return ok
