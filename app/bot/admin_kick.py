from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.bot.auth import is_owner
from app.bot.keyboards import kb_admin_menu
from app.db.models.subscription import Subscription
from app.db.models.vpn_peer import VpnPeer
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


def _sub_start_dt(sub: Subscription) -> datetime | None:
    """Return subscription start timestamp in a backwards-compatible way.

    The current Subscription model uses `start_at` (not `created_at`). Some
    older versions referenced `created_at`, so we resolve robustly.
    """
    return getattr(sub, "created_at", None) or getattr(sub, "start_at", None)


def _fmt_hours_left(target: datetime, now: datetime) -> str:
    """Human-friendly delta string in hours/minutes."""
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delta = target - now
    total_sec = int(delta.total_seconds())
    sign = 1
    if total_sec < 0:
        sign = -1
        total_sec = -total_sec
    hours = total_sec // 3600
    mins = (total_sec % 3600) // 60
    if sign > 0:
        return f"через {hours} ч {mins} мин"
    return f"просрочено на {hours} ч {mins} мин"


def _renewal_text(*, sub_end_at: datetime | None, coverage_end_at: datetime | None, has_membership: bool) -> str:
    """Return a truthful renewal text.

    We consider 'Продлевалась' only when user has a membership row with
    coverage_end_at and subscription end is AFTER that coverage end.
    If user is not in Yandex family, the renewal concept is not applicable.
    """
    if not has_membership:
        return "—"
    if not sub_end_at or not coverage_end_at:
        return "Не продлевалась"
    se = sub_end_at if sub_end_at.tzinfo else sub_end_at.replace(tzinfo=timezone.utc)
    ce = coverage_end_at if coverage_end_at.tzinfo else coverage_end_at.replace(tzinfo=timezone.utc)
    return "Продлевалась" if se > ce else "Не продлевалась"


@router.callback_query(lambda c: c.data == "admin:kick:report")
async def admin_kick_report(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    now = datetime.now(timezone.utc)

    # Show expiring subscriptions even if the user isn't currently added to a Yandex family.
    # Use only the latest active (removed_at IS NULL) membership row per TG to avoid duplicates.
    async with session_scope() as session:
        # Use an aliased membership table to avoid ORM join ambiguity on Postgres/SQLAlchemy.
        YM = aliased(YandexMembership)

        latest_active_ids = (
            select(
                YM.tg_id.label("tg_id"),
                func.max(YM.id).label("id"),
            )
            .where(YM.removed_at.is_(None))
            .group_by(YM.tg_id)
            .subquery()
        )

        base = (
            select(Subscription, YM)
            .select_from(Subscription)
            .outerjoin(latest_active_ids, latest_active_ids.c.tg_id == Subscription.tg_id)
            .outerjoin(YM, YM.id == latest_active_ids.c.id)
            .where(Subscription.end_at.is_not(None))
        )

        due_rows = (
            await session.execute(
                base.where(Subscription.end_at <= now)
                .order_by(Subscription.end_at.asc(), Subscription.tg_id.asc())
                .limit(200)
            )
        ).all()

        soon_rows = (
            await session.execute(
                base.where(Subscription.end_at > now)
                .order_by(Subscription.end_at.asc(), Subscription.tg_id.asc())
                .limit(30)
            )
        ).all()

        # Preload VPN peer states in one query to avoid per-user DB chatter.
        tg_ids: set[int] = {int(sub.tg_id) for sub, _m in (due_rows + soon_rows)}
        peer_states: dict[int, dict[str, bool]] = {tid: {"any": False, "active": False} for tid in tg_ids}
        if tg_ids:
            peer_rows = await session.execute(
                select(VpnPeer.tg_id, VpnPeer.is_active).where(VpnPeer.tg_id.in_(list(tg_ids)))
            )
            for tg_id, is_active in peer_rows.all():
                tid = int(tg_id)
                st = peer_states.setdefault(tid, {"any": False, "active": False})
                st["any"] = True
                if bool(is_active):
                    st["active"] = True

    lines: list[str] = []

    if not due_rows:
        lines.append("✅ <b>Сегодня участников для исключения нет.</b>")
    else:
        lines.append("🚨 <b>Сегодня пора исключить следующих участников:</b>\n")

        for i, (sub, m) in enumerate(due_rows, start=1):
            days_with_us = "—"
            try:
                started_at = _sub_start_dt(sub)
                if started_at:
                    created = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
                    days_with_us = f"{max((now - created).days, 0)} дн."
            except Exception:
                pass

            # VPN status (WireGuard):
            # - No peers at all -> not activated
            # - Peers exist but none active -> disabled
            # - Any active peer -> enabled
            st = peer_states.get(int(sub.tg_id), {"any": False, "active": False})
            if not st["any"]:
                vpn_state = "Не активирован"
            elif st["active"]:
                vpn_state = "Включен"
            else:
                vpn_state = "Отключен"

            fam = (
                (getattr(m, "account_label", None) if m else None)
                or (getattr(m, "family_label", None) if m else None)
                or "—"
            )
            slot = (m.slot_index if m else None) or "—"
            membership_state = "В семье" if m else "❗️Не добавлен в семью"

            started_at = _sub_start_dt(sub)
            end_at = sub.end_at if sub.end_at else None
            hours_line = "—"
            if end_at:
                hours_line = _fmt_hours_left(end_at, now)

            renewal = _renewal_text(
                sub_end_at=sub.end_at,
                coverage_end_at=getattr(m, "coverage_end_at", None) if m else None,
                has_membership=bool(m),
            )

            lines.append(
                f"<b>#{i}</b>\n"
                f"Пользователь ID TG: <code>{sub.tg_id}</code>\n"
                f"Дата приобретения подписки на сервис: <code>{_fmt_dt_short(started_at)}</code>\n"
                f"Дата окончания подписки на сервис: <code>{_fmt_dt_short(sub.end_at)}</code>\n"
                f"Статус Яндекс семьи: <b>{membership_state}</b>\n"
                f"Наименование семьи (label): <code>{fam}</code>\n"
                f"Номер слота: <code>{slot}</code>\n"
                f"VPN: <b>{vpn_state}</b>\n"
                f"Исключить: <b>{hours_line}</b>\n"
                f"Продление: <b>{renewal}</b>\n"
                f"Пользователь с нами: <b>{days_with_us}</b>\n"
            )

    if soon_rows:
        lines.append("\n📅 <b>Ближайшие к исключению (по дате окончания):</b>")
        for sub, m in soon_rows[:20]:
            if not sub.end_at:
                continue
            dt = sub.end_at if sub.end_at.tzinfo else sub.end_at.replace(tzinfo=timezone.utc)
            # Prefer hours/minutes for near-term expirations.
            seconds_left = int((dt - now).total_seconds())
            if seconds_left <= 0:
                when = "сейчас"
            elif seconds_left < 48 * 3600:
                when = _fmt_hours_left(dt, now)
            else:
                days_left = max((dt - now).days, 0)
                when = f"через {days_left} дн."
            fam = (
                (getattr(m, "account_label", None) if m else None)
                or (getattr(m, "family_label", None) if m else None)
                or "—"
            )
            lines.append(
                f"• <code>{sub.tg_id}</code> — до <code>{_fmt_dt_short(sub.end_at)}</code> ({when}) — семья: <code>{fam}</code>"
            )

    text = "\n".join(lines)

    # Telegram message limit is 4096 chars. If report is too long, show a short
    # summary and attach the full report as a file.
    try:
        if len(text) <= 3900:
            await cb.message.edit_text(text, reply_markup=kb_admin_menu(), parse_mode="HTML")
        else:
            from aiogram.types import BufferedInputFile
            import datetime as _dt

            summary = (
                "🚨 Отчёт сформирован, но он слишком большой для одного сообщения. "
                "Прикрепил файл с полным списком.\n\n"
                + "\n".join(lines[:60])
            )
            summary = summary[:3900]
            await cb.message.edit_text(summary, reply_markup=kb_admin_menu(), parse_mode="HTML")

            filename = f"kick-report-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}.txt"
            await cb.message.answer_document(
                document=BufferedInputFile(text.encode("utf-8"), filename=filename),
                caption="Полный список для исключения",
            )
    except Exception:
        try:
            await cb.message.answer(text[:3900], reply_markup=kb_admin_menu(), parse_mode="HTML")
        except Exception:
            pass
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
        # Find the latest membership row (even if already removed) to provide a useful answer.
        m = await session.scalar(
            select(YandexMembership)
            .where(YandexMembership.tg_id == tg_id)
            .order_by(YandexMembership.id.desc())
            .limit(1)
        )
        if not m:
            await state.clear()
            await message.answer(
                "ℹ️ Для этого TG ID нет записей YandexMembership. Возможно пользователь не был добавлен в семью.",
                reply_markup=kb_admin_menu(),
            )
            return

        # If already marked removed, just show info.
        if m.removed_at is not None:
            removed_at = m.removed_at if m.removed_at.tzinfo else m.removed_at.replace(tzinfo=timezone.utc)
            fam = getattr(m, "family_label", None) or getattr(m, "account_label", None)
            slot = m.slot_index
            await state.clear()
            await message.answer(
                "✅ Уже отмечен как исключённый ранее.\n\n"
                f"TG: <code>{tg_id}</code>\n"
                f"Семья: <code>{fam or '—'}</code>\n"
                f"Слот: <code>{slot if slot is not None else '—'}</code>\n"
                f"removed_at: <code>{removed_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')}</code>",
                parse_mode="HTML",
                reply_markup=kb_admin_menu(),
            )
            return

        m.removed_at = now
        m.status = "removed"
        fam = getattr(m, "family_label", None) or getattr(m, "account_label", None)
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
