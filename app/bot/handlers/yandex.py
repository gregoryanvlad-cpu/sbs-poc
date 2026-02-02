from __future__ import annotations

import asyncio

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.bot.keyboards import kb_main
from app.db.session import session_scope
from app.db.models.yandex_membership import YandexMembership
from app.repo import get_subscription
from app.bot.ui import utcnow
from app.services.yandex.service import yandex_service

router = Router()


def _kb_open_invite(invite_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть приглашение", url=invite_link)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


@router.callback_query(F.data == "yandex:issue")
async def on_yandex_issue(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    await cb.answer()

    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        now = utcnow()
        if not sub or not sub.end_at or sub.end_at <= now:
            await cb.answer("Подписка не активна. Оплатите доступ.", show_alert=True)
            return

        # если уже есть ссылка — просто покажем её
        ym = await session.scalar(
            select(YandexMembership)
            .where(YandexMembership.tg_id == tg_id)
            .order_by(YandexMembership.id.desc())
            .limit(1)
        )
        if ym and ym.invite_link:
            invite_link = ym.invite_link
        else:
            # Логин больше не нужен. Пишем placeholder, чтобы не ломать nullable=False.
            try:
                ym = await yandex_service.ensure_membership_for_user(
                    session=session,
                    tg_id=tg_id,
                    yandex_login="unknown",
                )
                await session.commit()
                invite_link = ym.invite_link
            except Exception as e:
                await cb.message.answer(
                    "❌ Не получилось выдать приглашение.\n\n"
                    f"<code>{type(e).__name__}: {e}</code>\n\n"
                    "Попробуй ещё раз через минуту.",
                    parse_mode="HTML",
                )
                return

    if not invite_link:
        await cb.message.answer(
            "⚠️ Сейчас нет доступных приглашений.\n"
            "Напиши в поддержку или попробуй позже."
        )
        return

    sent = await cb.message.answer(
        "✅ Приглашение готово.\n\n"
        "Нажми кнопку ниже и прими приглашение.\n"
        "Если не успел — ссылка всегда доступна в 🟡 Yandex Plus.",
        reply_markup=_kb_open_invite(invite_link),
    )

    # Через минуту превращаем сообщение обратно в главное меню,
    # но ссылка останется в разделе Yandex Plus.
    async def _auto_back() -> None:
        try:
            await asyncio.sleep(60)
            await cb.bot.edit_message_text(
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                text="Главное меню:",
                reply_markup=kb_main(),
            )
        except Exception:
            pass

    asyncio.create_task(_auto_back())
