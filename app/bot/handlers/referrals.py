from __future__ import annotations

import json
from decimal import Decimal

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.auth import is_owner
from app.bot.keyboards import kb_back_home
from app.core.config import settings
from app.db.models import User
from app.db.models.payout_request import PayoutRequest
from app.db.session import session_scope
from app.services.referrals.service import referral_service


router = Router()


class ReferralWithdrawFSM(StatesGroup):
    waiting_amount = State()
    waiting_requisites = State()


@router.callback_query(lambda c: c.data == "ref:withdraw")
async def on_ref_withdraw(cb: CallbackQuery, state: FSMContext) -> None:
    tg_id = cb.from_user.id

    async with session_scope() as session:
        available = await referral_service.available_balance(session, tg_id=tg_id)

    min_amount = int(getattr(settings, "referral_min_payout_rub", 50) or 50)
    if available < Decimal(min_amount):
        await cb.answer(
            f"Минимальная сумма на вывод — {min_amount} ₽\n"
            f"Сейчас доступно: {available} ₽",
            show_alert=True,
        )
        return

    await state.clear()
    await state.set_state(ReferralWithdrawFSM.waiting_amount)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:referrals")]]
    )

    await cb.message.edit_text(
        "💸 <b>Вывод средств</b>\n\n"
        f"Доступно: <b>{available} ₽</b>\n"
        f"Минимум: <b>{min_amount} ₽</b>\n\n"
        "Отправь сумму на вывод (целое число, ₽):",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(ReferralWithdrawFSM.waiting_amount)
async def on_withdraw_amount(message: Message, state: FSMContext) -> None:
    tg_id = message.from_user.id
    txt = (message.text or "").strip()

    if not txt.isdigit():
        await message.answer("❌ Введи сумму числом (например: 150)")
        return

    amount = int(txt)
    min_amount = int(getattr(settings, "referral_min_payout_rub", 50) or 50)
    if amount < min_amount:
        await message.answer(f"❌ Минимум на вывод: {min_amount} ₽")
        return

    async with session_scope() as session:
        available = await referral_service.available_balance(session, tg_id=tg_id)

    if Decimal(amount) > available:
        await message.answer(f"❌ Недостаточно средств. Доступно: {available} ₽")
        return

    await state.update_data(amount=amount)
    await state.set_state(ReferralWithdrawFSM.waiting_requisites)

    await message.answer(
        "🧾 <b>Реквизиты</b>\n\n"
        "Отправь одним сообщением куда перевести деньги (карта/СБП/кошелёк).\n"
        "Пример: <code>СБП +7...</code>",
        parse_mode="HTML",
        reply_markup=kb_back_home(),
    )


@router.message(ReferralWithdrawFSM.waiting_requisites)
async def on_withdraw_requisites(message: Message, state: FSMContext) -> None:
    tg_id = message.from_user.id
    req = (message.text or "").strip()
    if len(req) < 4:
        await message.answer("❌ Слишком коротко. Напиши реквизиты подробнее.")
        return

    data = await state.get_data()
    amount = int(data.get("amount") or 0)
    if amount <= 0:
        await state.clear()
        await message.answer("❌ Сессия сбилась. Открой «Рефералы → Вывод» ещё раз.")
        return

    async with session_scope() as session:
        # Reserve earnings and create request atomically.
        pr = await referral_service.create_payout_request(
            session,
            tg_id=tg_id,
            amount_rub=amount,
            requisites=req,
        )
        await session.commit()

    await state.clear()

    await message.answer(
        "✅ <b>Заявка на вывод создана</b>\n\n"
        f"Сумма: <b>{amount} ₽</b>\n"
        "Статус: <b>в обработке</b>\n\n"
        "Мы напишем, когда заявка будет обработана.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:referrals")]]
        ),
    )

    # Notify owner (best-effort).
    owner_id = int(getattr(settings, "owner_tg_id", 0) or 0)
    if owner_id:
        try:
            await message.bot.send_message(
                chat_id=owner_id,
                text=(
                    "💸 Новая заявка на вывод\n\n"
                    f"TG ID: {tg_id}\n"
                    f"Сумма: {amount} ₽\n"
                    f"Реквизиты: {req}" 
                ),
            )
        except Exception:
            pass
