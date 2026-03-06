from __future__ import annotations

import asyncio
import re
import os
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import func, select, literal

from dateutil.relativedelta import relativedelta

from app.bot.auth import is_owner
from app.bot.keyboards import kb_admin_menu, kb_admin_referrals_menu
from app.core.config import settings
from app.db.models import ReferralEarning, Subscription, User
from app.db.models import MessageAudit
from app.db.models.payout_request import PayoutRequest
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_invite_slot import YandexInviteSlot
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.repo import get_price_rub, set_app_setting_int, get_subscription, extend_subscription
from app.services.referrals.service import referral_service
from app.services.vpn.service import vpn_service
from app.services.regionvpn import RegionVpnService
from app.services.message_audit import audit_send_message




def _load_vpn_servers_admin() -> list[dict]:
    """Load VPN servers from the same env format as the user menu.

    Uses VPN_SERVERS_JSON (list of server dicts). Falls back to single-server
    env vars if JSON is not provided.
    """
    raw = os.environ.get('VPN_SERVERS_JSON') or os.environ.get('VPN_SERVERS')
    out: list[dict] = []
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and 'servers' in data:
                data = data['servers']
            if isinstance(data, list):
                for s in data:
                    if isinstance(s, dict):
                        out.append(s)
        except Exception:
            out = []
    if out:
        return out
    # fallback: single-server
    code = (os.environ.get('VPN_CODE') or 'NL').upper()
    return [{
        'code': code,
        'name': os.environ.get('VPN_NAME') or code,
        'host': os.environ.get('VPN_SSH_HOST'),
        'port': int(os.environ.get('VPN_SSH_PORT') or 22),
        'user': os.environ.get('VPN_SSH_USER'),
        'password': os.environ.get('VPN_SSH_PASSWORD'),
        'interface': os.environ.get('VPN_INTERFACE') or 'wg0',
        'server_public_key': os.environ.get('VPN_SERVER_PUBLIC_KEY') or os.environ.get('VPN_SERVER_PUBLIC'),
        'endpoint': os.environ.get('VPN_ENDPOINT'),
        'dns': os.environ.get('VPN_DNS') or '1.1.1.1',
    }]


async def _vpn_seats_by_server() -> dict[str, int]:
    """Return used seats per server_code among ACTIVE subscriptions.

    We count distinct tg_id with an active WireGuard peer (is_active=True)
    AND an active subscription (end_at > now, is_active=True).
    """
    from app.db.models import VpnPeer, Subscription
    now = datetime.now(timezone.utc)
    # Use a single SQLAlchemy literal instance to avoid generating different
    # bind params for the same constant in SELECT vs GROUP BY.
    # Otherwise Postgres may throw: "column vpn_peers.server_code must appear in the GROUP BY..."
    default_code = (os.environ.get('VPN_CODE') or 'NL').upper()
    default_code_lit = literal(default_code)
    async with session_scope() as session:
        q = (
            select(
                func.coalesce(VpnPeer.server_code, default_code_lit).label('code'),
                func.count(func.distinct(VpnPeer.tg_id)).label('cnt'),
            )
            .join(Subscription, Subscription.tg_id == VpnPeer.tg_id)
            .where(
                VpnPeer.is_active == True,  # noqa: E712
                Subscription.is_active == True,  # noqa: E712
                Subscription.end_at.is_not(None),
                Subscription.end_at > now,
            )
            .group_by(func.coalesce(VpnPeer.server_code, default_code_lit))
        )
        res = await session.execute(q)
        rows = res.all()
    return {str(code).upper(): int(cnt) for code, cnt in rows}
def _region_service() -> RegionVpnService:
    return RegionVpnService(
        ssh_host=settings.region_ssh_host,
        ssh_port=settings.region_ssh_port,
        ssh_user=settings.region_ssh_user,
        ssh_password=settings.region_ssh_password,
        xray_config_path=settings.region_xray_config_path,
        xray_api_port=settings.region_xray_api_port,
        max_clients=settings.region_max_clients,
    )

router = Router()

AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

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


def _kb_admin_users(page: int, pages: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if page > 1:
        b.button(text="⬅️", callback_data=f"admin:users:page:{page-1}")
    if page < pages:
        b.button(text="➡️", callback_data=f"admin:users:page:{page+1}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu"))
    return b.as_markup()


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


def _parse_end_at_input_to_utc(s: str) -> datetime | None:
    """Parse admin input into UTC datetime.

    Supported formats:
    - YYYY-MM-DD           -> end of day (23:59:59) Amsterdam time
    - YYYY-MM-DD HH:MM     -> exact time Amsterdam time
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            dt_local = datetime.strptime(s, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=AMSTERDAM_TZ
            )
            return dt_local.astimezone(timezone.utc)
        dt_local = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=AMSTERDAM_TZ)
        return dt_local.astimezone(timezone.utc)
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


def _fmt_sub_end_at(dt: datetime | None, *, active: bool) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    suffix = "" if active else " (не активна)"
    return f"{dt.date().isoformat()}{suffix}"


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


def _kb_ref_manage() -> InlineKeyboardMarkup:
    """Keyboard used inside referral management flows (assign/take)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:referrals:menu")],
            [InlineKeyboardButton(text="🏠 Админка", callback_data="admin:menu")],
        ]
    )


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


class AdminPriceFSM(StatesGroup):
    waiting_price = State()


class AdminUserInspectFSM(StatesGroup):
    waiting_user = State()


class AdminUserSetEndAtFSM(StatesGroup):
    waiting_end_at = State()


class AdminGiftSubFSM(StatesGroup):
    waiting_target = State()
    waiting_months = State()


class AdminBroadcastFSM(StatesGroup):
    waiting_target = State()
    waiting_text = State()


# ==========================
# ADMIN MENU
# ==========================

@router.callback_query(lambda c: c.data == "admin:menu")
async def admin_menu(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        # Не показываем админку не-владельцам, но обязательно отвечаем на callback,
        # иначе у пользователя будет "часики" и ощущение, что бот завис.
        await cb.answer()
        return

    # Answer ASAP to avoid "query is too old" когда мы делаем сетевые вызовы ниже.
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
                vpn_line = (
                    f"🌍 VPN: загрузка CPU ~<b>{cpu:.0f}%</b> | "
                    f"активных пиров <b>{act}</b>/<b>{tot}</b>"
                )
    except Exception:
        pass

    text = (
        "🛠 <b>Админка</b>\n\n"
        f"{vpn_line}\n\n"
        "Выберите действие:"
    )

    # Telegram не разрешает редактировать сообщение, если контент/клавиатура не изменились.
    # В таком случае отправим новое сообщение, чтобы пользователь увидел результат.
    try:
        await cb.message.edit_text(
            text,
            reply_markup=kb_admin_menu(),
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await cb.message.answer(text, reply_markup=kb_admin_menu(), parse_mode="HTML")
        else:
            raise


# ==========================
# ADMIN: USERS LIST
# ==========================


async def _render_users_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    per_page = 25
    page = max(1, int(page))
    now = _utcnow()

    async with session_scope() as session:
        total = await session.scalar(select(func.count()).select_from(User))
        total = int(total or 0)

        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)

        q = (
            select(User, Subscription)
            .outerjoin(Subscription, Subscription.tg_id == User.tg_id)
            .order_by(User.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        rows = (await session.execute(q)).all()

    lines: list[str] = []
    for u, sub in rows:
        username = f"@{u.tg_username}" if u.tg_username else "—"
        name_parts = [p for p in [u.first_name, u.last_name] if p]
        name = " ".join(name_parts) if name_parts else "—"
        end_at = None
        if sub and sub.end_at:
            end_at = sub.end_at if sub.end_at.tzinfo else sub.end_at.replace(tzinfo=timezone.utc)
        is_active = bool(sub and sub.is_active and end_at and end_at > now)
        status = "активна" if is_active else "не активна"
        lines.append(
            f"• <code>{u.tg_id}</code> | {username} | {name}\n"
            f"  Подписка: <b>{status}</b> | до: <b>{_fmt_sub_end_at(end_at, active=is_active)}</b>"
        )

    body = "\n\n".join(lines) if lines else "(пока нет пользователей)"
    text = (
        "👤 <b>Все зарегистрированные пользователи</b>\n\n"
        f"Всего: <b>{total}</b> | Страница: <b>{page}/{pages}</b>\n\n"
        f"{body}"
    )
    return text, _kb_admin_users(page, pages)


@router.callback_query(lambda c: c.data == "admin:users")
async def admin_users(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        await cb.answer()
    except Exception:
        pass

    text, kb = await _render_users_page(1)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
        else:
            raise


@router.callback_query(lambda c: (c.data or "").startswith("admin:users:page:"))
async def admin_users_page(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        await cb.answer()
    except Exception:
        pass

    try:
        page = int((cb.data or "").split(":")[-1])
    except Exception:
        page = 1

    text, kb = await _render_users_page(page)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
        else:
            raise



# ==========================
# ADMIN: VPN STATUS / ACTIVE PROFILES + REFERRALS MENU
# ==========================


def _kb_admin_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")]]
    )


def _kb_user_card(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin:user:card:{tg_id}")],
            [InlineKeyboardButton(text="📨 Напомнить об оплате", callback_data=f"admin:user:notify_expired:{tg_id}")],
            [InlineKeyboardButton(text="🗓 Изменить дату окончания", callback_data=f"admin:user:set_end_at:{tg_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
        ]
    )


def _fmt_dt_short(dt: datetime | None) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


async def _auto_disable_status(session, tg_id: int) -> str:
    """Human-friendly status of WG auto-disable for a user."""
    now = _utcnow()
    sub = await get_subscription(session, tg_id)
    sub_end = None
    sub_active = False
    if sub and sub.end_at:
        sub_end = sub.end_at if sub.end_at.tzinfo else sub.end_at.replace(tzinfo=timezone.utc)
        sub_active = bool(sub.is_active) and sub_end > now

    # latest peer (even if inactive)
    from app.db.models import VpnPeer
    peer = (await session.execute(
        select(VpnPeer)
        .where(VpnPeer.tg_id == tg_id)
        .order_by(VpnPeer.id.desc())
        .limit(1)
    )).scalar_one_or_none()

    if sub_active:
        return "✅ Подписка активна — автоотключение не требуется"

    if not peer:
        return "— VPN не активирован (peer не создавался)"

    reason = (peer.rotation_reason or "").strip().lower()
    if reason == "expired_purged":
        return "🗑️ Peer удалён (прошло > 24ч без оплаты)"
    if reason == "expired":
        until = None
        if peer.revoked_at:
            ra = peer.revoked_at if peer.revoked_at.tzinfo else peer.revoked_at.replace(tzinfo=timezone.utc)
            until = ra + timedelta(hours=24)
        if until and until > now:
            left = until - now
            hrs = int(left.total_seconds() // 3600)
            mins = int((left.total_seconds() % 3600) // 60)
            return (
                f"⛔️ Peer отключён. Восстановление без смены конфига возможно до <b>{_fmt_dt_short(until)}</b> "
                f"(осталось {hrs} ч {mins} мин)"
            )
        return "⛔️ Peer отключён (ожидание оплаты истекло)"

    if peer.is_active:
        return "⚠️ Подписка не активна, но peer ещё активен (проверь scheduler/сервер)"
    return "⛔️ Peer отключён"


async def _render_user_card(session, bot, tg_id: int) -> str:
    # User profile
    u = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
    sub = await get_subscription(session, tg_id)
    now = _utcnow()

    username = f"@{u.tg_username}" if u and u.tg_username else "—"
    name = " ".join([p for p in [getattr(u, 'first_name', None), getattr(u, 'last_name', None)] if p]) if u else "—"

    sub_end = None
    sub_active = False
    if sub and sub.end_at:
        sub_end = sub.end_at if sub.end_at.tzinfo else sub.end_at.replace(tzinfo=timezone.utc)
        sub_active = bool(sub.is_active) and sub_end > now

    # last notifications
    msgs = list(
        (
            await session.execute(
                select(MessageAudit)
                .where(MessageAudit.tg_id == tg_id)
                .order_by(MessageAudit.sent_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )

    # subscription-expiry related notifications (explicit, to answer "получал ли уведомления")
    expiry_kinds = [
        ("sub_warn_7d", "Подписка: -7 дней"),
        ("sub_warn_3d", "Подписка: -3 дня"),
        ("sub_warn_1d", "Подписка: -1 день"),
        ("trial_warn_3d", "Триал: -3 дня"),
        ("trial_warn_2d", "Триал: -2 дня"),
        ("trial_warn_1d", "Триал: -1 день"),
        ("trial_expired", "Триал: закончился"),
        ("sub_expired", "Подписка: истекла"),
    ]
    kinds_only = [k for k, _ in expiry_kinds]
    expiry_rows = list(
        (
            await session.execute(
                select(MessageAudit)
                .where(MessageAudit.tg_id == tg_id, MessageAudit.kind.in_(kinds_only))
                .order_by(MessageAudit.sent_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    last_by_kind: dict[str, MessageAudit] = {}
    for r in expiry_rows:
        if r.kind not in last_by_kind:
            last_by_kind[r.kind] = r

    lines = []
    lines.append("👤 <b>Карточка пользователя</b>")
    lines.append(f"ID: <code>{tg_id}</code>")
    lines.append(f"Профиль: {username} | {name}")
    if sub_end:
        lines.append(
            f"Подписка: <b>{'активна' if sub_active else 'не активна'}</b> | до: <b>{_fmt_dt_short(sub_end)}</b>"
        )
    else:
        lines.append("Подписка: —")

    lines.append("")
    lines.append("🔌 <b>WG автоотключение</b>")
    lines.append(await _auto_disable_status(session, tg_id))

    lines.append("")
    lines.append("📩 <b>Последние уведомления</b>")
    if not msgs:
        lines.append("— пока нет записей")
    else:
        for m in msgs:
            sent = _fmt_dt_short(m.sent_at)
            seen = _fmt_dt_short(m.seen_at) if m.seen_at else "не подтверждено"
            # concise preview
            preview = (m.text_preview or "").replace("\n", " ").strip()
            if len(preview) > 120:
                preview = preview[:119] + "…"
            lines.append(f"• <b>{m.kind}</b> | {sent} | 👁 {seen}\n  {preview}")

        lines.append("")
        lines.append("<i>👁 Статус «прочитано» — это best-effort: считается прочитанным, если пользователь взаимодействовал с ботом после отправки.</i>")

    lines.append("")
    lines.append("⏰ <b>Уведомления о продлении</b>")
    def _fmt_audit_line(m: MessageAudit | None, title: str) -> str:
        if not m:
            return f"• {title}: —"
        sent = _fmt_dt_short(m.sent_at)
        # If message_id is NULL -> send attempt failed.
        if m.message_id is None:
            reason = "ошибка отправки"
            head = (m.text_preview or "").split("\n", 1)[0].strip()
            if head.startswith("[SEND_FAILED:"):
                reason = head
            return f"• {title}: ❌ {sent} | {reason}"
        seen = _fmt_dt_short(m.seen_at) if m.seen_at else "не подтверждено"
        return f"• {title}: ✅ {sent} | 👁 {seen}"

    for kind, title in expiry_kinds:
        lines.append(_fmt_audit_line(last_by_kind.get(kind), title))

    return "\n".join(lines)


@router.callback_query(lambda c: c.data == "admin:price")
async def admin_price(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    async with session_scope() as session:
        current_price = await get_price_rub(session)

    text = (
        "💲 <b>Цена подписки</b>\n\n"
        f"Текущая цена: <b>{current_price} ₽</b>\n\n"
        "Введите новую цену (целое число в рублях), например: <code>299</code>"
    )

    await state.set_state(AdminPriceFSM.waiting_price)

    try:
        await cb.message.edit_text(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await cb.message.answer(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
        else:
            raise
    await cb.answer()


@router.callback_query(lambda c: c.data == "admin:user:inspect")
async def admin_user_inspect_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer()
    await state.set_state(AdminUserInspectFSM.waiting_user)
    await cb.message.edit_text(
        "🔎 <b>Карточка пользователя</b>\n\n"
        "Отправьте <code>tg_id</code> (числом) или <code>@username</code>.",
        reply_markup=_kb_admin_back(),
        parse_mode="HTML",
    )


@router.message(AdminUserInspectFSM.waiting_user)
async def admin_user_inspect_input(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return
    raw = (message.text or "").strip()
    tg_id = await _resolve_tg_id(message.bot, raw)
    if not tg_id:
        await message.answer("❌ Не удалось распознать пользователя. Укажите tg_id числом или @username.")
        return

    async with session_scope() as session:
        text = await _render_user_card(session, message.bot, tg_id)

    await state.clear()
    await message.answer(text, reply_markup=_kb_user_card(tg_id), parse_mode="HTML")


@router.callback_query(lambda c: (c.data or "").startswith("admin:user:card:"))
async def admin_user_card_refresh(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer()
    try:
        tg_id = int((cb.data or "").split(":")[-1])
    except Exception:
        return
    async with session_scope() as session:
        text = await _render_user_card(session, cb.bot, tg_id)
    await cb.message.edit_text(text, reply_markup=_kb_user_card(tg_id), parse_mode="HTML")


@router.callback_query(lambda c: (c.data or "").startswith("admin:user:notify_expired:"))
async def admin_user_notify_expired(cb: CallbackQuery) -> None:
    """Manual reminder to pay after subscription expired.

    Telegram may block bot messages if user blocked the bot.
    We log both success and failure via message_audit.
    """

    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await cb.answer()
    try:
        tg_id = int((cb.data or "").split(":")[-1])
    except Exception:
        return

    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        now = _utcnow()
        sub_end = None
        sub_active = False
        if sub and sub.end_at:
            sub_end = sub.end_at if sub.end_at.tzinfo else sub.end_at.replace(tzinfo=timezone.utc)
            sub_active = bool(sub.is_active) and sub_end > now

    if sub_active:
        try:
            await cb.message.answer("✅ У пользователя подписка активна — напоминание не требуется.")
        except Exception:
            pass
        return

    text_to_user = (
        "⛔️ Ваша подписка не активна.\n\n"
        "Чтобы снова включить VPN и продолжить пользоваться сервисом, оплатите подписку."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить подписку", callback_data="nav:pay")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )

    # best-effort: log attempt even if user blocked bot
    try:
        await audit_send_message(cb.bot, tg_id, text_to_user, kind="admin_sub_expired_manual", reply_markup=kb)
    except Exception:
        pass

    # refresh the card in-place
    async with session_scope() as session:
        card = await _render_user_card(session, cb.bot, tg_id)
    try:
        await cb.message.edit_text(card, reply_markup=_kb_user_card(tg_id), parse_mode="HTML")
    except Exception:
        try:
            await cb.message.answer(card, reply_markup=_kb_user_card(tg_id), parse_mode="HTML")
        except Exception:
            pass


@router.callback_query(lambda c: (c.data or "").startswith("admin:user:set_end_at:"))
async def admin_user_set_end_at_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer()
    try:
        tg_id = int((cb.data or "").split(":")[-1])
    except Exception:
        return

    await state.clear()
    await state.set_state(AdminUserSetEndAtFSM.waiting_end_at)
    await state.update_data(tg_id=tg_id)

    await cb.message.edit_text(
        "🗓 <b>Изменить дату окончания подписки</b>\n\n"
        "Отправьте дату/время в формате:\n"
        "• <code>YYYY-MM-DD</code> (до 23:59)\n"
        "• <code>YYYY-MM-DD HH:MM</code>\n\n"
        "Время интерпретируется по <b>Europe/Amsterdam</b>.",
        reply_markup=_kb_admin_back(),
        parse_mode="HTML",
    )


@router.message(AdminUserSetEndAtFSM.waiting_end_at)
async def admin_user_set_end_at_finish(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return
    data = await state.get_data()
    tg_id = int(data.get("tg_id") or 0)
    end_at_utc = _parse_end_at_input_to_utc(message.text or "")
    if not tg_id or not end_at_utc:
        await message.answer(
            "❌ Не понял дату. Пример: <code>2026-03-10 18:00</code> или <code>2026-03-10</code>",
            parse_mode="HTML",
        )
        return

    now = _utcnow()
    restored = 0
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not sub:
            sub = Subscription(tg_id=tg_id, start_at=now, end_at=end_at_utc, is_active=True)
            session.add(sub)
        else:
            sub.end_at = end_at_utc
        sub.is_active = bool(end_at_utc > now)

        if sub.is_active:
            # If subscription is re-activated, restore WG peers disabled due to expiration within grace.
            try:
                restored = await vpn_service.restore_expired_peers(session, tg_id, grace_hours=24)
            except Exception:
                restored = 0

        await session.commit()

    await state.clear()

    extra = ""
    if restored:
        extra = f"\n\n✅ Восстановлено WG peer(ов): <b>{restored}</b> (если были отключены по окончанию)"
    await message.answer(
        f"✅ Дата окончания обновлена.\nTG: <code>{tg_id}</code>\nНовая end_at (UTC): <code>{_fmt_dt_short(end_at_utc)}</code>{extra}",
        reply_markup=_kb_user_card(tg_id),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "admin:vpn:grace")
async def admin_vpn_grace_list(cb: CallbackQuery) -> None:
    """List users within the 24h grace window after subscription expiration."""
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer()

    now = _utcnow()
    cutoff = now - timedelta(hours=24)
    from app.db.models import VpnPeer

    async with session_scope() as session:
        q = (
            select(VpnPeer)
            .where(
                VpnPeer.is_active.is_(False),
                VpnPeer.rotation_reason == "expired",
                VpnPeer.revoked_at.is_not(None),
                VpnPeer.revoked_at >= cutoff,
            )
            .order_by(VpnPeer.revoked_at.desc())
            .limit(500)
        )
        peers = list((await session.execute(q)).scalars().all())

    if not peers:
        await cb.message.edit_text(
            "🕒 <b>WG grace (24ч)</b>\n\nСейчас нет пользователей в окне 24 часов после окончания подписки.",
            reply_markup=kb_admin_menu(),
            parse_mode="HTML",
        )
        return

    by_tg: dict[int, VpnPeer] = {}
    for p in peers:
        tid = int(p.tg_id)
        # keep the most recent revoked peer per user
        if tid not in by_tg:
            by_tg[tid] = p

    lines = [
        "🕒 <b>WG grace (24ч)</b>",
        "",
        "Пиры уже отключены, но их можно восстановить при оплате в течение 24 часов (без смены конфига):",
        "",
    ]
    for tid, p in list(by_tg.items())[:80]:
        ra = p.revoked_at if p.revoked_at.tzinfo else p.revoked_at.replace(tzinfo=timezone.utc)
        until = ra + timedelta(hours=24)
        left = until - now
        hrs = int(left.total_seconds() // 3600)
        mins = int((left.total_seconds() % 3600) // 60)
        lines.append(f"• <code>{tid}</code> — до <b>{_fmt_dt_short(until)}</b> (осталось {hrs} ч {mins} мин)")

    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_menu(), parse_mode="HTML")


@router.message(AdminPriceFSM.waiting_price)
async def admin_price_set(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    raw = (message.text or "").strip()
    # allow formats like "299", "299 ₽"
    raw = re.sub(r"[^0-9]", "", raw)
    if not raw:
        await message.answer("❌ Введите цену числом, например: 299", reply_markup=_kb_admin_back())
        return

    try:
        new_price = int(raw)
    except Exception:
        await message.answer("❌ Введите цену числом, например: 299", reply_markup=_kb_admin_back())
        return

    if new_price <= 0 or new_price > 1_000_000:
        await message.answer("❌ Некорректная цена. Укажите значение от 1 до 1 000 000 ₽", reply_markup=_kb_admin_back())
        return

    async with session_scope() as session:
        await set_app_setting_int(session, "price_rub", new_price)
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ Цена подписки обновлена: <b>{new_price} ₽</b>",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )


# ==========================
# ADMIN: GIFT SUBSCRIPTION
# ==========================


@router.callback_query(lambda c: c.data == "admin:sub:gift")
async def admin_sub_gift_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()
    await state.set_state(AdminGiftSubFSM.waiting_target)

    text = (
        "🎁 <b>Подарок: подписка</b>\n\n"
        "Отправьте Telegram ID пользователя (например <code>123456789</code>) "
        "или @username.\n\n"
        "⬅️ Для отмены нажмите «Назад»."
    )

    try:
        await cb.message.edit_text(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await cb.message.answer(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
        else:
            raise
    await cb.answer()


@router.message(AdminGiftSubFSM.waiting_target)
async def admin_sub_gift_target(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    raw = (message.text or "").strip()
    if not raw:
        await message.answer("❌ Укажите Telegram ID или @username.", reply_markup=_kb_admin_back())
        return

    tg_id = await _resolve_tg_id(message.bot, raw)
    if not tg_id:
        await message.answer(
            "❌ Не удалось определить пользователя.\n\n"
            "Принимаю <code>123456789</code> или @username (если пользователь уже писал боту).",
            reply_markup=_kb_admin_back(),
            parse_mode="HTML",
        )
        return

    await state.update_data(gift_tg_id=int(tg_id))
    await state.set_state(AdminGiftSubFSM.waiting_months)

    await message.answer(
        "⏳ На сколько месяцев подарить подписку?\n\n"
        "Введите число месяцев, например: <code>1</code> или <code>3</code>.",
        reply_markup=_kb_admin_back(),
        parse_mode="HTML",
    )


@router.message(AdminGiftSubFSM.waiting_months)
async def admin_sub_gift_months(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    raw = re.sub(r"[^0-9]", "", (message.text or "").strip())
    if not raw:
        await message.answer("❌ Введите число месяцев, например: 1", reply_markup=_kb_admin_back())
        return

    try:
        months = int(raw)
    except Exception:
        await message.answer("❌ Введите число месяцев, например: 1", reply_markup=_kb_admin_back())
        return

    if months <= 0 or months > 120:
        await message.answer("❌ Укажите от 1 до 120 месяцев.", reply_markup=_kb_admin_back())
        return

    data = await state.get_data()
    target_tg_id = int(data.get("gift_tg_id") or 0)
    if not target_tg_id:
        await state.clear()
        await message.answer("⚠️ Не найден получатель. Начните заново.", reply_markup=kb_admin_menu())
        return

    from app.db.models.subscription import Subscription

    now = _utcnow()
    async with session_scope() as session:
        sub = await session.get(Subscription, target_tg_id)
        if not sub:
            sub = await get_subscription(session, target_tg_id)

        base = sub.end_at if sub.end_at and sub.end_at > now else now
        new_end = base + relativedelta(months=months)

        # Mark as paid via a "gift" provider (amount 0) and extend.
        await extend_subscription(
            session,
            target_tg_id,
            months=months,
            days_legacy=months * 30,
            amount_rub=0,
            provider="gift",
            status="success",
            provider_payment_id=f"gift:{message.from_user.id}:{target_tg_id}:{int(now.timestamp())}",
        )

        # Restore WG peers if the user was expired recently.
        try:
            from app.services.vpn.service import vpn_service

            await vpn_service.restore_expired_peers(session, target_tg_id, grace_hours=24)
        except Exception:
            pass

        sub.end_at = new_end
        sub.is_active = True
        sub.status = "active"
        await session.commit()

    await state.clear()

    # Notify user (best-effort)
    notify_text = (
        "🎁 <b>Подарок!</b>\n\n"
        "Администратор подарил вам подписку на наш сервис, приятного пользования!"
    )
    try:
        await audit_send_message(message.bot, target_tg_id, notify_text, kind="admin_gift", reply_markup=None)
    except Exception:
        pass

    await message.answer(
        "✅ Подписка подарена.\n\n"
        f"Пользователь: <code>{target_tg_id}</code>\n"
        f"Срок: <b>{months}</b> мес.\n"
        f"Новая дата окончания: <b>{new_end.date().isoformat()}</b>",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "admin:broadcast:all")
async def admin_broadcast_all_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await state.clear()
    await state.update_data(broadcast_mode="all")
    await state.set_state(AdminBroadcastFSM.waiting_text)
    try:
        await cb.answer()
    except Exception:
        pass
    await cb.message.answer(
        "📣 <b>Рассылка всем пользователям</b>\n\nОтправьте следующий сообщением текст рассылки.",
        reply_markup=_kb_admin_back(),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "admin:broadcast:paid")
async def admin_broadcast_paid_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await state.clear()
    await state.update_data(broadcast_mode="paid")
    await state.set_state(AdminBroadcastFSM.waiting_text)
    try:
        await cb.answer()
    except Exception:
        pass
    await cb.message.answer(
        "🟢 <b>Рассылка пользователям с активной подпиской</b>\n\nОтправьте следующим сообщением текст рассылки.",
        reply_markup=_kb_admin_back(),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "admin:broadcast:unpaid")
async def admin_broadcast_unpaid_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await state.clear()
    await state.update_data(broadcast_mode="unpaid")
    await state.set_state(AdminBroadcastFSM.waiting_text)
    try:
        await cb.answer()
    except Exception:
        pass
    await cb.message.answer(
        "⚪️ <b>Рассылка пользователям без активной подписки</b>\n\nОтправьте следующим сообщением текст рассылки.",
        reply_markup=_kb_admin_back(),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "admin:broadcast:one")
async def admin_broadcast_one_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await state.clear()
    await state.set_state(AdminBroadcastFSM.waiting_target)
    try:
        await cb.answer()
    except Exception:
        pass
    await cb.message.answer(
        "✉️ <b>Сообщение пользователю</b>\n\nУкажите Telegram ID или @username.",
        reply_markup=_kb_admin_back(),
        parse_mode="HTML",
    )


@router.message(AdminBroadcastFSM.waiting_target)
async def admin_broadcast_one_target(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("❌ Укажите Telegram ID или @username.", reply_markup=_kb_admin_back())
        return
    tg_id = await _resolve_tg_id(message.bot, raw)
    if not tg_id:
        await message.answer(
            "❌ Не удалось определить пользователя. Используйте <code>123456789</code> или @username.",
            reply_markup=_kb_admin_back(),
            parse_mode="HTML",
        )
        return
    await state.update_data(broadcast_mode="one", broadcast_target=int(tg_id))
    await state.set_state(AdminBroadcastFSM.waiting_text)
    await message.answer(
        f"✍️ Теперь отправьте текст сообщения для <code>{tg_id}</code>.",
        reply_markup=_kb_admin_back(),
        parse_mode="HTML",
    )


@router.message(AdminBroadcastFSM.waiting_text)
async def admin_broadcast_send(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return
    payload = (message.text or "").strip()
    if not payload:
        await message.answer("❌ Текст рассылки не должен быть пустым.", reply_markup=_kb_admin_back())
        return

    data = await state.get_data()
    mode = str(data.get("broadcast_mode") or "")

    sent = 0
    failed = 0

    if mode == "one":
        target = int(data.get("broadcast_target") or 0)
        if not target:
            await state.clear()
            await message.answer("⚠️ Получатель не найден. Начните заново.", reply_markup=kb_admin_menu())
            return
        try:
            await audit_send_message(message.bot, target, payload, kind="admin_broadcast_one", reply_markup=None)
            sent = 1
        except Exception:
            failed = 1
        await state.clear()
        if sent:
            await message.answer(f"✅ Сообщение отправлено пользователю <code>{target}</code>.", reply_markup=kb_admin_menu(), parse_mode="HTML")
        else:
            await message.answer(f"⚠️ Не удалось отправить сообщение пользователю <code>{target}</code>.", reply_markup=kb_admin_menu(), parse_mode="HTML")
        return

    now = _utcnow()
    async with session_scope() as session:
        if mode == "paid":
            res = await session.execute(
                select(User.tg_id)
                .join(Subscription, Subscription.tg_id == User.tg_id)
                .where(
                    Subscription.is_active == True,  # noqa: E712
                    Subscription.end_at.is_not(None),
                    Subscription.end_at > now,
                )
                .order_by(User.created_at.asc())
            )
        elif mode == "unpaid":
            active_subq = (
                select(Subscription.tg_id)
                .where(
                    Subscription.is_active == True,  # noqa: E712
                    Subscription.end_at.is_not(None),
                    Subscription.end_at > now,
                )
            )
            res = await session.execute(
                select(User.tg_id)
                .where(User.tg_id.not_in(active_subq))
                .order_by(User.created_at.asc())
            )
        else:
            res = await session.execute(select(User.tg_id).order_by(User.created_at.asc()))
        targets = [int(x) for x in res.scalars().all()]

    for target in targets:
        try:
            await audit_send_message(
                message.bot,
                target,
                payload,
                kind=f"admin_broadcast_{mode or 'all'}",
                reply_markup=None,
            )
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.03)

    await state.clear()
    mode_label = {
        "all": "всем пользователям",
        "paid": "пользователям с активной подпиской",
        "unpaid": "пользователям без активной подписки",
    }.get(mode, "пользователям")
    await message.answer(
        "✅ <b>Рассылка завершена</b>\n\n"
        f"Сегмент: <b>{mode_label}</b>\n"
        f"Доставлено: <b>{sent}</b>\n"
        f"Ошибок: <b>{failed}</b>",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "admin:vpn:status")
async def admin_vpn_status(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        await cb.answer()
    except Exception:
        pass

    st = await vpn_service.get_server_status()
    if st.get("ok"):
        cpu = st.get("cpu_load_percent")
        act = st.get("active_peers")
        tot = st.get("total_peers")
        cpu_s = "—" if cpu is None else f"{cpu:.0f}%"
        act_s = "—" if act is None else str(act)
        tot_s = "—" if tot is None else str(tot)
        text = (
            "📊 <b>Статус VPN</b>\n\n"
            f"CPU: <b>{cpu_s}</b>\n"
            f"Активных пиров: <b>{act_s}</b>/<b>{tot_s}</b>\n\n"
            "Окно активности: последние ~3 минуты."
        )
    else:
        text = (
            "📊 <b>Статус VPN</b>\n\n"
            "⚠️ Статус сейчас недоступен (SSH/сервер не отвечает).\n"
            "Попробуй позже."
        )

    # Seats (capacity) by server/location: считаем по БД, поэтому показываем даже если SSH недоступен.
    try:
        cap = int(os.environ.get("VPN_MAX_ACTIVE", "40") or 40)
    except Exception:
        cap = 40

    try:
        used_map = await _vpn_seats_by_server()
        servers = _load_vpn_servers_admin()
        seat_lines: list[str] = []
        for s in servers:
            code = str(s.get("code") or os.environ.get("VPN_CODE", "NL")).upper()
            name = str(s.get("name") or code)
            used = int(used_map.get(code, 0))
            free = max(0, cap - used)
            seat_lines.append(f"{code} ({name}): <b>{used}</b>/{cap} | свободно: <b>{free}</b>")
        if seat_lines:
            text += "\n\n👥 <b>Места по локациям</b>\n" + "\n".join(seat_lines)
    except Exception:
        text += "\n\n👥 <b>Места по локациям</b>\n⚠️ Не удалось рассчитать свободные места."

    try:
        await cb.message.edit_text(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await cb.message.answer(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
        else:
            raise

@router.callback_query(lambda c: c.data == "admin:vpn:active_profiles")
async def admin_vpn_active_profiles(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        await cb.answer()
    except Exception:
        pass

    recent = await vpn_service.get_recent_peer_handshakes(window_seconds=180)
    if not recent:
        text = (
            "👥 <b>Активные VPN-профили</b>\n\n"
            "Сейчас не найдено активных пиров (за последние ~3 минуты)."
        )
        try:
            await cb.message.edit_text(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                await cb.message.answer(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
            else:
                raise
        return

    keys = [x.get("public_key") for x in recent if x.get("public_key")]
    keys = keys[:200]

    from app.db.models.vpn_peer import VpnPeer
    from app.db.models.subscription import Subscription

    peer_rows: dict[str, VpnPeer] = {}
    subs_by_tg: dict[int, Subscription] = {}

    async with session_scope() as session:
        res = await session.execute(
            select(VpnPeer).where(VpnPeer.client_public_key.in_(keys), VpnPeer.is_active == True)  # noqa: E712
        )
        for row in res.scalars().all():
            peer_rows[row.client_public_key] = row

        tg_ids = sorted({row.tg_id for row in peer_rows.values()})
        if tg_ids:
            res2 = await session.execute(select(Subscription).where(Subscription.tg_id.in_(tg_ids)))
            for sub in res2.scalars().all():
                subs_by_tg[int(sub.tg_id)] = sub

    lines = ["👥 <b>Активные VPN-профили</b>", "", f"Найдено активных рукопожатий: <b>{len(recent)}</b>", ""]

    # Resolve Telegram usernames for readability (best-effort)
    tg_label: dict[int, str] = {}
    try:
        unique_tg_ids = sorted({int(peer_rows[x.get("public_key")].tg_id) for x in recent if x.get("public_key") in peer_rows})
        # keep it bounded to avoid too many API calls
        unique_tg_ids = unique_tg_ids[:50]
        labels = await asyncio.gather(*[_tg_label(cb.bot, tid) for tid in unique_tg_ids], return_exceptions=True)
        for tid, lbl in zip(unique_tg_ids, labels):
            if isinstance(lbl, Exception):
                tg_label[tid] = f"ID {tid}"
            else:
                tg_label[tid] = str(lbl)
    except Exception:
        tg_label = {}

    shown = 0
    for item in recent:
        k = item.get("public_key")
        age = item.get("age_seconds")
        if not k or k not in peer_rows:
            continue
        row = peer_rows[k]
        sub = subs_by_tg.get(int(row.tg_id))
        sub_state = "✅" if (sub and bool(getattr(sub, "is_active", False))) else "—"
        age_s = "—" if age is None else f"{int(age)}s"
        shown += 1
        who = tg_label.get(int(row.tg_id)) or f"ID {row.tg_id}"
        # keep tg_id in the end for unambiguous matching
        lines.append(f"{shown}. {who} | <code>{row.client_public_key[:8]}…</code> | {row.client_ip} | sub {sub_state} | hs {age_s} | id <code>{row.tg_id}</code>")
        if shown >= 25:
            break

    if shown == 0:
        lines.append("(Не удалось сопоставить активные рукопожатия с пирами в БД.)")

    if len(recent) > shown:
        lines.append("")
        lines.append(f"Показано: <b>{shown}</b> (лимит 25)")

    text = "\n".join(lines)

    try:
        await cb.message.edit_text(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await cb.message.answer(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
        else:
            raise


@router.callback_query(lambda c: c.data == "admin:regionvpn:profiles")
async def admin_regionvpn_profiles(cb: CallbackQuery) -> None:
    """List provisioned VPN-Region profiles (VLESS clients in Xray config)."""
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        await cb.answer()
    except Exception:
        pass

    svc = _region_service()
    try:
        clients = await svc.list_clients()
    except Exception:
        text = (
            "🌐 <b>VPN-Region профили</b>\n\n"
            "⚠️ Не удалось подключиться к серверу/прочитать конфиг Xray (SSH).\n"
            "Проверь REGION_* переменные и доступность сервера."
        )
        try:
            await cb.message.edit_text(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                await cb.message.answer(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
            else:
                raise
        return

    # Parse tg_id from email "tg:<id>"
    parsed: list[tuple[int | None, str, str]] = []
    for c in clients:
        email = (c.get("email") or "").strip()
        cid = (c.get("id") or "").strip()
        flow = (c.get("flow") or "").strip()
        tid: int | None = None
        if email.startswith("tg:"):
            raw = email.split(":", 1)[1]
            if raw.isdigit():
                tid = int(raw)
        elif email.isdigit():
            tid = int(email)
        parsed.append((tid, cid, flow))

    # Resolve usernames for the first N entries
    tg_ids = [tid for tid, _, _ in parsed if tid is not None]
    tg_ids = list(dict.fromkeys(tg_ids))[:50]
    labels: dict[int, str] = {}
    if tg_ids:
        res = await asyncio.gather(*[_tg_label(cb.bot, tid) for tid in tg_ids], return_exceptions=True)
        for tid, lbl in zip(tg_ids, res):
            if isinstance(lbl, Exception):
                labels[tid] = f"ID {tid}"
            else:
                labels[tid] = str(lbl)

    lines = [
        "🌐 <b>VPN-Region профили</b>",
        "",
        f"Всего профилей в Xray: <b>{len(parsed)}</b>",
        "",
    ]

    shown = 0
    for tid, cid, flow in parsed:
        shown += 1
        who = labels.get(tid) if tid is not None else "(без tg_id)"
        tid_s = "—" if tid is None else str(tid)
        cid_s = cid[:8] + "…" if cid else "—"
        flow_s = flow or "—"
        lines.append(f"{shown}. {who} | uuid <code>{cid_s}</code> | flow {flow_s} | id <code>{tid_s}</code>")
        if shown >= 30:
            break

    if len(parsed) > shown:
        lines.append("")
        lines.append(f"Показано: <b>{shown}</b> (лимит 30)")

    text = "\n".join(lines)

    try:
        await cb.message.edit_text(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await cb.message.answer(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
        else:
            raise

@router.callback_query(lambda c: c.data == "admin:regionvpn:active")
async def admin_regionvpn_active(cb: CallbackQuery) -> None:
    """List active VPN-Region sessions (last device IP per user)."""
    try:
        async with session_scope() as s:
            rows = (
                await s.execute(
                    select(RegionVpnSession)
                    .where(RegionVpnSession.active_ip.isnot(None))
                    .order_by(RegionVpnSession.last_seen_at.desc().nullslast())
                    .limit(100)
                )
            ).scalars().all()

            if not rows:
                await cb.message.answer("📡 Активные VPN-Region: пока пусто.")
                await cb.answer()
                return

            lines = ["📡 *Активные VPN-Region (последнее устройство)*\n"]
            for row in rows:
                user = await s.get(User, row.tg_id)
                uname = (getattr(user, "username", None) or "").strip()
                label = f"@{uname}" if uname else f"tg:{row.tg_id}"
                ip = (row.active_ip or "").strip()
                seen = row.last_seen_at.isoformat() if row.last_seen_at else "-"
                lines.append(f"• {label} — `{ip}`\n  _last seen:_ {seen}")

            await cb.message.answer("\n".join(lines), parse_mode="Markdown")
            await cb.answer()
    except Exception:
        await cb.message.answer("❌ Не удалось получить активные VPN-Region сессии. Проверь логи.")
        await cb.answer()


@router.callback_query(lambda c: c.data == "admin:referrals:menu")
async def admin_referrals_menu(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.clear()

    try:
        await cb.answer()
    except Exception:
        pass

    text = "🔁 <b>Управление рефералами</b>\n\nВыберите действие:"

    try:
        await cb.message.edit_text(text, reply_markup=kb_admin_referrals_menu(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await cb.message.answer(text, reply_markup=kb_admin_referrals_menu(), parse_mode="HTML")
        else:
            raise


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
