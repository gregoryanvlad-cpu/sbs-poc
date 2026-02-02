from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot.auth import is_owner
from app.bot.keyboards import kb_admin_menu
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_invite_slot import YandexInviteSlot
from app.db.session import session_scope

router = Router()

# ==========================
# RU date parsing: "9 февраля 2026"
# ==========================

_MONTH_NUM_RU = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

_RU_DATE_RE = re.compile(r"^\s*(\d{1,2})\s+([а-яё]+)\s+(\d{4})\s*$", re.IGNORECASE)


def _parse_ru_date_to_utc_end_of_day(s: str) -> Optional[datetime]:
    s = (s or "").strip().lower().replace("ё", "е")
    m = _RU_DATE_RE.match(s)
    if not m:
        return None
    day = int(m.group(1))
    month = _MONTH_NUM_RU.get(m.group(2))
    year = int(m.group(3))
    if not month:
        return None
    return datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)


def _normalize_label(label: str) -> str:
    label = (label or "").strip()
    label = re.sub(r"\s+", "_", label)
    label = re.sub(r"[^A-Za-z0-9_\-]", "", label)
    return label[:64]


def _fmt_plus_end_at(dt: datetime | None) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.date().isoformat()


# ==========================
# FSM
# ==========================

class AdminYandexFSM(StatesGroup):
    waiting_label = State()
    waiting_plus_end = State()
    waiting_links = State()

    edit_waiting_label = State()
    edit_waiting_plus_end = State()
    edit_waiting_links = State()


# ==========================
# ADMIN MENU
# ==========================

@router.callback_query(lambda c: c.data == "admin:menu")
async def admin_menu(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await cb.message.edit_text(
        "🛠 <b>Админка</b>\n\n"
        "🟡 <b>Yandex Plus — ручной режим</b>\n"
        "• аккаунты добавляются вручную\n"
        "• ссылки — готовые\n"
        "• исключение пользователей — вручную\n",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


# =========================================================
# ADD ACCOUNT
# =========================================================

@router.callback_query(lambda c: c.data == "admin:yandex:add")
async def admin_yandex_add(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        return

    await state.clear()
    await state.set_state(AdminYandexFSM.waiting_label)

    await cb.message.edit_text(
        "➕ <b>Добавление Yandex аккаунта</b>\n\n"
        "Отправь название аккаунта (LABEL)\n"
        "Пример: <code>YA_ACC_1</code>",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )


@router.message(AdminYandexFSM.waiting_label)
async def admin_add_label(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return

    label = _normalize_label(message.text)
    if not label:
        await message.answer("❌ Некорректный label", reply_markup=kb_admin_menu())
        return

    await state.update_data(label=label)
    await state.set_state(AdminYandexFSM.waiting_plus_end)

    await message.answer(
        "📅 До какого числа активна подписка?\n"
        "<code>9 февраля 2026</code>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminYandexFSM.waiting_plus_end)
async def admin_add_plus_end(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return

    plus_end_at = _parse_ru_date_to_utc_end_of_day(message.text)
    if not plus_end_at:
        await message.answer("❌ Неверный формат даты", reply_markup=kb_admin_menu())
        return

    data = await state.get_data()
    label = data["label"]

    async with session_scope() as session:
        acc = await session.scalar(select(YandexAccount).where(YandexAccount.label == label))
        if not acc:
            acc = YandexAccount(label=label, status="active")
            session.add(acc)
            await session.flush()

        acc.plus_end_at = plus_end_at
        await session.commit()

    await state.set_state(AdminYandexFSM.waiting_links)

    await message.answer(
        "🔗 Отправь 3 ссылки (каждая с новой строки)",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminYandexFSM.waiting_links)
async def admin_add_links(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return

    links = [l.strip() for l in message.text.splitlines() if l.strip()]
    if len(links) != 3:
        await message.answer("❌ Нужно ровно 3 ссылки", reply_markup=kb_admin_menu())
        return

    data = await state.get_data()
    label = data["label"]

    async with session_scope() as session:
        acc = await session.scalar(select(YandexAccount).where(YandexAccount.label == label))
        for idx, link in enumerate(links, start=1):
            slot = await session.scalar(
                select(YandexInviteSlot)
                .where(YandexInviteSlot.yandex_account_id == acc.id)
                .where(YandexInviteSlot.slot_index == idx)
            )
            if not slot:
                slot = YandexInviteSlot(
                    yandex_account_id=acc.id,
                    slot_index=idx,
                    invite_link=link,
                    status="free",
                )
                session.add(slot)
        await session.commit()

    await state.clear()
    await message.answer("✅ Аккаунт добавлен", reply_markup=kb_admin_menu())
