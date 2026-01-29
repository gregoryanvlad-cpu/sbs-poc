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
from app.db.models import User
from app.db.session import session_scope
from app.repo import extend_subscription, get_subscription
from app.services.vpn.service import vpn_service

router = Router()


def _is_sub_active(sub_end_at: datetime | None) -> bool:
    if not sub_end_at:
        return False
    if sub_end_at.tzinfo is None:
        sub_end_at = sub_end_at.replace(tzinfo=timezone.utc)
    return sub_end_at > utcnow()


@router.callback_query(lambda c: c.data and c.data.startswith("nav:"))
async def on_nav(cb: CallbackQuery) -> None:
    where = cb.data.split(":", 1)[1]

    if where == "home":
        await cb.message.edit_text("Главное меню:", reply_markup=kb_main())
        await cb.answer()
        return

    if where == "cabinet":
        async with session_scope() as session:
            sub = await get_subscription(session, cb.from_user.id)
            user = await session.get(User, cb.from_user.id)

        text = (
            "👤 *Личный кабинет*\n\n"
            f"🆔 ID: `{cb.from_user.id}`\n\n"
            f"💳 Подписка: {'активна ✅' if _is_sub_active(sub.end_at) else 'не активна ❌'}\n"
            f"📅 До: {fmt_dt(sub.end_at)}\n"
            f"⏳ Осталось: {days_left(sub.end_at)} дн.\n\n"
            "🟡 *Yandex Plus*\n"
            f"— Логин: `{user.yandex_login if user and user.yandex_login else 'не задан'}`"
        )

        await cb.message.edit_text(text, reply_markup=kb_cabinet(), parse_mode="Markdown")
        await cb.answer()
        return

    if where == "pay":
        await cb.message.edit_text(
            f"💳 Оплата\n\nТариф: {settings.price_rub} ₽ / {settings.period_months} мес.",
            reply_markup=kb_pay(),
        )
        await cb.answer()
        return

    if where == "vpn":
        await cb.message.edit_text("🌍 VPN", reply_markup=kb_vpn())
        await cb.answer()
        return

    # =========================
    # 🟡 YANDEX PLUS
    # =========================
    if where == "yandex":
        async with session_scope() as session:
            sub = await get_subscription(session, cb.from_user.id)
            user = await session.get(User, cb.from_user.id)

        if not _is_sub_active(sub.end_at):
            await cb.answer(
                "Подписка не активна. Оплатите доступ.",
                show_alert=True,
            )
            return

        # ❌ ЛОГИН УЖЕ ЗАДАН → ЗАПРЕТ ПОВТОРНОГО ВВОДА
        if user and user.yandex_login:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🛠 Поддержка", callback_data="nav:support")],
                    [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav:home")],
                ]
            )

            await cb.message.edit_text(
                "🟡 *Yandex Plus*\n\n"
                f"Ваш логин: `{user.yandex_login}`\n\n"
                "Логин уже подтверждён и не может быть изменён.\n"
                "Если вы допустили ошибку — обратитесь в поддержку.",
                reply_markup=kb,
                parse_mode="Markdown",
            )
            await cb.answer()
            return

        # ✅ ЛОГИНА НЕТ → ЗАПУСК ВВОДА
        async with session_scope() as session:
            user = await session.get(User, cb.from_user.id)
            user.flow_state = "await_yandex_login"
            user.flow_data = None
            await session.commit()

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔎 Посмотреть свой логин", url="https://id.yandex.ru")],
                [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav:home")],
            ]
        )

        await cb.message.edit_text(
            "🟡 *Yandex Plus*\n\n"
            "Введите ваш логин Yandex ID.\n"
            "⚠️ После подтверждения изменить логин нельзя.",
            reply_markup=kb,
            parse_mode="Markdown",
        )
        await cb.answer()

        photo = FSInputFile("app/bot/assets/yandex_login_hint.jpg")
        hint = await cb.message.answer_photo(photo=photo)

        async with session_scope() as session:
            user = await session.get(User, cb.from_user.id)
            user.flow_data = json.dumps({"hint_msg_id": hint.message_id})
            await session.commit()

        await cb.message.answer("👇 Введите логин сообщением ниже")
        return

    if where == "support":
        await cb.message.edit_text(
            "🛠 Поддержка\n\nНапишите: @support (заглушка)",
            reply_markup=kb_back_home(),
        )
        await cb.answer()
        return

    await cb.answer("Неизвестный раздел")
