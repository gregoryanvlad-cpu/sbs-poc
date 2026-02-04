from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from app.bot.auth import is_owner
from app.bot.keyboards import kb_admin_menu
from app.bot.ui import utcnow
from app.core.config import settings
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vpn_peer import VpnPeer
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_invite_slot import YandexInviteSlot
from app.db.models.yandex_membership import YandexMembership
from app.db.models.payout_request import PayoutRequest
from app.db.models.payment import Payment
from app.db.models.referral_earning import ReferralEarning
from app.db.models.referral import Referral
from app.db.session import session_scope
from app.services.referrals.service import referral_service

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


def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


async def _vpn_is_enabled(session, tg_id: int) -> bool:
    """
    VPN включен = есть последний peer и он is_active=True и revoked_at is NULL.
    """
    q = (
        select(VpnPeer)
        .where(VpnPeer.tg_id == tg_id)
        .order_by(VpnPeer.id.desc())
        .limit(1)
    )
    peer = await session.scalar(q)
    return bool(peer and bool(getattr(peer, "is_active", False)) and peer.revoked_at is None)


# ==========================
# FSM
# ==========================

class AdminYandexFSM(StatesGroup):
    waiting_label = State()           # add: label
    waiting_plus_end = State()        # add: plus_end_at
    waiting_links = State()           # add: 3 links

    edit_waiting_label = State()      # edit: which account label
    edit_waiting_plus_end = State()   # edit: new date or skip
    edit_waiting_links = State()      # edit: new links (optional)

    kick_waiting_tg_id = State()
    reset_waiting_tg_id = State()


class AdminReferralMintFSM(StatesGroup):
    waiting_target_tg_id = State()
    waiting_amount = State()
    waiting_status = State()  # available / pending / paid


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
        "⚠️ Исключение пользователей из семьи делается вручную.\n",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


# ==========================
# TEST: MINT REFERRAL BALANCE
# ==========================

@router.callback_query(lambda c: c.data == "admin:ref:mint")
async def admin_ref_mint(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminReferralMintFSM.waiting_target_tg_id)
    await cb.message.edit_text(
        "💰 <b>Накрутка реф-баланса (TEST)</b>\n\n"
        "Отправь TG ID получателя.\n"
        "Или отправь <code>-</code>, чтобы накрутить себе.",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )
    await cb.answer()


@router.message(AdminReferralMintFSM.waiting_target_tg_id)
async def admin_ref_mint_target(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    txt = (message.text or "").strip()
    if txt == "-":
        target = int(message.from_user.id)
    else:
        try:
            target = int(txt)
        except Exception:
            await message.answer(
                "❌ Нужно число (TG ID) или <code>-</code>.",
                parse_mode="HTML",
                reply_markup=kb_admin_menu(),
            )
            return

    await state.update_data(target_tg_id=target)
    await state.set_state(AdminReferralMintFSM.waiting_amount)
    await message.answer(
        "💸 Введи сумму в рублях (целое число).\n"
        "Пример: <code>150</code>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminReferralMintFSM.waiting_amount)
async def admin_ref_mint_amount(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    try:
        amount = int((message.text or "").strip())
    except Exception:
        amount = 0

    if amount <= 0 or amount > 1_000_000:
        await message.answer("❌ Сумма должна быть > 0 и адекватной.", reply_markup=kb_admin_menu())
        return

    await state.update_data(amount_rub=amount)
    await state.set_state(AdminReferralMintFSM.waiting_status)
    await message.answer(
        "🧾 Выбери статус начисления (введи текстом):\n\n"
        "• <code>available</code> — сразу доступно к выводу\n"
        "• <code>pending</code> — на холде (как после реальной оплаты)\n"
        "• <code>paid</code> — сразу отмечено как выплаченное\n",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminReferralMintFSM.waiting_status)
async def admin_ref_mint_status(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    status = (message.text or "").strip().lower()
    if status not in {"available", "pending", "paid"}:
        await message.answer("❌ Статус должен быть: available / pending / paid", reply_markup=kb_admin_menu())
        return

    data = await state.get_data()
    target = int(data.get("target_tg_id") or message.from_user.id)
    amount = int(data.get("amount_rub") or 0)

    now = utcnow()

    # dummy referred: уникальный, чтобы не конфликтовать с реальными юзерами
    dummy_referred = int(f"9{target}") if len(str(target)) < 9 else target + 9_000_000_000

    async with session_scope() as session:
        # ensure dummy user exists
        dummy_user = await session.get(User, int(dummy_referred))
        if not dummy_user:
            dummy_user = User(tg_id=int(dummy_referred))
            session.add(dummy_user)
            await session.flush()

        # dummy successful payment
        pay = Payment(
            tg_id=int(dummy_referred),
            amount=amount,
            currency="RUB",
            provider="admin_mint",
            status="success",
            paid_at=now,
        )
        session.add(pay)
        await session.flush()

        # ensure referral relation exists so cabinet shows it too
        ref = await session.scalar(select(Referral).where(Referral.referred_tg_id == int(dummy_referred)).limit(1))
        if not ref:
            ref = Referral(
                referrer_tg_id=target,
                referred_tg_id=dummy_referred,
                status="active",
                first_payment_id=pay.id,
                activated_at=now,
            )
            session.add(ref)
            await session.flush()

        hold_days = int(getattr(settings, "referral_hold_days", 7) or 7)
        available_at = (now + timedelta(days=hold_days)) if status == "pending" else None

        e = ReferralEarning(
            referrer_tg_id=target,
            referred_tg_id=dummy_referred,
            payment_id=pay.id,
            payment_amount_rub=amount,
            percent=100,
            earned_rub=amount,
            status=status,
            available_at=available_at,
            paid_at=now if status == "paid" else None,
        )
        session.add(e)
        await session.commit()

    await state.clear()
    await message.answer(
        "✅ Накрутил начисление.\n\n"
        f"Получатель: <code>{target}</code>\n"
        f"Сумма: <b>{amount}</b> RUB\n"
        f"Статус: <code>{status}</code>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


# ==========================
# Admin: "Кого исключить сегодня" (report)
# ==========================

@router.callback_query(lambda c: c.data == "admin:kick:report")
async def admin_kick_report(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    now = utcnow()

    async with session_scope() as session:
        q = (
            select(YandexMembership, Subscription)
            .join(Subscription, Subscription.tg_id == YandexMembership.tg_id, isouter=True)
            .where(
                YandexMembership.coverage_end_at.is_not(None),
                YandexMembership.coverage_end_at <= now,
                YandexMembership.removed_at.is_(None),
            )
            .order_by(YandexMembership.coverage_end_at.asc(), YandexMembership.id.asc())
            .limit(100)
        )
        rows = (await session.execute(q)).all()

        if not rows:
            await cb.message.edit_text(
                "✅ Сегодня участников для исключения нет.",
                reply_markup=kb_admin_menu(),
            )
            await cb.answer()
            return

        lines = ["📋 <b>Сегодня пора исключить следующих участников:</b>\n"]
        for i, (m, sub) in enumerate(rows, start=1):
            vpn_on = await _vpn_is_enabled(session, int(m.tg_id))
            sub_end = getattr(sub, "end_at", None) if sub else None

            renewed = False
            if sub_end and m.coverage_end_at:
                try:
                    renewed = (sub_end > m.coverage_end_at)
                except Exception:
                    renewed = False

            lines.append(f"<b>#{i}</b>")
            lines.append(f"Пользователь ID TG: <code>{m.tg_id}</code>")
            lines.append(f"Дата окончания подписки на сервис: <code>{_fmt_dt(m.coverage_end_at)}</code>")
            lines.append(f"Наименование семьи (label): <code>{m.account_label or '—'}</code>")
            lines.append(f"Номер слота: <code>{m.slot_index or '—'}</code>")
            lines.append(f"VPN: <b>{'Включен' if vpn_on else 'Отключен'}</b>")
            lines.append(f"Подписка: <b>{'Продлевалась' if renewed else 'Не продлевалась'}</b>")
            lines.append("")

    await cb.message.edit_text("\n".join(lines).strip(), reply_markup=kb_admin_menu(), parse_mode="HTML")
    await cb.answer()


# ==========================
# Admin: "Отметить исключение" (mark removed)
# ==========================

@router.callback_query(lambda c: c.data == "admin:kick:mark")
async def admin_kick_mark(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminYandexFSM.kick_waiting_tg_id)

    await cb.message.edit_text(
        "🧾 <b>Отметить исключение из семьи</b>\n\n"
        "Отправь <b>Telegram ID</b> пользователя (числом).\n"
        "Я найду последнюю запись YandexMembership и отмечу removed_at.\n\n"
        "Пример:\n<code>123456789</code>",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(AdminYandexFSM.kick_waiting_tg_id)
async def admin_kick_mark_tg_id(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer(
            "❌ Нужен числовой Telegram ID. Пример: <code>123456789</code>",
            parse_mode="HTML",
            reply_markup=kb_admin_menu(),
        )
        return

    tg_id = int(raw)
    now = utcnow()

    async with session_scope() as session:
        m = await session.scalar(
            select(YandexMembership)
            .where(YandexMembership.tg_id == tg_id)
            .order_by(YandexMembership.id.desc())
            .limit(1)
        )
        if not m:
            await state.clear()
            await message.answer("❌ YandexMembership для этого TG ID не найден.", reply_markup=kb_admin_menu())
            return

        m.removed_at = now
        m.updated_at = now
        await session.commit()

        vpn_on = await _vpn_is_enabled(session, tg_id)
        fam_label = m.account_label or "—"
        slot_idx = m.slot_index or "—"

    await state.clear()
    await message.answer(
        "✅ Отмечено.\n\n"
        f"TG ID: <code>{tg_id}</code>\n"
        f"Семья: <code>{fam_label}</code>\n"
        f"Слот: <code>{slot_idx}</code>\n"
        f"VPN: <b>{'Включен' if vpn_on else 'Отключен'}</b>\n"
        f"removed_at: <code>{_fmt_dt(now)}</code>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


# ==========================
# Admin: RESET USER (TEST)
# ==========================

@router.callback_query(lambda c: c.data == "admin:reset:user")
async def admin_reset_user(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminYandexFSM.reset_waiting_tg_id)

    await cb.message.edit_text(
        "🧨 <b>Сбросить пользователя (TEST)</b>\n\n"
        "Отправь <b>Telegram ID</b> пользователя (числом).\n\n"
        "Я сделаю:\n"
        "— отключу VPN (peer'ы)\n"
        "— завершу подписку (end_at = сейчас)\n"
        "— помечу YandexMembership как removed\n\n"
        "Пример:\n<code>123456789</code>",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(AdminYandexFSM.reset_waiting_tg_id)
async def admin_reset_user_tg_id(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer(
            "❌ Нужен числовой Telegram ID. Пример: <code>123456789</code>",
            parse_mode="HTML",
            reply_markup=kb_admin_menu(),
        )
        return

    tg_id = int(raw)
    now = utcnow()

    y_info = {"label": "—", "slot": "—"}

    async with session_scope() as session:
        # 1) VPN: деактивируем все peer'ы пользователя (best-effort)
        try:
            peers = (
                await session.scalars(
                    select(VpnPeer).where(VpnPeer.tg_id == tg_id).order_by(VpnPeer.id.desc())
                )
            ).all()
            for p in peers:
                try:
                    p.is_active = False
                except Exception:
                    pass
                try:
                    p.revoked_at = now
                except Exception:
                    pass
        except Exception:
            pass

        # 2) Subscription: завершаем (best-effort)
        try:
            sub = await session.scalar(select(Subscription).where(Subscription.tg_id == tg_id).limit(1))
            if sub:
                try:
                    sub.end_at = now
                except Exception:
                    pass
                try:
                    sub.is_active = False
                except Exception:
                    pass
                try:
                    sub.status = "expired"
                except Exception:
                    pass
        except Exception:
            pass

        # 3) YandexMembership: помечаем removed (best-effort)
        try:
            m = await session.scalar(
                select(YandexMembership)
                .where(YandexMembership.tg_id == tg_id)
                .order_by(YandexMembership.id.desc())
                .limit(1)
            )
            if m:
                y_info["label"] = getattr(m, "account_label", None) or "—"
                y_info["slot"] = str(getattr(m, "slot_index", None) or "—")
                try:
                    m.status = "removed"
                except Exception:
                    pass
                try:
                    m.removed_at = now
                except Exception:
                    pass
                try:
                    m.updated_at = now
                except Exception:
                    pass
        except Exception:
            pass

        # 4) User: чистим flow_state/flow_data (best-effort)
        try:
            u = await session.get(User, tg_id)
            if u:
                try:
                    u.flow_state = None
                    u.flow_data = None
                except Exception:
                    pass
        except Exception:
            pass

        await session.commit()

    await state.clear()

    await message.answer(
        "✅ <b>Сброс выполнен</b>\n\n"
        f"TG ID: <code>{tg_id}</code>\n"
        f"Yandex семья: <code>{y_info['label']}</code>\n"
        f"Yandex слот: <code>{y_info['slot']}</code>\n"
        f"Время: <code>{_fmt_dt(now)}</code>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


# ==========================
# Legacy strikes button — now stub
# ==========================

@router.callback_query(lambda c: c.data == "admin:forgive:user")
async def admin_forgive_stub(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer("Strikes больше не используются в ручном режиме.", show_alert=True)


# =========================================================
# ADD ACCOUNT: label -> plus_end_at -> 3 links
# =========================================================

@router.callback_query(lambda c: c.data == "admin:yandex:add")
async def admin_yandex_add(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminYandexFSM.waiting_label)

    await cb.message.edit_text(
        "➕ <b>Добавление Yandex-аккаунта</b>\n\n"
        "1) Отправь <b>название аккаунта</b> (LABEL)\n"
        "Пример: <code>YA_ACC_1</code>\n\n"
        "Дальше я спрошу дату окончания Plus и 3 ссылки.",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
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
                max_slots=4,  # legacy field, keep
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
# PAYOUT REQUESTS (manual processing)
# ==========================

@router.callback_query(lambda c: c.data == "admin:payouts:list")
async def admin_payouts_list(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    async with session_scope() as session:
        reqs = (await session.scalars(
            select(PayoutRequest)
            .order_by(PayoutRequest.id.desc())
            .limit(15)
        )).all()

    if not reqs:
        await cb.message.edit_text(
            "💸 <b>Заявки на вывод</b>\n\nПока заявок нет.",
            reply_markup=kb_admin_menu(),
            parse_mode="HTML",
        )
        await cb.answer()
        return

    lines = ["💸 <b>Заявки на вывод (последние)</b>\n"]
    kb_rows: list[list[InlineKeyboardButton]] = []
    for r in reqs:
        lines.append(f"#{r.id} | tg: <code>{r.tg_id}</code> | {r.amount_rub}₽ | <b>{r.status}</b>")
        if r.status in ("created", "approved"):
            kb_rows.append([
                InlineKeyboardButton(text=f"✅ Approve #{r.id}", callback_data=f"admin:payouts:approve:{r.id}"),
                InlineKeyboardButton(text=f"💰 Paid #{r.id}", callback_data=f"admin:payouts:paid:{r.id}"),
            ])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await cb.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("admin:payouts:approve:"))
async def admin_payouts_approve(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    req_id = int(cb.data.split(":")[-1])
    async with session_scope() as session:
        req = await session.get(PayoutRequest, req_id)
        if not req:
            await cb.answer("Не найдено", show_alert=True)
            return
        if req.status != "created":
            await cb.answer("Статус уже изменён", show_alert=True)
            return
        req.status = "approved"
        await session.commit()
    await cb.answer("✅ Approved")


@router.callback_query(lambda c: c.data and c.data.startswith("admin:payouts:paid:"))
async def admin_payouts_paid(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    req_id = int(cb.data.split(":")[-1])
    async with session_scope() as session:
        req = await session.get(PayoutRequest, req_id)
        if not req:
            await cb.answer("Не найдено", show_alert=True)
            return
        if req.status not in ("created", "approved"):
            await cb.answer("Нельзя отметить оплаченным", show_alert=True)
            return

        # ✅ FIX: правильный аргумент — request_id
        await referral_service.mark_payout_paid(session, request_id=req_id)
        await session.commit()

    await cb.answer("💰 Marked as paid")
