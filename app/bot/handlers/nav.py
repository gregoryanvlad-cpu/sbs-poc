from __future__ import annotations

import asyncio
import io
import json
from datetime import datetime, timezone

import qrcode
from aiogram import Router
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from dateutil.relativedelta import relativedelta
from sqlalchemy import select

from app.bot.auth import is_owner
from app.bot.keyboards import (
    kb_back_home,
    kb_cabinet,
    kb_confirm_reset,
    kb_main,
    kb_pay,
    kb_vpn,
)
from app.bot.ui import days_left, fmt_dt, utcnow
from app.core.config import settings
from app.db.models import Payment, User
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.repo import extend_subscription, get_subscription
from app.services.vpn.service import vpn_service

router = Router()


# ======================
# helpers
# ======================

def _is_sub_active(sub_end_at: datetime | None) -> bool:
    if not sub_end_at:
        return False
    if sub_end_at.tzinfo is None:
        sub_end_at = sub_end_at.replace(tzinfo=timezone.utc)
    return sub_end_at > utcnow()


async def _get_yandex_membership(session, tg_id: int) -> YandexMembership | None:
    q = (
        select(YandexMembership)
        .where(YandexMembership.tg_id == tg_id)
        .order_by(YandexMembership.id.desc())
        .limit(1)
    )
    res = await session.execute(q)
    return res.scalar_one_or_none()


async def _safe_edit(cb: CallbackQuery, text: str, reply_markup=None, **kwargs):
    """
    Безопасный edit_text — не падает на message is not modified
    """
    try:
        await cb.message.edit_text(text, reply_markup=reply_markup, **kwargs)
    except Exception:
        pass


# ======================
# NAVIGATION
# ======================

@router.callback_query(lambda c: c.data and c.data.startswith("nav:"))
async def on_nav(cb: CallbackQuery) -> None:
    where = cb.data.split(":", 1)[1]

    # ---- HOME ----
    if where == "home":
        await _safe_edit(cb, "Главное меню:", reply_markup=kb_main())
        await cb.answer()
        return

    # ---- CABINET ----
    if where == "cabinet":
        async with session_scope() as session:
            sub = await get_subscription(session, cb.from_user.id)
            ym = await _get_yandex_membership(session, cb.from_user.id)

            q = (
                select(Payment)
                .where(Payment.tg_id == cb.from_user.id)
                .order_by(Payment.id.desc())
                .limit(5)
            )
            res = await session.execute(q)
            payments = list(res.scalars().all())

        y_status = ym.status if ym else "не подключено"
        y_login = ym.yandex_login if (ym and ym.yandex_login) else "—"

        pay_lines = [f"• {p.amount} {p.currency} / {p.provider} / {p.status}" for p in payments]
        pay_text = "\n".join(pay_lines) if pay_lines else "• оплат пока нет"

        text = (
            "👤 <b>Личный кабинет</b>\n\n"
            f"🆔 ID: <code>{cb.from_user.id}</code>\n\n"
            f"💳 Подписка: {'активна ✅' if _is_sub_active(sub.end_at) else 'не активна ❌'}\n"
            f"📅 До: {fmt_dt(sub.end_at)}\n"
            f"⏳ Осталось: {days_left(sub.end_at)} дн.\n\n"
            "🟡 <b>Yandex Plus</b>\n"
            f"— Статус: <b>{y_status}</b>\n"
            f"— Логин: <code>{y_login}</code>\n\n"
            "🧾 <b>Последние оплаты</b>\n"
            f"{pay_text}"
        )

        await _safe_edit(
            cb,
            text,
            reply_markup=kb_cabinet(is_owner=is_owner(cb.from_user.id)),
            parse_mode="HTML",
        )
        await cb.answer()
        return

    # ---- PAY ----
    if where == "pay":
        await _safe_edit(
            cb,
            f"💳 Оплата\n\nТариф: {settings.price_rub} ₽ / {settings.period_months} мес.",
            reply_markup=kb_pay(),
        )
        await cb.answer()
        return

    # ---- VPN ----
    if where == "vpn":
        await _safe_edit(cb, "🌍 VPN", reply_markup=kb_vpn())
        await cb.answer()
        return

    # ---- YANDEX ----
    if where == "yandex":
        async with session_scope() as session:
            sub = await get_subscription(session, cb.from_user.id)
            ym = await _get_yandex_membership(session, cb.from_user.id)

        if not _is_sub_active(sub.end_at):
            await cb.answer("Подписка не активна. Оплатите доступ.", show_alert=True)
            return

        if ym and ym.yandex_login:
            buttons = []
            if ym.status in ("awaiting_join", "pending") and ym.invite_link:
                buttons.append([InlineKeyboardButton(text="🔗 Открыть приглашение", url=ym.invite_link)])
            buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])

            await _safe_edit(
                cb,
                "🟡 <b>Yandex Plus</b>\n\n"
                f"Ваш логин: <code>{ym.yandex_login}</code>\n"
                f"Статус: <b>{ym.status}</b>\n\n"
                "Логин подтверждён и не может быть изменён.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="HTML",
            )
            await cb.answer()
            return

        # ждём логин
        async with session_scope() as session:
            user = await session.get(User, cb.from_user.id)
            if user:
                user.flow_state = "await_yandex_login"
                user.flow_data = None
                await session.commit()

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔎 Посмотреть свой логин", url="https://id.yandex.ru")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
            ]
        )

        await _safe_edit(
            cb,
            "🟡 <b>Yandex Plus</b>\n\n"
            "Введите ваш логин Yandex ID.\n"
            "⚠️ После подтверждения изменить логин нельзя.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await cb.answer()
        return

    # ---- FAQ ----
    if where == "faq":
        await _safe_edit(
            cb,
            "❓ FAQ\n\n— Как оплатить? В разделе «Оплата»\n— Как получить VPN? В разделе «VPN»",
            reply_markup=kb_back_home(),
        )
        await cb.answer()
        return

    # ---- SUPPORT ----
    if where == "support":
        await _safe_edit(
            cb,
            "🛠 Поддержка\n\nНапиши сюда: @support (заглушка)",
            reply_markup=kb_back_home(),
        )
        await cb.answer()
        return

    await cb.answer("Неизвестный раздел", show_alert=True)
