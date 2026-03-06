from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot.auth import is_owner
from app.bot.keyboards import kb_admin_menu
from app.db.models.subscription import Subscription
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope

router = Router()


class AdminKickFSM(StatesGroup):
    waiting_tg_id = State()


def _fmt_dt_short(dt: datetime | None) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


@router.callback_query(lambda c: c.data == "admin:kick:report")
async def admin_kick_report(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    now = datetime.now(timezone.utc)

    # Show expiring subscriptions even if the user isn't currently added to a Yandex family.
    # Use only the latest active (removed_at IS NULL) membership row per TG to avoid duplicates.
    async with session_scope() as session:
        latest_active_ids = (
            select(func.max(YandexMembership.id).label("id"))
            .where(YandexMembership.removed_at.is_(None))
            .group_by(YandexMembership.tg_id)
            .subquery()
        )

        base = (
            select(Subscription, YandexMembership)
            .outerjoin(latest_active_ids, latest_active_ids.c.id.is_not(None))
            .outerjoin(YandexMembership, YandexMembership.id == latest_active_ids.c.id)
            .where(Subscription.end_at.is_not(None))
        )

        due_rows = (await session.execute(
            base.where(Subscription.end_at <= now)
            .order_by(Subscription.end_at.asc(), Subscription.tg_id.asc())
            .limit(200)
        )).all()

        soon_rows = (await session.execute(
            base.where(Subscription.end_at > now)
            .order_by(Subscription.end_at.asc(), Subscription.tg_id.asc())
            .limit(30)
        )).all()

    lines: list[str] = []

    if not due_rows:
        lines.append("✅ <b>Сегодня участников для исключения нет.</b>")
    else:
        lines.append("🚨 <b>Сегодня пора исключить следующих участников:</b>\n")

        for i, (sub, m) in enumerate(due_rows, start=1):
            days_with_us = "—"
            try:
                if sub.created_at:
                    created = sub.created_at if sub.created_at.tzinfo else sub.created_at.replace(tzinfo=timezone.utc)
                    days_with_us = f"{max((now - created).days, 0)} дн."
            except Exception:
                pass

            # VPN status: if you later add an explicit flag on Subscription, show it.
            vpn_state = "—"
            try:
                vpn_state = "Включен" if bool(getattr(sub, "vpn_enabled")) else "Отключен"
            except Exception:
                vpn_state = "—"

            fam = (m.family_label if m else None) or "—"
            slot = (m.slot_index if m else None) or "—"
            membership_state = "В семье" if m else "❗️Не добавлен в семью"

            lines.append(
                f"<b>#{i}</b>\n"
                f"Пользователь ID TG: <code>{sub.tg_id}</code>\n"
                f"Дата приобретения подписки на сервис: <code>{_fmt_dt_short(sub.created_at)}</code>\n"
                f"Дата окончания подписки на сервис: <code>{_fmt_dt_short(sub.end_at)}</code>\n"
                f"Статус Яндекс семьи: <b>{membership_state}</b>\n"
                f"Наименование семьи (label): <code>{fam}</code>\n"
                f"Номер слота: <code>{slot}</code>\n"
                f"VPN: <b>{vpn_state}</b>\n"
                f"Подписка: <b>{'Продлевалась' if (sub.end_at and sub.created_at and sub.end_at > sub.created_at) else 'Не продлевалась'}</b>\n"
                f"Пользователь с нами: <b>{days_with_us}</b>\n"
            )

    if soon_rows:
        lines.append("\n📅 <b>Ближайшие к исключению (по дате окончания):</b>")
        for sub, m in soon_rows[:20]:
            if not sub.end_at:
                continue
            dt = sub.end_at if sub.end_at.tzinfo else sub.end_at.replace(tzinfo=timezone.utc)
            days_left = max((dt - now).days, 0)
            fam = (m.family_label if m else None) or "—"
            lines.append(
                f"• <code>{sub.tg_id}</code> — до <code>{_fmt_dt_short(sub.end_at)}</code> (через {days_left} дн.) — семья: <code>{fam}</code>"
            )

    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_menu(), parse_mode="HTML")
    await cb.answer()


@router.callback_query(lambda c: c.data == "admin:kick:mark")
async def admin_kick_mark_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminKickFSM.waiting_tg_id)

    await cb.message.edit_text(
        "🧾 <b>Отметить пользователя исключённым</b>\n\n"
        "Отправь <b>ID Telegram</b> пользователя (число).\n"
        "Я найду его последнюю запись YandexMembership без removed_at и помечу removed_at=сейчас.",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(AdminKickFSM.waiting_tg_id)
async def admin_kick_mark_finish(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    txt = (message.text or "").strip()
    try:
        tg_id = int(txt)
    except Exception:
        await message.answer("❌ Нужен числовой TG ID. Попробуй ещё раз.", reply_markup=kb_admin_menu())
        return

    now = datetime.now(timezone.utc)

    async with session_scope() as session:
        m = await session.scalar(
            select(YandexMembership)
            .where(
                YandexMembership.tg_id == tg_id,
                YandexMembership.removed_at.is_(None),
            )
            .order_by(YandexMembership.id.desc())
            .limit(1)
        )
        if not m:
            await state.clear()
            await message.answer(
                "ℹ️ Не нашёл активного участника (removed_at пустой) для этого TG ID.",
                reply_markup=kb_admin_menu(),
            )
            return

        m.removed_at = now
        m.status = "removed"
        fam = m.family_label
        slot = m.slot_index
        await session.commit()

    await state.clear()
    await message.answer(
        "✅ Отмечено как исключённый.\n\n"
        f"TG: <code>{tg_id}</code>\n"
        f"Семья: <code>{fam or '—'}</code>\n"
        f"Слот: <code>{slot or '—'}</code>",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )
