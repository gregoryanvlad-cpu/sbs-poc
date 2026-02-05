from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot.auth import is_owner
from app.bot.keyboards import kb_admin_menu
from app.core.config import settings
from app.db.models import Payment, Referral, ReferralEarning, Subscription, User, VpnPeer
from app.db.models.payout_request import PayoutRequest
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_invite_slot import YandexInviteSlot
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.services.referrals.service import referral_service

router = Router()

# ==========================
# Time helpers
# ==========================

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d")


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
    """Parse "9 февраля 2026" -> 2026-02-09 23:59:59 UTC."""
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
    return _fmt_dt(dt)


# ==========================
# FSM
# ==========================

class AdminFSM(StatesGroup):
    # yandex add
    waiting_label = State()
    waiting_plus_end = State()
    waiting_links = State()

    # yandex edit
    edit_waiting_label = State()
    edit_waiting_plus_end = State()
    edit_waiting_links = State()

    # kick mark
    kick_waiting_tg_id = State()

    # reset user
    reset_waiting_tg_id = State()

    # referral mint
    mint_waiting_amount = State()
    mint_waiting_status = State()  # "pending" / "available"


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
        "🟡 <b>Yandex Plus (ручной режим)</b>\n"
        "— добавляешь аккаунт и дату окончания Plus\n"
        "— загружаешь 3 готовые ссылки-приглашения (слоты 1..3)\n"
        "— бот выдаёт ссылки пользователям автоматически\n\n"
        "💰 <b>Рефералка</b>\n"
        "— можно создать тестовое начисление (mint) для проверки вывода\n\n"
        "⚠️ Исключение пользователей из семьи — вручную.\n",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


# ==========================
# PAYOUT REQUESTS (admin)
# ==========================


def _payout_kb(items: list[PayoutRequest]) -> "InlineKeyboardMarkup":
    """Inline buttons for the last payout requests.

    We keep it simple: for every request in status `created` we show two buttons:
    - mark paid
    - reject
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list[InlineKeyboardButton]] = []
    for req in items:
        if (req.status or "created") == "created":
            rows.append(
                [
                    InlineKeyboardButton(text=f"✅ Paid #{req.id}", callback_data=f"admin:payouts:paid:{req.id}"),
                    InlineKeyboardButton(text=f"❌ Reject #{req.id}", callback_data=f"admin:payouts:reject:{req.id}"),
                ]
            )

    # navigation
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:payouts:list")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(lambda c: c.data == "admin:payouts:list")
async def admin_payouts_list(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    async with session_scope() as session:
        items = (
            await session.scalars(
                select(PayoutRequest).order_by(PayoutRequest.id.desc()).limit(20)
            )
        ).all()

    if not items:
        await cb.message.edit_text(
            "💸 <b>Заявки на вывод</b>\n\nПока заявок нет.",
            parse_mode="HTML",
            reply_markup=kb_admin_menu(),
        )
        await cb.answer()
        return

    lines = ["💸 <b>Заявки на вывод (последние 20)</b>\n"]
    for req in items:
        created = req.created_at.isoformat() if getattr(req, "created_at", None) else "—"
        processed = req.processed_at.isoformat() if getattr(req, "processed_at", None) else "—"
        note = (req.note or "").strip()
        note_str = f" | note: {note}" if note else ""
        lines.append(
            f"• <b>#{req.id}</b> | user: <code>{req.tg_id}</code> | {req.amount_rub} RUB | "
            f"status: <b>{req.status}</b> | created: {created} | processed: {processed}{note_str}\n"
            f"  реквизиты: <code>{(req.requisites or '')[:120]}</code>"
        )

    from aiogram.types import InlineKeyboardMarkup

    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_payout_kb(items),
    )
    await cb.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("admin:payouts:paid:"))
async def admin_payouts_paid(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        req_id = int(cb.data.rsplit(":", 1)[1])
    except Exception:
        await cb.answer("bad request", show_alert=True)
        return

    async with session_scope() as session:
        try:
            await referral_service.mark_payout_paid(session, request_id=req_id)
            await session.commit()
        except Exception:
            await session.rollback()
            await cb.answer("Не удалось отметить как paid", show_alert=True)
            return

    await cb.answer("✅ Отмечено как paid")
    await admin_payouts_list(cb)


@router.callback_query(lambda c: c.data and c.data.startswith("admin:payouts:reject:"))
async def admin_payouts_reject(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        req_id = int(cb.data.rsplit(":", 1)[1])
    except Exception:
        await cb.answer("bad request", show_alert=True)
        return

    async with session_scope() as session:
        try:
            await referral_service.reject_payout(session, request_id=req_id, note="rejected by admin")
            await session.commit()
        except Exception:
            await session.rollback()
            await cb.answer("Не удалось отклонить", show_alert=True)
            return

    await cb.answer("❌ Отклонено")
    await admin_payouts_list(cb)


# =========================================================
# REFERRAL MINT (admin testing)
# =========================================================

@router.callback_query(lambda c: c.data == "admin:ref:mint")
async def admin_ref_mint_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminFSM.mint_waiting_amount)

    await cb.message.edit_text(
        "💰 <b>Mint (тестовое начисление)</b>\n\n"
        "Введи сумму в рублях (целое число).\n"
        "Пример: <code>150</code>\n\n"
        "Далее спрошу статус: pending/available.",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )
    await cb.answer()


@router.message(AdminFSM.mint_waiting_amount)
async def admin_ref_mint_amount(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("❌ Введи сумму числом. Пример: <code>150</code>", parse_mode="HTML")
        return

    amount = int(raw)
    if amount <= 0 or amount > 1_000_000:
        await message.answer("❌ Сумма выглядит странно. Введи 1..1000000")
        return

    await state.update_data(mint_amount=amount)
    await state.set_state(AdminFSM.mint_waiting_status)

    await message.answer(
        "Теперь введи статус начисления:\n"
        "— <code>pending</code> (на холде)\n"
        "— <code>available</code> (сразу доступно)\n\n"
        "Пример: <code>available</code>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminFSM.mint_waiting_status)
async def admin_ref_mint_status(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    status = (message.text or "").strip().lower()
    if status not in ("pending", "available"):
        await message.answer("❌ Нужно <code>pending</code> или <code>available</code>.", parse_mode="HTML")
        return

    data = await state.get_data()
    amount = int(data.get("mint_amount") or 0)
    if amount <= 0:
        await state.clear()
        await message.answer("❌ Сессия сбилась. Начни заново: Mint")
        return

    owner_id = int(message.from_user.id)
    now = utcnow()

    # To satisfy DB constraints we create:
    # - dummy referred user
    # - Payment(success) for dummy user
    # - Referral(owner -> dummy)
    # - ReferralEarning(owner, dummy, payment_id)
    dummy_referred_id = owner_id + 999_000

    async with session_scope() as session:
        # ensure users
        owner = await session.get(User, owner_id)
        if not owner:
            owner = User(tg_id=owner_id)
            session.add(owner)

        dummy = await session.get(User, dummy_referred_id)
        if not dummy:
            dummy = User(tg_id=dummy_referred_id)
            session.add(dummy)

        await session.flush()

        # IMPORTANT: Payment model doesn't have `created_at`.
        pay = Payment(
            tg_id=dummy_referred_id,
            amount=amount,
            currency="RUB",
            provider="mint",
            status="success",
            paid_at=now,
            payload=None,
        )
        session.add(pay)
        await session.flush()  # get pay.id

        ref = await session.scalar(
            select(Referral).where(Referral.referred_tg_id == dummy_referred_id).limit(1)
        )
        if not ref:
            ref = Referral(
                referrer_tg_id=owner_id,
                referred_tg_id=dummy_referred_id,
                status="active",
                first_payment_id=pay.id,
                activated_at=now,
            )
            session.add(ref)
            await session.flush()

        hold_days = int(getattr(settings, "referral_hold_days", 7) or 7)
        available_at = now + timedelta(days=hold_days)

        earning = ReferralEarning(
            referrer_tg_id=owner_id,
            referred_tg_id=dummy_referred_id,
            payment_id=pay.id,
            payment_amount_rub=amount,
            percent=100,
            earned_rub=amount,
            status=status,
            available_at=available_at if status == "pending" else None,
        )
        session.add(earning)

        await session.commit()

    await state.clear()

    await message.answer(
        "✅ <b>Mint выполнен</b>\n\n"
        f"Сумма: <code>{amount}</code> RUB\n"
        f"Статус: <code>{status}</code>\n"
        f"Dummy referred: <code>{dummy_referred_id}</code>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


# =========================================================
# YANDEX ACCOUNTS: add/edit/list
# =========================================================

@router.callback_query(lambda c: c.data == "admin:yandex:add")
async def admin_yandex_add(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminFSM.waiting_label)

    await cb.message.edit_text(
        "➕ <b>Добавление Yandex-аккаунта</b>\n\n"
        "1) Отправь <b>название аккаунта</b> (LABEL)\n"
        "Пример: <code>YA_ACC_1</code>\n\n"
        "Дальше я спрошу дату окончания Plus и 3 ссылки.",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(AdminFSM.waiting_label)
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
    await state.set_state(AdminFSM.waiting_plus_end)

    await message.answer(
        "📅 <b>До какого числа подписка активна?</b>\n\n"
        "Введи в формате:\n"
        "<code>9 февраля 2026</code>\n\n"
        "Это дата окончания Plus на этом аккаунте (вводишь вручную).",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminFSM.waiting_plus_end)
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
        await message.answer(
            "❌ Сессия сбилась. Нажми «➕ Добавить Yandex-аккаунт» ещё раз.",
            reply_markup=kb_admin_menu(),
        )
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
    await state.set_state(AdminFSM.waiting_links)

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


@router.message(AdminFSM.waiting_links)
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
        await message.answer(
            "❌ Сессия сбилась. Нажми «➕ Добавить Yandex-аккаунт» ещё раз.",
            reply_markup=kb_admin_menu(),
        )
        return

    async with session_scope() as session:
        acc = await session.scalar(select(YandexAccount).where(YandexAccount.label == label).limit(1))
        if not acc:
            await state.clear()
            await message.answer("❌ Аккаунт не найден. Начни добавление заново.", reply_markup=kb_admin_menu())
            return

        # Upsert 3 slots. IMPORTANT: do not overwrite issued/burned (S1).
        for idx, link in enumerate(lines, start=1):
            slot = await session.scalar(
                select(YandexInviteSlot)
                .where(
                    YandexInviteSlot.yandex_account_id == acc.id,
                    YandexInviteSlot.slot_index == idx,
                )
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

        lines: list[str] = ["📋 <b>Yandex аккаунты / слоты</b>\n"]
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


@router.callback_query(lambda c: c.data == "admin:yandex:edit")
async def admin_yandex_edit(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminFSM.edit_waiting_label)

    await cb.message.edit_text(
        "✏️ <b>Редактирование Yandex-аккаунта</b>\n\n"
        "Отправь <b>LABEL</b> аккаунта, который хочешь изменить.\n"
        "Пример: <code>YA_ACC_1</code>",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(AdminFSM.edit_waiting_label)
async def admin_yandex_edit_waiting_label(message: Message, state: FSMContext) -> None:
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

    async with session_scope() as session:
        acc = await session.scalar(select(YandexAccount).where(YandexAccount.label == label).limit(1))
        if not acc:
            await message.answer("❌ Аккаунт не найден. Проверь LABEL.", reply_markup=kb_admin_menu())
            return

        await state.update_data(edit_label=label)

        await state.set_state(AdminFSM.edit_waiting_plus_end)
        await message.answer(
            "📅 <b>Новая дата окончания Plus</b>\n\n"
            f"Сейчас: <code>{_fmt_plus_end_at(acc.plus_end_at)}</code>\n\n"
            "Введи новую дату в формате:\n"
            "<code>9 февраля 2026</code>\n\n"
            "Или отправь <code>-</code> чтобы не менять дату.",
            parse_mode="HTML",
            reply_markup=kb_admin_menu(),
        )


@router.message(AdminFSM.edit_waiting_plus_end)
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

    await state.set_state(AdminFSM.edit_waiting_links)
    await message.answer(
        "🔗 <b>Обновить ссылки слотов (опционально)</b>\n\n"
        "Если хочешь заменить ссылки — отправь 3 строки (слоты 1..3).\n"
        "⚠️ Будут обновлены только слоты со статусом <b>free</b>.\n"
        "Issued/Burned слоты не трогаем (S1).\n\n"
        "Если не нужно — отправь <code>-</code>.",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminFSM.edit_waiting_links)
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
        await message.answer(
            "❌ Нужно ровно 3 строки (или отправь <code>-</code>).",
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

        updated = 0
        skipped = 0
        for idx, link in enumerate(lines, start=1):
            slot = await session.scalar(
                select(YandexInviteSlot)
                .where(
                    YandexInviteSlot.yandex_account_id == acc.id,
                    YandexInviteSlot.slot_index == idx,
                )
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


# =========================================================
# RESET USER (admin)
# =========================================================

@router.callback_query(lambda c: c.data == "admin:reset:user")
async def admin_reset_user(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminFSM.reset_waiting_tg_id)

    await cb.message.edit_text(
        "🧨 <b>Сброс пользователя</b>\n\n"
        "Введи TG ID пользователя (число).\n"
        "Сбросит подписку, VPN-пир и Yandex Plus (в БД).",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )
    await cb.answer()


@router.message(AdminFSM.reset_waiting_tg_id)
async def admin_reset_user_tg(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("❌ TG ID должен быть числом.", reply_markup=kb_admin_menu())
        return

    tg_id = int(raw)
    now = utcnow()

    async with session_scope() as session:
        # subscription
        sub = await session.scalar(select(Subscription).where(Subscription.tg_id == tg_id).limit(1))
        if sub:
            sub.end_at = now
            sub.is_active = False
            sub.status = "inactive"

        # vpn peers
        peers = (await session.scalars(select(VpnPeer).where(VpnPeer.tg_id == tg_id))).all()
        for p in peers:
            p.is_active = False
            p.revoked_at = now

        # yandex membership: clear so cabinet doesn't show stale family/slot
        ym = await session.scalar(
            select(YandexMembership).where(YandexMembership.tg_id == tg_id).order_by(YandexMembership.id.desc()).limit(1)
        )
        if ym:
            ym.status = "pending"
            ym.yandex_account_id = None
            ym.account_label = None
            ym.slot_index = None
            ym.invite_link = None
            ym.invite_issued_at = None
            ym.invite_expires_at = None
            ym.removed_at = now
            ym.updated_at = now

        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ Пользователь <code>{tg_id}</code> сброшен.",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


# =========================================================
# KICK REPORT (manual removal reminder) + MARK REMOVED
# =========================================================

@router.callback_query(lambda c: c.data == "admin:kick:report")
async def admin_kick_report(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    today = utcnow().date()
    now = utcnow()

    async with session_scope() as session:
        q = (
            select(YandexMembership, Subscription)
            .join(Subscription, Subscription.tg_id == YandexMembership.tg_id)
            .where(
                YandexMembership.status == "active",
                Subscription.end_at.is_not(None),
            )
        )
        rows = (await session.execute(q)).all()

        due: list[tuple[YandexMembership, Subscription]] = []
        for ym, sub in rows:
            end_at = sub.end_at
            if not end_at:
                continue
            if end_at.tzinfo is None:
                end_at = end_at.replace(tzinfo=timezone.utc)
            if end_at.date() <= today:
                due.append((ym, sub))

    if not due:
        await cb.message.edit_text(
            "📣 <b>Отчёт по исключению</b>\n\nСегодня участников для исключения нет.",
            parse_mode="HTML",
            reply_markup=kb_admin_menu(),
        )
        await cb.answer()
        return

    lines = ["📣 <b>Сегодня пора исключить следующих участников из семей:</b>\n"]
    for i, (ym, sub) in enumerate(due, start=1):
        vpn_peer = "Отключен"
        # basic check: any active peer
        async with session_scope() as session:
            active_peer_cnt = await session.scalar(
                select(func.count(VpnPeer.id)).where(VpnPeer.tg_id == ym.tg_id, VpnPeer.is_active.is_(True))
            )
            if int(active_peer_cnt or 0) > 0:
                vpn_peer = "Включен"

        created = ym.created_at or now
        age_days = (now.date() - created.date()).days
        lines.append(
            f"#{i}\n"
            f"Пользователь ID TG: <code>{ym.tg_id}</code>\n"
            f"Дата приобретения подписки на сервис: <code>{_fmt_dt(sub.created_at)}</code>\n"
            f"Дата окончания подписки на сервис: <code>{_fmt_dt(sub.end_at)}</code>\n"
            f"Наименование семьи (label): <code>{ym.account_label or '—'}</code>\n"
            f"Номер слота: <code>{ym.slot_index or '—'}</code>\n"
            f"VPN: {vpn_peer}\n"
            f"Подписка: {'Продлевалась' if (sub.end_at and sub.end_at > now) else 'Не продлевалась'}\n"
            f"Пользователь с нами: <code>{age_days}</code> дней\n"
        )

    await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb_admin_menu())
    await cb.answer()


@router.callback_query(lambda c: c.data == "admin:kick:mark")
async def admin_kick_mark_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminFSM.kick_waiting_tg_id)

    await cb.message.edit_text(
        "✅ <b>Отметить как исключённого</b>\n\n"
        "Введи TG ID пользователя, которого ты уже исключил из семьи.\n"
        "Это нужно для учёта (в БД проставится removed).",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )
    await cb.answer()


@router.message(AdminFSM.kick_waiting_tg_id)
async def admin_kick_mark_apply(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("❌ TG ID должен быть числом.", reply_markup=kb_admin_menu())
        return

    tg_id = int(raw)
    now = utcnow()

    async with session_scope() as session:
        ym = await session.scalar(
            select(YandexMembership)
            .where(YandexMembership.tg_id == tg_id)
            .order_by(YandexMembership.id.desc())
            .limit(1)
        )
        if not ym:
            await state.clear()
            await message.answer("❌ Membership не найден.", reply_markup=kb_admin_menu())
            return

        ym.status = "removed"
        ym.removed_at = now
        ym.updated_at = now

        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ Отмечено: <code>{tg_id}</code> исключён.",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


# =========================================================
# PAYOUT ADMIN (optional): mark payout paid / reject
# =========================================================

@router.callback_query(lambda c: c.data and c.data.startswith("admin:payout:"))
async def admin_payout_actions(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    # Format: admin:payout:paid:<id> OR admin:payout:reject:<id>
    parts = (cb.data or "").split(":")
    if len(parts) != 4:
        await cb.answer()
        return

    action = parts[2]
    req_id = int(parts[3])

    async with session_scope() as session:
        req = await session.get(PayoutRequest, req_id)
        if not req:
            await cb.answer("not found", show_alert=True)
            return

        now = utcnow()
        if action == "paid":
            req.status = "paid"
            req.processed_at = now
            items = (await session.scalars(
                select(ReferralEarning).where(
                    ReferralEarning.payout_request_id == req_id,
                    ReferralEarning.status == "reserved",
                )
            )).all()
            for e in items:
                e.status = "paid"
                e.paid_at = now
            await session.commit()
            await cb.answer("✅ marked paid")
        elif action == "reject":
            req.status = "rejected"
            req.processed_at = now
            items = (await session.scalars(
                select(ReferralEarning).where(
                    ReferralEarning.payout_request_id == req_id,
                    ReferralEarning.status == "reserved",
                )
            )).all()
            for e in items:
                e.status = "available"
                e.payout_request_id = None
            await session.commit()
            await cb.answer("✅ rejected")
        else:
            await cb.answer()
