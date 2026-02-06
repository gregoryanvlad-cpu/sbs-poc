from __future__ import annotations

import asyncio

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.bot.keyboards import kb_main
from app.services.vpn.service import vpn_service
from app.db.session import session_scope
from app.db.models.yandex_membership import YandexMembership
from app.repo import get_subscription
from app.bot.ui import utcnow
from app.services.yandex.service import yandex_service

router = Router()


async def _home_text_with_vpn() -> str:
    """Local helper to keep main menu consistent."""
    line = "🌍 VPN: статус недоступен"
    try:
        st = await asyncio.wait_for(vpn_service.get_server_status(), timeout=4)
        if st.get("ok"):
            cpu = st.get("cpu_load_percent")
            act = st.get("active_peers")
            tot = st.get("total_peers")
            if cpu is not None and act is not None and tot is not None:
                line = f"🌍 VPN: загрузка ~<b>{cpu:.0f}%</b> | активных пиров <b>{act}</b>/<b>{tot}</b>"
    except Exception:
        pass
    return "🏠 <b>Главное меню</b>\n" + line


def _kb_open_invite(invite_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть приглашение", url=invite_link)],
            [InlineKeyboardButton(text="📋 Скопировать приглашение", callback_data="yandex:copy")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


@router.callback_query(lambda c: c.data == "yandex:copy")
async def yandex_copy_invite(cb: CallbackQuery) -> None:
    """Send invite link as plain text so user can copy it."""
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
                text=await _home_text_with_vpn(),
                reply_markup=kb_main(),
                parse_mode="HTML",
            )
        except Exception:
            pass

    asyncio.create_task(_auto_back())


async def _home_text_with_vpn() -> str:
    line = "🌍 VPN: статус недоступен"
    try:
        st = await asyncio.wait_for(vpn_service.get_server_status(), timeout=4)
        if st.get("ok"):
            cpu = st.get("cpu_load_percent")
            act = st.get("active_peers")
            tot = st.get("total_peers")
            if cpu is not None and act is not None and tot is not None:
                line = f"🌍 VPN: загрузка ~<b>{cpu:.0f}%</b> | активных пиров <b>{act}</b>/<b>{tot}</b>"
    except Exception:
        pass
    return "🏠 <b>Главное меню</b>\n" + line
