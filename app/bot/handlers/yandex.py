from __future__ import annotations

import asyncio
from html import escape as html_escape

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.bot.keyboards import kb_main
from app.db.models import User
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.repo import get_subscription
from app.bot.ui import utcnow
from app.core.config import settings
from app.services.yandex.service import yandex_service

router = Router()


async def _home_text_with_vpn() -> str:
    return (
        "🏠 <b>Главное меню</b>\n\n"
        '🇳🇱 Сервер "Нидерланды": <b>Работает ✅</b>\n'
        f'📶 "LTE-Обход": <b>{"Работает ✅" if settings.lte_enabled else "Отключён ⛔️"}</b>\n\n'
        "🔐 Форма шифрования: <b>ChaCha20-Poly1305</b>"
    )


def _kb_open_invite(invite_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть приглашение", url=invite_link)],
            [InlineKeyboardButton(text="📋 Скопировать приглашение", callback_data="yandex:copy")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


async def _notify_admins_yandex_issue(bot, *, tg_id: int, reason: str, sub_end_at) -> None:
    admin_ids: set[int] = set()
    try:
        admin_ids.add(int(settings.owner_tg_id))
    except Exception:
        pass
    try:
        admin_ids.update({int(x) for x in (settings.admin_tg_ids or [])})
    except Exception:
        pass
    admin_ids.discard(int(tg_id))
    if not admin_ids:
        return

    username = "—"
    full_name = "—"
    try:
        async with session_scope() as session:
            u = await session.scalar(select(User).where(User.tg_id == tg_id).limit(1))
            if u:
                username = u.username or "—"
                full_name = ((u.first_name or "") + (" " + u.last_name if u.last_name else "")).strip() or "—"
    except Exception:
        pass

    text = (
        "⚠️ <b>Ошибка выдачи приглашения Yandex Plus</b>\n\n"
        f"ID: <code>{tg_id}</code>\n"
        f"Профиль: @{html_escape(username)} | {html_escape(full_name)}\n"
        f"Подписка активна до: <b>{html_escape(str(sub_end_at) if sub_end_at else '—')}</b>\n"
        f"Причина: <code>{html_escape(reason)}</code>"
    )
    for aid in admin_ids:
        try:
            await bot.send_message(int(aid), text, parse_mode="HTML")
        except Exception:
            pass


@router.callback_query(lambda c: c.data == "yandex:copy")
async def yandex_copy_invite(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id

    async with session_scope() as session:
        ym = await session.scalar(
            select(YandexMembership)
            .where(YandexMembership.tg_id == tg_id)
            .order_by(YandexMembership.id.desc())
            .limit(1)
        )

    link = getattr(ym, "invite_link", None) if ym else None
    if not link:
        await cb.answer("Ссылка ещё не выдана", show_alert=True)
        return

    try:
        await cb.message.answer(
            "📋 Скопируй ссылку приглашения:\n\n" f"<code>{link}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await cb.answer("Ссылка отправлена")


@router.callback_query(F.data == "yandex:issue")
async def on_yandex_issue(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id

    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        now = utcnow()
        if not sub or not sub.end_at or sub.end_at <= now:
            await cb.answer("Подписка не активна. Оплатите доступ.", show_alert=True)
            return

        ym = await session.scalar(
            select(YandexMembership)
            .where(YandexMembership.tg_id == tg_id)
            .order_by(YandexMembership.id.desc())
            .limit(1)
        )
        if ym and ym.invite_link:
            invite_link = ym.invite_link
        else:
            try:
                ym = await yandex_service.ensure_membership_for_user(
                    session=session,
                    tg_id=tg_id,
                    yandex_login="unknown",
                )
                await session.commit()
                invite_link = ym.invite_link
            except Exception as e:
                await _notify_admins_yandex_issue(cb.bot, tg_id=tg_id, reason=f"{type(e).__name__}: {e}", sub_end_at=sub.end_at)
                await cb.message.answer(
                    "⚠️ Сейчас свободные места или приглашения временно закончились.\n\n"
                    "Переживать не нужно — в ближайшее время вам придёт приглашение. Мы уже получили уведомление и проверим это вручную.",
                    parse_mode="HTML",
                )
                return

    if not invite_link:
        await _notify_admins_yandex_issue(cb.bot, tg_id=tg_id, reason="invite_link is empty", sub_end_at=sub.end_at)
        await cb.message.answer(
            "⚠️ Сейчас свободные места или приглашения временно закончились.\n\n"
            "Переживать не нужно — в ближайшее время вам придёт приглашение. Мы уже получили уведомление и проверим это вручную.",
            parse_mode="HTML",
        )
        return

    try:
        await cb.answer()
    except Exception:
        pass

    sent = await cb.message.answer(
        "✅ Приглашение готово.\n\n"
        "Нажми кнопку ниже и прими приглашение.\n"
        "Если не успел — ссылка всегда доступна в 🟡 Yandex Plus.",
        reply_markup=_kb_open_invite(invite_link),
    )

    async def _auto_back() -> None:
        try:
            await asyncio.sleep(60)
            await cb.bot.edit_message_text(
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                text=await _home_text_with_vpn(),
                reply_markup=kb_main(),
                parse_mode="HTML",
            )
        except Exception:
            pass

    asyncio.create_task(_auto_back())
