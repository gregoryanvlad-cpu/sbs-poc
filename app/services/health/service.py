from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncssh
from sqlalchemy import func, select, and_, or_

from app.core.config import settings
from app.db.models.app_setting import AppSetting
from app.db.models.family_vpn_group import FamilyVpnGroup
from app.db.models.family_vpn_profile import FamilyVpnProfile
from app.db.models.lte_vpn_client import LteVpnClient
from app.db.models.message_audit import MessageAudit
from app.db.models.payment import Payment
from app.db.models.referral import Referral
from app.db.models.referral_earning import ReferralEarning
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vpn_peer import VpnPeer
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_invite_slot import YandexInviteSlot
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.services.vpn.ssh_provider import WireGuardSSHProvider

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    key: str
    title: str
    status: str  # ok|warn|fail
    summary: str
    details: list[str]


class HealthService:
    def __init__(self) -> None:
        self.started_at = datetime.now(timezone.utc)

    def _status_icon(self, status: str) -> str:
        return {"ok": "✅", "warn": "⚠️", "fail": "❌"}.get(status, "•")

    def _fmt_dt(self, dt: datetime | None) -> str:
        if not dt:
            return "—"
        try:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%d.%m %H:%M UTC")
        except Exception:
            return str(dt)

    async def _safe(self, key: str, title: str, coro) -> CheckResult:
        try:
            return await coro
        except Exception as e:
            log.exception("diag_check_failed key=%s", key)
            return CheckResult(key, title, "fail", f"{type(e).__name__}: {e}", ["Проверка упала с исключением."])

    def _load_vpn_servers(self) -> list[dict[str, Any]]:
        raw = os.environ.get("VPN_SERVERS_JSON") or os.environ.get("VPN_SERVERS")
        out: list[dict[str, Any]] = []
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and "servers" in data:
                    data = data["servers"]
                if isinstance(data, list):
                    out = [dict(x) for x in data if isinstance(x, dict)]
            except Exception:
                out = []
        if out:
            return out
        return [{
            "code": os.environ.get("VPN_CODE") or "NL1",
            "name": os.environ.get("VPN_NAME") or "Default",
            "host": os.environ.get("VPN_SSH_HOST") or "",
            "port": int(os.environ.get("VPN_SSH_PORT") or 22),
            "user": os.environ.get("VPN_SSH_USER") or "root",
            "password": os.environ.get("VPN_SSH_PASSWORD") or None,
            "interface": os.environ.get("VPN_INTERFACE") or "wg0",
            "tc_dev": os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or "",
            "max_active": int(os.environ.get("VPN_MAX_ACTIVE") or 40),
        }]

    async def _check_core(self) -> CheckResult:
        uptime = datetime.now(timezone.utc) - self.started_at
        details = [
            f"PID: {os.getpid()}",
            f"Uptime процесса: {str(uptime).split('.')[0]}",
            f"Scheduler enabled: {settings.scheduler_enabled}",
            f"LTE enabled: {settings.lte_enabled}",
            f"Yandex enabled: {settings.yandex_enabled}",
            f"RegionVPN enabled: {getattr(settings, 'regionvpn_enabled', False)}",
        ]
        warns = []
        if getattr(settings, "regionvpn_enabled", False):
            warns.append("старый RegionVPN всё ещё включён")
        status = "warn" if warns else "ok"
        if warns:
            details.extend([f"Риск: {w}" for w in warns])
        return CheckResult("core", "Приложение", status, "Базовые флаги и uptime", details)

    async def _check_db(self) -> CheckResult:
        async with session_scope() as session:
            ping = await session.execute(select(func.now()))
            _ = ping.scalar_one_or_none()
            counts = {
                "users": int(await session.scalar(select(func.count()).select_from(User)) or 0),
                "subs": int(await session.scalar(select(func.count()).select_from(Subscription)) or 0),
                "active_subs": int(await session.scalar(select(func.count()).select_from(Subscription).where(Subscription.is_active == True)) or 0),
                "wg_peers": int(await session.scalar(select(func.count()).select_from(VpnPeer)) or 0),
                "wg_active": int(await session.scalar(select(func.count()).select_from(VpnPeer).where(VpnPeer.is_active == True)) or 0),
                "family_groups": int(await session.scalar(select(func.count()).select_from(FamilyVpnGroup)) or 0),
                "family_profiles": int(await session.scalar(select(func.count()).select_from(FamilyVpnProfile)) or 0),
                "lte_clients": int(await session.scalar(select(func.count()).select_from(LteVpnClient)) or 0),
                "payments": int(await session.scalar(select(func.count()).select_from(Payment)) or 0),
                "yandex_memberships": int(await session.scalar(select(func.count()).select_from(YandexMembership)) or 0),
            }
            broken_active_subs = int(await session.scalar(
                select(func.count()).select_from(Subscription).outerjoin(VpnPeer, and_(VpnPeer.tg_id == Subscription.tg_id, VpnPeer.is_active == True))
                .where(Subscription.is_active == True, Subscription.end_at.is_not(None), Subscription.end_at > datetime.now(timezone.utc))
                .where(VpnPeer.id.is_(None))
            ) or 0)
        details = [f"{k}: {v}" for k, v in counts.items()]
        details.append(f"Активных VPN-подписок без активного WG peer: {broken_active_subs}")
        status = "warn" if broken_active_subs else "ok"
        return CheckResult("db", "База данных", status, "Подключение и базовые счётчики", details)

    async def _check_env(self) -> CheckResult:
        raw_env = dict(os.environ)
        region_keys = sorted([k for k in raw_env if k.startswith("REGION_") or k.startswith("REGIONVPN_")])
        missing = []
        if not settings.bot_token:
            missing.append("BOT_TOKEN")
        if not settings.database_url:
            missing.append("DATABASE_URL")
        if settings.lte_enabled:
            for k in ("LTE_SSH_HOST", "LTE_XRAY_CONFIG_PATH"):
                if not raw_env.get(k):
                    missing.append(k)
        details: list[str] = []
        if region_keys:
            details.append("Найдены legacy REGION-переменные: " + ", ".join(region_keys[:20]))
            if len(region_keys) > 20:
                details.append(f"... и ещё {len(region_keys) - 20}")
        if missing:
            details.append("Не хватает обязательных ENV: " + ", ".join(missing))
        status = "ok"
        if region_keys or missing:
            status = "warn" if not missing else "fail"
        if not details:
            details.append("Подозрительных ENV не найдено.")
        return CheckResult("env", "Конфигурация ENV", status, "Проверка опасных и устаревших переменных", details)

    async def _check_scheduler_state(self) -> CheckResult:
        async with session_scope() as session:
            now = datetime.now(timezone.utc)
            due_subs_7 = int(await session.scalar(select(func.count()).select_from(Subscription).where(
                Subscription.is_active == True,
                Subscription.end_at.is_not(None),
                Subscription.end_at > now,
                Subscription.end_at <= now + timedelta(days=7),
            )) or 0)
            due_trials = int(await session.scalar(select(func.count()).select_from(AppSetting).where(AppSetting.key.like("trial_end_ts:%"))) or 0)
            due_lte = int(await session.scalar(select(func.count()).select_from(LteVpnClient).where(
                LteVpnClient.is_enabled == True,
                LteVpnClient.cycle_anchor_end_at.is_not(None),
                LteVpnClient.cycle_anchor_end_at > now,
                LteVpnClient.cycle_anchor_end_at <= now + timedelta(days=7),
            )) or 0)
            recent_msg = await session.scalar(select(MessageAudit).order_by(MessageAudit.sent_at.desc()).limit(1))
        details = [
            f"scheduler_enabled: {settings.scheduler_enabled}",
            f"Подписок с окончанием <= 7 дней: {due_subs_7}",
            f"Trial ключей в app_settings: {due_trials}",
            f"LTE с окончанием <= 7 дней: {due_lte}",
            f"Последняя запись message_audit: {self._fmt_dt(getattr(recent_msg, 'sent_at', None))}",
        ]
        status = "ok" if settings.scheduler_enabled else "fail"
        return CheckResult("scheduler", "Scheduler и очереди", status, "Косвенная проверка задач и очередей уведомлений", details)

    async def _check_wg_servers(self) -> CheckResult:
        servers = self._load_vpn_servers()
        now_ts = int(datetime.now(timezone.utc).timestamp())
        lines: list[str] = []
        fail = 0
        warn = 0
        async with session_scope() as session:
            db_counts_raw = await session.execute(
                select(func.coalesce(func.upper(VpnPeer.server_code), "NL1"), func.count()).where(VpnPeer.is_active == True).group_by(func.coalesce(func.upper(VpnPeer.server_code), "NL1"))
            )
            db_counts = {str(code): int(cnt) for code, cnt in db_counts_raw.all()}
        async def one(server: dict[str, Any]) -> tuple[str, str]:
            code = str(server.get("code") or "").upper() or "?"
            title = f"{code} ({server.get('name') or code})"
            host = str(server.get("host") or "")
            if not host:
                return ("warn", f"{title}: host не задан")
            provider = WireGuardSSHProvider(
                host=host,
                port=int(server.get("port") or 22),
                user=str(server.get("user") or "root"),
                password=server.get("password") or None,
                interface=str(server.get("interface") or "wg0"),
                tc_dev=str(server.get("tc_dev") or server.get("wg_tc_dev") or "") or None,
                tc_parent_rate_mbit=int(server.get("tc_parent_rate_mbit") or 2000),
            )
            try:
                total = await provider.get_total_peers()
                active = await provider.get_active_peers(window_seconds=180)
                peers = await provider.list_peers()
                handshakes = await provider.get_latest_handshakes()
                none_hs = sum(1 for p in peers if int(handshakes.get(p, 0) or 0) == 0)
                stale = sum(1 for p in peers if int(handshakes.get(p, 0) or 0) > 0 and now_ts - int(handshakes.get(p, 0)) > 86400)
                db_count = int(db_counts.get(code, db_counts.get("NL1" if code == "NL" else code, 0)))
                cap = int(server.get("max_active") or os.environ.get("VPN_MAX_ACTIVE") or 40)
                free = max(0, cap - db_count)
                status = "ok"
                extra = []
                if abs(total - db_count) > 3:
                    status = "warn"
                    extra.append(f"расхождение WG/БД={total}/{db_count}")
                if free <= 2:
                    status = "warn"
                    extra.append(f"мало мест ({free})")
                return (status, f"{title}: ssh ok | wg={total} | active~3m={active} | db={db_count} | free={free} | no-hs={none_hs} | stale>24h={stale}" + (f" | {'; '.join(extra)}" if extra else ""))
            except Exception as e:
                return ("fail", f"{title}: SSH/WG ошибка: {type(e).__name__}: {e}")
        results = await asyncio.gather(*[one(s) for s in servers]) if servers else []
        for st, line in results:
            lines.append(line)
            if st == "fail":
                fail += 1
            elif st == "warn":
                warn += 1
        status = "fail" if fail else ("warn" if warn else "ok")
        if not lines:
            lines = ["VPN серверы не сконфигурированы."]
            status = "fail"
        return CheckResult("wg", "WireGuard серверы", status, "SSH/WG/лимиты/места по NL/FR", lines)

    async def _run_ssh_output(self, host: str, port: int, user: str, password: str | None, cmd: str, timeout: int = 15) -> str:
        key_b64 = os.environ.get("WG_SSH_PRIVATE_KEY_B64")
        key_obj = None
        if key_b64:
            try:
                import base64
                key_text = base64.b64decode(key_b64.encode()).decode()
                key_obj = asyncssh.import_private_key(key_text.strip())
            except Exception:
                key_obj = None
        conn = await asyncssh.connect(host, port=port, username=user, password=password if not key_obj else None, client_keys=[key_obj] if key_obj else None, known_hosts=None, connect_timeout=10, login_timeout=10)
        async with conn:
            res = await conn.run(f"PATH=/usr/sbin:/usr/bin:/sbin:/bin {cmd}", timeout=timeout, check=False)
            if res.exit_status != 0 and res.stderr:
                raise RuntimeError(res.stderr.strip() or f"exit={res.exit_status}")
            return (res.stdout or "").strip()

    async def _check_lte(self) -> CheckResult:
        if not settings.lte_enabled:
            return CheckResult("lte", "LTE / Xray", "warn", "LTE отключён флагом", ["settings.lte_enabled = False"])
        host = settings.lte_ssh_host
        user = settings.lte_ssh_user
        password = settings.lte_ssh_password
        lines: list[str] = [f"host={host} user={user} log={settings.lte_access_log_path}"]
        status = "ok"
        try:
            svc = await self._run_ssh_output(host, settings.lte_ssh_port, user, password, "systemctl is-active xray")
            lines.append(f"xray.service: {svc or 'unknown'}")
            if svc.strip() != "active":
                status = "fail"
            size = await self._run_ssh_output(host, settings.lte_ssh_port, user, password, f"stat -c '%s' {settings.lte_access_log_path} 2>/dev/null || echo 0")
            lines.append(f"access.log size: {size or '0'} bytes")
            tail = await self._run_ssh_output(host, settings.lte_ssh_port, user, password, f"tail -n 3 {settings.lte_access_log_path} 2>/dev/null || true")
            if tail:
                lines.append("Последние строки access.log:")
                lines.extend([f"  {ln}" for ln in tail.splitlines()[:3]])
            ss = await self._run_ssh_output(host, settings.lte_ssh_port, user, password, "ss -tn state established '( sport = :443 or dport = :443 )' | tail -n +2 | wc -l")
            lines.append(f"ESTABLISHED :443: {ss or '0'}")
        except Exception as e:
            status = "fail"
            lines.append(f"SSH/LTE ошибка: {type(e).__name__}: {e}")
        async with session_scope() as session:
            total = int(await session.scalar(select(func.count()).select_from(LteVpnClient)) or 0)
            enabled = int(await session.scalar(select(func.count()).select_from(LteVpnClient).where(LteVpnClient.is_enabled == True)) or 0)
            expiring = int(await session.scalar(select(func.count()).select_from(LteVpnClient).where(LteVpnClient.is_enabled == True, LteVpnClient.cycle_anchor_end_at.is_not(None), LteVpnClient.cycle_anchor_end_at <= datetime.now(timezone.utc) + timedelta(days=7))) or 0)
            dup_email = int(await session.scalar(select(func.count()).select_from(
                select(LteVpnClient.email).group_by(LteVpnClient.email).having(func.count() > 1).subquery()
            )) or 0)
        lines.append(f"БД LTE: total={total} enabled={enabled} expiring<=7d={expiring} dup_email={dup_email}")
        if dup_email:
            status = "warn" if status == "ok" else status
        return CheckResult("lte", "LTE / Xray", status, "SSH, xray, access.log, клиенты LTE", lines)

    async def _check_yandex(self) -> CheckResult:
        async with session_scope() as session:
            total_acc = int(await session.scalar(select(func.count()).select_from(YandexAccount)) or 0)
            active_acc = int(await session.scalar(select(func.count()).select_from(YandexAccount).where(YandexAccount.status == "active")) or 0)
            free_slots = int(await session.scalar(select(func.count()).select_from(YandexInviteSlot).where(YandexInviteSlot.status == "free")) or 0)
            issued_slots = int(await session.scalar(select(func.count()).select_from(YandexInviteSlot).where(YandexInviteSlot.status == "issued")) or 0)
            active_members = int(await session.scalar(select(func.count()).select_from(YandexMembership).where(YandexMembership.status.in_(["active", "pending", "issued"]))) or 0)
            no_invite = int(await session.scalar(select(func.count()).select_from(YandexMembership).where(YandexMembership.status == "active", YandexMembership.invite_link.is_(None))) or 0)
            expired_cov = int(await session.scalar(select(func.count()).select_from(YandexMembership).where(YandexMembership.coverage_end_at.is_not(None), YandexMembership.coverage_end_at < datetime.now(timezone.utc))) or 0)
        status = "ok"
        if free_slots <= 1 or no_invite or expired_cov:
            status = "warn"
        details = [
            f"Аккаунтов всего/active: {total_acc}/{active_acc}",
            f"Слотов free/issued: {free_slots}/{issued_slots}",
            f"Memberships active|pending|issued: {active_members}",
            f"Активных memberships без invite_link: {no_invite}",
            f"Memberships с уже истёкшим coverage_end_at: {expired_cov}",
        ]
        return CheckResult("yandex", "Yandex Plus", status, "Аккаунты, слоты, memberships, ротации", details)

    async def _check_family(self) -> CheckResult:
        now = datetime.now(timezone.utc)
        async with session_scope() as session:
            active = int(await session.scalar(select(func.count()).select_from(FamilyVpnGroup).where(FamilyVpnGroup.active_until.is_not(None), FamilyVpnGroup.active_until > now)) or 0)
            grace = int(await session.scalar(select(func.count()).select_from(FamilyVpnGroup).where(FamilyVpnGroup.active_until.is_not(None), FamilyVpnGroup.active_until <= now, FamilyVpnGroup.active_until > now - timedelta(hours=24))) or 0)
            expired = int(await session.scalar(select(func.count()).select_from(FamilyVpnGroup).where(FamilyVpnGroup.active_until.is_not(None), FamilyVpnGroup.active_until <= now - timedelta(hours=24))) or 0)
            mismatched = int(await session.scalar(select(func.count()).select_from(
                select(FamilyVpnProfile.owner_tg_id).group_by(FamilyVpnProfile.owner_tg_id).subquery()
            )) or 0)
            seat_rows = await session.execute(select(FamilyVpnGroup.owner_tg_id, FamilyVpnGroup.seats_total))
            seat_map = {int(tg): int(seats or 0) for tg, seats in seat_rows.all()}
            prof_rows = await session.execute(select(FamilyVpnProfile.owner_tg_id, func.count()).group_by(FamilyVpnProfile.owner_tg_id))
            bad_owners = []
            for owner, cnt in prof_rows.all():
                if int(cnt or 0) != int(seat_map.get(int(owner), 0) or 0):
                    bad_owners.append(int(owner))
        status = "warn" if grace or expired or bad_owners else "ok"
        details = [
            f"Активных family groups: {active}",
            f"В grace (<=24ч): {grace}",
            f"Просрочены после grace: {expired}",
            f"Owners с mismatch seats/profile count: {len(bad_owners)}",
        ]
        if bad_owners[:5]:
            details.append("Примеры owner_tg_id mismatch: " + ", ".join(map(str, bad_owners[:5])))
        return CheckResult("family", "Family groups / grace", status, "Grace, purge и консистентность мест", details)

    async def _check_reminders(self) -> CheckResult:
        now = datetime.now(timezone.utc)
        async with session_scope() as session:
            vpn_due_1 = int(await session.scalar(select(func.count()).select_from(Subscription).where(
                Subscription.is_active == True, Subscription.end_at.is_not(None), Subscription.end_at > now, Subscription.end_at <= now + timedelta(days=1)
            )) or 0)
            lte_due_1 = int(await session.scalar(select(func.count()).select_from(LteVpnClient).where(
                LteVpnClient.is_enabled == True, LteVpnClient.cycle_anchor_end_at.is_not(None), LteVpnClient.cycle_anchor_end_at > now, LteVpnClient.cycle_anchor_end_at <= now + timedelta(days=1)
            )) or 0)
            yandex_due_1 = int(await session.scalar(select(func.count()).select_from(YandexMembership).where(
                YandexMembership.coverage_end_at.is_not(None), YandexMembership.coverage_end_at > now, YandexMembership.coverage_end_at <= now + timedelta(days=1)
            )) or 0)
            recent_notifs = await session.execute(select(MessageAudit.kind, func.count()).where(MessageAudit.sent_at >= now - timedelta(days=2)).group_by(MessageAudit.kind).order_by(func.count().desc()).limit(10))
        details = [
            f"VPN истекает <=24ч: {vpn_due_1}",
            f"LTE истекает <=24ч: {lte_due_1}",
            f"Yandex coverage <=24ч: {yandex_due_1}",
            "Последние виды сообщений за 48ч:",
        ]
        rows = recent_notifs.all()
        if rows:
            details.extend([f"  {k}: {c}" for k, c in rows])
        else:
            details.append("  message_audit пуст за 48ч")
        status = "ok" if rows else "warn"
        return CheckResult("reminders", "Напоминания и уведомления", status, "Очереди истечения и фактические отправки", details)

    async def _check_payments_referrals(self) -> CheckResult:
        async with session_scope() as session:
            pending = int(await session.scalar(select(func.count()).select_from(Payment).where(Payment.status == "pending")) or 0)
            success = int(await session.scalar(select(func.count()).select_from(Payment).where(Payment.status == "success")) or 0)
            ref_pending = int(await session.scalar(select(func.count()).select_from(ReferralEarning).where(ReferralEarning.status == "pending")) or 0)
            ref_available = int(await session.scalar(select(func.count()).select_from(ReferralEarning).where(ReferralEarning.status == "available")) or 0)
            referrals = int(await session.scalar(select(func.count()).select_from(Referral)) or 0)
            orphan_ref = int(await session.scalar(select(func.count()).select_from(ReferralEarning).where(ReferralEarning.payment_id.is_(None), ReferralEarning.status != "paid")) or 0)
        status = "warn" if pending or orphan_ref else "ok"
        details = [
            f"Payments success/pending: {success}/{pending}",
            f"Referrals total: {referrals}",
            f"Referral earnings pending/available: {ref_pending}/{ref_available}",
            f"Referral earnings без payment_id (не paid): {orphan_ref}",
        ]
        return CheckResult("payments", "Платежи и рефералка", status, "Платежные статусы и referral hold", details)

    async def _check_business_anomalies(self) -> CheckResult:
        now = datetime.now(timezone.utc)
        async with session_scope() as session:
            dup_ip = int(await session.scalar(select(func.count()).select_from(select(VpnPeer.client_ip).where(VpnPeer.is_active == True).group_by(VpnPeer.client_ip).having(func.count() > 1).subquery())) or 0)
            dup_key = int(await session.scalar(select(func.count()).select_from(select(VpnPeer.client_public_key).where(VpnPeer.is_active == True).group_by(VpnPeer.client_public_key).having(func.count() > 1).subquery())) or 0)
            stale_active_peer = int(await session.scalar(select(func.count()).select_from(VpnPeer).join(Subscription, Subscription.tg_id == VpnPeer.tg_id).where(VpnPeer.is_active == True, Subscription.is_active == False)) or 0)
            enabled_lte_expired = int(await session.scalar(select(func.count()).select_from(LteVpnClient).where(LteVpnClient.is_enabled == True, LteVpnClient.cycle_anchor_end_at.is_not(None), LteVpnClient.cycle_anchor_end_at < now)) or 0)
            yandex_without_sub = int(await session.scalar(select(func.count()).select_from(YandexMembership).outerjoin(Subscription, Subscription.tg_id == YandexMembership.tg_id).where(YandexMembership.status == "active").where(or_(Subscription.tg_id.is_(None), Subscription.is_active == False))) or 0)
        details = [
            f"Дубликаты client_ip среди active WG peers: {dup_ip}",
            f"Дубликаты public_key среди active WG peers: {dup_key}",
            f"Active WG peer при inactive subscription: {stale_active_peer}",
            f"Enabled LTE с истёкшим cycle_anchor_end_at: {enabled_lte_expired}",
            f"Active Yandex membership без active subscription: {yandex_without_sub}",
        ]
        total_bad = dup_ip + dup_key + stale_active_peer + enabled_lte_expired + yandex_without_sub
        status = "warn" if total_bad else "ok"
        return CheckResult("anomalies", "Бизнес-анomalии", status, "Что может ломать пользовательский опыт прямо сейчас", details)

    async def run(self, *, full: bool = False) -> list[CheckResult]:
        checks = [
            self._safe("core", "Приложение", self._check_core()),
            self._safe("env", "Конфигурация ENV", self._check_env()),
            self._safe("db", "База данных", self._check_db()),
            self._safe("scheduler", "Scheduler и очереди", self._check_scheduler_state()),
            self._safe("wg", "WireGuard серверы", self._check_wg_servers()),
            self._safe("lte", "LTE / Xray", self._check_lte()),
            self._safe("yandex", "Yandex Plus", self._check_yandex()),
            self._safe("family", "Family groups / grace", self._check_family()),
            self._safe("reminders", "Напоминания и уведомления", self._check_reminders()),
            self._safe("payments", "Платежи и рефералка", self._check_payments_referrals()),
            self._safe("anomalies", "Бизнес-анomalии", self._check_business_anomalies()),
        ]
        results = await asyncio.gather(*checks)
        return results

    def render_summary(self, results: list[CheckResult], *, full: bool = False) -> str:
        fail = sum(1 for x in results if x.status == "fail")
        warn = sum(1 for x in results if x.status == "warn")
        ok = sum(1 for x in results if x.status == "ok")
        lines = ["🩺 <b>Диагностика системы</b>", "", f"✅ OK: <b>{ok}</b> | ⚠️ WARN: <b>{warn}</b> | ❌ FAIL: <b>{fail}</b>", f"Время проверки: <b>{self._fmt_dt(datetime.now(timezone.utc))}</b>", ""]
        for item in results:
            lines.append(f"{self._status_icon(item.status)} <b>{item.title}</b> — {item.summary}")
            lines.append(f"   {item.summary if False else item.summary}")
            if item.details:
                for d in item.details[:(10 if full else 3)]:
                    safe = str(d).replace("<", "&lt;").replace(">", "&gt;")
                    lines.append(f"   • {safe}")
                if len(item.details) > (10 if full else 3):
                    lines.append(f"   • ... ещё {len(item.details) - (10 if full else 3)}")
            lines.append("")
        return "\n".join(lines)


health_service = HealthService()
