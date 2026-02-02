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
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.worker import build_kick_report_text
from app.repo import utcnow as repo_utcnow

router = Router()

# =========================================================
# RU DATE PARSING: "9 февраля 2026"
# =========================================================

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
    try:
        return datetime(
            int(m.group(3)),
            _MONTH_NUM_RU[m.group(2)],
            int(m.group(1)),
            23, 59, 59,
            tzinfo=timezone.utc,
        )
    except Exception:
        return None


def _normalize_label(label: str) -> str:
    label = (label or "").strip()
    label = re.sub(r"\s+", "_", label)
    label = re.sub(r"[^A-Za-z0-9_\-]", "", label)
    return label[:64]


def _fmt_plus_end_at(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.date().isoformat()


# =========================================================
# FSM
# =========================================================

class AdminYandexFSM(StatesGroup):
    waiting_label = State()
    waiting_plus_end = State()
    waiting_links = State()

    edit_waiting_label = State()
    edit_waiting_plus_end = State()
    edit_waiting_links = State()


class AdminKickFSM(StatesGroup):
    waiting_tg_id = State()


# =========================================================
# ADMIN MENU
# =========================================================

@router.callback_query(lambda c: c.data == "admin:menu")
async def admin_menu(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        return

    await cb.message.edit_text(
        "🛠 <b>Админка</b>\n\n"
        "🟡 <b>Yandex Plus (ручной режим)</b>\n"
        "— добавляешь аккаунт + дату окончания Plus\n"
        "— загружаешь 3 инвайт-ссылки\n"
        "— бот выдаёт их автоматически\n\n"
        "⚠️ Исключение из семей — вручную.",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )


# =========================================================
# ADD ACCOUNT
# =========================================================

@router.callback_query(lambda c: c.data == "admin:yandex:add")
async def add_account(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        return

    await state.clear()
    await state.set_state(AdminYandexFSM.waiting_label)

    await cb.message.edit_text(
        "➕ Введи <b>LABEL</b> аккаунта\nПример: <code>YA_ACC_1</code>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminYandexFSM.waiting_label)
async def add_label(msg: Message, state: FSMContext):
    label = _normalize_label(msg.text)
    if not label:
        await msg.answer("❌ Неверный label", reply_markup=kb_admin_menu())
        return

    await state.update_data(label=label)
    await state.set_state(AdminYandexFSM.waiting_plus_end)

    await msg.answer(
        "📅 До какого числа Plus активен?\nПример: <code>9 февраля 2026</code>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminYandexFSM.waiting_plus_end)
async def add_plus_end(msg: Message, state: FSMContext):
    plus_end = _parse_ru_date_to_utc_end_of_day(msg.text)
    if not plus_end:
        await msg.answer("❌ Неверный формат даты", reply_markup=kb_admin_menu())
        return

    data = await state.get_data()
    label = data["label"]

    async with session_scope() as s:
        acc = await s.scalar(select(YandexAccount).where(YandexAccount.label == label))
        if not acc:
            acc = YandexAccount(label=label, status="active")
            s.add(acc)
            await s.flush()

        acc.plus_end_at = plus_end
        await s.commit()

    await state.set_state(AdminYandexFSM.waiting_links)
    await msg.answer(
        "🔗 Отправь 3 ссылки (каждая с новой строки)",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminYandexFSM.waiting_links)
async def add_links(msg: Message, state: FSMContext):
    lines = [x.strip() for x in msg.text.splitlines() if x.strip()]
    if len(lines) != 3:
        await msg.answer("❌ Нужно ровно 3 ссылки", reply_markup=kb_admin_menu())
        return

    data = await state.get_data()
    label = data["label"]

    async with session_scope() as s:
        acc = await s.scalar(select(YandexAccount).where(YandexAccount.label == label))
        for i, link in enumerate(lines, start=1):
            slot = await s.scalar(
                select(YandexInviteSlot)
                .where(YandexInviteSlot.yandex_account_id == acc.id,
                       YandexInviteSlot.slot_index == i)
            )
            if not slot:
                s.add(YandexInviteSlot(
                    yandex_account_id=acc.id,
                    slot_index=i,
                    invite_link=link,
                    status="free",
                ))
        await s.commit()

    await state.clear()
    await msg.answer("✅ Аккаунт добавлен", reply_markup=kb_admin_menu())


# =========================================================
# LIST ACCOUNTS
# =========================================================

@router.callback_query(lambda c: c.data == "admin:yandex:list")
async def list_accounts(cb: CallbackQuery):
    async with session_scope() as s:
        accs = (await s.scalars(select(YandexAccount))).all()

    if not accs:
        await cb.message.edit_text("Пока пусто", reply_markup=kb_admin_menu())
        return

    text = ["📋 <b>Аккаунты</b>\n"]
    async with session_scope() as s:
        for a in accs:
            free = await s.scalar(
                select(func.count()).where(
                    YandexInviteSlot.yandex_account_id == a.id,
                    YandexInviteSlot.status == "free",
                )
            )
            text.append(
                f"• <code>{a.label}</code> | Plus до: <code>{_fmt_plus_end_at(a.plus_end_at)}</code> | free: {free}"
            )

    await cb.message.edit_text("\n".join(text), parse_mode="HTML", reply_markup=kb_admin_menu())


# =========================================================
# DAILY KICK REPORT
# =========================================================

@router.callback_query(lambda c: c.data == "admin:kick:report")
async def kick_report(cb: CallbackQuery):
    async with session_scope() as s:
        text = await build_kick_report_text(s)

    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admin_menu())


# =========================================================
# MARK USER REMOVED
# =========================================================

@router.callback_query(lambda c: c.data == "admin:kick:mark")
async def kick_mark_start(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminKickFSM.waiting_tg_id)

    await cb.message.edit_text(
        "🧾 Введи <b>Telegram ID</b> исключённого пользователя",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminKickFSM.waiting_tg_id)
async def kick_mark_apply(msg: Message, state: FSMContext):
    if not msg.text.isdigit():
        await msg.answer("❌ Нужен TG ID", reply_markup=kb_admin_menu())
        return

    tg_id = int(msg.text)

    async with session_scope() as s:
        m = await s.scalar(
            select(YandexMembership)
            .where(YandexMembership.tg_id == tg_id)
            .order_by(YandexMembership.id.desc())
        )
        if not m:
            await msg.answer("❌ Не найдено", reply_markup=kb_admin_menu())
            return

        if m.removed_at:
            await msg.answer("ℹ️ Уже отмечен", reply_markup=kb_admin_menu())
            return

        m.removed_at = repo_utcnow()
        await s.commit()

    await state.clear()
    await msg.answer("✅ Пользователь отмечен как исключённый", reply_markup=kb_admin_menu())
