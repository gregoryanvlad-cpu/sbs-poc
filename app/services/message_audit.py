from __future__ import annotations

import re
from html import unescape as html_unescape
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, MessageEntity

from app.db.models.message_audit import MessageAudit
from app.db.session import session_scope


def _strip_markup(text: str) -> str:
    t = text or ""
    t = re.sub(r"<[^>]+>", "", t)
    t = html_unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _preview(text: str, limit: int = 700, *, parse_mode: str | None = None) -> str:
    t = (text or "").strip()
    if str(parse_mode or "").upper() == "HTML":
        t = _strip_markup(t)
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
    photo: str | None = None,
    entities: list[MessageEntity] | None = None,
    caption_entities: list[MessageEntity] | None = None,
) -> bool:
    """Send a text message or photo+caption and store it to message_audit (best-effort)."""

    ok = False
    msg = None
    err_tag = ""
    err_text = ""

    try:
        if photo:
            msg = await bot.send_photo(
                int(tg_id),
                photo=photo,
                caption=text or None,
                reply_markup=reply_markup,
                parse_mode=parse_mode if not caption_entities else None,
                caption_entities=caption_entities,
            )
        else:
            msg = await bot.send_message(
                int(tg_id),
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode if not entities else None,
                entities=entities,
            )
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

    preview = _preview(("[PHOTO]\n" if photo else "") + (text or ""), parse_mode=parse_mode)
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
        pass

    return ok


async def audit_log_event(tg_id: int, *, kind: str, text_preview: str = "") -> None:
    """Persist an internal analytics event to message_audit without sending a message."""

    preview = (text_preview or kind or "event").strip()[:700]
    try:
        async with session_scope() as session:
            session.add(
                MessageAudit(
                    tg_id=int(tg_id),
                    kind=str(kind)[:64],
                    chat_id=None,
                    message_id=None,
                    text_preview=preview or str(kind)[:64],
                    sent_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()
    except Exception:
        pass
