from __future__ import annotations

import asyncio
import re
import os
import json
import html
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
from sqlalchemy import func, select, literal, and_, or_, delete, text

from dateutil.relativedelta import relativedelta

from app.bot.auth import is_owner, is_admin
from app.bot.keyboards import kb_admin_menu, kb_admin_referrals_menu
from app.core.config import settings
from app.db.models import Referral, ReferralEarning, Subscription, User, Payment
from app.db.models.vpn_peer import VpnPeer
from app.db.models.family_vpn_group import FamilyVpnGroup
from app.db.models.family_vpn_profile import FamilyVpnProfile
from app.db.models.lte_vpn_client import LteVpnClient
from app.db.models import MessageAudit
from app.db.models.app_setting import AppSetting
from app.db.models.payout_request import PayoutRequest
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_invite_slot import YandexInviteSlot
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.repo import get_price_rub, set_app_setting_int, get_subscription, extend_subscription, get_app_setting_int
from app.services.referrals.service import referral_service
from app.services.vpn.service import vpn_service, gen_keys
from app.services.vpn.ssh_provider import WireGuardSSHProvider
from app.services.regionvpn import RegionVpnService
from app.services.lte_vpn.service import lte_vpn_service
from app.services.message_audit import audit_send_message
from app.services.health import health_service


log = logging.getLogger(__name__)


def _split_html_lines(lines: list[str], *, limit: int = 3500) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in lines:
        extra = len(line) + (1 if cur else 0)
        if cur and cur_len + extra > limit:
            parts.append("\n".join(cur))
            cur = [line]
            cur_len = len(line)
            continue
        cur.append(line)
        cur_len += extra
    if cur:
        parts.append("\n".join(cur))
    return parts

async def _send_html_chunks(message: Message, parts: list[str], *, reply_markup=None, edit_first: bool = True) -> None:
    sent_any = False
    for idx, raw in enumerate(parts):
        chunk = (raw or '').strip() or '—'
        try:
            if idx == 0 and edit_first:
                await message.edit_text(chunk, reply_markup=reply_markup, parse_mode="HTML")
            else:
                await message.answer(chunk, reply_markup=reply_markup if (idx == 0 and not edit_first) else None, parse_mode="HTML")
            sent_any = True
            continue
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if 'message is not modified' in msg:
                sent_any = True
                continue
            if 'message_too_long' not in msg and 'message is too long' not in msg and "can't parse entities" not in msg:
                raise
        plain = re.sub(r"</?[^>]+>", "", chunk).strip() or '—'
        subparts = [plain[i:i+2500] for i in range(0, len(plain), 2500)] or ['—']
        for sub_idx, sub in enumerate(subparts):
            if idx == 0 and edit_first and not sent_any and sub_idx == 0:
                try:
                    await message.edit_text(sub, reply_markup=reply_markup)
                    sent_any = True
                    continue
                except TelegramBadRequest:
                    pass
            await message.answer(sub, reply_markup=reply_markup if (idx == 0 and sub_idx == 0 and not edit_first) else None)
            sent_any = True


async def _send_plain_chunks(message: Message, parts: list[str], *, reply_markup=None, edit_first: bool = False) -> None:
    sent_any = False
    for idx, raw in enumerate(parts):
        chunk = (raw or '').strip() or '—'
        chunk = chunk[:3500]
        try:
            if idx == 0 and edit_first:
                await message.edit_text(chunk, reply_markup=reply_markup)
            else:
                await message.answer(chunk, reply_markup=reply_markup if (idx == 0 and not edit_first) else None)
            sent_any = True
            continue
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if 'message is not modified' in msg:
                sent_any = True
                continue
            if 'message_too_long' not in msg and 'message is too long' not in msg:
                raise
        subparts = [chunk[i:i+2000] for i in range(0, len(chunk), 2000)] or ['—']
        for sub_idx, sub in enumerate(subparts):
            try:
                if idx == 0 and edit_first and not sent_any and sub_idx == 0:
                    await message.edit_text(sub, reply_markup=reply_markup)
                else:
                    await message.answer(sub, reply_markup=reply_markup if (idx == 0 and sub_idx == 0 and not edit_first) else None)
                sent_any = True
            except TelegramBadRequest:
                try:
                    await message.answer(sub[:1500])
                    sent_any = True
                except Exception:
                    pass


def _fmt_bytes_short(num: int) -> str:
    try:
        n = int(num or 0)
    except Exception:
        n = 0
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    idx = 0
    while v >= 1024.0 and idx < len(units) - 1:
        v /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(v)} {units[idx]}"
    if v >= 100:
        return f"{v:.0f} {units[idx]}"
    if v >= 10:
        return f"{v:.1f} {units[idx]}"
    return f"{v:.2f} {units[idx]}"



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



async def _vpn_server_enabled_map_admin(session, servers: list[dict] | None = None) -> dict[str, bool]:
    servers = servers or _load_vpn_servers_admin()
    codes = [str((s or {}).get("code") or "").strip().upper() for s in servers if str((s or {}).get("code") or "").strip()]
    if not codes:
        return {}
    rows = (await session.execute(
        select(AppSetting.key, AppSetting.int_value).where(AppSetting.key.in_([f"vpn_server_enabled:{c}" for c in codes]))
    )).all()
    raw = {str(k).split(":", 1)[1].upper(): (None if v is None else int(v)) for k, v in rows}
    return {c: (raw.get(c, 1) != 0) for c in codes}


def _kb_admin_vpn_servers(servers: list[dict], enabled_map: dict[str, bool]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for s in servers:
        code = str((s or {}).get("code") or "").strip().upper()
        if not code:
            continue
        name = str((s or {}).get("name") or code)
        enabled = enabled_map.get(code, True)
        prefix = "🟢" if enabled else "🔴"
        action = "выкл" if enabled else "вкл"
        b.button(text=f"{prefix} {name} ({code})", callback_data=f"admin:vpn:servers:view:{code}")
        b.button(text=f"{action.upper()} {code}", callback_data=f"admin:vpn:servers:toggle:{code}")
    b.button(text="⬅️ Назад", callback_data="admin:back")
    b.adjust(1, 1, 1)
    return b.as_markup()


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

def _vpn_capacity_limit_admin(server: dict | None = None) -> int:
    try:
        if server and server.get("max_active") is not None:
            return max(1, int(server.get("max_active")))
        return max(1, int(os.environ.get("VPN_MAX_ACTIVE", "40") or 40))
    except Exception:
        return 40


def _queue_pick_first_fit(servers: list[dict], used: dict[str, int]) -> dict | None:
    for server in servers:
        code = str(server.get("code") or os.environ.get("VPN_CODE", "NL")).upper()
        if int(used.get(code, 0)) < _vpn_capacity_limit_admin(server):
            return server
    return None


def _simulate_queue_sequence(servers: list[dict], initial_used: dict[str, int], *, allocations: int) -> tuple[list[str], dict[str, int], bool]:
    used = {str(k).upper(): int(v or 0) for k, v in (initial_used or {}).items()}
    order: list[str] = []
    exhausted = False
    for _ in range(max(0, allocations)):
        picked = _queue_pick_first_fit(servers, used)
        if not picked:
            exhausted = True
            break
        code = str(picked.get("code") or os.environ.get("VPN_CODE", "NL")).upper()
        used[code] = int(used.get(code, 0)) + 1
        order.append(code)
    return order, used, exhausted


def _format_server_fill_line(servers: list[dict], used: dict[str, int]) -> list[str]:
    lines: list[str] = []
    for idx, server in enumerate(servers, start=1):
        code = str(server.get("code") or os.environ.get("VPN_CODE", "NL")).upper()
        name = str(server.get("name") or code)
        cap = _vpn_capacity_limit_admin(server)
        seats = int(used.get(code, 0))
        left = max(0, cap - seats)
        lines.append(
            f"• <b>#{idx} {html.escape(name)}</b> — <code>{code}</code> — занято <b>{seats}/{cap}</b>, свободно <b>{left}</b>"
        )
    return lines


def _build_queue_test_scenarios(servers: list[dict], live_used: dict[str, int]) -> list[dict]:
    scenarios: list[dict] = []
    codes = [str(s.get("code") or os.environ.get("VPN_CODE", "NL")).upper() for s in servers]

    scenarios.append({
        "title": "Текущая живая загрузка",
        "used": {code: int(live_used.get(code, 0)) for code in codes},
        "allocations": min(10, max(1, len(codes) * 2)),
    })

    if len(servers) >= 3:
        c1, c2, c3 = codes[:3]
        cap1 = _vpn_capacity_limit_admin(servers[0])
        cap2 = _vpn_capacity_limit_admin(servers[1])
        cap3 = _vpn_capacity_limit_admin(servers[2])
        scenarios.append({
            "title": "Пример: 46/47, 47/47, 0/47",
            "used": {c1: max(cap1 - 1, 0), c2: cap2, c3: 0},
            "allocations": 4,
        })
        scenarios.append({
            "title": "Пример: 47/47, 46/47, 0/47",
            "used": {c1: cap1, c2: max(cap2 - 1, 0), c3: 0},
            "allocations": 4,
        })
        scenarios.append({
            "title": "Пример: 47/47, 47/47, 0/47",
            "used": {c1: cap1, c2: cap2, c3: 0},
            "allocations": 4,
        })

    if servers:
        used_all_empty = {code: 0 for code in codes}
        scenarios.append({
            "title": "Все серверы пустые",
            "used": used_all_empty,
            "allocations": min(6, sum(_vpn_capacity_limit_admin(s) for s in servers)),
        })

        used_almost_full = {}
        for idx, server in enumerate(servers):
            code = codes[idx]
            cap = _vpn_capacity_limit_admin(server)
            used_almost_full[code] = cap if idx < len(servers) - 1 else max(cap - 1, 0)
        scenarios.append({
            "title": "Свободно только одно место на последнем сервере",
            "used": used_almost_full,
            "allocations": 3,
        })

    return scenarios


def _build_queue_sim_report(servers: list[dict], live_used: dict[str, int]) -> str:
    lines: list[str] = []
    lines.append("🧪 <b>Симуляция очереди выдачи VPN-конфигов</b>")
    lines.append("")
    lines.append("Это <b>dry-run</b>: реальные конфиги, peer'ы и записи в БД не создаются.")
    lines.append("Проверяется текущая логика <b>first-fit</b>: бот идет по списку серверов сверху вниз и берет <b>первый</b> сервер, где есть свободное место.")
    lines.append("")
    lines.append("<b>Список серверов в порядке выдачи:</b>")
    lines.extend(_format_server_fill_line(servers, live_used))

    for idx, scenario in enumerate(_build_queue_test_scenarios(servers, live_used), start=1):
        used = {str(k).upper(): int(v or 0) for k, v in (scenario.get("used") or {}).items()}
        allocations = int(scenario.get("allocations") or 0)
        sequence, final_used, exhausted = _simulate_queue_sequence(servers, used, allocations=allocations)
        lines.append("")
        lines.append(f"<b>{idx}. {html.escape(str(scenario.get('title') or 'Сценарий'))}</b>")
        lines.append("Старт:")
        lines.extend(_format_server_fill_line(servers, used))
        pretty_sequence = " → ".join(f"<code>{html.escape(code)}</code>" for code in sequence) if sequence else "—"
        lines.append(f"Выдано эмулированных конфигов: <b>{len(sequence)}</b> из <b>{allocations}</b>")
        lines.append(f"Порядок выдачи: {pretty_sequence}")
        if exhausted:
            lines.append("Результат: <b>в процессе кончились свободные места</b>.")
        else:
            lines.append("Результат: <b>все эмулированные выдачи прошли по текущей логике</b>.")
        lines.append("Финиш:")
        lines.extend(_format_server_fill_line(servers, final_used))

    lines.append("")
    lines.append("<b>Как читать результат:</b> если в сценарии 46/47, 47/47, 0/47 первым будет выбран <b>первый сервер</b>, потому что на нем еще есть 1 слот. Только после его заполнения очередь перейдет на следующий доступный сервер.")
    return "\n".join(lines)


async def _vpn_seats_by_server() -> dict[str, int]:
    """Return occupied WG slots per configured server code.

    Legacy DB rows may contain aliases like NL/NL1/SERVER1 for the same first
    server. We therefore normalize DB counts into the configured server code and
    only then reconcile them with the real `wg show` peer count from SSH.
    """
    from app.db.models import VpnPeer

    servers = _load_vpn_servers_admin()
    default_code = (os.environ.get('VPN_CODE') or 'NL').upper()
    default_code_lit = literal(default_code)

    canonical_for_alias: dict[str, str] = {}
    for s in servers:
        code = str(s.get('code') or default_code).upper()
        for alias in _server_code_aliases(servers, code):
            canonical_for_alias[str(alias).upper()] = code
        canonical_for_alias.setdefault(code, code)

    result: dict[str, int] = {str(s.get('code') or default_code).upper(): 0 for s in servers}

    async with session_scope() as session:
        code_expr = func.coalesce(func.upper(VpnPeer.server_code), default_code_lit)
        q = (
            select(
                code_expr.label('code'),
                func.count(VpnPeer.id).label('cnt'),
            )
            .where(VpnPeer.is_active == True)  # noqa: E712
            .group_by(text("1"))
        )
        res = await session.execute(q)
        for raw_code, cnt in res.all():
            raw = str(raw_code or default_code).upper()
            canonical = canonical_for_alias.get(raw, raw)
            result[canonical] = int(result.get(canonical, 0)) + int(cnt or 0)

    for s in servers:
        code = str(s.get('code') or default_code).upper()
        host = str(s.get('host') or '').strip()
        user = str(s.get('user') or '').strip()
        if not host or not user:
            continue
        try:
            st = await vpn_service.get_server_status_for(
                host=host,
                port=int(s.get('port') or 22),
                user=user,
                password=s.get('password'),
                interface=str(s.get('interface') or os.environ.get('VPN_INTERFACE', 'wg0')),
            )
            if st.get('ok') and st.get('total_peers') is not None:
                result[code] = max(int(result.get(code, 0)), int(st.get('total_peers') or 0))
        except Exception:
            pass

    if not servers:
        result.setdefault(default_code, 0)
    return result





def _ensure_tz_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _main_payment_duration_days(pay: Payment) -> int:
    days = int(getattr(pay, "period_days", 0) or 0)
    months = int(getattr(pay, "period_months", 0) or 0)
    if days > 0:
        return days
    if months > 0:
        return months * 30
    return 30


def _is_main_subscription_provider(provider: str | None) -> bool:
    p = str(provider or "").strip().lower()
    if not p:
        return False
    if p in {"trial", "gift", "platega_lte", "mock_lte", "mock_family"}:
        return False
    if p.startswith("platega_family_"):
        return False
    return True


def _is_main_success_payment(pay: Payment) -> bool:
    return (
        str(getattr(pay, "status", "") or "").lower() == "success"
        and int(getattr(pay, "amount", 0) or 0) > 0
        and _is_main_subscription_provider(getattr(pay, "provider", None))
    )


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _safe_div(n: float, d: float) -> float:
    if not d:
        return 0.0
    return float(n) / float(d)

def _month_start_utc(dt: datetime) -> datetime:
    dtu = _ensure_tz_utc(dt) or _utcnow()
    return dtu.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month_start_utc(dt: datetime) -> datetime:
    return _month_start_utc(dt) + relativedelta(months=1)


def _month_label_ru(dt: datetime) -> str:
    months = [
        "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
    ]
    dtu = _ensure_tz_utc(dt) or _utcnow()
    return f"{months[dtu.month - 1]} {dtu.year}"


def _iter_month_starts(start_dt: datetime, end_dt: datetime) -> list[datetime]:
    cur = _month_start_utc(start_dt)
    end_month = _month_start_utc(end_dt)
    out: list[datetime] = []
    while cur <= end_month:
        out.append(cur)
        cur = cur + relativedelta(months=1)
    return out


def _build_main_payment_intervals(payments: list[Payment], *, now: datetime) -> tuple[dict[int, list[tuple[datetime, datetime]]], dict[int, datetime], dict[int, datetime], dict[int, int]]:
    intervals: dict[int, list[tuple[datetime, datetime]]] = {}
    first_paid_at: dict[int, datetime] = {}
    last_paid_at: dict[int, datetime] = {}
    payment_counts: dict[int, int] = {}
    current_end_by_user: dict[int, datetime | None] = {}

    for pay in payments:
        if not _is_main_success_payment(pay):
            continue
        tg_id = int(pay.tg_id)
        paid_at = _ensure_tz_utc(getattr(pay, 'paid_at', None)) or now
        start_at = current_end_by_user.get(tg_id)
        if not start_at or start_at <= paid_at:
            start_at = paid_at
        end_at = start_at + timedelta(days=_main_payment_duration_days(pay))
        intervals.setdefault(tg_id, []).append((start_at, end_at))
        current_end_by_user[tg_id] = end_at
        first_paid_at.setdefault(tg_id, paid_at)
        last_paid_at[tg_id] = paid_at
        payment_counts[tg_id] = payment_counts.get(tg_id, 0) + 1

    return intervals, first_paid_at, last_paid_at, payment_counts


def _active_users_at_snapshot(intervals: dict[int, list[tuple[datetime, datetime]]], snapshot: datetime) -> set[int]:
    snap = _ensure_tz_utc(snapshot) or _utcnow()
    active: set[int] = set()
    for uid, spans in intervals.items():
        for start_at, end_at in spans:
            if start_at <= snap < end_at:
                active.add(int(uid))
                break
    return active


def _build_paid_growth_series(payments: list[Payment], *, now: datetime) -> dict[str, object]:
    intervals, first_paid_at, last_paid_at, payment_counts = _build_main_payment_intervals(payments, now=now)
    if not first_paid_at:
        return {
            'weekly': [],
            'monthly': [],
            'churned': [],
            'active_now': set(),
            'paid_intervals': intervals,
            'first_paid_at': first_paid_at,
            'last_paid_at': last_paid_at,
            'payment_counts': payment_counts,
        }

    first_dt = min(first_paid_at.values())
    active_now = _active_users_at_snapshot(intervals, now)

    # Weekly new payer growth (first successful paid purchase in each 7-day bucket)
    weekly: list[dict[str, object]] = []
    bucket_start = first_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    prev_count = 0
    while bucket_start <= now:
        bucket_end = min(bucket_start + timedelta(days=7), now)
        bucket_users = {uid for uid, dt in first_paid_at.items() if bucket_start <= dt < bucket_end}
        count = len(bucket_users)
        weekly.append({
            'start': bucket_start,
            'end': bucket_end,
            'count': count,
            'delta': count - prev_count,
        })
        prev_count = count
        bucket_start = bucket_start + timedelta(days=7)

    # Monthly active paid base (snapshot at month end, current month = now)
    monthly: list[dict[str, object]] = []
    prev_count = 0
    for month_start in _iter_month_starts(first_dt, now):
        next_month = _next_month_start_utc(month_start)
        snapshot = min(next_month, now)
        active_set = _active_users_at_snapshot(intervals, snapshot - timedelta(microseconds=1) if snapshot > month_start else snapshot)
        count = len(active_set)
        monthly.append({
            'month_start': month_start,
            'label': _month_label_ru(month_start),
            'count': count,
            'delta': count - prev_count,
        })
        prev_count = count

    churned: list[dict[str, object]] = []
    for uid, spans in intervals.items():
        if int(uid) in active_now or not spans:
            continue
        end_at = max(end for _, end in spans)
        churned.append({
            'tg_id': int(uid),
            'expired_at': end_at,
            'first_paid_at': first_paid_at.get(int(uid)),
            'last_paid_at': last_paid_at.get(int(uid)),
            'payments_count': payment_counts.get(int(uid), 0),
        })
    churned.sort(key=lambda x: _ensure_tz_utc(x.get('expired_at')) or datetime(1970,1,1,tzinfo=timezone.utc), reverse=True)

    return {
        'weekly': weekly,
        'monthly': monthly,
        'churned': churned,
        'active_now': active_now,
        'paid_intervals': intervals,
        'first_paid_at': first_paid_at,
        'last_paid_at': last_paid_at,
        'payment_counts': payment_counts,
    }



def _forecast_sales_block(*, sales_last_30: int, revenue_last_30: int, sales_last_7: int, revenue_last_7: int,
                          active_trial_using_now: int, hist_trial_conv: float, now: datetime) -> dict[str, object]:
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month_start = month_start + relativedelta(months=1)
    next_next_month_start = next_month_start + relativedelta(months=1)
    days_current_month = max(1, int((next_month_start - month_start).days))
    days_next_month = max(1, int((next_next_month_start - next_month_start).days))

    daily_sales_30 = _safe_div(sales_last_30, 30.0)
    daily_sales_7 = _safe_div(sales_last_7, 7.0)
    daily_rev_30 = _safe_div(revenue_last_30, 30.0)
    daily_rev_7 = _safe_div(revenue_last_7, 7.0)
    blended_daily_sales = (daily_sales_30 * 0.65) + (daily_sales_7 * 0.35)
    blended_daily_revenue = (daily_rev_30 * 0.65) + (daily_rev_7 * 0.35)

    pipeline_sales = int(round(active_trial_using_now * _clip(hist_trial_conv, 0.0, 1.0)))
    trend_ratio = _safe_div(daily_sales_7, daily_sales_30) if daily_sales_30 > 0 else 1.0
    next_month_growth = _clip(1.0 + ((trend_ratio - 1.0) * 0.45), 0.85, 1.20)

    forecast_current_sales = int(round((blended_daily_sales * days_current_month) + pipeline_sales))
    forecast_current_revenue = int(round((blended_daily_revenue * days_current_month) + (pipeline_sales * 224)))
    forecast_next_sales = int(round((blended_daily_sales * days_next_month * next_month_growth)))
    forecast_next_revenue = int(round((blended_daily_revenue * days_next_month * next_month_growth)))

    forecast_next_sales_cons = int(round(forecast_next_sales * 0.85))
    forecast_next_sales_real = int(round(forecast_next_sales))
    forecast_next_sales_opt = int(round(forecast_next_sales * 1.15))
    forecast_next_revenue_cons = int(round(forecast_next_revenue * 0.85))
    forecast_next_revenue_real = int(round(forecast_next_revenue))
    forecast_next_revenue_opt = int(round(forecast_next_revenue * 1.15))

    return {
        "month_start": month_start,
        "next_month_start": next_month_start,
        "days_current_month": days_current_month,
        "days_next_month": days_next_month,
        "daily_sales_30": daily_sales_30,
        "daily_sales_7": daily_sales_7,
        "daily_revenue_30": daily_rev_30,
        "daily_revenue_7": daily_rev_7,
        "blended_daily_sales": blended_daily_sales,
        "blended_daily_revenue": blended_daily_revenue,
        "trend_ratio": trend_ratio,
        "pipeline_sales": pipeline_sales,
        "forecast_current_sales": forecast_current_sales,
        "forecast_current_revenue": forecast_current_revenue,
        "forecast_next_sales_cons": forecast_next_sales_cons,
        "forecast_next_sales_real": forecast_next_sales_real,
        "forecast_next_sales_opt": forecast_next_sales_opt,
        "forecast_next_revenue_cons": forecast_next_revenue_cons,
        "forecast_next_revenue_real": forecast_next_revenue_real,
        "forecast_next_revenue_opt": forecast_next_revenue_opt,
    }


def _ad_forecast_scenarios(avg_check_rub: int = 224) -> list[dict[str, object]]:
    warm_niches = [
        "мамские / женские",
        "lifestyle / family",
        "технологии / privacy / crypto",
    ]
    # Warm Telegram channels benchmark used for admin forecast.
    base_cpm = 1400.0
    ctr = 0.19
    click_to_trial = 0.15
    trial_to_paid = 0.30
    paid_per_view = ctr * click_to_trial * trial_to_paid

    scenarios: list[dict[str, object]] = []
    for budget in (30000, 60000, 100000):
        views = int(round((float(budget) / base_cpm) * 1000.0))
        sales = int(round(views * paid_per_view))
        revenue = int(round(sales * avg_check_rub))
        profit = int(revenue - budget)
        roi = (_safe_div(profit, budget) * 100.0) if budget > 0 else 0.0
        scenarios.append({
            "budget": budget,
            "niches": ", ".join(warm_niches[:2] if budget == 30000 else warm_niches),
            "views": views,
            "sales": sales,
            "revenue": revenue,
            "profit": profit,
            "roi": roi,
        })
    return scenarios


async def _collect_full_bot_stats() -> dict[str, object]:
    now = _utcnow()
    window_start = now - timedelta(days=30)

    async with session_scope() as session:
        total_users = int(await session.scalar(select(func.count()).select_from(User)) or 0)

        blocked_users = int(
            await session.scalar(
                select(func.count(func.distinct(MessageAudit.tg_id))).where(
                    MessageAudit.text_preview.like("[SEND_FAILED:FORBIDDEN]%")
                )
            )
            or 0
        )

        trial_rows = (
            await session.execute(
                select(AppSetting.key, AppSetting.int_value).where(
                    or_(AppSetting.key.like("trial_used:%"), AppSetting.key.like("trial_end_ts:%"))
                )
            )
        ).all()
        trial_user_ids: set[int] = set()
        trial_end_ts_by_user: dict[int, int] = {}
        for key, int_value in trial_rows:
            skey = str(key or "")
            try:
                uid = int(skey.split(":", 1)[1])
            except Exception:
                continue
            if skey.startswith("trial_used:") and int(int_value or 0) == 1:
                trial_user_ids.add(uid)
            elif skey.startswith("trial_end_ts:") and int(int_value or 0) > 0:
                trial_end_ts_by_user[uid] = int(int_value or 0)
        trial_periods = len(trial_user_ids)

        all_payments = (
            await session.execute(
                select(Payment).where(Payment.status == "success").order_by(Payment.tg_id.asc(), Payment.paid_at.asc(), Payment.id.asc())
            )
        ).scalars().all()

        users_last_30 = int(
            await session.scalar(select(func.count()).select_from(User).where(User.created_at >= window_start)) or 0
        )

        message_rows = (
            await session.execute(
                select(MessageAudit.kind, MessageAudit.tg_id, MessageAudit.seen_at)
                .where(
                    MessageAudit.kind.in_(
                        [
                            "trial_d1",
                            "trial_d2",
                            "upsell_after_first_payment",
                            "referral_link_opened",
                            "referral_registered",
                            "referral_paid",
                            "referral_earning_created",
                            "referral_hold_released",
                        ]
                    )
                )
            )
        ).all()

        try:
            peers_total = int(sum((await _vpn_seats_by_server()).values()))
        except Exception:
            peers_total = int(await session.scalar(select(func.count()).select_from(VpnPeer).where(VpnPeer.is_active == True)) or 0)  # noqa: E712

        active_peers = (
            await session.execute(
                select(VpnPeer.tg_id, VpnPeer.client_public_key).where(VpnPeer.is_active == True)  # noqa: E712
            )
        ).all()

    trial_to_paid_users: set[int] = set()
    active_paid_subscription_users = 0
    renewals_last_30_days = 0
    churn_cohort = 0
    churn_count = 0
    pay_199 = 0
    upsell_99 = 0
    lte_99 = 0
    family_upsell_orders = 0
    sales_last_30 = 0
    revenue_last_30 = 0
    sales_last_7 = 0
    revenue_last_7 = 0
    paid_users_all: set[int] = set()

    current_user_tg_id: int | None = None
    current_end: datetime | None = None
    active_at_window_start = False
    had_prior_main_payment = False

    def flush_user_state() -> None:
        nonlocal active_paid_subscription_users, churn_cohort, churn_count
        if current_user_tg_id is None:
            return
        active_now = bool(current_end and current_end > now)
        if active_now:
            active_paid_subscription_users += 1
        if active_at_window_start:
            churn_cohort += 1
            if not active_now:
                churn_count += 1

    for pay in all_payments:
        tg_id = int(pay.tg_id)
        if current_user_tg_id != tg_id:
            flush_user_state()
            current_user_tg_id = tg_id
            current_end = None
            active_at_window_start = False
            had_prior_main_payment = False

        provider = str(getattr(pay, "provider", "") or "")
        amount = int(getattr(pay, "amount", 0) or 0)
        paid_at = _ensure_tz_utc(getattr(pay, "paid_at", None)) or now

        if provider in {"platega_lte", "mock_lte"} and amount == 99:
            lte_99 += 1
            continue
        if provider.startswith("platega_family_") or provider == "mock_family":
            family_upsell_orders += 1
            continue

        if not _is_main_success_payment(pay):
            continue

        paid_users_all.add(tg_id)
        if tg_id in trial_user_ids:
            trial_to_paid_users.add(tg_id)
        if amount == 199:
            pay_199 += 1
        if amount == 99:
            upsell_99 += 1
        if paid_at >= window_start:
            sales_last_30 += 1
            revenue_last_30 += amount
        if paid_at >= (now - timedelta(days=7)):
            sales_last_7 += 1
            revenue_last_7 += amount
        if paid_at >= window_start and had_prior_main_payment:
            renewals_last_30_days += 1

        start_at = current_end if current_end and current_end > paid_at else paid_at
        end_at = start_at + timedelta(days=_main_payment_duration_days(pay))
        if start_at <= window_start < end_at:
            active_at_window_start = True
        current_end = end_at
        had_prior_main_payment = True

    flush_user_state()

    active_trial_users: set[int] = set()
    for uid in trial_user_ids:
        end_ts = int(trial_end_ts_by_user.get(uid, 0) or 0)
        if end_ts <= 0:
            continue
        if datetime.fromtimestamp(end_ts, tz=timezone.utc) <= now:
            continue
        if uid in paid_users_all:
            continue
        active_trial_users.add(uid)

    active_trial_using_now = 0
    trial_public_keys: set[str] = set()
    if active_trial_users:
        for peer_uid, public_key in active_peers:
            try:
                if int(peer_uid) in active_trial_users and str(public_key or '').strip():
                    trial_public_keys.add(str(public_key).strip())
            except Exception:
                continue
    if trial_public_keys:
        try:
            recent_usage = await vpn_service.get_recent_peer_handshakes(window_seconds=86400)
            active_trial_using_now = len({item.get("public_key") for item in recent_usage if str(item.get("public_key") or "") in trial_public_keys})
        except Exception:
            active_trial_using_now = 0

    hist_trial_conv = _safe_div(len(trial_to_paid_users), trial_periods) if trial_periods > 0 else 0.0
    forecast = _forecast_sales_block(
        sales_last_30=sales_last_30,
        revenue_last_30=revenue_last_30,
        sales_last_7=sales_last_7,
        revenue_last_7=revenue_last_7,
        active_trial_using_now=active_trial_using_now,
        hist_trial_conv=hist_trial_conv,
        now=now,
    )
    ad_scenarios = _ad_forecast_scenarios(avg_check_rub=224)

    kinds_sent: dict[str, set[int]] = {}
    kinds_seen: dict[str, set[int]] = {}
    for kind, tg_id, seen_at in message_rows:
        k = str(kind or "")
        uid = int(tg_id)
        kinds_sent.setdefault(k, set()).add(uid)
        if seen_at is not None:
            kinds_seen.setdefault(k, set()).add(uid)

    churn_percent = (float(churn_count) / float(churn_cohort) * 100.0) if churn_cohort > 0 else 0.0
    return {
        "total_users": total_users,
        "blocked_users": blocked_users,
        "peers_total": peers_total,
        "trial_periods": trial_periods,
        "trial_to_paid": len(trial_to_paid_users),
        "active_trials_now": len(active_trial_users),
        "active_trials_using_now": int(active_trial_using_now),
        "active_trials_idle_now": max(0, int(len(active_trial_users) - int(active_trial_using_now))),
        "hist_trial_conversion_percent": float(hist_trial_conv) * 100.0,
        "payments_199": pay_199,
        "upsells_99": upsell_99,
        "lte_99": lte_99,
        "family_upsell_orders": family_upsell_orders,
        "trial_d1_sent": len(kinds_sent.get("trial_d1", set())),
        "trial_d1_seen": len(kinds_seen.get("trial_d1", set())),
        "trial_d2_sent": len(kinds_sent.get("trial_d2", set())),
        "trial_d2_seen": len(kinds_seen.get("trial_d2", set())),
        "upsell_after_pay_sent": len(kinds_sent.get("upsell_after_first_payment", set())),
        "referral_link_opened": len(kinds_sent.get("referral_link_opened", set())),
        "referral_registered": len(kinds_sent.get("referral_registered", set())),
        "referral_paid": len(kinds_sent.get("referral_paid", set())),
        "referral_earning_created": len(kinds_sent.get("referral_earning_created", set())),
        "referral_hold_released": len(kinds_sent.get("referral_hold_released", set())),
        "active_paid_users": active_paid_subscription_users,
        "renewals_last_30_days": renewals_last_30_days,
        "churn_count": churn_count,
        "churn_cohort": churn_cohort,
        "churn_percent": churn_percent,
        "sales_last_30": sales_last_30,
        "revenue_last_30": revenue_last_30,
        "sales_last_7": sales_last_7,
        "revenue_last_7": revenue_last_7,
        "users_last_30": users_last_30,
        "avg_new_users_per_day": _safe_div(users_last_30, 30.0),
        "forecast": forecast,
        "ad_scenarios": ad_scenarios,
        "window_start": window_start,
        "now": now,
    }


async def _collect_full_bot_stats_v2() -> dict[str, object]:
    now = _utcnow()
    window30 = now - timedelta(days=30)
    window7 = now - timedelta(days=7)
    window3 = now - timedelta(days=3)

    async with session_scope() as session:
        total_users = int(await session.scalar(select(func.count()).select_from(User)) or 0)
        blocked_users = int(await session.scalar(select(func.count(func.distinct(MessageAudit.tg_id))).where(MessageAudit.text_preview.like("[SEND_FAILED:FORBIDDEN]%"))) or 0)
        users_last_30 = int(await session.scalar(select(func.count()).select_from(User).where(User.created_at >= window30)) or 0)

        trial_rows = (await session.execute(select(AppSetting.key, AppSetting.int_value).where(or_(AppSetting.key.like("trial_used:%"), AppSetting.key.like("trial_end_ts:%"))))).all()
        trial_user_ids: set[int] = set()
        trial_end_ts_by_user: dict[int, int] = {}
        for key, int_value in trial_rows:
            skey = str(key or "")
            try:
                uid = int(skey.split(":", 1)[1])
            except Exception:
                continue
            if skey.startswith("trial_used:") and int(int_value or 0) == 1:
                trial_user_ids.add(uid)
            elif skey.startswith("trial_end_ts:") and int(int_value or 0) > 0:
                trial_end_ts_by_user[uid] = int(int_value or 0)

        users = list((await session.execute(select(User))).scalars().all())
        user_created = {int(u.tg_id): _ensure_tz_utc(getattr(u, 'created_at', None)) or now for u in users}

        payments = list((await session.execute(select(Payment).where(Payment.status == "success").order_by(Payment.tg_id.asc(), Payment.paid_at.asc(), Payment.id.asc()))).scalars().all())
        peers = list((await session.execute(select(VpnPeer))).scalars().all())
        active_peers = [p for p in peers if bool(getattr(p, 'is_active', False))]
        referrals = list((await session.execute(select(Referral))).scalars().all())
        ref_earnings = list((await session.execute(select(ReferralEarning))).scalars().all())
        family_groups = list((await session.execute(select(FamilyVpnGroup))).scalars().all())
        family_profiles = list((await session.execute(select(FamilyVpnProfile))).scalars().all())
        msg_rows = list((await session.execute(select(MessageAudit))).scalars().all())
        yandex_memberships = list((await session.execute(select(YandexMembership))).scalars().all())

    trial_starts: dict[int, datetime] = {}
    first_main_paid: dict[int, datetime] = {}
    first_gift_at: dict[int, datetime] = {}
    distinct_payers_30: set[int] = set()
    trial_to_paid_users: set[int] = set()
    paid_users_all: set[int] = set()
    pay_199 = upsells_99 = lte_99 = family_upsell_orders = 0
    sales_last_30 = revenue_last_30 = sales_last_7 = revenue_last_7 = 0
    renewals_last_30_days = 0
    active_paid_subscription_users = 0
    churn_cohort = churn_count = 0
    current_user_tg_id = None
    current_end = None
    active_at_window_start = False
    had_prior_main_payment = False

    def flush_user_state() -> None:
        nonlocal active_paid_subscription_users, churn_cohort, churn_count
        if current_user_tg_id is None:
            return
        active_now = bool(current_end and current_end > now)
        if active_now:
            active_paid_subscription_users += 1
        if active_at_window_start:
            churn_cohort += 1
            if not active_now:
                churn_count += 1

    for pay in payments:
        tg_id = int(pay.tg_id)
        provider = str(getattr(pay, 'provider', '') or '')
        amount = int(getattr(pay, 'amount', 0) or 0)
        paid_at = _ensure_tz_utc(getattr(pay, 'paid_at', None)) or now
        if provider == 'trial' and tg_id not in trial_starts:
            trial_starts[tg_id] = paid_at
        if provider == 'gift' and tg_id not in first_gift_at:
            first_gift_at[tg_id] = paid_at
        if provider in {"platega_lte", "mock_lte"} and amount == 99:
            lte_99 += 1
            continue
        if provider.startswith("platega_family_") or provider == "mock_family":
            family_upsell_orders += 1
            continue
        if not _is_main_success_payment(pay):
            continue
        if tg_id not in first_main_paid:
            first_main_paid[tg_id] = paid_at
        if tg_id in trial_user_ids:
            trial_to_paid_users.add(tg_id)
        paid_users_all.add(tg_id)
        if amount == 199:
            pay_199 += 1
        if amount == 99:
            upsells_99 += 1
        if paid_at >= window30:
            sales_last_30 += 1
            revenue_last_30 += amount
            distinct_payers_30.add(tg_id)
        if paid_at >= window7:
            sales_last_7 += 1
            revenue_last_7 += amount
        if current_user_tg_id != tg_id:
            flush_user_state()
            current_user_tg_id = tg_id
            current_end = None
            active_at_window_start = False
            had_prior_main_payment = False
        if paid_at >= window30 and had_prior_main_payment:
            renewals_last_30_days += 1
        start_at = current_end if current_end and current_end > paid_at else paid_at
        end_at = start_at + timedelta(days=_main_payment_duration_days(pay))
        if start_at <= window30 < end_at:
            active_at_window_start = True
        current_end = end_at
        had_prior_main_payment = True
    flush_user_state()

    growth_series = _build_paid_growth_series(payments, now=now)

    active_trial_users = {uid for uid in trial_user_ids if int(trial_end_ts_by_user.get(uid, 0) or 0) > int(now.timestamp()) and uid not in paid_users_all}
    peer_by_user: dict[int, list] = {}
    for peer in peers:
        peer_by_user.setdefault(int(peer.tg_id), []).append(peer)
    active_peer_keys = {str(p.client_public_key): p for p in active_peers if str(p.client_public_key or '').strip()}
    ever_connected_trial_users = {uid for uid in trial_user_ids if peer_by_user.get(uid)}

    try:
        recent24 = await vpn_service.get_recent_peer_handshakes(window_seconds=86400)
    except Exception:
        recent24 = []
    try:
        recent3d = await vpn_service.get_recent_peer_handshakes(window_seconds=3 * 86400)
    except Exception:
        recent3d = []
    try:
        recent7d = await vpn_service.get_recent_peer_handshakes(window_seconds=7 * 86400)
    except Exception:
        recent7d = []

    recent24_keys = {str(i.get('public_key') or ''): i for i in recent24}
    recent3_keys = {str(i.get('public_key') or ''): i for i in recent3d}
    recent7_keys = {str(i.get('public_key') or ''): i for i in recent7d}

    active_trial_using_now = len({uid for uid in active_trial_users for p in peer_by_user.get(uid, []) if str(p.client_public_key or '') in recent24_keys})

    invite_click_users = {int(m.tg_id) for m in msg_rows if str(getattr(m, 'kind', '') or '') == 'yandex_invite_open_clicked'}
    yandex_membership_users = {int(getattr(m, 'tg_id', 0)) for m in yandex_memberships if int(getattr(m, 'tg_id', 0) or 0) > 0 and str(getattr(m, 'invite_link', '') or '').strip()}
    trial_activation_or_yandex_users = {uid for uid in trial_user_ids if peer_by_user.get(uid) or uid in invite_click_users or uid in yandex_membership_users}

    activation_deltas = []
    ttfh_deltas = []
    for uid in trial_user_ids:
        start = trial_starts.get(uid) or user_created.get(uid)
        plist = sorted(peer_by_user.get(uid, []), key=lambda p: _ensure_tz_utc(getattr(p, 'created_at', None)) or now)
        if plist:
            first_peer_at = _ensure_tz_utc(getattr(plist[0], 'created_at', None)) or now
            delta = max(0.0, (first_peer_at - start).total_seconds() / 3600.0)
            activation_deltas.append(delta)
            if uid in ever_connected_trial_users:
                ttfh_deltas.append(delta)
    time_to_paid = []
    for uid, paid_at in first_main_paid.items():
        start = trial_starts.get(uid)
        if start:
            time_to_paid.append(max(0.0, (paid_at - start).total_seconds() / 3600.0))

    gifts_total = len([p for p in payments if str(getattr(p, 'provider', '')) == 'gift'])
    gift_users = {int(p.tg_id) for p in payments if str(getattr(p, 'provider', '')) == 'gift'}
    gift_days_total = sum(int(getattr(p, 'period_days', 0) or 0) + (int(getattr(p, 'period_months', 0) or 0) * 30) for p in payments if str(getattr(p, 'provider', '')) == 'gift')
    gift_to_paid = len({uid for uid in gift_users if uid in first_main_paid and first_main_paid[uid] > (first_gift_at.get(uid) or now)})

    # server distribution
    servers = _load_vpn_servers_admin()
    label_by_code = {str((s or {}).get('code') or '').upper(): _server_numbered_label(servers, str((s or {}).get('code') or '').upper()) for s in servers}
    active_by_server: dict[str, int] = {}
    hs24_by_server: dict[str, int] = {}
    for p in active_peers:
        code = str(getattr(p, 'server_code', '') or '').upper() or 'DEFAULT'
        active_by_server[code] = active_by_server.get(code, 0) + 1
    for item in recent24:
        code = str(item.get('server_code') or '').upper() or 'DEFAULT'
        hs24_by_server[code] = hs24_by_server.get(code, 0) + 1
    peers_total = len(active_peers)
    server_distribution = []
    for code, cnt in sorted(active_by_server.items(), key=lambda kv: kv[1], reverse=True):
        server_distribution.append({
            'code': code,
            'label': label_by_code.get(code, code),
            'active_peers': cnt,
            'share': (cnt / peers_total * 100.0) if peers_total else 0.0,
            'handshakes_24h': hs24_by_server.get(code, 0),
        })

    # renewal funnel
    msg_by_kind = [m for m in msg_rows if str(m.kind or '') in {'sub_warn_3d', 'sub_warn_1d', 'trial_d1', 'trial_d2', 'upsell_after_first_payment', 'referral_link_opened', 'referral_paid', 'referral_earning_created', 'referral_hold_released'}]
    def _renewed_after(uid: int, sent_at: datetime, within_days: int) -> bool:
        paid_at = first_main_paid.get(uid)
        if not paid_at:
            return False
        return sent_at <= paid_at <= (sent_at + timedelta(days=within_days))
    sub_warn_3d_sent = [m for m in msg_rows if str(m.kind or '') == 'sub_warn_3d' and m.message_id is not None]
    sub_warn_1d_sent = [m for m in msg_rows if str(m.kind or '') == 'sub_warn_1d' and m.message_id is not None]
    renew_after_3d = len({int(m.tg_id) for m in sub_warn_3d_sent if _renewed_after(int(m.tg_id), _ensure_tz_utc(m.sent_at) or now, 4)})
    renew_after_1d = len({int(m.tg_id) for m in sub_warn_1d_sent if _renewed_after(int(m.tg_id), _ensure_tz_utc(m.sent_at) or now, 2)})

    # reset health via rotated peers
    reset_7 = len([p for p in peers if str(getattr(p, 'rotation_reason', '') or '') == 'manual_reset' and (_ensure_tz_utc(getattr(p, 'created_at', None)) or now) >= window7])
    reset_30 = len([p for p in peers if str(getattr(p, 'rotation_reason', '') or '') == 'manual_reset' and (_ensure_tz_utc(getattr(p, 'created_at', None)) or now) >= window30])

    # referral
    ref_active = len([r for r in referrals if str(getattr(r, 'status', '') or '') == 'active'])
    ref_pending = len([e for e in ref_earnings if str(getattr(e, 'status', '') or '') == 'pending'])
    ref_available = len([e for e in ref_earnings if str(getattr(e, 'status', '') or '') == 'available'])
    ref_paid_out = len([e for e in ref_earnings if str(getattr(e, 'status', '') or '') == 'paid'])

    # family analytics
    family_profiles_active = len([fp for fp in family_profiles if getattr(fp, 'vpn_peer_id', None)])
    family_groups_active = len([g for g in family_groups if (_ensure_tz_utc(getattr(g, 'active_until', None)) or datetime(1970,1,1,tzinfo=timezone.utc)) > now and int(getattr(g, 'seats_total', 0) or 0) > 0])
    upsell_seen = len({int(m.tg_id) for m in msg_rows if str(m.kind or '') == 'upsell_after_first_payment'})

    # risk metrics
    active_paid_users_set = set()
    current_user_tg_id = None
    current_end = None
    for pay in payments:
        if not _is_main_success_payment(pay):
            continue
        tg_id = int(pay.tg_id)
        paid_at = _ensure_tz_utc(getattr(pay, 'paid_at', None)) or now
        if current_user_tg_id != tg_id:
            current_user_tg_id = tg_id
            current_end = None
        start_at = current_end if current_end and current_end > paid_at else paid_at
        current_end = start_at + timedelta(days=_main_payment_duration_days(pay))
        if current_end > now:
            active_paid_users_set.add(tg_id)
    silent_3 = silent_7 = 0
    for uid in active_paid_users_set:
        plist = [p for p in active_peers if int(p.tg_id) == uid]
        if not plist:
            continue
        old3 = [p for p in plist if (_ensure_tz_utc(getattr(p, 'created_at', None)) or now) <= window3]
        old7 = [p for p in plist if (_ensure_tz_utc(getattr(p, 'created_at', None)) or now) <= window7]
        has3 = any(str(p.client_public_key or '') in recent3_keys for p in old3) if old3 else True
        has7 = any(str(p.client_public_key or '') in recent7_keys for p in old7) if old7 else True
        if old3 and not has3:
            silent_3 += 1
        if old7 and not has7:
            silent_7 += 1

    payer_count_30 = len(distinct_payers_30)
    arppu_30 = (revenue_last_30 / payer_count_30) if payer_count_30 else 0.0
    mrr_current = active_paid_subscription_users * 224
    mrr_after_churn = int(round(mrr_current * max(0.0, (100.0 - ((churn_count / churn_cohort * 100.0) if churn_cohort else 0.0)) / 100.0)))
    trial_periods = len(trial_user_ids)
    trial_activation_rate = (len(ever_connected_trial_users) / trial_periods * 100.0) if trial_periods else 0.0
    hist_trial_conv = (len(trial_to_paid_users) / trial_periods) if trial_periods else 0.0
    family_conv = (family_upsell_orders / upsell_seen * 100.0) if upsell_seen else 0.0
    churn_percent = (churn_count / churn_cohort * 100.0) if churn_cohort else 0.0

    kinds_sent: dict[str, set[int]] = {}
    kinds_seen: dict[str, set[int]] = {}
    for m in msg_by_kind:
        k = str(m.kind or '')
        uid = int(m.tg_id)
        kinds_sent.setdefault(k, set()).add(uid)
        if m.seen_at is not None:
            kinds_seen.setdefault(k, set()).add(uid)

    return {
        'now': now,
        'total_users': total_users,
        'blocked_users': blocked_users,
        'peers_total': peers_total,
        'trial_periods': trial_periods,
        'active_trials_now': len(active_trial_users),
        'active_trials_using_now': int(active_trial_using_now),
        'active_trials_idle_now': max(0, len(active_trial_users) - int(active_trial_using_now)),
        'trial_to_paid': len(trial_to_paid_users),
        'hist_trial_conversion_percent': hist_trial_conv * 100.0,
        'start_to_trial_percent': (_safe_div(trial_periods, total_users) * 100.0) if total_users else 0.0,
        'trial_to_activation_or_yandex_count': len(trial_activation_or_yandex_users),
        'trial_to_activation_or_yandex_percent': (_safe_div(len(trial_activation_or_yandex_users), trial_periods) * 100.0) if trial_periods else 0.0,
        'payments_199': pay_199,
        'upsells_99': upsells_99,
        'lte_99': lte_99,
        'family_upsell_orders': family_upsell_orders,
        'active_paid_users': active_paid_subscription_users,
        'renewals_last_30_days': renewals_last_30_days,
        'churn_count': churn_count,
        'churn_cohort': churn_cohort,
        'churn_percent': churn_percent,
        'sales_last_30': sales_last_30,
        'revenue_last_30': revenue_last_30,
        'sales_last_7': sales_last_7,
        'revenue_last_7': revenue_last_7,
        'users_last_30': users_last_30,
        'avg_new_users_per_day': _safe_div(users_last_30, 30.0),
        'mrr_current': mrr_current,
        'mrr_after_churn': mrr_after_churn,
        'arppu_30': arppu_30,
        'trial_activation_rate': trial_activation_rate,
        'avg_hours_to_first_handshake': _safe_div(sum(ttfh_deltas), len(ttfh_deltas)) if ttfh_deltas else 0.0,
        'avg_hours_to_paid': _safe_div(sum(time_to_paid), len(time_to_paid)) if time_to_paid else 0.0,
        'renew_warn_3d_sent': len({int(m.tg_id) for m in sub_warn_3d_sent}),
        'renew_after_3d': renew_after_3d,
        'renew_warn_1d_sent': len({int(m.tg_id) for m in sub_warn_1d_sent}),
        'renew_after_1d': renew_after_1d,
        'reset_7': reset_7,
        'reset_30': reset_30,
        'server_distribution': server_distribution,
        'gift_count': gifts_total,
        'gift_users': len(gift_users),
        'gift_days_total': gift_days_total,
        'gift_to_paid': gift_to_paid,
        'referral_active': ref_active,
        'referral_earning_pending': ref_pending,
        'referral_earning_available': ref_available,
        'referral_earning_paid': ref_paid_out,
        'family_groups_active': family_groups_active,
        'family_profiles_active': family_profiles_active,
        'family_upsell_seen': upsell_seen,
        'family_upsell_conversion': family_conv,
        'silent_risk_3d': silent_3,
        'silent_risk_7d': silent_7,
        'trial_d1_sent': len(kinds_sent.get('trial_d1', set())),
        'trial_d1_seen': len(kinds_seen.get('trial_d1', set())),
        'trial_d2_sent': len(kinds_sent.get('trial_d2', set())),
        'trial_d2_seen': len(kinds_seen.get('trial_d2', set())),
        'upsell_after_pay_sent': len(kinds_sent.get('upsell_after_first_payment', set())),
        'referral_link_opened': len(kinds_sent.get('referral_link_opened', set())),
        'referral_paid': len(kinds_sent.get('referral_paid', set())),
        'referral_earning_created': len(kinds_sent.get('referral_earning_created', set())),
        'referral_hold_released': len(kinds_sent.get('referral_hold_released', set())),
        'paid_growth_weekly': growth_series['weekly'],
        'paid_growth_monthly': growth_series['monthly'],
        'churned_users': growth_series['churned'],
        'yandex_invite_open_clicks': len(invite_click_users),
    }


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

    # fallback to default single-server env ONLY when multi-server JSON is absent
    if not servers:
        _add(
            os.environ.get('WG_SSH_HOST') or os.environ.get('VPN_SSH_HOST'),
            int(os.environ.get('WG_SSH_PORT') or os.environ.get('VPN_SSH_PORT') or 22),
            os.environ.get('WG_SSH_USER') or os.environ.get('VPN_SSH_USER'),
            os.environ.get('WG_SSH_PASSWORD') or os.environ.get('VPN_SSH_PASSWORD'),
            os.environ.get('VPN_INTERFACE') or 'wg0',
        )
    return providers



async def _create_admin_test_vpn_peer(*, tg_id: int, preferred_code: str | None = None) -> tuple[dict, dict, str]:
    from app.bot.handlers.nav import _pick_available_vpn_server
    from app.services.vpn import crypto as vpn_crypto

    preferred = str(preferred_code or "").strip().upper()
    if preferred and preferred != "AUTO":
        servers = _load_vpn_servers_admin()
        server = next((s for s in servers if str(s.get("code") or "").strip().upper() == preferred), None)
        if not server:
            raise RuntimeError(f"Сервер {preferred} не найден в VPN_SERVERS_JSON")
        used = await _vpn_seats_by_server()
        cap = _vpn_capacity_limit_admin(server)
        if int(used.get(preferred, 0)) >= cap:
            raise RuntimeError(f"На сервере {preferred} нет свободных мест: {int(used.get(preferred, 0))}/{cap}")
    else:
        server = await _pick_available_vpn_server(current_tg_id=None)
        if not server:
            raise RuntimeError("Нет доступных VPN-серверов для выдачи тестового конфига")

    code = str(server.get("code") or os.environ.get("VPN_CODE", "NL")).upper()
    host = str(server.get("host") or "")
    user = str(server.get("user") or "")
    port = int(server.get("port") or 22)
    password = server.get("password")
    interface = str(server.get("interface") or os.environ.get("VPN_INTERFACE", "wg0"))
    tc_dev = str(server.get("tc_dev") or server.get("wg_tc_dev") or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or "")
    # IMPORTANT: for admin test configs we must use the selected server strictly as-is.
    # Falling back to global VPN defaults here can silently generate a config for
    # another server (for example, default server #2) even when admin selected #1.
    server_public_key = str(server.get("server_public_key") or "")
    endpoint = str(server.get("endpoint") or "")
    dns = str(server.get("dns") or vpn_service.dns)

    if not host or not user or not server_public_key or not endpoint:
        raise RuntimeError(
            f"Сервер {code} настроен не полностью: нужны host/user/server_public_key/endpoint в VPN_SERVERS_JSON"
        )

    async with session_scope() as session:
        try:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": int(tg_id)})
        except Exception:
            pass

        client_ip = await vpn_service._alloc_ip_unique(session, tg_id=tg_id)
        client_priv, client_pub = gen_keys()

        provider = vpn_service._provider_for(
            host=host,
            port=port,
            user=user,
            password=password,
            interface=interface,
            tc_dev=tc_dev,
            tc_parent_rate_mbit=int(server.get("tc_parent_rate_mbit") or server.get("wg_tc_parent_rate_mbit") or os.environ.get("WG_TC_PARENT_RATE_MBIT") or os.environ.get("VPN_TC_PARENT_RATE_MBIT") or 1000),
        )
        await provider.add_peer(client_pub, client_ip, tg_id=tg_id)

        row = VpnPeer(
            tg_id=tg_id,
            client_public_key=client_pub,
            client_private_key_enc=vpn_crypto.encrypt(client_priv),
            client_ip=client_ip,
            server_code=code,
            is_active=True,
            revoked_at=None,
            rotation_reason="admin_test",
        )
        session.add(row)
        await session.flush()

        try:
            await vpn_service.ensure_rate_limit_for_server(
                tg_id=tg_id,
                ip=client_ip,
                host=host,
                port=port,
                user=user,
                password=password,
                interface=interface,
                tc_dev=tc_dev,
            )
        except Exception:
            pass

        await session.commit()

        peer = {
            "peer_id": row.id,
            "tg_id": tg_id,
            "client_ip": client_ip,
            "public_key": client_pub,
            "client_private_key_enc": row.client_private_key_enc,
            "client_private_key_plain": client_priv,
        }
        conf_text = vpn_service.build_wg_conf(
            peer,
            user_label=f"admin-test-{tg_id}",
            server_public_key=server_public_key,
            endpoint=endpoint,
            dns=dns,
        )
        return server, peer, conf_text

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


class AdminReferralPercentFSM(StatesGroup):
    waiting_target = State()
    waiting_percent = State()


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


class AdminPromoCreateFSM(StatesGroup):
    waiting_code = State()
    waiting_price = State()


class AdminPromoDeleteFSM(StatesGroup):
    waiting_code = State()


def _promo_norm(code: str) -> str:
    code = (code or "").strip().upper()
    code = "".join(ch for ch in code if ch.isalnum() or ch in "_-")
    return code[:32]


async def _promo_defs(session) -> dict[str, int]:
    rows = (await session.execute(select(AppSetting).where(AppSetting.key.like("promo:def:%")))).scalars().all()
    out: dict[str, int] = {}
    for row in rows:
        code = str(row.key or "").split(":", 2)[-1].strip().upper()
        price = int(getattr(row, "int_value", 0) or 0)
        if code and price > 0:
            out[code] = price
    return dict(sorted(out.items()))


def _kb_admin_promos() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin:promos:create")],
        [InlineKeyboardButton(text="🗑 Удалить промокод", callback_data="admin:promos:delete")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
    ])


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
                    f"онлайн сейчас <b>{act}</b> | WG-пиров <b>{tot}</b>"
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




def _kb_admin_diag() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Быстрая проверка", callback_data="admin:diag:quick")],
            [InlineKeyboardButton(text="🧪 Полная диагностика", callback_data="admin:diag:full")],
            [InlineKeyboardButton(text="🧹 Починить дубли WG", callback_data="admin:diag:dedupe_wg")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
        ]
    )


def _kb_admin_diag_refresh(full: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin:diag:{'full' if full else 'quick'}")],
            [InlineKeyboardButton(text="⚡ Быстрая проверка", callback_data="admin:diag:quick"), InlineKeyboardButton(text="🧪 Полная", callback_data="admin:diag:full")],
            [InlineKeyboardButton(text="🧹 Починить дубли WG", callback_data="admin:diag:dedupe_wg")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
        ]
    )


@router.callback_query(lambda c: c.data == "admin:diag")
async def admin_diag_menu(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer()
    text = (
        "🩺 <b>Проверка системы</b>\n\n"
        "Доступны два режима:\n"
        "⚡ Быстрая — основные контуры и критичные ошибки.\n"
        "🧪 Полная — максимум live-проверок по DB, WG, LTE, Yandex, family grace, напоминаниям и аномалиям.\n\n"
        "Диагностика read-only: ничего не чинит автоматически, только проверяет."
    )
    try:
        await cb.message.edit_text(text, reply_markup=_kb_admin_diag(), parse_mode="HTML")
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=_kb_admin_diag(), parse_mode="HTML")


@router.callback_query(lambda c: c.data in {"admin:diag:quick", "admin:diag:full"})
async def admin_diag_run(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    full = cb.data.endswith(':full')
    try:
        await cb.answer("Проверяю систему…")
    except Exception:
        pass
    try:
        results = await health_service.run(full=full)
        text = health_service.render_summary(results, full=full)
        parts = _split_html_lines(text.splitlines(), limit=3500)
        await _send_html_chunks(cb.message, parts, reply_markup=_kb_admin_diag_refresh(full=full), edit_first=False)
    except Exception:
        log.exception("admin_diag_failed")
        await cb.message.answer(
            "❌ Диагностика упала. Проверь логи последнего деплоя.",
            reply_markup=_kb_admin_diag_refresh(full=full),
        )



@router.callback_query(lambda c: c.data == "admin:diag:dedupe_wg")
async def admin_diag_dedupe_wg(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    try:
        await cb.answer("Чищу дубли WG…")
    except Exception:
        pass
    try:
        async with session_scope() as session:
            stats = await vpn_service.dedupe_active_peers(session)
            await session.commit()
        msg = (
            "🧹 <b>Чистка дублей WG завершена</b>\n\n"
            f"Дубликатных IP-групп: <b>{int(stats.get('duplicate_ips', 0) or 0)}</b>\n"
            f"Деактивировано записей в БД: <b>{int(stats.get('deactivated', 0) or 0)}</b>\n"
            f"Удалено с серверов: <b>{int(stats.get('remote_removed', 0) or 0)}</b>"
        )
        await cb.message.answer(msg, parse_mode="HTML", reply_markup=_kb_admin_diag())
    except Exception:
        log.exception("admin_diag_dedupe_wg_failed")
        await cb.message.answer("❌ Не удалось почистить дубли WG. Проверь логи.", reply_markup=_kb_admin_diag())

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


def _kb_admin_test_config_servers() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    servers = _load_vpn_servers_admin()
    for idx, srv in enumerate(servers, start=1):
        code = str(srv.get("code") or "").strip().upper()
        if not code:
            continue
        b.button(
            text=f"🧪 Server #{idx} — {str(srv.get('name') or code)}",
            callback_data=f"admin:vpn:test_config:create:{code}",
        )
    if servers:
        b.button(text="🤖 Автовыбор по очереди", callback_data="admin:vpn:test_config:create:auto")
    b.button(text="⬅️ Назад", callback_data="admin:menu")
    b.adjust(1)
    return b.as_markup()


def _kb_admin_test_config_reset_servers() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    servers = _load_vpn_servers_admin()
    for idx, srv in enumerate(servers, start=1):
        code = str(srv.get("code") or "").strip().upper()
        if not code:
            continue
        b.button(
            text=f"🧹 Server #{idx} — {str(srv.get('name') or code)}",
            callback_data=f"admin:vpn:test_config:reset_server:{code}",
        )
    if servers:
        b.button(text="🧹 Удалить на всех серверах", callback_data="admin:vpn:test_config:reset_all")
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
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin:user:card:{tg_id}"),
                InlineKeyboardButton(text="↩️ Забрать подарок", callback_data=f"admin:user:gift_revoke:{tg_id}"),
            ],
            [
                InlineKeyboardButton(text="📨 Напомнить об оплате", callback_data=f"admin:user:notify_expired:{tg_id}"),
                InlineKeyboardButton(text="🗓 Изменить дату окончания", callback_data=f"admin:user:set_end_at:{tg_id}"),
            ],
            [InlineKeyboardButton(text="💰 Цена места семьи", callback_data=f"admin:user:set_family_price:{tg_id}")],
            [InlineKeyboardButton(text="🗑 Удалить peer", callback_data=f"admin:user:peer_delete_menu:{tg_id}")],
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

    username = html.escape(f"@{u.tg_username}") if u and u.tg_username else "—"
    raw_name = " ".join([p for p in [getattr(u, 'first_name', None), getattr(u, 'last_name', None)] if p]) if u else "—"
    name = html.escape(raw_name) if raw_name else "—"

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

    current_cycle_by_kind: dict[str, MessageAudit] = {}
    if sub_end is not None:
        cycle_from = sub_end - timedelta(days=8)
        cycle_to = sub_end + timedelta(days=2)
        for r in expiry_rows:
            sent_at = _ensure_tz_utc(getattr(r, 'sent_at', None))
            if sent_at is None or not (cycle_from <= sent_at <= cycle_to):
                continue
            if r.kind in {"sub_warn_7d", "sub_warn_3d", "sub_warn_1d", "sub_expired"} and r.kind not in current_cycle_by_kind:
                current_cycle_by_kind[r.kind] = r

    lines = []
    lines.append("👤 <b>Карточка пользователя</b>")
    lines.append(f"ID: <code>{tg_id}</code>")
    lines.append(f"Профиль: {username} | {name}")
    if u:
        try:
            lines.append(f"Создан: <b>{_fmt_dt_short(u.created_at)}</b>")
        except Exception:
            pass

    # Acquisition / referral source
    try:
        inviter_id = await referral_service.get_current_owner_tg_id(session, referred_tg_id=tg_id)
    except Exception:
        inviter_id = None

    if inviter_id:
        inviter = (await session.execute(select(User).where(User.tg_id == int(inviter_id)))).scalar_one_or_none()
        inviter_username = html.escape(f"@{inviter.tg_username}") if inviter and inviter.tg_username else "—"
        inviter_raw_name = " ".join([p for p in [getattr(inviter, 'first_name', None), getattr(inviter, 'last_name', None)] if p]) if inviter else "—"
        inviter_name = html.escape(inviter_raw_name) if inviter_raw_name else "—"
        active_ref = (await session.execute(
            select(Referral)
            .where(Referral.referred_tg_id == tg_id, Referral.status == "active")
            .order_by(Referral.id.desc())
            .limit(1)
        )).scalar_one_or_none()
        ref_state = "активирован" if active_ref else "ожидает первой оплаты"
        lines.append("Источник: <b>👥 По реферальной ссылке</b>")
        lines.append(
            f"Пригласил: {inviter_username} | {inviter_name} | ID: <code>{int(inviter_id)}</code> | Статус: <b>{ref_state}</b>"
        )
    else:
        lines.append("Источник: <b>🧍 Пришёл самостоятельно</b>")

    # Referral balance snapshot
    try:
        bal_available, bal_pending, bal_paid = await referral_service.get_balances(session, tg_id)
        lines.append(
            f"Реф-баланс: доступно <b>{int(bal_available)} ₽</b> | в холде <b>{int(bal_pending)} ₽</b> | выплачено <b>{int(bal_paid)} ₽</b>"
        )
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

    try:
        pay_rows = list((await session.execute(
            select(Payment)
            .where(Payment.tg_id == tg_id)
            .order_by(Payment.paid_at.desc(), Payment.id.desc())
            .limit(10)
        )).scalars().all())
    except Exception:
        pay_rows = []

    if pay_rows:
        lines.append("")
        lines.append("💳 <b>Последние оплаты</b>")
        for pay in pay_rows:
            provider = html.escape(str(pay.provider or '—'))
            status = html.escape(str(pay.status or '—'))
            amount = int(pay.amount or 0)
            paid_at = _fmt_dt_short(pay.paid_at)
            pid = html.escape(str(pay.provider_payment_id or '—'))
            if len(pid) > 32:
                pid = pid[:32] + '…'
            lines.append(
                f"• #{pay.id} | <b>{status}</b> | {amount} ₽ | {provider} | {paid_at} | <code>{pid}</code>"
            )

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
            lines.append(f"Автосчёт семьи: <b>{'да' if bool(getattr(grp, 'billing_opt_in', False)) else 'нет'}</b>")
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
                    f"Yandex: ✅ в семье | label: <b>{html.escape(str(ym.account_label or '—'))}</b> | слот: <b>{ym.slot_index if ym.slot_index is not None else '—'}</b> | покрытие до: <b>{_fmt_dt_short(ym.coverage_end_at)}</b>"
                )
            else:
                lines.append(
                    f"Yandex: ❌ исключён | label: <b>{html.escape(str(ym.account_label or '—'))}</b> | слот: <b>{ym.slot_index if ym.slot_index is not None else '—'}</b> | removed: <b>{_fmt_dt_short(ym.removed_at)}</b>"
                )
        else:
            lines.append("Yandex: ❗️нет записи")
    except Exception:
        pass

    # LTE snapshot (best-effort)
    try:
        has_success_payment = bool(await session.scalar(select(Payment.id).where(Payment.tg_id == tg_id, Payment.status == "success").limit(1)))
        lte_state = await lte_vpn_service.reconcile_access(
            tg_id,
            subscription_end_at=sub.end_at if sub else None,
            has_success_payment=has_success_payment,
            ensure_remote=False,
        )
        lte_row = lte_state.get("row")
        if lte_row is not None:
            lte_until = lte_state.get("until")
            if lte_until and lte_until.tzinfo is None:
                lte_until = lte_until.replace(tzinfo=timezone.utc)
            lte_active = bool(lte_state.get("allowed"))
            last_seen_txt = _fmt_dt_short(lte_row.last_seen_at) if lte_row.last_seen_at else '—'
            lines.append(
                f"LTE: {'✅ активна' if lte_active else '⛔️ не активна'} | enabled: <b>{'yes' if lte_row.is_enabled else 'no'}</b> | до: <b>{_fmt_dt_short(lte_until)}</b> | last_seen: <b>{last_seen_txt}</b>"
            )
        else:
            lines.append("LTE: —")
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
                    tc_dev=str(srv.get('tc_dev') or srv.get('wg_tc_dev') or os.environ.get('WG_TC_DEV') or os.environ.get('VPN_TC_DEV') or ''),
                )
                try:
                    eps = await asyncio.wait_for(prov.get_peer_endpoints(), timeout=2.5)
                    for k in keys:
                        if k in eps:
                            endpoints_by_key[k] = eps[k]
                except Exception:
                    pass
                try:
                    hs = await asyncio.wait_for(prov.get_latest_handshakes(), timeout=2.5)
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
            srv_label = html.escape(_server_numbered_label(servers, code))
            vpn_ip = html.escape(str(p.client_ip or '—'))
            ep = endpoints_by_key.get(p.client_public_key)
            ep_txt = html.escape(str(ep if ep else '—'))
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
            preview_raw = re.sub(r"<[^>]+>", "", (m.text_preview or "").replace("\n", " ").strip())
            preview = html.escape(preview_raw)
            if len(preview) > 120:
                preview = preview[:119] + "…"
            lines.append(f"• <b>{html.escape(str(m.kind or '—'))}</b> | {sent} | 👁 {seen}\n  {preview}")

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

    current_cycle_kinds = {"sub_warn_7d", "sub_warn_3d", "sub_warn_1d", "sub_expired"}
    for kind, title in expiry_kinds:
        row = current_cycle_by_kind.get(kind) if kind in current_cycle_kinds else last_by_kind.get(kind)
        lines.append(_fmt_audit_line(row, title))

    if sub_end is not None:
        lines.append(
            f"<i>ℹ️ Напоминания по подписке в этом блоке показаны для текущей даты окончания: {_fmt_dt_short(sub_end)}. После продления или подарочных дней цикл уведомлений пересчитывается заново.</i>"
        )

    return "\n".join(lines)




@router.callback_query(lambda c: c.data == "admin:payments:reconcile")
async def admin_payments_reconcile(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        await cb.answer("Проверяю оплаты…")
    except Exception:
        pass

    from app.scheduler.worker import _job_reconcile_pending_platega_payments

    async with session_scope() as session:
        before_pending = int(await session.scalar(select(func.count()).select_from(Payment).where(Payment.status == "pending")) or 0)
        before_success = int(await session.scalar(select(func.count()).select_from(Payment).where(Payment.status == "success")) or 0)

    await _job_reconcile_pending_platega_payments(cb.bot)

    async with session_scope() as session:
        after_pending = int(await session.scalar(select(func.count()).select_from(Payment).where(Payment.status == "pending")) or 0)
        after_success = int(await session.scalar(select(func.count()).select_from(Payment).where(Payment.status == "success")) or 0)
        latest = list((await session.execute(
            select(Payment)
            .where(Payment.provider.like("platega%"))
            .order_by(Payment.paid_at.desc(), Payment.id.desc())
            .limit(8)
        )).scalars().all())

    lines = [
        "💳 <b>Проверка оплат</b>",
        "",
        f"Pending до: <b>{before_pending}</b>",
        f"Pending после: <b>{after_pending}</b>",
        f"Success до/после: <b>{before_success}</b>/<b>{after_success}</b>",
    ]
    if before_pending > after_pending:
        lines.append(f"\n✅ Автодоводка обработала: <b>{before_pending - after_pending}</b> pending-платеж(ей).")
    elif before_pending == after_pending:
        lines.append("\nℹ️ Новых изменений не найдено.")
    if latest:
        lines.append("")
        lines.append("Последние платежи Platega:")
        for pay in latest:
            pid = html.escape(str(pay.provider_payment_id or '—'))
            if len(pid) > 28:
                pid = pid[:28] + '…'
            lines.append(
                f"• #{pay.id} | tg <code>{pay.tg_id}</code> | <b>{html.escape(str(pay.status or '—'))}</b> | {int(pay.amount or 0)} ₽ | {_fmt_dt_short(pay.paid_at)} | <code>{pid}</code>"
            )

    await cb.message.answer("\n".join(lines), reply_markup=kb_admin_menu(), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "admin:lte:repair")
async def admin_lte_repair(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        await cb.answer("Проверяю LTE…")
    except Exception:
        pass

    stats = await lte_vpn_service.repair_active_clients(limit=300)
    touched_ids = [int(x) for x in (stats.get('touched_tg_ids') or [])][:10]
    touched_labels: list[str] = []
    if touched_ids:
        try:
            labels = await asyncio.gather(*[_tg_label(cb.bot, tid) for tid in touched_ids], return_exceptions=True)
            for tid, lbl in zip(touched_ids, labels):
                if isinstance(lbl, Exception):
                    touched_labels.append(f"ID {tid}")
                else:
                    touched_labels.append(str(lbl))
        except Exception:
            touched_labels = [f"ID {tid}" for tid in touched_ids]

    lines = [
        "📶 <b>Починка LTE-профилей</b>",
        "",
        f"Проверено: <b>{int(stats.get('scanned', 0))}</b>",
        f"Реально починено: <b>{int(stats.get('repaired', 0))}</b>",
        f"Повторно включено в БД: <b>{int(stats.get('reenabled', 0))}</b>",
        f"Уже были в порядке: <b>{int(stats.get('already_ok', 0))}</b>",
        f"Ошибок: <b>{int(stats.get('failed', 0))}</b>",
    ]
    if touched_labels:
        lines.append("")
        lines.append("Изменённые профили:")
        for label in touched_labels:
            lines.append(f"• {html.escape(label)}")
    if int(stats.get('failed', 0)) == 0:
        lines.append("\n✅ Проверка завершена без ошибок.")
    else:
        lines.append("\n⚠️ Есть ошибки. Проверь логи последнего деплоя.")

    await cb.message.answer("\n".join(lines), reply_markup=kb_admin_menu(), parse_mode="HTML")

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

    await state.clear()
    progress = await message.answer("⏳ Загружаю карточку пользователя…")
    try:
        async with session_scope() as session:
            card_text = await _render_user_card(session, message.bot, tg_id)
        try:
            await progress.edit_text(card_text, reply_markup=_kb_user_card(tg_id), parse_mode="HTML")
        except Exception:
            await message.answer(card_text, reply_markup=_kb_user_card(tg_id), parse_mode="HTML")
    except Exception:
        log.exception("admin_user_inspect_input_failed tg_id=%s raw=%s", tg_id, raw)
        try:
            await progress.edit_text(
                "❌ Не удалось загрузить карточку пользователя. Попробуйте ещё раз через минуту.",
                reply_markup=_kb_admin_back(),
                parse_mode="HTML",
            )
        except Exception:
            await message.answer(
                "❌ Не удалось загрузить карточку пользователя. Попробуйте ещё раз через минуту.",
                reply_markup=_kb_admin_back(),
                parse_mode="HTML",
            )


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


async def _gift_revoke_menu_text(session, tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = list((await session.execute(
        select(Payment)
        .where(Payment.tg_id == tg_id, Payment.provider == "gift")
        .order_by(Payment.paid_at.desc(), Payment.id.desc())
        .limit(10)
    )).scalars().all())

    lines = ["↩️ <b>Откат подарков</b>", f"Пользователь: <code>{tg_id}</code>"]
    kb_rows: list[list[InlineKeyboardButton]] = []
    active_rows = []
    for pay in rows:
        status = str(pay.status or "—")
        paid_at = _fmt_dt_short(pay.paid_at)
        months = int(getattr(pay, "period_months", 0) or 0)
        days = int(getattr(pay, "period_days", 0) or 0)
        period_label = []
        if months:
            period_label.append(f"{months}м")
        if days:
            period_label.append(f"{days}д")
        period_text = "+".join(period_label) if period_label else "?"
        lines.append(f"• #{int(pay.id)} | {html.escape(status)} | {paid_at} | {period_text}")
        if status == "success":
            active_rows.append(pay)
            kb_rows.append([InlineKeyboardButton(text=f"↩️ Забрать #{int(pay.id)} ({period_text})", callback_data=f"admin:user:gift_revoke_pick:{tg_id}:{int(pay.id)}")])

    if not rows:
        lines.append("")
        lines.append("Подарков не найдено.")
    elif not active_rows:
        lines.append("")
        lines.append("Активных подарков для отката нет.")

    kb_rows.append([InlineKeyboardButton(text="⬅️ К карточке", callback_data=f"admin:user:card:{tg_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)




async def _peer_delete_menu_text(session, tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    peers = list(
        (
            await session.execute(
                select(VpnPeer)
                .where(VpnPeer.tg_id == int(tg_id))
                .order_by(VpnPeer.is_active.desc(), VpnPeer.id.desc())
                .limit(20)
            )
        ).scalars().all()
    )
    lines = ["🗑 <b>Удаление peer</b>", f"Пользователь: <code>{int(tg_id)}</code>"]
    kb_rows: list[list[InlineKeyboardButton]] = []
    if not peers:
        lines.extend(["", "У пользователя нет peer'ов."])
    else:
        lines.append("")
        lines.append("Выберите peer для удаления:")
        for peer in peers:
            status = "✅" if bool(getattr(peer, 'is_active', False)) else "⛔️"
            server = html.escape(str(getattr(peer, 'server_name', '') or getattr(peer, 'server_code', '') or '—'))
            vpn_ip = html.escape(str(getattr(peer, 'assigned_ip', '') or '—'))
            label = f"{status} peer#{int(peer.id)} | {server} | {vpn_ip}"
            kb_rows.append([
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=f"admin:user:peer_delete_confirm:{int(tg_id)}:{int(peer.id)}",
                )
            ])
    kb_rows.append([InlineKeyboardButton(text="⬅️ К карточке", callback_data=f"admin:user:card:{int(tg_id)}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(lambda c: (c.data or "").startswith("admin:user:peer_delete_menu:"))
async def admin_user_peer_delete_menu(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer()
    try:
        tg_id = int((cb.data or "").split(":")[-1])
    except Exception:
        return
    async with session_scope() as session:
        text, kb = await _peer_delete_menu_text(session, tg_id)
    await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: (c.data or "").startswith("admin:user:peer_delete_confirm:"))
async def admin_user_peer_delete_confirm(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer()
    try:
        parts = (cb.data or "").split(":")
        tg_id = int(parts[-2])
        peer_id = int(parts[-1])
    except Exception:
        return
    async with session_scope() as session:
        peer = await session.get(VpnPeer, peer_id)
        if not peer or int(getattr(peer, 'tg_id', 0) or 0) != tg_id:
            await cb.message.edit_text(
                "❌ Peer не найден.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К карточке", callback_data=f"admin:user:card:{tg_id}")]]),
                parse_mode="HTML",
            )
            return
        server = html.escape(str(getattr(peer, 'server_name', '') or getattr(peer, 'server_code', '') or '—'))
        vpn_ip = html.escape(str(getattr(peer, 'assigned_ip', '') or '—'))
        pub = html.escape(str(getattr(peer, 'client_public_key', '') or '—'))
        text = (
            "⚠️ <b>Подтверждение удаления peer</b>\n"
            f"Пользователь: <code>{tg_id}</code>\n"
            f"Peer: <b>#{peer_id}</b>\n"
            f"Сервер: <b>{server}</b>\n"
            f"VPN-IP: <b>{vpn_ip}</b>\n"
            f"Public key: <code>{pub[:32]}…</code>\n\n"
            "Peer будет удалён из базы и, по возможности, с VPN-сервера. Это действие необратимо."
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Удалить peer", callback_data=f"admin:user:peer_delete_apply:{tg_id}:{peer_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=f"admin:user:peer_delete_menu:{tg_id}")],
        [InlineKeyboardButton(text="⬅️ К карточке", callback_data=f"admin:user:card:{tg_id}")],
    ])
    await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: (c.data or "").startswith("admin:user:peer_delete_apply:"))
async def admin_user_peer_delete_apply(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer("Удаляю peer…")
    try:
        parts = (cb.data or "").split(":")
        tg_id = int(parts[-2])
        peer_id = int(parts[-1])
    except Exception:
        return
    remote_removed = None
    peer_descr = f"peer#{peer_id}"
    async with session_scope() as session:
        peer = await session.get(VpnPeer, peer_id)
        if not peer or int(getattr(peer, 'tg_id', 0) or 0) != tg_id:
            await cb.message.edit_text(
                "❌ Peer не найден или уже удалён.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К карточке", callback_data=f"admin:user:card:{tg_id}")]]),
                parse_mode="HTML",
            )
            return
        peer_descr = f"peer#{peer_id}"
        try:
            remote_removed = await vpn_service.remove_peer_for_server(
                str(getattr(peer, 'client_public_key', '') or ''),
                server_code=getattr(peer, 'server_code', None),
            )
        except Exception:
            log.exception("admin_user_peer_delete_remote_failed tg_id=%s peer_id=%s", tg_id, peer_id)
            remote_removed = False
        await session.execute(
            text("UPDATE family_vpn_profiles SET vpn_peer_id = NULL WHERE vpn_peer_id = :peer_id"),
            {"peer_id": peer_id},
        )
        await session.delete(peer)
        await session.commit()

    remote_txt = "удалён с сервера" if remote_removed else "с сервера удалён best-effort"
    summary_text = (
        f"✅ <b>{html.escape(peer_descr)} удалён</b>\n"
        f"Пользователь: <code>{tg_id}</code>\n"
        f"Удаление из БД выполнено, {remote_txt}."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить ещё peer", callback_data=f"admin:user:peer_delete_menu:{tg_id}")],
        [InlineKeyboardButton(text="⬅️ К карточке", callback_data=f"admin:user:card:{tg_id}")],
    ])
    await cb.message.edit_text(summary_text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(lambda c: (c.data or "").startswith("admin:user:gift_revoke:"))
async def admin_user_gift_revoke_menu(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    try:
        tg_id = int((cb.data or "").split(":")[-1])
    except Exception:
        await cb.answer("Некорректный пользователь", show_alert=True)
        return
    async with session_scope() as session:
        text, kb = await _gift_revoke_menu_text(session, tg_id)
    await cb.answer()
    await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: (c.data or "").startswith("admin:user:gift_revoke_pick:"))
async def admin_user_gift_revoke_pick(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    parts = (cb.data or "").split(":")
    if len(parts) < 5:
        await cb.answer("Некорректные данные", show_alert=True)
        return
    try:
        tg_id = int(parts[-2])
        payment_id = int(parts[-1])
    except Exception:
        await cb.answer("Некорректные данные", show_alert=True)
        return

    async with session_scope() as session:
        pay = await session.get(Payment, payment_id)
        if not pay or int(pay.tg_id or 0) != tg_id or str(pay.provider or "") != "gift":
            await cb.answer("Подарок не найден", show_alert=True)
            return
        if str(pay.status or "") != "success":
            text, kb = await _gift_revoke_menu_text(session, tg_id)
            await cb.answer("Этот подарок уже откатан", show_alert=True)
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return

        sub = await session.get(Subscription, tg_id)
        if not sub:
            sub = await get_subscription(session, tg_id)
        if not sub:
            await cb.answer("Подписка не найдена", show_alert=True)
            return

        months = int(getattr(pay, "period_months", 0) or 0)
        days = int(getattr(pay, "period_days", 0) or 0)
        delta_months = months
        delta_days = days if months == 0 else 0
        old_end = sub.end_at
        base_end = old_end or _utcnow()
        new_end = base_end
        if delta_months:
            new_end = new_end - relativedelta(months=delta_months)
        if delta_days:
            new_end = new_end - timedelta(days=delta_days)
        now = _utcnow()
        if new_end < now:
            new_end = now

        sub.end_at = new_end
        sub.is_active = bool(sub.end_at and sub.end_at > now)
        sub.status = "active" if sub.is_active else "expired"
        pay.status = "revoked"
        await session.commit()

        text = await _render_user_card(session, cb.bot, tg_id)

    await cb.answer("Подарок откатан")
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


def _kb_yandex_gate(*, blocked: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("✅ Включить выдачу приглашений" if blocked else "⛔️ Остановить выдачу приглашений"),
            callback_data=("admin:yandex:gate:off" if blocked else "admin:yandex:gate:on"),
        )],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
    ])


@router.callback_query(lambda c: c.data == "admin:yandex:gate")
async def admin_yandex_gate(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer()
    async with session_scope() as session:
        blocked = bool(await get_app_setting_int(session, "yandex_invites_blocked", default=0) or 0)
    state_text = "⛔️ остановлена" if blocked else "✅ включена"
    text = (
        "🟡 <b>Выдача приглашений Yandex</b>\n\n"
        f"Текущий статус: <b>{state_text}</b>\n\n"
        "Когда стоп включён, новые пользователи без ранее выданного приглашения не смогут получить invite. "
        "Пользователи, у которых ссылка уже есть, продолжат видеть и открывать своё приглашение."
    )
    await cb.message.edit_text(text, reply_markup=_kb_yandex_gate(blocked=blocked), parse_mode="HTML")


@router.callback_query(lambda c: (c.data or "") in {"admin:yandex:gate:on", "admin:yandex:gate:off"})
async def admin_yandex_gate_toggle(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    blocked = (cb.data or "").endswith(":on")
    async with session_scope() as session:
        await set_app_setting_int(session, "yandex_invites_blocked", 1 if blocked else 0)
        await session.commit()
    await cb.answer("Сохранено")
    await admin_yandex_gate(cb)


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
        f"Сейчас в окне восстановления: <b>{len(by_tg)}</b> пользовател(ей)",
        "Пиры уже отключены, но их можно восстановить при оплате в течение 24 часов без смены конфига:",
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




@router.callback_query(lambda c: c.data == "admin:promos")
async def admin_promos(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await state.clear()
    async with session_scope() as session:
        defs = await _promo_defs(session)
        base_price = int(await get_price_rub(session) or 0)
    lines = ["🎟 <b>Промокоды</b>", "", f"Базовая цена подписки: <b>{base_price} ₽</b>", ""]
    if defs:
        lines.append("Активные промокоды:")
        for code, price in defs.items():
            lines.append(f"• <code>{html.escape(code)}</code> → <b>{price} ₽</b>")
    else:
        lines.append("Активных промокодов пока нет.")
    await cb.message.edit_text("\n".join(lines), reply_markup=_kb_admin_promos(), parse_mode="HTML")
    await cb.answer()


@router.callback_query(lambda c: c.data == "admin:promos:create")
async def admin_promos_create_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await state.set_state(AdminPromoCreateFSM.waiting_code)
    await cb.message.edit_text("🎟 <b>Создание промокода</b>\n\nОтправьте код промокода.\nДопустимы буквы, цифры, <code>_</code> и <code>-</code>.", reply_markup=_kb_admin_back(), parse_mode="HTML")
    await cb.answer()


@router.message(AdminPromoCreateFSM.waiting_code)
async def admin_promos_create_code(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return
    code = _promo_norm(message.text or "")
    if not code:
        await message.answer("❌ Некорректный код промокода.", reply_markup=_kb_admin_back())
        return
    await state.update_data(promo_code=code)
    await state.set_state(AdminPromoCreateFSM.waiting_price)
    await message.answer(f"Код: <code>{html.escape(code)}</code>\n\nТеперь отправьте новую цену в рублях.", reply_markup=_kb_admin_back(), parse_mode="HTML")


@router.message(AdminPromoCreateFSM.waiting_price)
async def admin_promos_create_price(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return
    raw = re.sub(r"[^0-9]", "", (message.text or "").strip())
    if not raw:
        await message.answer("❌ Введите цену числом, например: 149", reply_markup=_kb_admin_back())
        return
    price = int(raw)
    data = await state.get_data()
    code = _promo_norm(str(data.get("promo_code") or ""))
    if not code or price <= 0:
        await message.answer("❌ Не удалось создать промокод.", reply_markup=kb_admin_menu())
        await state.clear()
        return
    async with session_scope() as session:
        base_price = int(await get_price_rub(session) or 0)
        if price >= base_price:
            await message.answer(f"❌ Цена по промокоду должна быть ниже базовой цены <b>{base_price} ₽</b>.", reply_markup=_kb_admin_back(), parse_mode="HTML")
            return
        await set_app_setting_int(session, f"promo:def:{code}", price)
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Промокод <code>{html.escape(code)}</code> создан. Новая цена: <b>{price} ₽</b>", reply_markup=kb_admin_menu(), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "admin:promos:delete")
async def admin_promos_delete_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await state.set_state(AdminPromoDeleteFSM.waiting_code)
    await cb.message.edit_text("🗑 <b>Удаление промокода</b>\n\nОтправьте код промокода, который нужно удалить.", reply_markup=_kb_admin_back(), parse_mode="HTML")
    await cb.answer()


@router.message(AdminPromoDeleteFSM.waiting_code)
async def admin_promos_delete_finish(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return
    code = _promo_norm(message.text or "")
    if not code:
        await message.answer("❌ Укажите код промокода.", reply_markup=_kb_admin_back())
        return
    async with session_scope() as session:
        row = await session.get(AppSetting, f"promo:def:{code}")
        if not row:
            await message.answer("❌ Такой промокод не найден.", reply_markup=_kb_admin_back())
            return
        await session.delete(row)
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Промокод <code>{html.escape(code)}</code> удалён.", reply_markup=kb_admin_menu(), parse_mode="HTML")


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
            from app.services.vpn.service import vpn_service, gen_keys

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
        "🎁 <b>Подарок от администрации</b>\n\n"
        f"Администрация проекта подарила вам подписку на <b>{months}</b> мес.\n"
        f"Новая дата окончания: <b>{new_end.astimezone(AMSTERDAM_TZ).strftime('%Y-%m-%d %H:%M')}</b>\n\n"
        "Приятного пользования!"
    )
    try:
        await audit_send_message(message.bot, target_tg_id, notify_text, kind="admin_gift", reply_markup=None, parse_mode="HTML")
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

        target_ids = sorted({int(x) for x in target_ids if int(x) > 0})
        touched = 0
        activated = 0
        restored_peers = 0
        new_end_by_tg: dict[int, datetime] = {}

        for tg_id in target_ids:
            sub = await session.get(Subscription, tg_id)
            if not sub:
                sub = await get_subscription(session, tg_id)

            was_active = bool(sub.is_active and sub.end_at and sub.end_at > now)
            await extend_subscription(
                session,
                tg_id,
                period_days=days,
                amount_rub=0,
                provider="gift",
                status="success",
                provider_payment_id=f"gift_days:{mode}:{message.from_user.id}:{tg_id}:{days}:{int(now.timestamp())}",
            )
            sub = await session.get(Subscription, tg_id)
            touched += 1
            if not was_active:
                activated += 1
            if sub and sub.end_at:
                new_end_by_tg[tg_id] = sub.end_at

            try:
                from app.services.vpn.service import vpn_service
                restored = await vpn_service.restore_expired_peers(session, tg_id, grace_hours=72)
                if restored:
                    restored_peers += int(restored)
            except Exception:
                pass

        await session.commit()

    notified = 0
    mode_label = "активным пользователям" if mode == "active" else "всем пользователям"
    for tg_id in target_ids:
        end_at = new_end_by_tg.get(tg_id)
        if not end_at:
            continue
        try:
            await audit_send_message(
                message.bot,
                tg_id,
                (
                    "🎁 <b>Подарок от администрации</b>\n\n"
                    f"Администрация проекта подарила вам <b>{days}</b> дн. подписки.\n"
                    f"Новая дата окончания: <b>{end_at.astimezone(AMSTERDAM_TZ).strftime('%Y-%m-%d %H:%M')}</b>\n\n"
                    "Приятного пользования!"
                ),
                kind="admin_gift_days",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
                ),
            )
            notified += 1
        except Exception:
            pass

    await state.clear()

    await message.answer(
        "✅ Дни подарены.\n\n"
        f"Кому: <b>{mode_label}</b>\n"
        f"Дней: <b>{days}</b>\n"
        f"Обработано пользователей: <b>{touched}</b>\n"
        f"Из них были неактивны и стали активны: <b>{activated}</b>\n"
        f"Восстановлено WG peer (best-effort): <b>{restored_peers}</b>\n"
        f"Уведомлений отправлено: <b>{notified}</b>",
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


@router.callback_query(lambda c: c.data == "admin:stats:full")
async def admin_full_stats(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    try:
        await cb.answer("Собираю полную статистику…")
    except Exception:
        pass
    try:
        stats = await _collect_full_bot_stats_v2()
    except Exception:
        log.exception("admin_full_stats_failed")
        await cb.message.answer("❌ Не удалось собрать полную статистику бота. Проверь логи.", reply_markup=_kb_admin_back())
        return

    lines = [
        "📈 <b>Полная статистика бота</b>",
        "",
        "1️⃣ <b>💸 Доход от текущей платящей базы</b>",
        f"• Ожидаемый месячный доход сейчас: <b>{int(stats['mrr_current'])} ₽</b>",
        f"• С учётом текущего оттока: <b>{int(stats['mrr_after_churn'])} ₽</b>",
        "",
        "2️⃣ <b>🧾 Средний доход с одного платящего</b>",
        f"• В среднем за 30 дней: <b>{float(stats['arppu_30']):.1f} ₽</b>",
        "",
        "3️⃣ <b>🎁 Использование пробного периода</b>",
        f"• Всего брали пробный: <b>{int(stats['trial_periods'])}</b>",
        f"• Сейчас на пробном: <b>{int(stats['active_trials_now'])}</b>",
        f"• Из них реально пользуются VPN: <b>{int(stats['active_trials_using_now'])}</b>",
        f"• Не подключились или не пользуются: <b>{int(stats['active_trials_idle_now'])}</b>",
        f"• Конверсия пробный → оплата: <b>{float(stats['hist_trial_conversion_percent']):.1f}%</b>",
        "",
        "4️⃣ <b>⏱ Как быстро люди начинают пользоваться VPN</b>",
        f"• Среднее время до первого подключения: <b>{float(stats['avg_hours_to_first_handshake']):.1f} ч</b>",
        "",
        "5️⃣ <b>💳 Как быстро люди доходят до оплаты</b>",
        f"• Среднее время от пробного до первой оплаты: <b>{float(stats['avg_hours_to_paid']):.1f} ч</b>",
        "",
        "5️⃣1️⃣ <b>🎯 Ключевые конверсии</b>",
        f"• Start → Пробный период: <b>{float(stats['start_to_trial_percent']):.1f}%</b>",
        f"• Пробный → Handshake или Yandex-приглашение: <b>{float(stats['trial_to_activation_or_yandex_percent']):.1f}%</b>",
        "",
        "6️⃣ <b>🔁 Продления подписки</b>",
        f"• Получили напоминание за 3 дня: <b>{int(stats['renew_warn_3d_sent'])}</b> → оплатили: <b>{int(stats['renew_after_3d'])}</b>",
        f"• Получили напоминание за 1 день: <b>{int(stats['renew_warn_1d_sent'])}</b> → оплатили: <b>{int(stats['renew_after_1d'])}</b>",
        f"• Всего продлений за 30 дней: <b>{int(stats['renewals_last_30_days'])}</b>",
        f"• Отток за месяц: <b>{int(stats['churn_count'])}</b> из <b>{int(stats['churn_cohort'])}</b> (<b>{float(stats['churn_percent']):.1f}%</b>)",
        "",
        "7️⃣ <b>♻️ Сбросы VPN</b>",
        f"• Успешных сбросов за 7 дней: <b>{int(stats['reset_7'])}</b>",
        f"• Успешных сбросов за 30 дней: <b>{int(stats['reset_30'])}</b>",
        "",
        "8️⃣ <b>🌍 Нагрузка по серверам</b>",
    ]
    if stats.get('server_distribution'):
        for item in stats['server_distribution']:
            lines.append(f"• {html.escape(str(item['label']))}: <b>{int(item['active_peers'])}</b> активных peer ({float(item['share']):.1f}%) | 🟢 активных подключений за 24ч: <b>{int(item['handshakes_24h'])}</b>")
    else:
        lines.append("• Пока данных нет")
    lines += [
        "",
        "9️⃣ <b>🎁 Подарки от администрации</b>",
        f"• Всего выдано подарков: <b>{int(stats['gift_count'])}</b>",
        f"• Уникальных получателей: <b>{int(stats['gift_users'])}</b>",
        f"• Подарочных дней суммарно: <b>{int(stats['gift_days_total'])}</b>",
        f"• После подарка сами оплатили: <b>{int(stats['gift_to_paid'])}</b>",
        "",
        "🔟 <b>👥 Реферальная воронка</b>",
        f"• Нажали Start по реф-ссылке: <b>{int(stats['referral_link_opened'])}</b>",
        f"• Активных рефералов: <b>{int(stats['referral_active'])}</b>",
        f"• Реферал дошёл до оплаты: <b>{int(stats['referral_paid'])}</b>",
        f"• Начисления: в холде <b>{int(stats['referral_earning_pending'])}</b> / доступны <b>{int(stats['referral_earning_available'])}</b> / выплачены <b>{int(stats['referral_earning_paid'])}</b>",
        "",
        "1️⃣1️⃣ <b>👨‍👩‍👧‍👦 Семейные места</b>",
        f"• Увидели предложение семейного места: <b>{int(stats['family_upsell_seen'])}</b>",
        f"• Купили семейное место: <b>{int(stats['family_upsell_orders'])}</b>",
        f"• Конверсия в покупку: <b>{float(stats['family_upsell_conversion']):.1f}%</b>",
        f"• Активных семейных групп: <b>{int(stats['family_groups_active'])}</b> | активных профилей: <b>{int(stats['family_profiles_active'])}</b>",
        "",
        "1️⃣2️⃣ <b>😶 Кто платит, но почти не пользуется VPN</b>",
        f"• Есть активный VPN, но не было подключений 3+ дня: <b>{int(stats['silent_risk_3d'])}</b>",
        f"• Есть активный VPN, но не было подключений 7+ дней: <b>{int(stats['silent_risk_7d'])}</b>",
        "",
        "📌 <b>Основные цифры по боту</b>",
        f"• 👥 Всего пользователей: <b>{int(stats['total_users'])}</b> | 🚫 заблокировали бота: <b>{int(stats['blocked_users'])}</b>",
        f"• 🌐 Активных peer: <b>{int(stats['peers_total'])}</b> | 💳 Платящих сейчас: <b>{int(stats['active_paid_users'])}</b>",
        f"• 💰 Покупок на 199 ₽: <b>{int(stats['payments_199'])}</b> | ➕ Апселлов +99 ₽: <b>{int(stats['upsells_99'])}</b> | 📶 LTE на 99 ₽: <b>{int(stats['lte_99'])}</b>",
        f"• 📨 D1: <b>{int(stats['trial_d1_sent'])}</b>/<b>{int(stats['trial_d1_seen'])}</b> | D2: <b>{int(stats['trial_d2_sent'])}</b>/<b>{int(stats['trial_d2_seen'])}</b>",
        f"• 📅 Продажи за 30 дней: <b>{int(stats['sales_last_30'])}</b> / <b>{int(stats['revenue_last_30'])} ₽</b> | за 7 дней: <b>{int(stats['sales_last_7'])}</b> / <b>{int(stats['revenue_last_7'])} ₽</b>",
    ]
    parts = _split_html_lines(lines, limit=3400)
    await _send_html_chunks(cb.message, parts, reply_markup=_kb_admin_back(), edit_first=True)


@router.callback_query(lambda c: c.data == "admin:stats:conversions")
async def admin_conversions_report(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    try:
        await cb.answer("Собираю конверсии…")
    except Exception:
        pass

    try:
        stats = await _collect_full_bot_stats_v2()
    except Exception:
        log.exception("admin_conversions_report_failed")
        await cb.message.answer("❌ Не удалось собрать конверсии.", reply_markup=_kb_admin_back())
        return

    lines = [
        "🎯 <b>Конверсии и рост</b>",
        "",
        "<b>Основные конверсии</b>",
        f"• Start → Пробный период: <b>{float(stats['start_to_trial_percent']):.1f}%</b> (<b>{int(stats['trial_periods'])}</b> из <b>{int(stats['total_users'])}</b>)",
        f"• Пробный → Handshake или Yandex-приглашение: <b>{float(stats['trial_to_activation_or_yandex_percent']):.1f}%</b> (<b>{int(stats['trial_to_activation_or_yandex_count'])}</b> из <b>{int(stats['trial_periods'])}</b>)",
        f"• Пробный → Первая оплата: <b>{float(stats['hist_trial_conversion_percent']):.1f}%</b> (<b>{int(stats['trial_to_paid'])}</b> из <b>{int(stats['trial_periods'])}</b>)",
        "",
        "<b>Прирост новых платящих по 7-дневным периодам</b>",
    ]

    weekly = list(stats.get('paid_growth_weekly') or [])
    if weekly:
        show_weekly = weekly[-12:]
        for item in show_weekly:
            start_txt = (_ensure_tz_utc(item['start']) or _utcnow()).strftime('%d.%m.%Y')
            end_txt = (_ensure_tz_utc(item['end']) or _utcnow()).strftime('%d.%m.%Y')
            delta = int(item.get('delta', 0) or 0)
            sign = '+' if delta > 0 else ''
            lines.append(f"• {start_txt} — {end_txt}: <b>{int(item.get('count', 0) or 0)}</b> новых платящих | Разбор: <b>{sign}{delta}</b>")
    else:
        lines.append("• Пока нет данных")

    lines += ["", "<b>Платящая база по месяцам</b>"]
    monthly = list(stats.get('paid_growth_monthly') or [])
    if monthly:
        for item in monthly:
            lines.append(f"{html.escape(str(item.get('label') or '—'))}, кол-во платящих: <b>{int(item.get('count', 0) or 0)}</b>")
            delta = int(item.get('delta', 0) or 0)
            sign = '+' if delta > 0 else ''
            lines.append(f"Разбор: <b>{sign}{delta} платящих</b>")
    else:
        lines.append("Пока нет оплаченных подписок.")

    parts = _split_html_lines(lines, limit=3400)
    await _send_html_chunks(cb.message, parts, reply_markup=_kb_admin_back(), edit_first=True)


@router.callback_query(lambda c: c.data == "admin:stats:churn")
async def admin_churn_report(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    try:
        await cb.answer("Собираю отток…")
    except Exception:
        pass

    now = _utcnow()
    try:
        stats = await _collect_full_bot_stats_v2()
    except Exception:
        log.exception("admin_churn_report_failed")
        await cb.message.answer("❌ Не удалось собрать отток.", reply_markup=_kb_admin_back())
        return

    churned = list(stats.get('churned_users') or [])
    if not churned:
        await cb.message.edit_text(
            "📉 <b>Отток оплат</b>\n\nСейчас нет пользователей, у которых оплаченная подписка закончилась без следующего продления.",
            reply_markup=_kb_admin_back(),
            parse_mode="HTML",
        )
        return

    async with session_scope() as session:
        ids = [int(item['tg_id']) for item in churned[:200]]
        users = list((await session.execute(select(User).where(User.tg_id.in_(ids)))).scalars().all()) if ids else []
        user_by_id = {int(u.tg_id): u for u in users}

    lines = [
        "📉 <b>Пользователи в оттоке</b>",
        "",
        f"Всего в оттоке сейчас: <b>{len(churned)}</b>",
        "Показываю свежие случаи сверху.",
        "",
    ]
    for item in churned[:50]:
        uid = int(item['tg_id'])
        user = user_by_id.get(uid)
        name_parts = [str(getattr(user, 'first_name', '') or '').strip(), str(getattr(user, 'last_name', '') or '').strip()]
        name = ' '.join([p for p in name_parts if p]).strip() or '—'
        username = str(getattr(user, 'tg_username', '') or '').strip()
        username_txt = f"@{html.escape(username)}" if username else '—'
        expired_at = _ensure_tz_utc(item.get('expired_at'))
        days_in_churn = max(0, int((now - expired_at).total_seconds() // 86400)) if expired_at else 0
        lines += [
            f"• <code>{uid}</code> | <b>{html.escape(name)}</b> | {username_txt}",
            f"  Последняя оплата: <b>{_fmt_dt_short(item.get('last_paid_at'))}</b>",
            f"  Подписка закончилась: <b>{_fmt_dt_short(expired_at)}</b>",
            f"  В оттоке: <b>{days_in_churn}</b> дн. | Всего оплат: <b>{int(item.get('payments_count', 0) or 0)}</b>",
            "",
        ]

    parts = _split_html_lines(lines, limit=3400)
    await _send_html_chunks(cb.message, parts, reply_markup=_kb_admin_back(), edit_first=True)


@router.callback_query(lambda c: c.data == "admin:vpn:servers")
async def admin_vpn_servers(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    try:
        await cb.answer()
    except Exception:
        pass

    servers = _load_vpn_servers_admin()
    async with session_scope() as session:
        enabled_map = await _vpn_server_enabled_map_admin(session, servers)

    lines = ["🛠 <b>Управление VPN-серверами</b>", "", "Отключённый сервер больше не будет выдаваться новым пользователям, при сбросе, при смене локации и для семейных профилей."]
    for s in servers:
        code = str((s or {}).get("code") or "").strip().upper()
        if not code:
            continue
        name = str((s or {}).get("name") or code)
        state = "🟢 включён" if enabled_map.get(code, True) else "🔴 отключён"
        lines.append(f"• <b>{html.escape(name)}</b> ({html.escape(code)}) — {state}")

    await cb.message.edit_text("\n".join(lines), reply_markup=_kb_admin_vpn_servers(servers, enabled_map), parse_mode="HTML")


@router.callback_query(lambda c: (c.data or "").startswith("admin:vpn:servers:view:"))
async def admin_vpn_servers_view(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    code = (cb.data or "").split(":")[-1].upper()
    servers = _load_vpn_servers_admin()
    srv = next((s for s in servers if str((s or {}).get("code") or "").strip().upper() == code), None)
    if not srv:
        await cb.answer("Сервер не найден", show_alert=True)
        return
    async with session_scope() as session:
        enabled_map = await _vpn_server_enabled_map_admin(session, servers)
    enabled = enabled_map.get(code, True)
    name = str((srv or {}).get("name") or code)
    txt = (
        "🛠 <b>VPN-сервер</b>\n\n"
        f"Название: <b>{html.escape(name)}</b>\n"
        f"Код: <code>{html.escape(code)}</code>\n"
        f"Статус выдачи: <b>{'включён' if enabled else 'отключён'}</b>\n\n"
        "Когда сервер отключён, новая выдача на него не идёт по обычной логике проекта."
    )
    await cb.message.edit_text(txt, reply_markup=_kb_admin_vpn_servers(servers, enabled_map), parse_mode="HTML")


@router.callback_query(lambda c: (c.data or "").startswith("admin:vpn:servers:toggle:"))
async def admin_vpn_servers_toggle(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    code = (cb.data or "").split(":")[-1].upper()
    servers = _load_vpn_servers_admin()
    codes = [str((s or {}).get("code") or "").strip().upper() for s in servers if str((s or {}).get("code") or "").strip()]
    if code not in codes:
        await cb.answer("Сервер не найден", show_alert=True)
        return
    async with session_scope() as session:
        enabled_map = await _vpn_server_enabled_map_admin(session, servers)
        current = enabled_map.get(code, True)
        if current and sum(1 for c in codes if enabled_map.get(c, True)) <= 1:
            await cb.answer("Нельзя отключить последний активный сервер", show_alert=True)
            return
        await set_app_setting_int(session, f"vpn_server_enabled:{code}", 0 if current else 1)
        await session.commit()
        enabled_map = await _vpn_server_enabled_map_admin(session, servers)

    await cb.answer(("Сервер отключён" if current else "Сервер включён"), show_alert=False)
    lines = ["🛠 <b>Управление VPN-серверами</b>", "", "Отключённый сервер больше не будет выдаваться новым пользователям, при сбросе, при смене локации и для семейных профилей."]
    for s in servers:
        c = str((s or {}).get("code") or "").strip().upper()
        if not c:
            continue
        name = str((s or {}).get("name") or c)
        state = "🟢 включён" if enabled_map.get(c, True) else "🔴 отключён"
        lines.append(f"• <b>{html.escape(name)}</b> ({html.escape(c)}) — {state}")
    await cb.message.edit_text("\n".join(lines), reply_markup=_kb_admin_vpn_servers(servers, enabled_map), parse_mode="HTML")

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
            used = int(used_map.get(code, 0))
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
        last_error = None
        for _ in range(n):
            try:
                peer = await vpn_svc.create_extra_peer(session, tg_id)
                try:
                    if peer.get("host") and peer.get("user"):
                        await vpn_svc.ensure_rate_limit_for_server(
                            tg_id=tg_id,
                            ip=str(peer.get("client_ip") or ""),
                            host=str(peer.get("host") or ""),
                            port=int(peer.get("port") or 22),
                            user=str(peer.get("user") or ""),
                            password=peer.get("password"),
                            interface=str(peer.get("interface") or "wg0"),
                            tc_dev=str(peer.get("tc_dev") or ""),
                        )
                    else:
                        await vpn_svc.ensure_rate_limit(tg_id=tg_id, ip=str(peer.get("client_ip") or ""))
                except Exception:
                    pass
                conf_text = vpn_svc.build_wg_conf(
                    peer,
                    user_label=str(tg_id),
                    server_public_key=str(peer.get("server_public_key") or vpn_svc.server_pub),
                    endpoint=str(peer.get("endpoint") or vpn_svc.endpoint),
                    dns=str(peer.get("dns") or vpn_svc.dns),
                )
                filename = f"admin-{tg_id}-extra-{int(peer.get('peer_id') or 0)}.conf"
                await message.answer_document(
                    document=BufferedInputFile(conf_text.encode("utf-8"), filename=filename),
                    caption="WireGuard конфиг (доп. устройство).",
                )
                created += 1
            except Exception as e:
                last_error = str(e)
                log.exception("admin_vpn_extra_failed tg_id=%s", tg_id)

        await session.commit()

    await state.clear()
    tail = ""
    if created == 0 and last_error:
        tail = f"\n\n⚠️ Ошибка: <code>{html.escape(str(last_error)[:300])}</code>"
    await message.answer(
        f"✅ Создано конфигов: <b>{created}</b> из <b>{n}</b>.{tail}",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )



@router.callback_query(lambda c: c.data == "admin:vpn:queue_sim")
async def admin_vpn_queue_sim(cb: CallbackQuery) -> None:
    if not (is_owner(cb.from_user.id) or is_admin(cb.from_user.id)):
        await cb.answer()
        return

    try:
        await cb.answer("Запускаю симуляцию очереди…")
    except Exception:
        pass

    servers = _load_vpn_servers_admin()
    ready_servers = [s for s in servers if str(s.get("host") or "").strip() and str(s.get("user") or "").strip()]
    if not ready_servers:
        ready_servers = servers

    if not ready_servers:
        await cb.message.answer("⚠️ Не найдено ни одного VPN-сервера для симуляции.", reply_markup=_kb_admin_back())
        return

    try:
        live_used = await _vpn_seats_by_server()
    except Exception:
        live_used = {}

    text = _build_queue_sim_report(ready_servers, live_used)
    parts = _split_html_lines(text.splitlines())
    await _send_html_chunks(cb.message, parts, reply_markup=_kb_admin_back(), edit_first=False)


@router.callback_query(lambda c: c.data == "admin:vpn:test_config")
async def admin_vpn_test_config(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        await cb.answer()
    except Exception:
        pass

    await cb.message.answer(
        "Выбери сервер, на котором нужно создать тестовый конфиг.",
        reply_markup=_kb_admin_test_config_servers(),
    )


@router.callback_query(lambda c: c.data.startswith("admin:vpn:test_config:create:"))
async def admin_vpn_test_config_create(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    preferred_code = str(cb.data or "").split(":")[-1].strip().upper()
    try:
        await cb.answer("Создаю тестовый конфиг…")
    except Exception:
        pass

    from aiogram.types import BufferedInputFile

    tg_id = int(cb.from_user.id)
    try:
        server, peer, conf_text = await _create_admin_test_vpn_peer(
            tg_id=tg_id,
            preferred_code=None if preferred_code == "AUTO" else preferred_code,
        )
        code = str(server.get("code") or "").upper()
        filename = f"vpn-test-{code.lower()}-{tg_id}-{int(peer.get('peer_id') or 0)}.conf"
        await cb.message.answer_document(
            document=BufferedInputFile(conf_text.encode("utf-8"), filename=filename),
            caption=(
                f"🧪 Тестовый WireGuard-конфиг\n"
                f"Сервер: <b>{html.escape(_server_numbered_label(_load_vpn_servers_admin(), code))}</b>\n"
                f"IP: <code>{html.escape(str(peer.get('client_ip') or ''))}</code>\n\n"
                f"После проверки нажми в админке <b>«🧹 Удалить тестовый конфиг»</b>."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        await cb.message.answer(
            "⚠️ Не удалось создать тестовый конфиг.\n\n"
            f"Причина: <code>{html.escape(type(e).__name__)}: {html.escape(str(e)[:350])}</code>",
            reply_markup=_kb_admin_test_config_servers(),
            parse_mode="HTML",
        )


@router.callback_query(lambda c: c.data == "admin:vpn:test_config:reset")
async def admin_vpn_test_config_reset(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        await cb.answer()
    except Exception:
        pass

    await cb.message.answer(
        "Выбери сервер, с которого нужно удалить тестовый конфиг.",
        reply_markup=_kb_admin_test_config_reset_servers(),
    )


async def _reset_admin_test_configs_for_codes(*, tg_id: int, server_codes: set[str] | None = None) -> tuple[int, list[str], list[str]]:
    removed = 0
    touched_servers: list[str] = []
    errors: list[str] = []

    async with session_scope() as session:
        q = select(VpnPeer).where(
            VpnPeer.tg_id == tg_id,
            VpnPeer.rotation_reason.in_(["admin_test", "admin_test_reset"]),
        ).order_by(VpnPeer.id.desc())
        rows = list((await session.execute(q)).scalars().all())

        filtered: list[VpnPeer] = []
        for row in rows:
            row_code = str(row.server_code or "").strip().upper()
            if server_codes and row_code not in server_codes:
                continue
            filtered.append(row)

        for row in filtered:
            row_code = str(row.server_code or "").strip().upper()
            providers = _providers_for_server_code_admin(row_code)
            removed_remote = False
            last_error: str | None = None

            for provider in providers:
                try:
                    await provider.remove_peer(str(row.client_public_key))
                    removed_remote = True
                    break
                except Exception as e:
                    last_error = f"{type(e).__name__}: {e}"
                    continue

            if not removed_remote and row.is_active:
                if providers:
                    errors.append(f"{row_code or '—'}: не удалось удалить peer {row.client_public_key[:16]}… ({html.escape(str(last_error or 'unknown error')[:140])})")
                    continue
                errors.append(f"{row_code or '—'}: для peer {row.client_public_key[:16]}… не найден SSH-провайдер")
                continue

            row.is_active = False
            row.revoked_at = _utcnow()
            row.rotation_reason = "admin_test_reset"
            removed += 1
            if row_code and row_code not in touched_servers:
                touched_servers.append(row_code)

        await session.commit()

    return removed, touched_servers, errors


@router.callback_query(lambda c: c.data.startswith("admin:vpn:test_config:reset_server:"))
async def admin_vpn_test_config_reset_server(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    code = str(cb.data or "").split(":")[-1].strip().upper()
    try:
        await cb.answer(f"Удаляю тестовые конфиги с {code}…")
    except Exception:
        pass

    removed, touched_servers, errors = await _reset_admin_test_configs_for_codes(
        tg_id=int(cb.from_user.id),
        server_codes={code},
    )

    if removed == 0 and not errors:
        await cb.message.answer(
            f"ℹ️ На сервере <b>{html.escape(code)}</b> активных тестовых конфигов не найдено.",
            reply_markup=_kb_admin_back(),
            parse_mode="HTML",
        )
        return

    lines = [
        "🧹 <b>Удаление тестовых конфигов завершено</b>",
        f"Сервер: <b>{html.escape(code)}</b>",
        f"Удалено профилей: <b>{removed}</b>",
    ]
    if touched_servers:
        lines.append(f"Затронутые серверы: <b>{html.escape(', '.join(touched_servers))}</b>")
    if errors:
        lines.append("")
        lines.append("⚠️ Ошибки:")
        lines.extend(f"• {err}" for err in errors[:10])
    await cb.message.answer("\n".join(lines), reply_markup=_kb_admin_back(), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "admin:vpn:test_config:reset_all")
async def admin_vpn_test_config_reset_all(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        await cb.answer("Удаляю тестовые конфиги на всех серверах…")
    except Exception:
        pass

    removed, touched_servers, errors = await _reset_admin_test_configs_for_codes(
        tg_id=int(cb.from_user.id),
        server_codes=None,
    )

    if removed == 0 and not errors:
        await cb.message.answer(
            "ℹ️ Активных тестовых VPN-конфигов не найдено.",
            reply_markup=_kb_admin_back(),
        )
        return

    servers_text = ", ".join(html.escape(x) for x in touched_servers) if touched_servers else "—"
    lines = [
        "🧹 <b>Тестовые конфиги удалены</b>",
        f"Удалено профилей: <b>{removed}</b>",
        f"Серверы: <b>{servers_text}</b>",
    ]
    if errors:
        lines.append("")
        lines.append("⚠️ Ошибки:")
        lines.extend(f"• {err}" for err in errors[:10])
    await cb.message.answer("\n".join(lines), reply_markup=_kb_admin_back(), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "admin:vpn:usage")

async def admin_vpn_usage(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    try:
        await cb.answer()
    except Exception:
        pass

    try:
        used = await vpn_service.get_used_peer_stats()
    except Exception:
        log.exception("admin_vpn_usage_failed")
        await cb.message.answer(
            "❌ Не удалось собрать статистику по VPN. Проверь SSH-доступы до серверов и логи.",
            reply_markup=_kb_admin_back(),
        )
        return

    try:
        servers = _load_vpn_servers_admin()
        if not used:
            await _send_plain_chunks(
                cb.message,
                [
                    "📈 Кто пользовался VPN\n\nПока не найдено peer'ов с handshake или трафиком на текущих серверах.",
                ],
                reply_markup=_kb_admin_back(),
                edit_first=False,
            )
            return

        keys = [x.get("public_key") for x in used if x.get("public_key")][:1000]
        peer_rows: dict[str, VpnPeer] = {}
        subs_by_tg: dict[int, Subscription] = {}

        async with session_scope() as session:
            if keys:
                res = await session.execute(select(VpnPeer).where(VpnPeer.client_public_key.in_(keys)))
                for row in res.scalars().all():
                    peer_rows[row.client_public_key] = row

            tg_ids = sorted({int(row.tg_id) for row in peer_rows.values()})
            if tg_ids:
                res2 = await session.execute(select(Subscription).where(Subscription.tg_id.in_(tg_ids)))
                for sub in res2.scalars().all():
                    subs_by_tg[int(sub.tg_id)] = sub

        user_stats: dict[int, dict] = {}
        unknown_count = 0
        server_counts: dict[str, int] = {}

        for item in used:
            key = item.get("public_key")
            row = peer_rows.get(key)
            code = str((item.get("server_code") or (getattr(row, 'server_code', None) if row else None) or "NL")).upper()
            server_counts[code] = int(server_counts.get(code, 0) or 0) + 1
            if not row:
                unknown_count += 1
                continue
            tid = int(row.tg_id)
            st = user_stats.setdefault(tid, {
                "tg_id": tid,
                "peer_count": 0,
                "total_bytes": 0,
                "latest_hs": 0,
                "servers": set(),
                "has_active_sub": False,
            })
            st["peer_count"] += 1
            st["total_bytes"] += int(item.get("total_bytes", 0) or 0)
            st["latest_hs"] = max(int(st.get("latest_hs", 0) or 0), int(item.get("handshake_ts", 0) or 0))
            st["servers"].add(code)
            sub = subs_by_tg.get(tid)
            if sub and bool(getattr(sub, "is_active", False)):
                st["has_active_sub"] = True

        users = list(user_stats.values())
        users.sort(key=lambda x: (int(x.get("latest_hs", 0) or 0), int(x.get("total_bytes", 0) or 0)), reverse=True)

        tg_label: dict[int, str] = {}
        try:
            unique_tg_ids = [int(x["tg_id"]) for x in users[:50]]
            labels = await asyncio.gather(*[_tg_label(cb.bot, tid) for tid in unique_tg_ids], return_exceptions=True)
            for tid, lbl in zip(unique_tg_ids, labels):
                if isinstance(lbl, Exception):
                    tg_label[tid] = f"ID {tid}"
                else:
                    tg_label[tid] = str(lbl).replace("\n", " ").strip() or f"ID {tid}"
        except Exception:
            pass

        summary_lines = ["📈 Кто пользовался VPN", ""]
        summary_lines.append(f"Пользователей с признаками использования: {len(users)}")
        summary_lines.append(f"Peer'ов с handshake/трафиком на серверах: {len(used)}")
        if server_counts:
            parts = []
            for code, cnt in sorted(server_counts.items(), key=lambda x: x[0]):
                parts.append(f"{_server_numbered_label(servers, code)} — {cnt}")
            summary_lines.append("По серверам: " + " | ".join(parts))
        if unknown_count:
            summary_lines.append(f"Не сопоставилось с БД: {unknown_count}")
        summary_lines.append("")

        detail_lines: list[str] = []
        for idx, st in enumerate(users[:20], start=1):
            tid = int(st["tg_id"])
            who = tg_label.get(tid) or f"ID {tid}"
            hs = int(st.get("latest_hs", 0) or 0)
            last_seen = "—"
            if hs > 0:
                try:
                    last_seen = _fmt_dt_short(datetime.fromtimestamp(hs, tz=timezone.utc))
                except Exception:
                    last_seen = str(hs)
            server_labels = ", ".join(_server_numbered_label(servers, c) for c in sorted(st.get("servers") or [])) or "—"
            sub_state = "active" if st.get("has_active_sub") else "—"
            detail_lines.extend([
                f"{idx}. {who}",
                f"   peers: {int(st.get('peer_count', 0) or 0)} | traffic: {_fmt_bytes_short(int(st.get('total_bytes', 0) or 0))} | sub: {sub_state}",
                f"   last handshake: {last_seen}",
                f"   servers: {server_labels}",
                f"   id: {tid}",
                "",
            ])

        if len(users) > 20:
            detail_lines.append(f"Показаны первые 20 из {len(users)} пользователей.")
            detail_lines.append("")
        detail_lines.append("Отчёт строится по текущим peer'ам на серверах: handshake > 0 или есть трафик.")

        parts = _split_html_lines([re.sub(r"[<>]", "", x) for x in (summary_lines + detail_lines)], limit=2800)
        if not parts:
            parts = ["📈 Кто пользовался VPN\n\nНет данных."]
        await _send_plain_chunks(cb.message, parts, reply_markup=_kb_admin_back(), edit_first=False)
    except Exception:
        log.exception("admin_vpn_usage_render_failed")
        await cb.message.answer(
            "❌ Кнопка статистики VPN упала уже на этапе вывода. Логи сохранил. Попробуй ещё раз после выката фикса.",
            reply_markup=_kb_admin_back(),
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
        try:
            await vpn_service.reconcile_live_peers(session)
            await session.commit()
        except Exception:
            await session.rollback()
        res = await session.execute(
            select(VpnPeer)
            .where(VpnPeer.client_public_key.in_(keys))
            .order_by(VpnPeer.id.desc())
        )
        for row in res.scalars().all():
            existing = peer_rows.get(row.client_public_key)
            if existing is None:
                peer_rows[row.client_public_key] = row
                continue
            if bool(getattr(row, "is_active", False)) and not bool(getattr(existing, "is_active", False)):
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
        db_state = "✅" if bool(getattr(row, "is_active", False)) else "⚠️"
        age_s = "—" if age is None else f"{int(age)}s"
        shown += 1
        who = tg_label.get(int(row.tg_id)) or f"ID {row.tg_id}"
        code = (item.get("server_code") or row.server_code or os.environ.get("VPN_CODE", "NL")).upper()
        srv_label = html.escape(_server_numbered_label(servers, code))
        # keep tg_id in the end for unambiguous matching
        lines.append(
            f"{shown}. {who} | {srv_label} | <code>{row.client_public_key[:8]}…</code> | "
            f"{row.client_ip} | sub {sub_state} | db {db_state} | hs {age_s} | id <code>{row.tg_id}</code>"
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
    from app.db.models.family_vpn_group import FamilyVpnGroup
    from app.db.models.family_vpn_profile import FamilyVpnProfile

    now = datetime.now(timezone.utc)
    async with session_scope() as session:
        try:
            await vpn_service.reconcile_live_peers(session)
            await session.commit()
        except Exception:
            await session.rollback()
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


@router.callback_query(lambda c: c.data == "admin:ref:reset")
async def admin_ref_reset(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await state.clear()
    await state.set_state(AdminReferralAssignFSM.waiting_referred)
    await state.update_data(mode="reset")
    await cb.message.edit_text(
        "🧹 <b>Сбросить реферала</b>\n\n"
        "Отправь TG ID пользователя или @username. После сброса он будет считаться пришедшим самостоятельно, а новые реферальные начисления больше не пойдут.",
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

    if mode == "reset":
        async with session_scope() as session:
            ok, prev = await referral_service.admin_reset_referral(
                session, referred_tg_id=referred_id,
            )
            await session.commit()

        ref_lbl = await _format_user_label(message.bot, referred_id)
        prev_lbl = await _format_user_label(message.bot, prev) if prev else "—"
        await state.clear()
        await message.answer(
            "✅ <b>Готово</b>\n\n"
            f"Пользователь: <b>{ref_lbl}</b>\n"
            f"Был у: <b>{prev_lbl}</b>\n"
            "Теперь отображается как: <b>пришёл самостоятельно</b>",
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


@router.callback_query(lambda c: c.data == "admin:ref:percent")
async def admin_ref_percent_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await state.clear()
    await state.set_state(AdminReferralPercentFSM.waiting_target)
    await cb.message.edit_text(
        "🎯 <b>Изменить % реферальных отчислений</b>\n\n"
        "Отправь TG ID пользователя или @username, для которого нужно изменить процент.",
        reply_markup=_kb_ref_manage(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(AdminReferralPercentFSM.waiting_target)
async def admin_ref_percent_wait_target(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    target_id = await _resolve_tg_id_from_text(message.bot, message.text or "")
    if not target_id:
        await message.answer("❌ Не получилось распознать пользователя. Пришли TG ID (цифры) или @username")
        return

    async with session_scope() as session:
        progress = await referral_service.percent_progress(session, int(target_id))

    await state.update_data(target_tg_id=int(target_id))
    await state.set_state(AdminReferralPercentFSM.waiting_percent)

    current_pct = int(progress.get("current_percent", 0) or 0)
    has_override = bool(progress.get("has_override"))
    hint = "Сейчас у пользователя персональные условия." if has_override else "Сейчас у пользователя обычная лестница уровней."
    await message.answer(
        "🎯 <b>Изменить % реферальных отчислений</b>\n\n"
        f"Пользователь: <code>{int(target_id)}</code>\n"
        f"Текущий процент: <b>{current_pct}%</b>\n"
        f"{hint}\n\n"
        "Отправь новый процент числом от 0 до 100.\n"
        "Чтобы вернуть стандартную лестницу, отправь <code>reset</code> или <code>сброс</code>.",
        parse_mode="HTML",
        reply_markup=_kb_ref_manage(),
    )


@router.message(AdminReferralPercentFSM.waiting_percent)
async def admin_ref_percent_wait_percent(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    data = await state.get_data()
    target_tg_id = int(data.get("target_tg_id") or 0)
    if not target_tg_id:
        await state.clear()
        await message.answer("❌ Сессия сбилась. Открой управление рефералами заново.", reply_markup=kb_admin_menu())
        return

    raw = (message.text or "").strip().lower()
    reset_words = {"reset", "сброс", "очистить", "clear", "default", "по умолчанию"}
    percent_value = None
    if raw not in reset_words:
        try:
            percent_value = int(raw)
        except Exception:
            await message.answer("❌ Пришли число от 0 до 100 или reset/сброс для возврата к стандартной лестнице.")
            return
        if percent_value < 0 or percent_value > 100:
            await message.answer("❌ Процент должен быть от 0 до 100.")
            return

    async with session_scope() as session:
        await referral_service.set_percent_override(session, tg_id=int(target_tg_id), percent=percent_value)
        progress = await referral_service.percent_progress(session, int(target_tg_id))
        await session.commit()

    await state.clear()

    if percent_value is None:
        status_line = "Персональные условия сняты. Пользователь снова на стандартной лестнице."
    else:
        status_line = f"Установлен персональный процент: <b>{int(percent_value)}%</b>."

    next_line = "Следующий уровень: <b>персональные условия</b>"
    if not bool(progress.get("has_override")):
        next_percent = progress.get("next_percent")
        next_target = progress.get("next_target_active")
        left = progress.get("referrals_left_to_next")
        if next_percent is None or next_target is None:
            next_line = "Следующий уровень: <b>максимальный достигнут</b>"
        else:
            next_line = (
                f"Следующий уровень: <b>{int(next_percent)}%</b> при <b>{int(next_target)}</b> активных рефералах "
                f"(осталось <b>{int(left or 0)}</b>)"
            )

    await message.answer(
        "✅ <b>Готово</b>\n\n"
        f"Пользователь: <code>{int(target_tg_id)}</code>\n"
        f"{status_line}\n"
        f"Текущий процент: <b>{int(progress.get('current_percent', 0) or 0)}%</b>\n"
        f"{next_line}",
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
