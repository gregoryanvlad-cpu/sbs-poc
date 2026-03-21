from __future__ import annotations

import asyncio
import re
import os
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import func, select, literal, and_, or_, delete

from dateutil.relativedelta import relativedelta

from app.bot.auth import is_owner, is_admin
from app.bot.keyboards import kb_admin_menu, kb_admin_referrals_menu
from app.core.config import settings
from app.db.models import ReferralEarning, Subscription, User, Payment
from app.db.models.vpn_peer import VpnPeer
from app.db.models.family_vpn_profile import FamilyVpnProfile
from app.db.models.lte_vpn_client import LteVpnClient
from app.db.models import MessageAudit
from app.db.models.payout_request import PayoutRequest
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_invite_slot import YandexInviteSlot
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.repo import get_price_rub, set_app_setting_int, get_subscription, extend_subscription, get_app_setting_int
from app.services.referrals.service import referral_service
from app.services.vpn.service import vpn_service
from app.services.vpn.ssh_provider import WireGuardSSHProvider
from app.services.regionvpn import RegionVpnService
from app.services.lte_vpn.service import lte_vpn_service
from app.services.message_audit import audit_send_message


log = logging.getLogger(__name__)


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




def _server_code_aliases(servers: list[dict], code: str) -> set[str]:
    """Return DB aliases for the same logical server code.

    We have legacy data where the first/second server could be stored as
    NL/NL1/NL2, SERVER1/SERVER2 or SERVER #1/SERVER #2.
    This helper keeps admin views consistent across those historical values.
    """
    code_u = str(code or '').strip().upper()
    if not code_u:
        return set()

    aliases = {code_u}
    compact = code_u.replace(' ', '')
    aliases.add(compact)

    # Determine server ordinal from configured servers, when possible.
    ordinal = None
    for idx, s in enumerate(servers, start=1):
        if str(s.get('code') or '').strip().upper() == code_u:
            ordinal = idx
            break

    if ordinal is None:
        if code_u.startswith('NL') and code_u[2:].isdigit():
            ordinal = int(code_u[2:])
        elif code_u == 'NL':
            ordinal = 1
        else:
            digits = ''.join(ch for ch in code_u if ch.isdigit())
            if digits:
                try:
                    ordinal = int(digits)
                except Exception:
                    ordinal = None

    if ordinal is not None:
        aliases.update({f'SERVER{ordinal}', f'SERVER #{ordinal}', f'NL{ordinal}'})
        if ordinal == 1:
            aliases.add('NL')

    if code_u in {'NL', 'NL1'}:
        aliases.update({'NL', 'NL1', 'SERVER1', 'SERVER #1'})
    if code_u == 'NL2':
        aliases.update({'SERVER2', 'SERVER #2'})

    return {a for a in aliases if a}
def _server_numbered_label(servers: list[dict], code: str, *, include_name: bool = True) -> str:
    code_u = (code or '').upper()
    aliases = {code_u}
    if code_u == 'NL':
        aliases.add('NL1')
    for idx, s in enumerate(servers, start=1):
        sc = str(s.get('code') or os.environ.get('VPN_CODE', 'NL')).upper()
        if sc in aliases:
            name = str(s.get('name') or sc)
            return f"Server #{idx} — {name}" if include_name else f"#{idx}"
    return code_u or '—'


async def _vpn_seats_by_server() -> dict[str, int]:
    """Return occupied WG slots per server from DB.

    We intentionally count active VPN peer rows with active subscriptions from DB,
    not raw `wg show peers`, because runtime WireGuard can temporarily contain
    stale/manual peers and must not distort admin capacity, routing or UI.
    """
    from app.db.models import VpnPeer, Subscription

    servers = _load_vpn_servers_admin()
    result: dict[str, int] = {}
    now = datetime.now(timezone.utc)
    default_code = (os.environ.get('VPN_CODE') or 'NL').upper()
    default_code_lit = literal(default_code)

    async with session_scope() as session:
        q = (
            select(
                func.coalesce(func.upper(VpnPeer.server_code), default_code_lit).label('code'),
                func.count(VpnPeer.id).label('cnt'),
            )
            .join(Subscription, Subscription.tg_id == VpnPeer.tg_id)
            .where(
                VpnPeer.is_active == True,  # noqa: E712
                Subscription.is_active == True,  # noqa: E712
                Subscription.end_at.is_not(None),
                Subscription.end_at > now,
            )
            .group_by(func.coalesce(func.upper(VpnPeer.server_code), default_code_lit))
        )
        res = await session.execute(q)
        result = {str(code).upper(): int(cnt) for code, cnt in res.all()}

    for s in servers:
        code = str(s.get('code') or default_code).upper()
        result.setdefault(code, 0)
    if not servers:
        result.setdefault(default_code, 0)
    return result



def _kb_admin_self_cleanup_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="admin:vpn:self_cleanup:do")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
    ])


def _providers_for_server_code_admin(code: str | None) -> list[WireGuardSSHProvider]:
    servers = _load_vpn_servers_admin()
    code_u = str(code or '').strip().upper()
    aliases = _server_code_aliases(servers, code_u) if code_u else set()
    providers: list[WireGuardSSHProvider] = []
    seen: set[tuple[str, int, str, str]] = set()

    def _add(host: str | None, port: int | None, user: str | None, password: str | None, interface: str | None) -> None:
        h = str(host or '').strip()
        u = str(user or '').strip()
        iface = str(interface or 'wg0').strip() or 'wg0'
        prt = int(port or 22)
        if not h or not u:
            return
        key = (h, prt, u, iface)
        if key in seen:
            return
        seen.add(key)
        providers.append(WireGuardSSHProvider(host=h, port=prt, user=u, password=(password or None), interface=iface))

    for srv in servers:
        srv_code = str(srv.get('code') or '').strip().upper()
        if aliases and srv_code not in aliases:
            continue
        _add(srv.get('host'), srv.get('port') or 22, srv.get('user'), srv.get('password'), srv.get('interface') or 'wg0')

    # fallback to default single-server env
    _add(
        os.environ.get('WG_SSH_HOST') or os.environ.get('VPN_SSH_HOST'),
        int(os.environ.get('WG_SSH_PORT') or os.environ.get('VPN_SSH_PORT') or 22),
        os.environ.get('WG_SSH_USER') or os.environ.get('VPN_SSH_USER'),
        os.environ.get('WG_SSH_PASSWORD') or os.environ.get('VPN_SSH_PASSWORD'),
        os.environ.get('VPN_INTERFACE') or 'wg0',
    )
    return providers

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


class AdminLtePriceFSM(StatesGroup):
    waiting_price = State()


class AdminUserInspectFSM(StatesGroup):
    waiting_user = State()


class AdminUserSetEndAtFSM(StatesGroup):
    waiting_end_at = State()


class AdminFamilyPriceMenuFSM(StatesGroup):
    waiting_user = State()
    waiting_price = State()


class AdminUserSetFamilyPriceFSM(StatesGroup):
    waiting_price = State()


class AdminVpnExtraFSM(StatesGroup):
    waiting_count = State()


class AdminGiftSubFSM(StatesGroup):
    waiting_target = State()
    waiting_months = State()


class AdminGiftDaysFSM(StatesGroup):
    waiting_days = State()


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
    if not (is_owner(cb.from_user.id) or is_admin(cb.from_user.id)):
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


def _kb_server_users_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    servers = _load_vpn_servers_admin()
    for idx, s in enumerate(servers, start=1):
        name = str(s.get("name") or s.get("code") or f"Server #{idx}")
        b.button(text=f"👥 Server #{idx} — {name}", callback_data=f"admin:vpn:server_users:{idx}")
    b.button(text="⬅️ Назад", callback_data="admin:menu")
    b.adjust(1)
    return b.as_markup()


def _kb_home_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
    )


def _message_html_text(message: Message) -> str:
    html = getattr(message, "html_text", None)
    if html:
        return str(html).strip()
    text = getattr(message, "text", None)
    return (text or "").strip()


def _message_html_caption(message: Message) -> str:
    html = getattr(message, "html_caption", None)
    if html:
        return str(html).strip()
    caption = getattr(message, "caption", None)
    return (caption or "").strip()


def _kb_user_card(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin:user:card:{tg_id}")],
            [InlineKeyboardButton(text="📨 Напомнить об оплате", callback_data=f"admin:user:notify_expired:{tg_id}")],
            [InlineKeyboardButton(text="🗓 Изменить дату окончания", callback_data=f"admin:user:set_end_at:{tg_id}")],
            [InlineKeyboardButton(text="💰 Цена места семьи", callback_data=f"admin:user:set_family_price:{tg_id}")],
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
    if u:
        try:
            lines.append(f"Создан: <b>{_fmt_dt_short(u.created_at)}</b>")
        except Exception:
            pass
    if sub_end:
        lines.append(
            f"Подписка: <b>{'активна' if sub_active else 'не активна'}</b> | до: <b>{_fmt_dt_short(sub_end)}</b>"
        )
    else:
        lines.append("Подписка: —")

    # Payments summary (best-effort)
    try:
        r = await session.execute(
            select(func.count(Payment.id), func.coalesce(func.sum(Payment.amount), 0), func.max(Payment.paid_at))
            .where(Payment.tg_id == tg_id, Payment.status == "success")
        )
        cnt, total_amt, last_paid = r.first() or (0, 0, None)
        if int(cnt or 0) > 0:
            lines.append(
                f"Оплаты: <b>{int(cnt)}</b> | Сумма: <b>{int(total_amt)} ₽</b> | Последняя: <b>{_fmt_dt_short(last_paid)}</b>"
            )
        else:
            lines.append("Оплаты: 0")
    except Exception:
        pass

    # Activity counters (best-effort, from app_settings; updated by middleware)
    try:
        total_actions = int(await get_app_setting_int(session, f"ua:actions_total:{tg_id}", default=0) or 0)
        msg_in = int(await get_app_setting_int(session, f"ua:messages_in:{tg_id}", default=0) or 0)
        clicks_in = int(await get_app_setting_int(session, f"ua:clicks_in:{tg_id}", default=0) or 0)
        last_ts = int(await get_app_setting_int(session, f"ua:last_interaction_ts:{tg_id}", default=0) or 0)
        last_seen = "—"
        if last_ts > 0:
            last_seen = _fmt_dt_short(datetime.fromtimestamp(last_ts, tz=timezone.utc))
        lines.append(f"Действия: <b>{total_actions}</b> (сообщ.: {msg_in}, клики: {clicks_in}) | Последняя активность: <b>{last_seen}</b>")
    except Exception:
        pass

    # Family VPN group (best-effort)
    try:
        from app.db.models import FamilyVpnGroup, FamilyVpnProfile

        grp = await session.scalar(select(FamilyVpnGroup).where(FamilyVpnGroup.owner_tg_id == tg_id).limit(1))
        if grp and int(grp.seats_total or 0) > 0:
            active_until = grp.active_until if grp.active_until and grp.active_until.tzinfo else (
                grp.active_until.replace(tzinfo=timezone.utc) if grp.active_until else None
            )
            # active profiles count
            prof_cnt = await session.scalar(
                select(func.count(FamilyVpnProfile.id)).where(
                    FamilyVpnProfile.owner_tg_id == tg_id, FamilyVpnProfile.vpn_peer_id.is_not(None)
                )
            )
            lines.append(
                f"Семейная группа VPN: мест <b>{int(grp.seats_total)}</b> | профилей создано: <b>{int(prof_cnt or 0)}</b> | до: <b>{_fmt_dt_short(active_until)}</b>"
            )
        else:
            lines.append("Семейная группа VPN: —")

        # show price override
        ov = int(await get_app_setting_int(session, f"family_seat_price_override:{tg_id}", default=0) or 0)
        default_price = int(await get_app_setting_int(session, "family_seat_price_default", default=100) or 100)
        if ov and ov > 0:
            lines.append(f"Цена места семьи: <b>{ov} ₽</b> (override)")
        else:
            lines.append(f"Цена места семьи: <b>{default_price} ₽</b> (общая)")
    except Exception:
        pass

    # Yandex membership snapshot (best-effort)
    try:
        ym = (
            await session.execute(
                select(YandexMembership)
                .where(YandexMembership.tg_id == tg_id)
                .order_by(YandexMembership.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if ym:
            if ym.removed_at is None:
                lines.append(
                    f"Yandex: ✅ в семье | label: <b>{ym.account_label or '—'}</b> | слот: <b>{ym.slot_index if ym.slot_index is not None else '—'}</b> | покрытие до: <b>{_fmt_dt_short(ym.coverage_end_at)}</b>"
                )
            else:
                lines.append(
                    f"Yandex: ❌ исключён | label: <b>{ym.account_label or '—'}</b> | слот: <b>{ym.slot_index if ym.slot_index is not None else '—'}</b> | removed: <b>{_fmt_dt_short(ym.removed_at)}</b>"
                )
        else:
            lines.append("Yandex: ❗️нет записи")
    except Exception:
        pass

    # WireGuard peers & IPs (best-effort)
    try:
        peers = list(
            (
                await session.execute(
                    select(VpnPeer)
                    .where(VpnPeer.tg_id == tg_id)
                    .order_by(VpnPeer.id.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
    except Exception:
        peers = []

    if peers:
        lines.append("")
        lines.append("🧷 <b>WireGuard профили</b>")

        # endpoints & latest handshakes per server (SSH best-effort)
        endpoints_by_key: dict[str, str] = {}
        handshakes_by_key: dict[str, int] = {}
        try:
            servers = _load_vpn_servers_admin()
            servers_by_code = {str(s.get('code') or '').upper(): s for s in servers}

            by_code: dict[str, list[str]] = {}
            for p in peers:
                code = str((p.server_code or '')).upper() if p.server_code else ''
                if not code:
                    # fall back to first server
                    code = next(iter(servers_by_code.keys()), '')
                by_code.setdefault(code, []).append(p.client_public_key)

            for code, keys in by_code.items():
                srv = servers_by_code.get(code)
                if not srv:
                    continue
                host = srv.get('host') or srv.get('VPN_SSH_HOST')
                user_ = srv.get('user')
                if not host or not user_:
                    continue
                prov = vpn_service._provider_for(
                    host=str(host),
                    port=int(srv.get('port') or 22),
                    user=str(user_),
                    password=srv.get('password'),
                    interface=str(srv.get('interface') or 'wg0'),
                )
                try:
                    eps = await prov.get_peer_endpoints()
                    for k in keys:
                        if k in eps:
                            endpoints_by_key[k] = eps[k]
                except Exception:
                    pass
                try:
                    hs = await prov.get_latest_handshakes()
                    for k in keys:
                        if k in hs:
                            handshakes_by_key[k] = int(hs[k] or 0)
                except Exception:
                    pass
        except Exception:
            pass

        for p in peers:
            active_txt = '✅' if p.is_active else '⛔️'
            code = (p.server_code or '—').upper() if p.server_code else '—'
            srv_label = _server_numbered_label(servers, code)
            vpn_ip = p.client_ip or '—'
            ep = endpoints_by_key.get(p.client_public_key)
            ep_txt = ep if ep else '—'
            hs_ts = int(handshakes_by_key.get(p.client_public_key, 0) or 0)
            hs_txt = '—'
            if hs_ts > 0:
                hs_txt = _fmt_dt_short(datetime.fromtimestamp(hs_ts, tz=timezone.utc))
            lines.append(
                f"• {active_txt} peer#{p.id} | {srv_label} | VPN-IP: <code>{vpn_ip}</code> | endpoint: <code>{ep_txt}</code> | last: {hs_txt}"
            )

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



@router.callback_query(lambda c: (c.data or "") == "admin:family_price")
async def admin_family_price_menu_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer()
    await state.clear()
    await state.set_state(AdminFamilyPriceMenuFSM.waiting_user)
    await cb.message.edit_text(
        "💰 <b>Цена места семьи</b>\n\n"
        "Отправьте <b>TG ID</b> пользователя (например <code>123456789</code>).\n"
        "(Username можно только если он уже писал боту.)",
        reply_markup=_kb_admin_back(),
        parse_mode="HTML",
    )


@router.message(AdminFamilyPriceMenuFSM.waiting_user)
async def admin_family_price_menu_user(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return
    raw = (message.text or "").strip()
    try:
        tg_id = int(raw)
    except Exception:
        await message.answer("Пришлите TG ID числом, например 123456789")
        return

    await state.update_data(tg_id=int(tg_id))
    await state.set_state(AdminFamilyPriceMenuFSM.waiting_price)
    await message.answer(
        "Отправьте цену в рублях (например <code>100</code>).\n"
        "Чтобы сбросить к общей цене — отправьте <code>0</code>.",
        parse_mode="HTML",
    )


@router.message(AdminFamilyPriceMenuFSM.waiting_price)
async def admin_family_price_menu_price(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return
    data = await state.get_data()
    tg_id = int(data.get("tg_id") or 0)
    raw = (message.text or "").strip()
    try:
        price = int(raw)
    except Exception:
        await message.answer("Введите число, например 100")
        return

    price = max(0, min(10_000, price))

    async with session_scope() as session:
        if price == 0:
            await set_app_setting_int(session, f"family_seat_price_override:{tg_id}", 0)
        else:
            await set_app_setting_int(session, f"family_seat_price_override:{tg_id}", price)
        await session.commit()
        card = await _render_user_card(session, message.bot, tg_id)

    await state.clear()
    await message.answer(card, parse_mode="HTML", reply_markup=_kb_admin_user_card_actions(tg_id))


@router.callback_query(lambda c: (c.data or "").startswith("admin:user:set_family_price:"))
async def admin_user_set_family_price_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer()
    try:
        tg_id = int((cb.data or "").split(":")[-1])
    except Exception:
        return
    await state.clear()
    await state.set_state(AdminUserSetFamilyPriceFSM.waiting_price)
    await state.update_data(tg_id=tg_id)
    await cb.message.edit_text(
        "💰 <b>Цена места в семейной группе (для этого пользователя)</b>\n\n"
        "Отправьте число в рублях (например <code>100</code>).\n"
        "Чтобы сбросить к общей цене — отправьте <code>0</code>.",
        reply_markup=_kb_admin_back(),
        parse_mode="HTML",
    )


@router.message(AdminUserSetFamilyPriceFSM.waiting_price)
async def admin_user_set_family_price_input(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return
    data = await state.get_data()
    tg_id = int(data.get("tg_id") or 0)
    raw = (message.text or "").strip()
    try:
        price = int(raw)
    except Exception:
        await message.answer("Введите число, например 100")
        return

    price = max(0, min(10_000, price))

    async with session_scope() as session:
        if price == 0:
            await set_app_setting_int(session, f"family_seat_price_override:{tg_id}", 0)
        else:
            await set_app_setting_int(session, f"family_seat_price_override:{tg_id}", price)
        await session.commit()
        card = await _render_user_card(session, message.bot, tg_id)

    await state.clear()
    await message.answer("✅ Сохранено.")
    await message.answer(card, reply_markup=_kb_user_card(tg_id), parse_mode="HTML")


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




@router.callback_query(lambda c: c.data == "admin:lte_price")
async def admin_lte_price(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    async with session_scope() as session:
        current_price = await get_app_setting_int(session, "lte_activation_rub", default=settings.lte_activation_rub)

    text = (
        "📶 <b>Цена VPN LTE</b>\n\n"
        f"Текущая цена активации: <b>{current_price} ₽</b>\n\n"
        "Введите новую цену для активации VPN LTE (целое число в рублях), например: <code>99</code>"
    )

    await state.set_state(AdminLtePriceFSM.waiting_price)

    try:
        await cb.message.edit_text(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await cb.message.answer(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
        else:
            raise
    await cb.answer()


@router.message(AdminLtePriceFSM.waiting_price)
async def admin_lte_price_set(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    raw = re.sub(r"[^0-9]", "", (message.text or "").strip())
    if not raw:
        await message.answer("❌ Введите цену числом, например: 99", reply_markup=_kb_admin_back())
        return

    try:
        new_price = int(raw)
    except Exception:
        await message.answer("❌ Введите цену числом, например: 99", reply_markup=_kb_admin_back())
        return

    if new_price < 0 or new_price > 1_000_000:
        await message.answer("❌ Некорректная цена. Укажите значение от 0 до 1 000 000 ₽", reply_markup=_kb_admin_back())
        return

    async with session_scope() as session:
        await set_app_setting_int(session, "lte_activation_rub", new_price)
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ Цена активации VPN LTE обновлена: <b>{new_price} ₽</b>",
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




@router.callback_query(lambda c: (c.data or "") in {"admin:sub:gift_days:all", "admin:sub:gift_days:active"})
async def admin_sub_gift_days_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    mode = "active" if (cb.data or "").endswith(":active") else "all"
    await state.clear()
    await state.update_data(gift_days_mode=mode)
    await state.set_state(AdminGiftDaysFSM.waiting_days)

    title = "активным" if mode == "active" else "всем"
    text = (
        f"🎁 <b>Подарить дни {title}</b>\n\n"
        "Введите количество дней целым числом, например: <code>3</code> или <code>7</code>.\n\n"
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


@router.message(AdminGiftDaysFSM.waiting_days)
async def admin_sub_gift_days_finish(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    raw = re.sub(r"[^0-9]", "", (message.text or "").strip())
    if not raw:
        await message.answer("❌ Введите количество дней числом, например: 3", reply_markup=_kb_admin_back())
        return

    try:
        days = int(raw)
    except Exception:
        await message.answer("❌ Введите количество дней числом, например: 3", reply_markup=_kb_admin_back())
        return

    if days <= 0 or days > 3650:
        await message.answer("❌ Укажите от 1 до 3650 дней.", reply_markup=_kb_admin_back())
        return

    data = await state.get_data()
    mode = str(data.get("gift_days_mode") or "all")
    now = _utcnow()

    async with session_scope() as session:
        if mode == "active":
            rows = await session.execute(
                select(Subscription.tg_id).where(
                    Subscription.is_active == True,
                    Subscription.end_at.is_not(None),
                    Subscription.end_at > now,
                )
            )
            target_ids = [int(x) for x in rows.scalars().all()]
        else:
            rows = await session.execute(select(User.tg_id))
            target_ids = [int(x) for x in rows.scalars().all()]

        touched = 0
        activated = 0
        for tg_id in target_ids:
            sub = await session.get(Subscription, tg_id)
            if not sub:
                sub = await get_subscription(session, tg_id)

            was_active = bool(sub.is_active and sub.end_at and sub.end_at > now)
            base = sub.end_at if sub.end_at and sub.end_at > now else now
            sub.end_at = base + timedelta(days=days)
            if not sub.start_at:
                sub.start_at = now
            sub.is_active = True
            sub.status = "active"
            touched += 1
            if not was_active:
                activated += 1

        await session.commit()

    await state.clear()

    mode_label = "активным пользователям" if mode == "active" else "всем пользователям"
    await message.answer(
        "✅ Дни подарены.\n\n"
        f"Кому: <b>{mode_label}</b>\n"
        f"Дней: <b>{days}</b>\n"
        f"Обработано пользователей: <b>{touched}</b>\n"
        f"Продлено подписок: <b>{touched}</b>\n"
        f"Из них были неактивны и стали активны: <b>{activated}</b>",
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
        f"✍️ Теперь отправьте текст сообщения или фото с подписью для <code>{tg_id}</code>.\n\nФорматирование <b>жирным</b> и <i>курсивом</i> сохранится у пользователя.",
        reply_markup=_kb_admin_back(),
        parse_mode="HTML",
    )


@router.message(AdminBroadcastFSM.waiting_text)
async def admin_broadcast_send(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    photo = None
    payload = ""
    parse_mode = "HTML"
    entities = None
    caption_entities = None

    if message.photo:
        photo = message.photo[-1].file_id
        # Важно: при отправке с entities нельзя подставлять html_caption,
        # потому что offsets у caption_entities рассчитаны для исходного raw caption.
        payload = (message.caption or "").strip()
        caption_entities = list(message.caption_entities or []) or None
        if not payload:
            await message.answer(
                "❌ У фото должна быть подпись. Добавьте текст к фото, чтобы отправить рассылку.",
                reply_markup=_kb_admin_back(),
            )
            return
    else:
        # Аналогично для текста: либо raw text + entities, либо HTML без entities.
        # Здесь используем raw text, чтобы корректно сохранить форматирование.
        payload = (message.text or "").strip()
        entities = list(message.entities or []) or None
        if not payload:
            await message.answer(
                "❌ Отправьте текст рассылки или фото с подписью.",
                reply_markup=_kb_admin_back(),
            )
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
            ok = await audit_send_message(
                message.bot,
                target,
                payload,
                kind="admin_broadcast_one",
                reply_markup=_kb_home_menu(),
                parse_mode=parse_mode,
                photo=photo,
                entities=entities,
                caption_entities=caption_entities,
            )
            sent = 1 if ok else 0
            failed = 0 if ok else 1
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
        targets = list(dict.fromkeys(int(x) for x in res.scalars().all()))

    for target in targets:
        try:
            ok = await audit_send_message(
                message.bot,
                target,
                payload,
                kind=f"admin_broadcast_{mode or 'all'}",
                reply_markup=_kb_home_menu(),
                parse_mode=parse_mode,
                photo=photo,
                entities=entities,
                caption_entities=caption_entities,
            )
            if ok:
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.03)

    await state.clear()
    mode_label = {
        "all": "всем пользователям",
        "paid": "пользователям с активной подпиской",
        "unpaid": "пользователям без активной подписки",
    }.get(mode, "пользователям")
    content_label = "фото с подписью" if photo else "текст"
    await message.answer(
        "✅ <b>Рассылка завершена</b>\n\n"
        f"Сегмент: <b>{mode_label}</b>\n"
        f"Тип: <b>{content_label}</b>\n"
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
        online_now = st.get("active_peers")
        server_peers = st.get("total_peers")
        cpu_s = "—" if cpu is None else f"{cpu:.0f}%"
        online_s = "—" if online_now is None else str(online_now)
        server_peers_s = "—" if server_peers is None else str(server_peers)
        text = (
            "📊 <b>Статус VPN</b>\n\n"
            f"CPU: <b>{cpu_s}</b>\n"
            f"Онлайн сейчас: <b>{online_s}</b>\n"
            f"WG-пиров на сервере: <b>{server_peers_s}</b>\n\n"
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
        used_map = await _vpn_seats_by_server()
        servers = _load_vpn_servers_admin()
        seat_lines: list[str] = []
        for idx, s in enumerate(servers, start=1):
            code = str(s.get("code") or os.environ.get("VPN_CODE", "NL")).upper()
            name = str(s.get("name") or code)
            used = sum(int(used_map.get(alias, 0)) for alias in _server_code_aliases(servers, code))
            try:
                cap = max(1, int(s.get("max_active") if s.get("max_active") is not None else os.environ.get("VPN_MAX_ACTIVE", "40")))
            except Exception:
                cap = 40
            free = max(0, cap - used)
            seat_lines.append(f"Server #{idx} — {name}: WG-слотов <b>{used}</b>/{cap} | свободно: <b>{free}</b>")
        if seat_lines:
            text += "\n\n👥 <b>Места по локациям</b>\n" + "\n".join(seat_lines)
    except Exception:
        text += "\n\n👥 <b>Места по локациям</b>\n⚠️ Не удалось рассчитать свободные места."

    try:
        async with session_scope() as session:
            lte_price = await get_app_setting_int(session, "lte_activation_rub", default=settings.lte_activation_rub)
        lte_used = await lte_vpn_service.active_clients_count() if settings.lte_enabled else 0
        lte_free = max(0, int(settings.lte_max_clients) - int(lte_used))
        text += (
            f"\n\n📶 <b>VPN LTE</b>\n"
            f"Цена активации: <b>{lte_price} ₽</b>\n"
            f"Активных профилей: <b>{lte_used}</b>/<b>{int(settings.lte_max_clients)}</b>\n"
            f"Свободно: <b>{lte_free}</b>"
        )
    except Exception:
        text += "\n\n📶 <b>VPN LTE</b>\n⚠️ Не удалось получить статус мест."

    try:
        await cb.message.edit_text(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await cb.message.answer(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
        else:
            raise


@router.callback_query(lambda c: c.data == "admin:vpn:extra")
async def admin_vpn_extra_start(cb: CallbackQuery, state: FSMContext) -> None:
    """Admins: create extra WG configs for themselves (multiple devices)."""
    if not is_admin(cb.from_user.id):
        await cb.answer()
        return

    await cb.answer()
    await state.clear()
    await state.set_state(AdminVpnExtraFSM.waiting_count)
    await cb.message.edit_text(
        "➕ <b>Доп. устройства для админа</b>\n\n"
        "Сколько дополнительных WireGuard-конфигов создать для вас?\n"
        "Введите число от <b>1</b> до <b>5</b>.",
        reply_markup=_kb_admin_back(),
        parse_mode="HTML",
    )


@router.message(AdminVpnExtraFSM.waiting_count)
async def admin_vpn_extra_finish(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    raw = (message.text or "").strip()
    try:
        n = int(raw)
    except Exception:
        await message.answer("Введите число от 1 до 5.")
        return
    if n < 1 or n > 5:
        await message.answer("Введите число от 1 до 5.")
        return

    tg_id = int(message.from_user.id)

    from app.services.vpn.service import VPNService
    from aiogram.types import BufferedInputFile

    try:
        vpn_svc = VPNService()
    except Exception:
        await state.clear()
        await message.answer("⚠️ VPN сервис не настроен (нет WG_* env).", reply_markup=kb_admin_menu())
        return

    created = 0
    async with session_scope() as session:
        for _ in range(n):
            try:
                peer = await vpn_svc.create_extra_peer(session, tg_id)
                try:
                    await vpn_svc.ensure_rate_limit(tg_id=tg_id, ip=str(peer.get("client_ip") or ""))
                except Exception:
                    pass
                conf_text = vpn_svc.build_wg_conf(
                    peer,
                    user_label=str(tg_id),
                    server_public_key=vpn_svc.server_pub,
                    endpoint=vpn_svc.endpoint,
                    dns=vpn_svc.dns,
                )
                filename = f"admin-{tg_id}-extra-{int(peer.get('peer_id') or 0)}.conf"
                await message.answer_document(
                    document=BufferedInputFile(conf_text.encode("utf-8"), filename=filename),
                    caption="WireGuard конфиг (доп. устройство).",
                )
                created += 1
            except Exception:
                pass

        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ Создано конфигов: <b>{created}</b> из <b>{n}</b>.",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )



@router.callback_query(lambda c: c.data == "admin:vpn:test_peer:2")
async def admin_vpn_test_peer_server2(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        await cb.answer("Создаю тестовый пир…")
    except Exception:
        pass

    from aiogram.types import BufferedInputFile

    servers = _load_vpn_servers_admin()
    if len(servers) < 2:
        await cb.message.answer(
            "⚠️ Server #2 не найден в VPN_SERVERS_JSON.",
            reply_markup=_kb_admin_back(),
        )
        return

    srv = servers[1]
    code = str(srv.get("code") or os.environ.get("VPN_CODE", "NL")).upper()
    host = str(srv.get("host") or "")
    user = str(srv.get("user") or "")
    port = int(srv.get("port") or 22)
    password = srv.get("password")
    interface = str(srv.get("interface") or os.environ.get("VPN_INTERFACE", "wg0"))
    server_public_key = str(srv.get("server_public_key") or "")
    endpoint = str(srv.get("endpoint") or "")
    dns = str(srv.get("dns") or os.environ.get("VPN_DNS") or "1.1.1.1")

    if not host or not user or not server_public_key or not endpoint:
        await cb.message.answer(
            "⚠️ Server #2 настроен не полностью. Нужны host/user/server_public_key/endpoint.",
            reply_markup=_kb_admin_back(),
        )
        return

    tg_id = int(cb.from_user.id)
    try:
        async with session_scope() as session:
            peer = await vpn_service.ensure_peer_for_server(
                session,
                tg_id,
                server_code=code,
                host=host,
                port=port,
                user=user,
                password=password,
                interface=interface,
            )
            try:
                await vpn_service.ensure_rate_limit_for_server(
                    tg_id=tg_id,
                    ip=str(peer.get("client_ip") or ""),
                    host=host,
                    port=port,
                    user=user,
                    password=password,
                    interface=interface,
                )
            except Exception:
                pass
            await session.commit()

        conf_text = vpn_service.build_wg_conf(
            peer,
            user_label=f"admin-test-{tg_id}",
            server_public_key=server_public_key,
            endpoint=endpoint,
            dns=dns,
        )
        filename = f"server2-test-{tg_id}.conf"
        await cb.message.answer_document(
            document=BufferedInputFile(conf_text.encode("utf-8"), filename=filename),
            caption=(
                f"🧪 Тестовый WireGuard-конфиг для <b>{_server_numbered_label(servers, code)}</b>\n"
                f"IP: <code>{peer.get('client_ip')}</code>"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        await cb.message.answer(
            "⚠️ Не удалось создать тестовый пир для Server #2.\n\n"
            f"Причина: <code>{type(e).__name__}: {str(e)[:350]}</code>",
            reply_markup=_kb_admin_back(),
            parse_mode="HTML",
        )

@router.callback_query(lambda c: c.data == "admin:vpn:test_peer:2:reset")
async def admin_vpn_test_peer_server2_reset(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    try:
        await cb.answer("Сбрасываю test peer Server #2…")
    except Exception:
        pass

    servers = _load_vpn_servers_admin()
    server = None
    for s in servers:
        code = str(s.get("code") or "").upper()
        if code in {"NL2", "SERVER2", "SERVER #2"}:
            server = s
            break
    if not server:
        await cb.message.answer("⚠️ Server #2 не найден в VPN_SERVERS_JSON.", reply_markup=_kb_admin_back())
        return

    host = str(server.get("host") or "")
    port = int(server.get("port") or 22)
    user = str(server.get("user") or "")
    password = server.get("password") or None
    interface = str(server.get("interface") or os.environ.get("VPN_INTERFACE", "wg0"))
    code = str(server.get("code") or "NL2").upper()

    removed = 0
    async with session_scope() as session:
        from app.db.models.vpn_peer import VpnPeer
        q = (
            select(VpnPeer)
            .where(VpnPeer.tg_id == cb.from_user.id, VpnPeer.is_active == True)
            .where(func.coalesce(func.upper(VpnPeer.server_code), literal('NL')).in_([code]))
            .order_by(VpnPeer.id.desc())
        )
        rows = list((await session.execute(q)).scalars().all())
        provider = vpn_service._provider_for(host=host, port=port, user=user, password=password, interface=interface)
        for row in rows:
            try:
                await provider.remove_peer(row.client_public_key)
            except Exception:
                pass
            row.is_active = False
            row.revoked_at = utcnow()
            row.rotation_reason = "admin_test_reset"
            removed += 1
        await session.commit()

    await cb.message.answer(
        f"🧹 TEST Server #2 сброшен. Деактивировано профилей: <b>{removed}</b>",
        reply_markup=_kb_admin_back(),
        parse_mode="HTML",
    )

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
    servers = _load_vpn_servers_admin()

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
        code = (row.server_code or os.environ.get("VPN_CODE", "NL")).upper()
        srv_label = _server_numbered_label(servers, code)
        # keep tg_id in the end for unambiguous matching
        lines.append(
            f"{shown}. {who} | {srv_label} | <code>{row.client_public_key[:8]}…</code> | "
            f"{row.client_ip} | sub {sub_state} | hs {age_s} | id <code>{row.tg_id}</code>"
        )
        if shown >= 25:
            break

    if shown == 0:
        lines.append("(Не удалось сопоставить активные рукопожатия с пирами в БД.)")

    if len(recent) > shown:
        lines.append("")
        lines.append(f"Показано: <b>{shown}</b> (лимит 25)")

    now = datetime.now(timezone.utc)
    lte_cutoff = now - timedelta(seconds=180)
    async with session_scope() as session:
        lte_res = await session.execute(
            select(LteVpnClient, Subscription)
            .join(Subscription, Subscription.tg_id == LteVpnClient.tg_id)
            .where(
                LteVpnClient.is_enabled == True,  # noqa: E712
                LteVpnClient.last_seen_at.is_not(None),
                LteVpnClient.last_seen_at > lte_cutoff,
                Subscription.is_active == True,  # noqa: E712
                Subscription.end_at.is_not(None),
                Subscription.end_at > now,
            )
            .order_by(LteVpnClient.last_seen_at.desc())
            .limit(25)
        )
        lte_rows = lte_res.all()

    if lte_rows:
        lines.append("")
        lines.append("📶 <b>Активные LTE-профили</b>")
        lines.append("")
        lte_tg_ids = sorted({int(row.tg_id) for row, _sub in lte_rows})[:50]
        try:
            lte_labels = await asyncio.gather(*[_tg_label(cb.bot, tid) for tid in lte_tg_ids], return_exceptions=True)
            lte_label_map = {}
            for tid, lbl in zip(lte_tg_ids, lte_labels):
                lte_label_map[tid] = f"ID {tid}" if isinstance(lbl, Exception) else str(lbl)
        except Exception:
            lte_label_map = {}
        for idx, (row, _sub) in enumerate(lte_rows, start=1):
            who = lte_label_map.get(int(row.tg_id)) or f"ID {row.tg_id}"
            seen_dt = row.last_seen_at
            if seen_dt and seen_dt.tzinfo is None:
                seen_dt = seen_dt.replace(tzinfo=timezone.utc)
            age_s = "—" if not seen_dt else f"{int((now - seen_dt).total_seconds())}s"
            lines.append(f"{idx}. {who} | <code>{row.uuid[:8]}…</code> | LTE | hs {age_s} | id <code>{row.tg_id}</code>")

    text = "\n".join(lines)

    try:
        await cb.message.edit_text(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await cb.message.answer(text, reply_markup=_kb_admin_back(), parse_mode="HTML")
        else:
            raise


@router.callback_query(lambda c: c.data == "admin:vpn:server_users")
async def admin_vpn_server_users_menu(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer()
    text = "🗂 <b>Пользователи по серверам</b>\n\nВыберите сервер."
    try:
        await cb.message.edit_text(text, reply_markup=_kb_server_users_menu(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await cb.message.answer(text, reply_markup=_kb_server_users_menu(), parse_mode="HTML")
        else:
            raise


@router.callback_query(lambda c: (c.data or "").startswith("admin:vpn:server_users:"))
async def admin_vpn_server_users_list(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer()
    try:
        idx = int((cb.data or '').split(':')[-1])
    except Exception:
        idx = 1
    servers = _load_vpn_servers_admin()
    if idx < 1 or idx > len(servers):
        await cb.message.answer("⚠️ Сервер не найден.", reply_markup=_kb_server_users_menu())
        return
    srv = servers[idx-1]
    code = str(srv.get('code') or os.environ.get('VPN_CODE', 'NL')).upper()
    aliases = _server_code_aliases(servers, code)

    from app.db.models.vpn_peer import VpnPeer
    from app.db.models.subscription import Subscription
    from app.db.models.family_vpn_profile import FamilyVpnProfile

    now = datetime.now(timezone.utc)
    async with session_scope() as session:
        q = (
            select(VpnPeer, Subscription)
            .outerjoin(Subscription, Subscription.tg_id == VpnPeer.tg_id)
            .where(VpnPeer.is_active == True, func.coalesce(func.upper(VpnPeer.server_code), literal('NL')).in_(list(aliases)))  # noqa: E712
            .order_by(VpnPeer.tg_id.asc(), VpnPeer.id.asc())
            .limit(500)
        )
        rows = (await session.execute(q)).all()
        peer_ids = [int(row.id) for row, _sub in rows]
        family_rows = []
        if peer_ids:
            family_rows = list((await session.execute(
                select(FamilyVpnProfile).where(FamilyVpnProfile.vpn_peer_id.in_(peer_ids))
            )).scalars().all())

    text_lines = [f"🗂 <b>Пользователи { _server_numbered_label(servers, code) }</b>", ""]
    if not rows:
        text_lines.append("Сейчас на этом сервере нет активных VPN-профилей в БД.")
    else:
        tg_ids = sorted({int(row.tg_id) for row, _sub in rows})[:300]
        try:
            labels = await asyncio.gather(*[_tg_label(cb.bot, tid) for tid in tg_ids], return_exceptions=True)
            label_map = {tid: (f"ID {tid}" if isinstance(lbl, Exception) else str(lbl)) for tid, lbl in zip(tg_ids, labels)}
        except Exception:
            label_map = {}

        family_by_peer_id = {int(fp.vpn_peer_id): fp for fp in family_rows if getattr(fp, 'vpn_peer_id', None)}
        by_user: dict[int, dict] = {}
        family_total = 0
        for row, sub in rows:
            tg_id = int(row.tg_id)
            bucket = by_user.setdefault(tg_id, {
                'rows': [],
                'active_sub': False,
                'family_profiles': [],
                'personal_profiles': [],
            })
            bucket['rows'].append(row)
            if sub and getattr(sub, 'is_active', False) and getattr(sub, 'end_at', None) and sub.end_at > now:
                bucket['active_sub'] = True
            fp = family_by_peer_id.get(int(row.id))
            if fp is not None:
                bucket['family_profiles'].append((row, fp))
                family_total += 1
            else:
                bucket['personal_profiles'].append(row)

        extra_total = 0
        for info in by_user.values():
            personal_cnt = len(info['personal_profiles'])
            if personal_cnt > 1:
                extra_total += personal_cnt - 1

        text_lines.append(f"Пользователей: <b>{len(by_user)}</b>")
        text_lines.append(f"Всего WG-профилей: <b>{len(rows)}</b>")
        text_lines.append(f"Семейных профилей: <b>{family_total}</b>")
        text_lines.append(f"Доп. устройств: <b>{extra_total}</b>")
        text_lines.append("")

        items = sorted(by_user.items(), key=lambda kv: (-(len(kv[1]['rows'])), kv[0]))
        for n, (tg_id, info) in enumerate(items[:100], start=1):
            who = label_map.get(tg_id) or f"ID {tg_id}"
            personal_cnt = len(info['personal_profiles'])
            family_cnt = len(info['family_profiles'])
            parts = []
            if personal_cnt > 0:
                parts.append(f"личных: {personal_cnt}")
            if family_cnt > 0:
                parts.append(f"семейных: {family_cnt}")
            details = ', '.join(parts) if parts else 'профилей: 0'

            personal_ips = [r.client_ip for r in info['personal_profiles'][:3]]
            family_desc = []
            for _r, fp in sorted(info['family_profiles'], key=lambda x: int(x[1].slot_no or 0))[:3]:
                slot_no = int(fp.slot_no or 0)
                label = (fp.label or '').strip()
                family_desc.append(f"#{slot_no}{' ' + label if label else ''}".strip())
            detail_tail = []
            if personal_ips:
                detail_tail.append("IP: " + ', '.join(f"<code>{ip}</code>" for ip in personal_ips))
            if family_desc:
                detail_tail.append("Семья: " + ', '.join(family_desc))
            tail = f" | {' | '.join(detail_tail)}" if detail_tail else ''

            text_lines.append(
                f"{n}. {who} | {details} | sub {'✅' if info['active_sub'] else '—'} | id <code>{tg_id}</code>{tail}"
            )
        if len(items) > 100:
            text_lines.append("")
            text_lines.append(f"Показано: <b>100</b> из <b>{len(items)}</b> пользователей")
    try:
        await cb.message.edit_text("\n".join(text_lines), reply_markup=_kb_server_users_menu(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await cb.message.answer("\n".join(text_lines), reply_markup=_kb_server_users_menu(), parse_mode="HTML")
        else:
            raise


@router.callback_query(lambda c: c.data == "admin:vpn:active_lte_profiles")
async def admin_vpn_active_lte_profiles(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        await cb.answer()
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    online_cutoff = now - timedelta(seconds=180)
    async with session_scope() as session:
        q = (
            select(LteVpnClient, Subscription)
            .outerjoin(Subscription, Subscription.tg_id == LteVpnClient.tg_id)
            .where(
                LteVpnClient.is_enabled == True,  # noqa: E712
                or_(
                    and_(Subscription.is_active == True, Subscription.end_at.is_not(None), Subscription.end_at > now),
                    and_(LteVpnClient.cycle_anchor_end_at.is_not(None), LteVpnClient.cycle_anchor_end_at > now),
                ),
            )
            .order_by(LteVpnClient.last_seen_at.desc().nullslast(), LteVpnClient.updated_at.desc().nullslast())
            .limit(50)
        )
        rows = (await session.execute(q)).all()

    lines = ["📶 <b>Активные LTE-профили</b>", ""]
    if not rows:
        lines.append("Сейчас нет активированных LTE-профилей.")
    else:
        lines.append(f"Занято мест: <b>{len(rows)}</b>/<b>{int(settings.lte_max_clients)}</b>")
        lines.append("")
        tg_ids = sorted({int(row.tg_id) for row, _ in rows})[:50]
        try:
            labels = await asyncio.gather(*[_tg_label(cb.bot, tid) for tid in tg_ids], return_exceptions=True)
            label_map = {tid: (f"ID {tid}" if isinstance(lbl, Exception) else str(lbl)) for tid, lbl in zip(tg_ids, labels)}
        except Exception:
            label_map = {}
        for idx, (row, sub) in enumerate(rows, start=1):
            who = label_map.get(int(row.tg_id)) or f"ID {row.tg_id}"
            seen_dt = row.last_seen_at
            if seen_dt and seen_dt.tzinfo is None:
                seen_dt = seen_dt.replace(tzinfo=timezone.utc)
            is_online = bool(seen_dt and seen_dt > online_cutoff)
            online_text = "🟢 онлайн" if is_online else "⚪️ не в сети"
            age_s = "—" if not seen_dt else f"{int((now - seen_dt).total_seconds())}s назад"
            until_dt = None
            if sub and getattr(sub, 'end_at', None):
                until_dt = sub.end_at
            elif row.cycle_anchor_end_at:
                until_dt = row.cycle_anchor_end_at
            until_txt = _fmt_dt_short(until_dt) if until_dt else "—"
            lines.append(
                f"{idx}. {who} | <code>{(row.uuid or '')[:8]}…</code> | {online_text} | последняя активность: <b>{age_s}</b> | активно до: <b>{until_txt}</b> | id <code>{row.tg_id}</code>"
            )

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


@router.callback_query(lambda c: c.data == "admin:vpn:self_cleanup")
async def admin_vpn_self_cleanup(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer()
        return

    tg_id = int(cb.from_user.id)
    async with session_scope() as session:
        fam_peer_ids = set((await session.execute(
            select(FamilyVpnProfile.vpn_peer_id).where(
                FamilyVpnProfile.owner_tg_id == tg_id,
                FamilyVpnProfile.vpn_peer_id.is_not(None),
            )
        )).scalars().all())

        q = select(VpnPeer).where(VpnPeer.tg_id == tg_id).order_by(VpnPeer.id.asc())
        rows = list((await session.execute(q)).scalars().all())

    personal = [p for p in rows if int(getattr(p, 'id', 0) or 0) not in fam_peer_ids]
    family_cnt = len([p for p in rows if int(getattr(p, 'id', 0) or 0) in fam_peer_ids])
    ips = [str(getattr(p, 'client_ip', '') or '') for p in personal[:10] if getattr(p, 'client_ip', None)]

    text = [
        '🧹 <b>Очистка моих личных WireGuard-профилей</b>',
        '',
        f'Ваш TG ID: <code>{tg_id}</code>',
        f'Личных профилей будет удалено: <b>{len(personal)}</b>',
        f'Семейных профилей останется: <b>{family_cnt}</b>',
    ]
    if ips:
        text.append('IP личных профилей: ' + ', '.join(f'<code>{ip}</code>' for ip in ips))
    if len(personal) > 10:
        text.append(f'И ещё: <b>{len(personal) - 10}</b>')
    text += [
        '',
        '⚠️ Будут удалены именно <b>личные</b> WG-профили из БД и с сервера.',
        'Семейные профили эта кнопка не трогает.',
    ]
    try:
        await cb.message.edit_text('\n'.join(text), reply_markup=_kb_admin_self_cleanup_confirm(), parse_mode='HTML')
    except TelegramBadRequest as e:
        if 'message is not modified' in str(e):
            await cb.message.answer('\n'.join(text), reply_markup=_kb_admin_self_cleanup_confirm(), parse_mode='HTML')
        else:
            raise
    await cb.answer()


@router.callback_query(lambda c: c.data == "admin:vpn:self_cleanup:do")
async def admin_vpn_self_cleanup_do(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer()
        return

    tg_id = int(cb.from_user.id)
    status = await cb.message.edit_text('⏳ Удаляю ваши личные WG-профили...', reply_markup=None)

    removed_db = 0
    removed_remote = 0
    remote_fail = 0
    async with session_scope() as session:
        fam_peer_ids = set((await session.execute(
            select(FamilyVpnProfile.vpn_peer_id).where(
                FamilyVpnProfile.owner_tg_id == tg_id,
                FamilyVpnProfile.vpn_peer_id.is_not(None),
            )
        )).scalars().all())

        q = select(VpnPeer).where(VpnPeer.tg_id == tg_id).order_by(VpnPeer.id.asc())
        all_rows = list((await session.execute(q)).scalars().all())
        personal = [p for p in all_rows if int(getattr(p, 'id', 0) or 0) not in fam_peer_ids]

        for peer in personal:
            removed_here = False
            for provider in _providers_for_server_code_admin(getattr(peer, 'server_code', None)):
                try:
                    await provider.remove_peer(str(peer.client_public_key))
                    removed_remote += 1
                    removed_here = True
                    break
                except Exception:
                    log.exception('admin_self_cleanup_remove_remote_failed tg_id=%s peer_id=%s host=%s', tg_id, getattr(peer, 'id', None), getattr(provider, 'host', None))
            if not removed_here:
                remote_fail += 1

        peer_ids = [int(p.id) for p in personal if getattr(p, 'id', None)]
        if peer_ids:
            await session.execute(delete(VpnPeer).where(VpnPeer.id.in_(peer_ids)))
            removed_db = len(peer_ids)
        await session.commit()

    text = [
        '✅ <b>Личные WG-профили очищены</b>',
        '',
        f'Удалено из БД: <b>{removed_db}</b>',
        f'Удалено с сервера: <b>{removed_remote}</b>',
    ]
    if remote_fail:
        text.append(f'Не удалось снять с сервера автоматически: <b>{remote_fail}</b>')
    text += [
        '',
        'Семейные профили не затронуты.',
    ]
    await status.edit_text('\n'.join(text), reply_markup=kb_admin_menu(), parse_mode='HTML')
    await cb.answer('Готово')


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
