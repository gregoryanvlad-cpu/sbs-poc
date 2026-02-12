from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import func, select

from app.bot.auth import is_owner
from app.bot.keyboards import kb_admin_menu, kb_admin_referrals_menu
from app.core.config import settings
from app.db.models import ReferralEarning, User
from app.db.models.payout_request import PayoutRequest
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_invite_slot import YandexInviteSlot
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.services.referrals.service import referral_service
from app.services.vpn.service import vpn_service

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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ru_date_to_utc_end_of_day(s: str) -> Optional[datetime]:
    """
    Parse "9 февраля 2026" -> 2026-02-09 23:59:59 UTC
    """
    s = (s or "").strip().lower().replace("ё", "е")
    m = _RU_DATE_RE.match(s)
    if not m:
        return None
    day = int(m.group(1))
    month_name = m.group(2).lower()
    year = int(m.group(3))
    month = _MONTH_NUM_RU.get(month_name)
    if not month:
        return None
    try:
        return datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)
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
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.date().isoformat()


async def _resolve_tg_id(bot, raw: str) -> int | None:
    """Resolve input like '123', '@username' to tg_id.

    Best-effort: if username can't be resolved (e.g., user didn't start bot), returns None.
    """
    s = (raw or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s.startswith("@"):  # try resolve via get_chat
        try:
            chat = await bot.get_chat(s)
            return int(chat.id)
        except Exception:
            return None
    return None


async def _tg_label(bot, tg_id: int) -> str:
    """Human-readable label: First Last (@username)."""
    try:
        chat = await bot.get_chat(int(tg_id))
        name = " ".join([p for p in [getattr(chat, "first_name", ""), getattr(chat, "last_name", "")] if p]).strip()
        username = getattr(chat, "username", None)
        if username:
            return f"{name or 'Пользователь'} (@{username})"
        return name or f"ID {tg_id}"
    except Exception:
        return f"ID {tg_id}"


def _kb_user_nav() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="nav:cabinet")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


# ==========================
# FSM
# ==========================

class AdminYandexFSM(StatesGroup):
    # add yandex account
    waiting_label = State()
    waiting_plus_end = State()
    waiting_links = State()

    # edit yandex account
    edit_waiting_label = State()
    edit_waiting_plus_end = State()
    edit_waiting_links = State()

    # reset user
    reset_wait_user_id = State()

    # mint referral earnings
    mint_wait_target_tg = State()
    mint_wait_amount = State()
    mint_wait_status = State()

    # payouts
    payout_wait_action = State()
    payout_wait_request_id = State()
    payout_wait_reject_note = State()

    # approve holds
    hold_wait_user_id = State()


class AdminReferralAssignFSM(StatesGroup):
    waiting_referred = State()
    waiting_new_owner = State()


class AdminReferralOwnerFSM(StatesGroup):
    waiting_referred = State()


# ==========================
# ADMIN MENU
# ==========================

@router.callback_query(lambda c: c.data == "admin:menu")
async def admin_menu(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    # Answer ASAP to avoid "query is too old" when we do network calls below.
    try:
        await cb.answer()
    except Exception:
        pass

    # Best-effort VPN status block (never fail admin menu)
    vpn_line = "🌍 VPN: статус недоступен"
    try:
        st = await asyncio.wait_for(vpn_service.get_server_status(), timeout=4)
        if st.get("ok"):
            cpu = st.get("cpu_load_percent")
            act = st.get("active_peers")
            tot = st.get("total_peers")
            if cpu is not None and act is not None and tot is not None:
                vpn_line = f"🌍 VPN: загрузка CPU ~<b>{cpu:.0f}%</b> | активных пиров <b>{act}</b>/<b>{tot}</b>"
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "admin:ref:take:self")
async def admin_ref_take_self(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await state.clear()
    await state.set_state(AdminReferralAssignFSM.waiting_referred)
    await state.update_data(mode="take_self")
    await cb.message.edit_text(
        "👑 <b>Забрать реферала себе</b>\n\n"
        "Отправь TG ID реферала или @username:",
        reply_markup=_kb_ref_manage(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.callback_query(lambda c: c.data == "admin:ref:assign")
async def admin_ref_assign(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await state.clear()
    await state.set_state(AdminReferralAssignFSM.waiting_referred)
    await state.update_data(mode="assign")
    await cb.message.edit_text(
        "🔁 <b>Назначить реферала</b>\n\n"
        "Отправь TG ID реферала или @username:",
        reply_markup=_kb_ref_manage(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(AdminReferralAssignFSM.waiting_referred)
async def admin_ref_wait_referred(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    referred_id = await _resolve_tg_id_from_text(message.bot, message.text or "")
    if not referred_id:
        await message.answer("❌ Не получилось распознать пользователя. Пришли TG ID (цифры) или @username")
        return

    data = await state.get_data()
    mode = data.get("mode")

    if mode == "take_self":
        new_owner_id = int(getattr(settings, "owner_tg_id", 0) or 0) or int(message.from_user.id)
        async with session_scope() as session:
            ok, prev = await referral_service.admin_reassign_referral(
                session, referred_tg_id=referred_id, new_referrer_tg_id=new_owner_id
            )
            await session.commit()

        ref_lbl = await _format_user_label(message.bot, referred_id)
        prev_lbl = await _format_user_label(message.bot, prev) if prev else "—"
        await state.clear()
        await message.answer(
            "✅ <b>Готово</b>\n\n"
            f"Реферал: <b>{ref_lbl}</b>\n"
            f"Был у: <b>{prev_lbl}</b>\n"
            f"Теперь у: <b>{await _format_user_label(message.bot, new_owner_id)}</b>",
            parse_mode="HTML",
            reply_markup=kb_admin_menu(),
        )
        return

    # assign to a specific owner
    await state.update_data(referred_id=referred_id)
    await state.set_state(AdminReferralAssignFSM.waiting_new_owner)
    await message.answer(
        "👤 Отправь TG ID нового владельца или @username (кому назначить реферала):",
        reply_markup=_kb_ref_manage(),
    )


@router.message(AdminReferralAssignFSM.waiting_new_owner)
async def admin_ref_wait_new_owner(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    new_owner_id = await _resolve_tg_id_from_text(message.bot, message.text or "")
    if not new_owner_id:
        await message.answer("❌ Не получилось распознать пользователя. Пришли TG ID (цифры) или @username")
        return

    data = await state.get_data()
    referred_id = int(data.get("referred_id") or 0)
    if not referred_id:
        await state.clear()
        await message.answer("❌ Сессия сбилась. Открой управление рефералами заново.", reply_markup=kb_admin_menu())
        return

    async with session_scope() as session:
        ok, prev = await referral_service.admin_reassign_referral(
            session, referred_tg_id=referred_id, new_referrer_tg_id=int(new_owner_id)
        )
        await session.commit()

    ref_lbl = await _format_user_label(message.bot, referred_id)
    prev_lbl = await _format_user_label(message.bot, prev) if prev else "—"
    await state.clear()

    await message.answer(
        "✅ <b>Готово</b>\n\n"
        f"Реферал: <b>{ref_lbl}</b>\n"
        f"Был у: <b>{prev_lbl}</b>\n"
        f"Теперь у: <b>{await _format_user_label(message.bot, int(new_owner_id))}</b>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.callback_query(lambda c: c.data == "admin:ref:owner")
async def admin_ref_owner(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await state.clear()
    await state.set_state(AdminReferralOwnerFSM.waiting_referred)
    await cb.message.edit_text(
        "🔍 <b>Узнать владельца реферала</b>\n\n"
        "Отправь TG ID реферала или @username:",
        reply_markup=_kb_ref_manage(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(AdminReferralOwnerFSM.waiting_referred)
async def admin_ref_owner_wait(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    referred_id = await _resolve_tg_id_from_text(message.bot, message.text or "")
    if not referred_id:
        await message.answer("❌ Не получилось распознать пользователя. Пришли TG ID (цифры) или @username")
        return

    async with session_scope() as session:
        owner = await referral_service.get_current_referrer_tg_id(session, referred_tg_id=referred_id)

    ref_lbl = await _format_user_label(message.bot, referred_id)
    owner_lbl = await _format_user_label(message.bot, owner) if owner else "—"
    await state.clear()
    await message.answer(
        "🔍 <b>Владелец реферала</b>\n\n"
        f"Реферал: <b>{ref_lbl}</b>\n"
        f"Владелец: <b>{owner_lbl}</b>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


# =========================================================
# ADD ACCOUNT (step-by-step): label -> plus_end_at -> 3 links
# =========================================================

@router.callback_query(lambda c: c.data == "admin:yandex:add")
async def admin_yandex_add(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminYandexFSM.waiting_label)

    try:
        await cb.message.edit_text(
            "➕ <b>Добавление Yandex-аккаунта</b>\n\n"
            "1) Отправь <b>название аккаунта</b> (LABEL)\n"
            "Пример: <code>YA_ACC_1</code>\n\n"
            "Дальше я спрошу дату окончания Plus и 3 ссылки.",
            reply_markup=kb_admin_menu(),
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        # Telegram не даёт отредактировать сообщение, если текст/клавиатура не изменились.
        if "message is not modified" not in str(e):
            raise
    await cb.answer()


@router.message(AdminYandexFSM.waiting_label)
async def admin_yandex_waiting_label(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    label = _normalize_label(message.text or "")
    if not label:
        await message.answer(
            "❌ Не понял label. Пример: <code>YA_ACC_1</code>",
            parse_mode="HTML",
            reply_markup=kb_admin_menu(),
        )
        return

    await state.update_data(label=label)
    await state.set_state(AdminYandexFSM.waiting_plus_end)

    await message.answer(
        "📅 <b>До какого числа подписка активна?</b>\n\n"
        "Введи в формате:\n"
        "<code>9 февраля 2026</code>\n\n"
        "Это дата окончания Plus на этом аккаунте (вводишь вручную).",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminYandexFSM.waiting_plus_end)
async def admin_yandex_waiting_plus_end(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    plus_end_at = _parse_ru_date_to_utc_end_of_day(message.text or "")
    if not plus_end_at:
        await message.answer(
            "❌ Формат даты неверный.\n\n"
            "Нужно: <code>9 февраля 2026</code>\n"
            "Попробуй ещё раз.",
            parse_mode="HTML",
            reply_markup=kb_admin_menu(),
        )
        return

    data = await state.get_data()
    label = data.get("label")
    if not label:
        await state.clear()
        await message.answer("❌ Сессия сбилась. Нажми «➕ Добавить Yandex-аккаунт» ещё раз.", reply_markup=kb_admin_menu())
        return

    async with session_scope() as session:
        acc = await session.scalar(select(YandexAccount).where(YandexAccount.label == label).limit(1))
        if not acc:
            acc = YandexAccount(
                label=label,
                status="active",
                max_slots=4,  # legacy field
                used_slots=0,
            )
            session.add(acc)
            await session.flush()

        acc.plus_end_at = plus_end_at
        acc.status = "active"
        await session.commit()

    await state.update_data(plus_end_at_iso=plus_end_at.isoformat())
    await state.set_state(AdminYandexFSM.waiting_links)

    await message.answer(
        "🔗 <b>Теперь отправь 3 ссылки (слоты 1..3)</b>\n\n"
        "Одна ссылка — одна строка:\n"
        "<code>LINK_SLOT_1</code>\n"
        "<code>LINK_SLOT_2</code>\n"
        "<code>LINK_SLOT_3</code>\n\n"
        f"Аккаунт: <code>{label}</code>\n"
        f"Plus до: <code>{plus_end_at.date().isoformat()}</code>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminYandexFSM.waiting_links)
async def admin_yandex_waiting_links(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    lines = [ln.strip() for ln in (message.text or "").splitlines() if ln.strip()]
    if len(lines) != 3:
        await message.answer(
            "❌ Нужно ровно 3 строки — три ссылки (слоты 1..3).",
            reply_markup=kb_admin_menu(),
        )
        return

    data = await state.get_data()
    label = data.get("label")
    if not label:
        await state.clear()
        await message.answer("❌ Сессия сбилась. Нажми «➕ Добавить Yandex-аккаунт» ещё раз.", reply_markup=kb_admin_menu())
        return

    async with session_scope() as session:
        acc = await session.scalar(select(YandexAccount).where(YandexAccount.label == label).limit(1))
        if not acc:
            await state.clear()
            await message.answer("❌ Аккаунт не найден. Начни добавление заново.", reply_markup=kb_admin_menu())
            return

        for idx, link in enumerate(lines, start=1):
            slot = await session.scalar(
                select(YandexInviteSlot)
                .where(YandexInviteSlot.yandex_account_id == acc.id, YandexInviteSlot.slot_index == idx)
                .limit(1)
            )
            if not slot:
                slot = YandexInviteSlot(
                    yandex_account_id=acc.id,
                    slot_index=idx,
                    invite_link=link,
                    status="free",
                )
                session.add(slot)
            else:
                # IMPORTANT: do not overwrite issued/burned (S1)
                if (slot.status or "free") == "free":
                    slot.invite_link = link

        await session.commit()

    await state.clear()

    await message.answer(
        "✅ <b>Готово!</b>\n\n"
        f"Аккаунт: <code>{label}</code>\n"
        "Слоты 1..3 загружены (free слоты обновлены, issued/burned не тронуты).",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


# ==========================
# LIST ACCOUNTS/SLOTS
# ==========================

@router.callback_query(lambda c: c.data == "admin:yandex:list")
async def admin_yandex_list(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    async with session_scope() as session:
        accounts = (await session.scalars(select(YandexAccount).order_by(YandexAccount.id.asc()))).all()
        if not accounts:
            await cb.message.edit_text(
                "📋 <b>Yandex аккаунты</b>\n\nПока пусто. Нажми «➕ Добавить Yandex-аккаунт».",
                reply_markup=kb_admin_menu(),
                parse_mode="HTML",
            )
            await cb.answer()
            return

        lines = ["📋 <b>Yandex аккаунты / слоты</b>\n"]
        for acc in accounts:
            free_cnt = await session.scalar(
                select(func.count(YandexInviteSlot.id)).where(
                    YandexInviteSlot.yandex_account_id == acc.id,
                    YandexInviteSlot.status == "free",
                )
            )
            issued_cnt = await session.scalar(
                select(func.count(YandexInviteSlot.id)).where(
                    YandexInviteSlot.yandex_account_id == acc.id,
                    YandexInviteSlot.status != "free",
                )
            )
            plus_str = _fmt_plus_end_at(acc.plus_end_at)
            lines.append(
                f"• <code>{acc.label}</code> — {acc.status} | Plus до: <code>{plus_str}</code> | "
                f"slots free/issued: <b>{int(free_cnt or 0)}</b>/<b>{int(issued_cnt or 0)}</b>"
            )

    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_menu(), parse_mode="HTML")
    await cb.answer()


# ==========================
# EDIT ACCOUNT (label -> new date -> optional links)
# ==========================

@router.callback_query(lambda c: c.data == "admin:yandex:edit")
async def admin_yandex_edit(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminYandexFSM.edit_waiting_label)

    await cb.message.edit_text(
        "✏️ <b>Редактирование Yandex-аккаунта</b>\n\n"
        "Отправь <b>LABEL</b> аккаунта, который хочешь изменить.\n"
        "Пример: <code>YA_ACC_1</code>",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(AdminYandexFSM.edit_waiting_label)
async def admin_yandex_edit_waiting_label(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    label = _normalize_label(message.text or "")
    if not label:
        await message.answer("❌ Не понял label. Пример: <code>YA_ACC_1</code>", parse_mode="HTML", reply_markup=kb_admin_menu())
        return

    async with session_scope() as session:
        acc = await session.scalar(select(YandexAccount).where(YandexAccount.label == label).limit(1))
        if not acc:
            await message.answer("❌ Аккаунт не найден. Проверь LABEL.", reply_markup=kb_admin_menu())
            return

        await state.update_data(edit_label=label)

        await state.set_state(AdminYandexFSM.edit_waiting_plus_end)
        await message.answer(
            "📅 <b>Новая дата окончания Plus</b>\n\n"
            f"Сейчас: <code>{_fmt_plus_end_at(acc.plus_end_at)}</code>\n\n"
            "Введи новую дату в формате:\n"
            "<code>9 февраля 2026</code>\n\n"
            "Или отправь <code>-</code> чтобы не менять дату.",
            parse_mode="HTML",
            reply_markup=kb_admin_menu(),
        )


@router.message(AdminYandexFSM.edit_waiting_plus_end)
async def admin_yandex_edit_waiting_plus_end(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    txt = (message.text or "").strip()
    data = await state.get_data()
    label = data.get("edit_label")
    if not label:
        await state.clear()
        await message.answer("❌ Сессия сбилась. Начни редактирование заново.", reply_markup=kb_admin_menu())
        return

    new_dt: datetime | None = None
    if txt != "-":
        new_dt = _parse_ru_date_to_utc_end_of_day(txt)
        if not new_dt:
            await message.answer(
                "❌ Формат даты неверный.\nНужно: <code>9 февраля 2026</code> или <code>-</code>",
                parse_mode="HTML",
                reply_markup=kb_admin_menu(),
            )
            return

    async with session_scope() as session:
        acc = await session.scalar(select(YandexAccount).where(YandexAccount.label == label).limit(1))
        if not acc:
            await state.clear()
            await message.answer("❌ Аккаунт не найден.", reply_markup=kb_admin_menu())
            return

        if new_dt:
            acc.plus_end_at = new_dt
        await session.commit()

    await state.set_state(AdminYandexFSM.edit_waiting_links)
    await message.answer(
        "🔗 <b>Обновить ссылки слотов (опционально)</b>\n\n"
        "Если хочешь заменить ссылки — отправь 3 строки (слоты 1..3).\n"
        "⚠️ Будут обновлены только слоты со статусом <b>free</b>.\n"
        "Issued/Burned слоты не трогаем (S1).\n\n"
        "Если не нужно — отправь <code>-</code>.",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminYandexFSM.edit_waiting_links)
async def admin_yandex_edit_waiting_links(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    txt = (message.text or "").strip()
    data = await state.get_data()
    label = data.get("edit_label")
    if not label:
        await state.clear()
        await message.answer("❌ Сессия сбилась. Начни редактирование заново.", reply_markup=kb_admin_menu())
        return

    if txt == "-":
        await state.clear()
        await message.answer("✅ Изменения сохранены.", reply_markup=kb_admin_menu())
        return

    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    if len(lines) != 3:
        await message.answer("❌ Нужно ровно 3 строки (или отправь <code>-</code>).", parse_mode="HTML", reply_markup=kb_admin_menu())
        return

    async with session_scope() as session:
        acc = await session.scalar(select(YandexAccount).where(YandexAccount.label == label).limit(1))
        if not acc:
            await state.clear()
            await message.answer("❌ Аккаунт не найден.", reply_markup=kb_admin_menu())
            return

        updated = 0
        skipped = 0
        for idx, link in enumerate(lines, start=1):
            slot = await session.scalar(
                select(YandexInviteSlot)
                .where(YandexInviteSlot.yandex_account_id == acc.id, YandexInviteSlot.slot_index == idx)
                .limit(1)
            )
            if not slot:
                slot = YandexInviteSlot(
                    yandex_account_id=acc.id,
                    slot_index=idx,
                    invite_link=link,
                    status="free",
                )
                session.add(slot)
                updated += 1
            else:
                if (slot.status or "free") == "free":
                    slot.invite_link = link
                    updated += 1
                else:
                    skipped += 1

        await session.commit()

    await state.clear()
    await message.answer(
        "✅ Аккаунт обновлён.\n\n"
        f"Ссылки обновлены (free): {updated}\n"
        f"Пропущено (issued/burned): {skipped}",
        reply_markup=kb_admin_menu(),
    )


# ==========================
# RESET USER (FULL)  + YANDEX MEMBERSHIP CLEANUP
# ==========================

@router.callback_query(lambda c: c.data == "admin:reset:user")
async def admin_reset_user(cb: CallbackQuery, state: FSMContext) -> None:
    """
    Полный сброс пользователя (TEST):
    - подписка
    - VPN
    - Yandex membership/слот
    - сброс flow_state/flow_data
    """
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminYandexFSM.reset_wait_user_id)

    await cb.message.edit_text(
        "🧨 <b>Полный сброс пользователя</b>\n\n"
        "Отправь TG ID пользователя (число).\n"
        "⚠️ Будут удалены: подписка, VPN, Yandex membership/слот.",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(AdminYandexFSM.reset_wait_user_id)
async def admin_reset_user_apply(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("❌ Нужно число (TG ID).", reply_markup=kb_admin_menu())
        return

    tg_id = int(txt)
    await state.clear()

    from app.services.admin.reset_user import AdminResetUserService

    msg = await message.answer("⏳ Сбрасываю пользователя...", reply_markup=kb_admin_menu())
    try:
        await AdminResetUserService().reset_user(tg_id=tg_id)
    except Exception as e:
        # чтобы не зависало "⏳ ..." при падении в reset_user
        await msg.edit_text(
            f"❌ Ошибка при сбросе пользователя <code>{tg_id}</code>:\n"
            f"<code>{type(e).__name__}: {e}</code>",
            parse_mode="HTML",
            reply_markup=kb_admin_menu(),
        )
        return

    await msg.edit_text(
        f"✅ Пользователь <code>{tg_id}</code> полностью сброшен.\n"
        "Теперь он как новый.",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )

# ==========================
# REFERRALS: MINT (TEST EARNINGS)
# ==========================

@router.callback_query(lambda c: c.data == "admin:ref:mint")
async def admin_ref_mint(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminYandexFSM.mint_wait_target_tg)

    await cb.message.edit_text(
        "🧪 <b>Mint реф. денег</b>\n\n"
        "Шаг 1/3: отправь TG ID получателя (кому начислить).",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(AdminYandexFSM.mint_wait_target_tg)
async def admin_ref_mint_target(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("❌ Нужно число (TG ID).", reply_markup=kb_admin_menu())
        return

    await state.update_data(target_tg=int(txt))
    await state.set_state(AdminYandexFSM.mint_wait_amount)

    await message.answer(
        "Шаг 2/3: отправь сумму в ₽ (целое число).\n"
        "Пример: <code>150</code>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminYandexFSM.mint_wait_amount)
async def admin_ref_mint_amount(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("❌ Нужно целое число (₽).", reply_markup=kb_admin_menu())
        return

    await state.update_data(amount=int(txt))
    await state.set_state(AdminYandexFSM.mint_wait_status)

    await message.answer(
        "Шаг 3/3: статус начисления:\n"
        "— <code>pending</code> (в холде)\n"
        "— <code>available</code> (сразу доступно)\n\n"
        "Отправь <code>pending</code> или <code>available</code>.",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminYandexFSM.mint_wait_status)
async def admin_ref_mint_status(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    status = (message.text or "").strip().lower()
    if status not in ("pending", "available"):
        await message.answer("❌ Нужно: <code>pending</code> или <code>available</code>.", parse_mode="HTML", reply_markup=kb_admin_menu())
        return

    data = await state.get_data()
    await state.clear()

    target_tg = int(data.get("target_tg") or 0)
    amount = int(data.get("amount") or 0)
    if not target_tg or amount <= 0:
        await message.answer("❌ Сессия сбилась. Начни заново.", reply_markup=kb_admin_menu())
        return

    async with session_scope() as session:
        # ensure user exists (owner can mint to anyone)
        u = await session.get(User, target_tg)
        if not u:
            u = User(tg_id=target_tg)
            session.add(u)
            await session.flush()

        available_at = None
        if status == "pending":
            hold_days = int(getattr(settings, "referral_hold_days", 7) or 7)
            available_at = _utcnow() + timedelta(days=hold_days)

        e = ReferralEarning(
            referrer_tg_id=target_tg,
            referred_tg_id=target_tg,
            payment_id=None,
            payment_amount_rub=0,
            percent=0,
            earned_rub=amount,
            status=status,
            available_at=available_at,
        )
        session.add(e)
        await session.commit()

    await message.answer(
        "✅ Mint выполнен.\n\n"
        f"Кому: <code>{target_tg}</code>\n"
        f"Сумма: <b>{amount} ₽</b>\n"
        f"Статус: <b>{status}</b>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


# ==========================
# REFERRALS: HOLDS (approve pending -> available)
# ==========================

@router.callback_query(lambda c: c.data == "admin:ref:holds")
async def admin_ref_holds(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    async with session_scope() as session:
        total_pending = await session.scalar(
            select(func.coalesce(func.sum(ReferralEarning.earned_rub), 0)).where(ReferralEarning.status == "pending")
        )

        # Список тех, у кого есть pending (чтобы админ видел "кто именно ждёт")
        # Показываем агрегировано по referrer_tg_id: сумма, количество и ближайшая дата available_at.
        q = (
            select(
                ReferralEarning.referrer_tg_id.label("tg_id"),
                func.coalesce(func.sum(ReferralEarning.earned_rub), 0).label("sum_rub"),
                func.count(ReferralEarning.id).label("cnt"),
                func.min(ReferralEarning.available_at).label("min_available_at"),
            )
            .where(ReferralEarning.status == "pending")
            .group_by(ReferralEarning.referrer_tg_id)
            .order_by(func.coalesce(func.sum(ReferralEarning.earned_rub), 0).desc())
            .limit(30)
        )
        pending_rows = (await session.execute(q)).all()

    def _fmt_dt(dt):
        if not dt:
            return "—"
        # dt может быть tz-aware; отображаем компактно
        try:
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return str(dt)[:10]

    pending_list_text = ""
    if pending_rows:
        lines = []
        for tg_id, sum_rub, cnt, min_available_at in pending_rows:
            lines.append(
                f"• <code>{tg_id}</code> — <b>{int(sum_rub or 0)} ₽</b> ({int(cnt)} шт.), ближайшая дата: <code>{_fmt_dt(min_available_at)}</code>"
            )
        pending_list_text = (
            "\n<b>Кто сейчас в pending (топ-30):</b>\n" + "\n".join(lines) + "\n"
        )

    await state.clear()
    await state.set_state(AdminYandexFSM.hold_wait_user_id)

    await cb.message.edit_text(
        "⏳ <b>Холды рефералки</b>\n\n"
        f"Всего pending (холд): <b>{int(total_pending or 0)} ₽</b>\n\n"
        f"{pending_list_text}\n"
        "Введи TG ID пользователя чтобы посмотреть его pending и (опционально) одобрить.\n"
        "Или отправь <code>all</code> чтобы одобрить ВСЁ pending, где уже прошла дата available_at.",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )
    await cb.answer()


@router.message(AdminYandexFSM.hold_wait_user_id)
async def admin_ref_hold_action(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    txt = (message.text or "").strip().lower()

    if txt == "all":
        async with session_scope() as session:
            moved_count = await referral_service.release_pending(session)
            await session.commit()

        await state.clear()
        await message.answer(
            f"✅ Одобрено pending→available: <b>{moved_count}</b> начислений.",
            parse_mode="HTML",
            reply_markup=kb_admin_menu(),
        )
        return

    if not txt.isdigit():
        await message.answer("❌ Нужно: TG ID (число) или <code>all</code>.", parse_mode="HTML", reply_markup=kb_admin_menu())
        return

    tg_id = int(txt)

    async with session_scope() as session:
        pending_sum = await session.scalar(
            select(func.coalesce(func.sum(ReferralEarning.earned_rub), 0)).where(
                ReferralEarning.referrer_tg_id == tg_id,
                ReferralEarning.status == "pending",
            )
        )
        available, pending, paid = await referral_service.get_balances(session, tg_id)

        # approve this user's pending immediately (manual override)
        q = select(ReferralEarning).where(
            ReferralEarning.referrer_tg_id == tg_id,
            ReferralEarning.status == "pending",
        )
        items = (await session.scalars(q)).all()
        moved = 0
        for e in items:
            moved += int(e.earned_rub or 0)
            e.status = "available"
            e.available_at = None

        await session.commit()

    await state.clear()

    await message.answer(
        "✅ Готово.\n\n"
        f"Пользователь: <code>{tg_id}</code>\n"
        f"Одобрено pending→available: <b>{moved} ₽</b>\n\n"
        f"Баланс сейчас:\n"
        f"— Доступно: <b>{available} ₽</b>\n"
        f"— В холде: <b>{pending_sum} ₽</b>\n"
        f"— Выплачено: <b>{paid} ₽</b>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )

    # notify user (FIXED: no broken multiline strings)
    try:
        async with session_scope() as session:
            avail, pend, paid = await referral_service.get_balances(session, tg_id)
        await message.bot.send_message(
            chat_id=int(tg_id),
            text=(
                "✅ <b>Реферальные начисления одобрены</b>\n\n"
                f"Переведено: <b>{moved} ₽</b> (pending → available)\n\n"
                "Ваш баланс:\n"
                f"— Доступно: <b>{avail} ₽</b>\n"
                f"— В холде: <b>{pend} ₽</b>\n"
                f"— Выплачено: <b>{paid} ₽</b>"
            ),
            reply_markup=_kb_user_nav(),
            parse_mode="HTML",
        )
    except Exception:
        pass


# ==========================
# PAYOUT REQUESTS (ADMIN)
# ==========================

@router.callback_query(lambda c: c.data == "admin:payouts")
async def admin_payouts(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()

    async with session_scope() as session:
        reqs = (
            await session.scalars(
                select(PayoutRequest).order_by(PayoutRequest.id.desc()).limit(20)
            )
        ).all()

    if not reqs:
        await cb.message.edit_text(
            "📤 <b>Заявки на вывод</b>\n\nПока заявок нет.",
            reply_markup=kb_admin_menu(),
            parse_mode="HTML",
        )
        await cb.answer()
        return

    lines = ["📤 <b>Заявки на вывод</b>\n"]
    for r in reqs:
        lines.append(
            f"• ID <code>{r.id}</code> | TG <code>{r.tg_id}</code> | "
            f"{r.amount_rub} ₽ | <b>{r.status}</b>"
        )

    lines.append("\nОтправь ID заявки чтобы обработать (approve/reject).")

    await state.set_state(AdminYandexFSM.payout_wait_request_id)
    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_menu(), parse_mode="HTML")
    await cb.answer()


@router.message(AdminYandexFSM.payout_wait_request_id)
async def admin_payout_choose(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("❌ Нужно число (ID заявки).", reply_markup=kb_admin_menu())
        return

    req_id = int(txt)
    await state.update_data(payout_req_id=req_id)
    await state.set_state(AdminYandexFSM.payout_wait_action)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Одобрить (paid)", callback_data="admin:payout:approve")],
            [InlineKeyboardButton(text="❌ Отклонить (rejected)", callback_data="admin:payout:reject")],
            [InlineKeyboardButton(text="🏠 Назад", callback_data="admin:menu")],
        ]
    )

    await message.answer(
        f"Заявка <code>{req_id}</code>.\nВыбери действие:",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(lambda c: c.data == "admin:payout:approve")
async def admin_payout_approve(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    data = await state.get_data()
    req_id = int(data.get("payout_req_id") or 0)
    await state.clear()

    if not req_id:
        await cb.message.edit_text("❌ Сессия сбилась.", reply_markup=kb_admin_menu())
        await cb.answer()
        return

    async with session_scope() as session:
        req = await session.get(PayoutRequest, req_id)
        if not req:
            await cb.message.edit_text("❌ Заявка не найдена.", reply_markup=kb_admin_menu())
            await cb.answer()
            return

        # mark paid
        await referral_service.mark_payout_paid(session, request_id=req_id)
        await session.commit()

        tg_id = int(req.tg_id)
        avail, pend, paid = await referral_service.get_balances(session, tg_id)

    await cb.message.edit_text(
        f"✅ Заявка <code>{req_id}</code> отмечена как <b>paid</b>.",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )
    await cb.answer()

    # notify user
    try:
        await cb.bot.send_message(
            chat_id=tg_id,
            text=(
                "✅ <b>Выплата обработана</b>\n\n"
                f"Заявка: <code>{req_id}</code>\n"
                f"Статус: <b>paid</b>\n\n"
                "Ваш баланс:\n"
                f"— Доступно: <b>{avail} ₽</b>\n"
                f"— В холде: <b>{pend} ₽</b>\n"
                f"— Выплачено: <b>{paid} ₽</b>"
            ),
            reply_markup=_kb_user_nav(),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "admin:payout:reject")
async def admin_payout_reject(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.set_state(AdminYandexFSM.payout_wait_reject_note)

    await cb.message.edit_text(
        "❌ <b>Отклонение заявки</b>\n\n"
        "Отправь комментарий (почему отклонено). Можно коротко.\n"
        "Если не нужен — отправь <code>-</code>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )
    await cb.answer()


@router.message(AdminYandexFSM.payout_wait_reject_note)
async def admin_payout_reject_note(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    note = (message.text or "").strip()
    if note == "-":
        note = ""

    data = await state.get_data()
    req_id = int(data.get("payout_req_id") or 0)
    await state.clear()

    if not req_id:
        await message.answer("❌ Сессия сбилась.", reply_markup=kb_admin_menu())
        return

    async with session_scope() as session:
        req = await session.get(PayoutRequest, req_id)
        if not req:
            await message.answer("❌ Заявка не найдена.", reply_markup=kb_admin_menu())
            return

        await referral_service.reject_payout(session, request_id=req_id, note=note)
        await session.commit()

        tg_id = int(req.tg_id)
        avail, pend, paid = await referral_service.get_balances(session, tg_id)

    await message.answer(
        f"✅ Заявка <code>{req_id}</code> отмечена как <b>rejected</b>.",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )

    try:
        await message.bot.send_message(
            chat_id=tg_id,
            text=(
                "❌ <b>Выплата отклонена</b>\n\n"
                f"Заявка: <code>{req_id}</code>\n"
                f"Статус: <b>rejected</b>\n"
                f"Комментарий: <i>{note or '—'}</i>\n\n"
                "Ваш баланс:\n"
                f"— Доступно: <b>{avail} ₽</b>\n"
                f"— В холде: <b>{pend} ₽</b>\n"
                f"— Выплачено: <b>{paid} ₽</b>"
            ),
            reply_markup=_kb_user_nav(),
            parse_mode="HTML",
        )
    except Exception:
        pass


# ==========================
# BULK APPROVE PENDING -> AVAILABLE (NOTIFY USERS)
# ==========================

@router.callback_query(lambda c: c.data == "admin:ref:approve_pending")
async def admin_ref_approve_pending(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    async with session_scope() as session:
        # take snapshot grouped by user for notifications
        rows = (await session.execute(
            select(
                ReferralEarning.referrer_tg_id,
                func.coalesce(func.sum(ReferralEarning.earned_rub), 0).label("sum_rub"),
            )
            .where(ReferralEarning.status == "pending")
            .group_by(ReferralEarning.referrer_tg_id)
        )).all()

        moved_count = await referral_service.release_pending(session)
        await session.commit()

    await cb.message.edit_text(
        f"✅ Pending→available выполнено.\nОдобрено начислений: <b>{moved_count}</b>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )
    await cb.answer()

    # notify each affected user with correct balances (FIXED)
    for r in rows:
        try:
            tg_id = int(r.referrer_tg_id)
            moved_sum_rub = int(r.sum_rub or 0)

            async with session_scope() as session:
                available, pending, paid = await referral_service.get_balances(session, tg_id)

            await cb.bot.send_message(
                chat_id=int(tg_id),
                text=(
                    "✅ <b>Реферальные начисления одобрены</b>\n\n"
                    f"Переведено: <b>{moved_count}</b> начислений на сумму <b>{moved_sum_rub} ₽</b> (pending → available)\n\n"
                    "Ваш баланс:\n"
                    f"— Доступно: <b>{available} ₽</b>\n"
                    f"— В холде: <b>{pending} ₽</b>\n"
                    f"— Выплачено: <b>{paid} ₽</b>"
                ),
                reply_markup=_kb_user_nav(),
                parse_mode="HTML",
            )
        except Exception:
            continue
