from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import calendar
from typing import Optional

import asyncssh
from sqlalchemy import and_, func, or_, select

from app.core.config import settings
from app.db.models import LteVpnClient, Subscription
from app.db.session import session_scope

log = logging.getLogger(__name__)
ENV_PATH = "PATH=/usr/sbin:/usr/bin:/sbin:/bin"
_EMAIL_RE = re.compile(r"(?P<email>\d+@lte)")
_IPV4_RE = re.compile(r"(?<![\d.])(?P<ip>(?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
_TS_PATTERNS = (
    re.compile(r"(?P<ts>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})"),
    re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"),
)
_PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
)


@dataclass
class _InboundRef:
    inbound: dict
    clients: list


@dataclass
class LtePollResult:
    connected_ids: list[int]
    warned_ids: list[int]
    strict_disabled_ids: list[int]


class LteVpnService:
    def __init__(self) -> None:
        self.ssh_host = settings.lte_ssh_host
        self.ssh_port = settings.lte_ssh_port
        self.ssh_user = settings.lte_ssh_user
        self.ssh_password = settings.lte_ssh_password
        self.xray_config_path = settings.lte_xray_config_path
        self.restart_command = settings.lte_xray_restart_command
        self.access_log_path = settings.lte_access_log_path
        self.max_clients = settings.lte_max_clients
        self.vless_host = settings.lte_vless_host or settings.lte_ssh_host
        self.vless_port = settings.lte_vless_port
        self.ws_path = settings.lte_ws_path or "/"
        self.inbound_tag = settings.lte_inbound_tag or "inbound-clients"
        self._key_obj = None
        self._seen_line_hashes: deque[str] = deque(maxlen=4000)
        self._seen_line_hash_set: set[str] = set()
        self._events_by_email: dict[str, deque[tuple[datetime, str]]] = defaultdict(lambda: deque(maxlen=200))
        self._anti_share_cooldown_until: dict[int, datetime] = {}
        key_b64 = (os.environ.get("LTE_SSH_PRIVATE_KEY_B64") or "").strip()
        if key_b64:
            try:
                key_text = base64.b64decode(key_b64.encode()).decode()
                self._key_obj = asyncssh.import_private_key(key_text.strip())
            except Exception:
                log.exception("[ltevpn] failed to import LTE_SSH_PRIVATE_KEY_B64")

    def _validate(self) -> None:
        if not settings.lte_enabled:
            raise RuntimeError("lte_disabled")
        if not self.ssh_host:
            raise RuntimeError("lte_not_configured")

    async def _connect(self):
        self._validate()
        return await asyncssh.connect(
            self.ssh_host,
            port=self.ssh_port,
            username=self.ssh_user,
            password=self.ssh_password if self._key_obj is None else None,
            client_keys=[self._key_obj] if self._key_obj is not None else None,
            known_hosts=None,
        )

    async def _run_output(self, cmd: str, *, check: bool = True) -> str:
        async with await self._connect() as conn:
            result = await conn.run(f"{ENV_PATH} {cmd}", check=check, timeout=20)
            return (result.stdout or "").strip()

    async def _run(self, cmd: str) -> None:
        await self._run_output(cmd)

    async def _restart_xray(self) -> None:
        await self._run(f"sudo {self.restart_command}" if not self.restart_command.strip().startswith("sudo") else self.restart_command)

    async def _read_xray_config(self) -> dict:
        out = await self._run_output(f"cat {self.xray_config_path}")
        return json.loads(out)

    async def _write_xray_config(self, cfg: dict) -> None:
        text = json.dumps(cfg, ensure_ascii=False, indent=2)
        marker = "__LTE_XRAY_CFG__"
        tmp_path = "/tmp/xray_lte_config.json"
        cmd = (
            f"cat > {tmp_path} <<'{marker}'\n"
            f"{text}\n"
            f"{marker}\n"
            f"sudo install -m 644 {tmp_path} {self.xray_config_path}"
        )
        await self._run(cmd)

    def _find_inbound(self, cfg: dict) -> _InboundRef:
        for ib in (cfg.get("inbounds") or []):
            if not isinstance(ib, dict):
                continue
            if (ib.get("tag") or "") == self.inbound_tag or (ib.get("protocol") or "").lower() == "vless":
                st = ib.setdefault("settings", {})
                clients = st.setdefault("clients", [])
                if isinstance(clients, list):
                    return _InboundRef(inbound=ib, clients=clients)
        raise RuntimeError("lte_inbound_not_found")

    @staticmethod
    def email_for_tg_id(tg_id: int) -> str:
        return f"{int(tg_id)}@lte"

    def build_vless_url(self, client_uuid: str, *, tg_id: int) -> str:
        path = self.ws_path if self.ws_path.startswith("/") else f"/{self.ws_path}"
        params = f"type=ws&security=none&path={self._url_escape(path)}&host={self._url_escape(self.vless_host)}"
        return f"vless://{client_uuid}@{self.vless_host}:{self.vless_port}?{params}#{self._url_escape(f'VPN LTE {tg_id}')}"

    @staticmethod
    def _url_escape(s: str) -> str:
        return str(s).replace("%", "%25").replace(" ", "%20").replace("#", "%23").replace("?", "%3F").replace("&", "%26").replace("/", "%2F")

    @staticmethod
    def _add_one_month(dt: datetime) -> datetime:
        base = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        year = base.year
        month = base.month + 1
        if month > 12:
            month = 1
            year += 1
        day = min(base.day, calendar.monthrange(year, month)[1])
        return base.replace(year=year, month=month, day=day)

    @staticmethod
    def _parse_timestamp(line: str) -> datetime | None:
        for rx in _TS_PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            raw = m.group("ts")
            try:
                if "/" in raw:
                    return datetime.strptime(raw, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
                norm = raw.replace("Z", "+00:00")
                if re.match(r".*[+-]\d{4}$", norm):
                    norm = norm[:-5] + norm[-5:-2] + ":" + norm[-2:]
                dt = datetime.fromisoformat(norm)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
        return None

    @staticmethod
    def _extract_public_ip(line: str) -> str | None:
        for m in _IPV4_RE.finditer(line):
            raw = m.group("ip")
            try:
                ip = ipaddress.ip_address(raw)
            except Exception:
                continue
            if any(ip in net for net in _PRIVATE_NETS):
                continue
            return raw
        return None

    def _remember_line(self, line: str) -> bool:
        digest = hashlib.sha1(line.encode("utf-8", errors="ignore")).hexdigest()
        if digest in self._seen_line_hash_set:
            return False
        if len(self._seen_line_hashes) == self._seen_line_hashes.maxlen:
            old = self._seen_line_hashes.popleft()
            self._seen_line_hash_set.discard(old)
        self._seen_line_hashes.append(digest)
        self._seen_line_hash_set.add(digest)
        return True

    async def active_clients_count(self) -> int:
        async with session_scope() as session:
            now = datetime.now(timezone.utc)
            q = (
                select(func.count())
                .select_from(LteVpnClient)
                .outerjoin(Subscription, Subscription.tg_id == LteVpnClient.tg_id)
                .where(
                    LteVpnClient.is_enabled == True,
                    or_(
                        and_(Subscription.is_active == True, Subscription.end_at.is_not(None), Subscription.end_at > now),
                        and_(LteVpnClient.cycle_anchor_end_at.is_not(None), LteVpnClient.cycle_anchor_end_at > now),
                    ),
                )
            )
            val = await session.scalar(q)
            return int(val or 0)

    async def get_or_create_client(self, tg_id: int, *, subscription_end_at: datetime | None, force_rotate: bool = False) -> LteVpnClient:
        async with session_scope() as session:
            row = await session.get(LteVpnClient, tg_id)
            email = self.email_for_tg_id(tg_id)
            if row is None:
                row = LteVpnClient(tg_id=tg_id, uuid=str(uuid.uuid4()), email=email, rate_mbit=settings.lte_rate_mbit)
                session.add(row)
            if force_rotate:
                row.uuid = str(uuid.uuid4())
                row.notified_at = None
                row.last_seen_at = None
            row.email = email
            row.is_enabled = True
            if row.cycle_anchor_end_at is None and subscription_end_at is not None:
                row.cycle_anchor_end_at = subscription_end_at
            row.updated_at = datetime.now(timezone.utc)
            await session.flush()
            await session.commit()
            return row

    async def get_client(self, tg_id: int) -> LteVpnClient | None:
        async with session_scope() as session:
            return await session.get(LteVpnClient, tg_id)

    async def activate_paid_month(self, tg_id: int) -> LteVpnClient:
        now = datetime.now(timezone.utc)
        async with session_scope() as session:
            row = await session.get(LteVpnClient, tg_id)
            email = self.email_for_tg_id(tg_id)
            if row is None:
                row = LteVpnClient(tg_id=tg_id, uuid=str(uuid.uuid4()), email=email, rate_mbit=settings.lte_rate_mbit)
                session.add(row)
            row.email = email
            row.is_enabled = True
            anchor = row.cycle_anchor_end_at if row.cycle_anchor_end_at and row.cycle_anchor_end_at > now else now
            row.cycle_anchor_end_at = self._add_one_month(anchor)
            row.updated_at = now
            await session.flush()
            await session.commit()
            return row

    async def has_lte_access(self, tg_id: int, *, subscription_end_at: datetime | None, has_success_payment: bool) -> bool:
        if not subscription_end_at:
            return False
        if not has_success_payment:
            return True
        now = datetime.now(timezone.utc)
        async with session_scope() as session:
            row = await session.get(LteVpnClient, tg_id)
            if not row or not row.cycle_anchor_end_at or not row.is_enabled:
                return False
            try:
                anchor = row.cycle_anchor_end_at.astimezone(timezone.utc)
            except Exception:
                anchor = row.cycle_anchor_end_at
            return bool(anchor and anchor > now)

    async def ensure_remote_client(self, tg_id: int, client_uuid: str) -> None:
        email = self.email_for_tg_id(tg_id)
        cfg = await self._read_xray_config()
        ref = self._find_inbound(cfg)
        changed = False
        filtered = []
        for c in ref.clients:
            if not isinstance(c, dict):
                filtered.append(c)
                continue
            if c.get("email") == email:
                if c.get("id") != client_uuid:
                    c["id"] = client_uuid
                    changed = True
                filtered.append(c)
            else:
                filtered.append(c)
        if not any(isinstance(c, dict) and c.get("email") == email for c in filtered):
            filtered.append({"id": client_uuid, "email": email})
            changed = True
        ref.inbound.setdefault("settings", {})["clients"] = filtered
        if changed:
            await self._write_xray_config(cfg)
            await self._restart_xray()

    async def disable_remote_client(self, tg_id: int) -> None:
        email = self.email_for_tg_id(tg_id)
        cfg = await self._read_xray_config()
        ref = self._find_inbound(cfg)
        new_clients = [c for c in ref.clients if not (isinstance(c, dict) and c.get("email") == email)]
        if len(new_clients) != len(ref.clients):
            ref.inbound.setdefault("settings", {})["clients"] = new_clients
            await self._write_xray_config(cfg)
            await self._restart_xray()

    async def sync_client(self, tg_id: int, *, subscription_end_at: datetime | None, force_rotate: bool = False) -> LteVpnClient:
        row = await self.get_or_create_client(tg_id, subscription_end_at=subscription_end_at, force_rotate=force_rotate)
        await self.ensure_remote_client(tg_id, row.uuid)
        return row

    async def _remote_client_map(self) -> dict[str, str]:
        cfg = await self._read_xray_config()
        ref = self._find_inbound(cfg)
        result: dict[str, str] = {}
        for c in ref.clients:
            if not isinstance(c, dict):
                continue
            email = str(c.get("email") or "").strip()
            cid = str(c.get("id") or "").strip()
            if email:
                result[email] = cid
        return result

    async def repair_active_clients(self, *, tg_id: int | None = None, limit: int = 200) -> dict[str, int]:
        """Best-effort self-heal for active LTE profiles.

        Re-enables locally disabled rows and re-syncs the remote Xray client
        for users who still have a valid main subscription or paid LTE cycle.
        Returns precise counters so admin UI can distinguish between scanned
        rows and actually fixed rows.
        """
        now = datetime.now(timezone.utc)
        async with session_scope() as session:
            q = (
                select(LteVpnClient.tg_id, LteVpnClient.uuid, LteVpnClient.is_enabled, LteVpnClient.cycle_anchor_end_at, Subscription.end_at, Subscription.is_active)
                .outerjoin(Subscription, Subscription.tg_id == LteVpnClient.tg_id)
                .where(
                    or_(
                        and_(Subscription.is_active == True, Subscription.end_at.is_not(None), Subscription.end_at > now),
                        and_(LteVpnClient.cycle_anchor_end_at.is_not(None), LteVpnClient.cycle_anchor_end_at > now),
                    )
                )
                .order_by(LteVpnClient.tg_id.asc())
                .limit(int(limit))
            )
            if tg_id is not None:
                q = q.where(LteVpnClient.tg_id == int(tg_id))
            rows = (await session.execute(q)).all()

        remote_by_email: dict[str, str] = {}
        try:
            remote_by_email = await self._remote_client_map()
        except Exception:
            log.exception("lte_repair_remote_read_failed")

        scanned = len(rows)
        repaired = 0
        reenabled = 0
        already_ok = 0
        failed = 0
        touched_tg_ids: list[int] = []

        for row in rows:
            row_tg_id = int(row.tg_id)
            sub_end = row.end_at
            email = self.email_for_tg_id(row_tg_id)
            remote_uuid = str(remote_by_email.get(email) or "").strip()
            local_uuid = str(row.uuid or "").strip()
            needs_remote_fix = (not remote_uuid) or (local_uuid and remote_uuid != local_uuid)
            needs_reenable = not bool(row.is_enabled)
            try:
                await self.sync_client(row_tg_id, subscription_end_at=sub_end, force_rotate=False)
                if needs_reenable:
                    reenabled += 1
                if needs_reenable or needs_remote_fix:
                    repaired += 1
                    touched_tg_ids.append(row_tg_id)
                else:
                    already_ok += 1
            except Exception:
                failed += 1
                log.exception("lte_repair_active_client_failed tg_id=%s", row_tg_id)

        return {
            "scanned": scanned,
            "repaired": repaired,
            "reenabled": reenabled,
            "already_ok": already_ok,
            "failed": failed,
            "touched_tg_ids": touched_tg_ids,
        }

    async def tail_access_log(self, lines: int = 500) -> str:
        return await self._run_output(f"tail -n {int(lines)} {self.access_log_path}", check=False)

    async def _disable_client_locally(self, tg_id: int) -> None:
        async with session_scope() as session:
            row = await session.get(LteVpnClient, tg_id)
            if row is not None:
                row.is_enabled = False
                row.updated_at = datetime.now(timezone.utc)
                await session.commit()

    def _windowed_ip_counts(self, email: str, now: datetime) -> dict[str, int]:
        dq = self._events_by_email[email]
        cutoff = now - timedelta(seconds=max(30, settings.lte_anti_sharing_window_seconds))
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        counts: dict[str, int] = {}
        for _, ip in dq:
            counts[ip] = counts.get(ip, 0) + 1
        return counts

    def _should_flag_antishare(self, tg_id: int, email: str, now: datetime) -> bool:
        mode = (settings.lte_anti_sharing_mode or "off").lower()
        if mode == "off":
            return False
        until = self._anti_share_cooldown_until.get(int(tg_id))
        if until and until > now:
            return False
        counts = self._windowed_ip_counts(email, now)
        if len(counts) < 2:
            return False
        min_per_ip = max(2, int(settings.lte_anti_sharing_min_events_per_ip or 2))
        strong_ips = [ip for ip, c in counts.items() if c >= min_per_ip]
        total = sum(counts.values())
        return len(strong_ips) >= 2 and total >= max(4, int(settings.lte_anti_sharing_min_total_events or 4))

    async def poll_new_connections(self) -> LtePollResult:
        if not settings.lte_enabled or not settings.lte_access_log_poll_enabled:
            return LtePollResult([], [], [])
        raw = await self.tail_access_log()
        now = datetime.now(timezone.utc)
        connected: list[int] = []
        warned: list[int] = []
        strict_disabled: list[int] = []
        async with session_scope() as session:
            rows = (await session.execute(select(LteVpnClient))).scalars().all()
            by_email = {r.email: r for r in rows}
            for line in raw.splitlines():
                if not line.strip() or not self._remember_line(line):
                    continue
                email_match = _EMAIL_RE.search(line)
                if not email_match:
                    continue
                email = email_match.group("email")
                row = by_email.get(email)
                if row is None:
                    continue
                line_ts = self._parse_timestamp(line) or now
                ip = self._extract_public_ip(line)
                recent = row.notified_at and (now - row.notified_at).total_seconds() < 1800
                if not recent:
                    row.notified_at = now
                    row.last_seen_at = now
                    row.updated_at = now
                    connected.append(int(row.tg_id))
                if ip:
                    self._events_by_email[email].append((line_ts, ip))
                    if self._should_flag_antishare(int(row.tg_id), email, now):
                        cooldown = now + timedelta(seconds=max(300, int(settings.lte_anti_sharing_cooldown_seconds or 1800)))
                        self._anti_share_cooldown_until[int(row.tg_id)] = cooldown
                        mode = (settings.lte_anti_sharing_mode or "off").lower()
                        if mode == "strict":
                            strict_disabled.append(int(row.tg_id))
                        elif mode == "warn":
                            warned.append(int(row.tg_id))
            await session.commit()
        # de-dup lists while preserving order
        def _uniq(vals: list[int]) -> list[int]:
            seen: set[int] = set()
            out: list[int] = []
            for v in vals:
                if v in seen:
                    continue
                seen.add(v)
                out.append(v)
            return out
        warned = [x for x in _uniq(warned) if x not in strict_disabled]
        strict_disabled = _uniq(strict_disabled)
        for tg_id in strict_disabled:
            try:
                await self.disable_remote_client(tg_id)
                await self._disable_client_locally(tg_id)
            except Exception:
                log.exception("lte_antishare_disable_failed tg_id=%s", tg_id)
        return LtePollResult(connected_ids=_uniq(connected), warned_ids=warned, strict_disabled_ids=strict_disabled)


lte_vpn_service = LteVpnService()
