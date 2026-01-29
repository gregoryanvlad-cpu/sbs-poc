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


async def _get_yandex_membership_safe(session, tg_id: int):
    for mod_path, cls_name in (
        ("app.db.models", "YandexMembership"),
        ("app.db.models.yandex_membership", "YandexMembership"),
        ("app.db.models.yandex", "YandexMembership"),
    ):
        try:
            module = __import__(mod_path, fromlist=[cls_name])
            YM = getattr(module, cls_name)
            col = getattr(YM, "user_id", None) or getattr(YM, "tg_id", None)
            if not col:
                continue
            q = YM.__table__.select().where(col == tg_id).order_by(YM.id.desc()).limit(1)
            res = await session.execute(q)
            row = res.first()
            return row[0] if row else None
        except Exception:
            continue
    return None


@router.callback_query(lambda c: c.data and c.data.startswith("nav:"))
async def on_nav(cb: CallbackQuery) -> None:
    where = cb.data.split(":", 1)[1]

    # =========================
    # 🧹 ГЛАВНОЕ МЕНЮ (очистка)
    # =========================
    if where == "home":
        async with session_scope() as session:
            user = await session.get(User, cb.from_user.id)
            if user and user.flow_data:
                try:
                    data = json.loads(user.flow_data)
                    for msg_id in data.get("hint_msg_ids", []):
                        try:
                            await cb.bot.delete_message(cb.message.chat.id, msg_id)
                        except Exception:
                            pass
                except Exception:
                    pass
                user.flow_state = None
                user.flow_data = None
                await session.commit()

        await cb.message.edit_text("Главное меню:", reply_markup=kb_main())
        await cb.answer()
        return

    if where == "cabinet":
        async with session_scope() as session:
            sub = await get_subscription(session, cb.from_user.id)
            ym = await _get_yandex_membership_safe(session, cb.from_user.id)

        text = (
            "👤 *Личный кабинет*\n\n"
            f"🆔 ID: `{cb.from_user.id}`\n\n"
            f"💳 Подписка: {'активна ✅' if _is_sub_active(sub.end_at) else 'не активна ❌'}\n"
            f"📅 До: {fmt_dt(sub.end_at)}\n"
            f"⏳ Осталось: {days_left(sub.end_at)} дн.\n\n"
            "🟡 *Yandex Plus*\n"
            f"— Статус: *{getattr(ym, 'status', 'не подключено') if ym else 'не подключено'}*\n"
            f"— Логин: `{getattr(ym, 'yandex_login', '—') if ym else '—'}`"
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
            ym = await _get_yandex_membership_safe(session, cb.from_user.id)

        if not _is_sub_active(sub.end_at):
            await cb.answer("Подписка не активна. Оплатите доступ.", show_alert=True)
            return

        if ym and getattr(ym, "yandex_login", None):
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🛠 Поддержка", callback_data="nav:support")],
                    [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav:home")],
                ]
            )
            await cb.message.edit_text(
                "🟡 *Yandex Plus*\n\n"
                f"Ваш логин: `{ym.yandex_login}`\n"
                f"Статус: *{getattr(ym, 'status', '—')}*\n\n"
                "Логин уже подтверждён и не может быть изменён.",
                reply_markup=kb,
                parse_mode="Markdown",
            )
            await cb.answer()
            return

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
        prompt = await cb.message.answer("👇 Введите логин сообщением ниже")

        async with session_scope() as session:
            user = await session.get(User, cb.from_user.id)
            user.flow_data = json.dumps({
                "hint_msg_ids": [hint.message_id, prompt.message_id]
            })
            await session.commit()
        return

    if where == "support":
        await cb.message.edit_text(
            "🛠 Поддержка\n\nНапишите: @support (заглушка)",
            reply_markup=kb_back_home(),
        )
        await cb.answer()
        return

    await cb.answer("Неизвестный раздел")
