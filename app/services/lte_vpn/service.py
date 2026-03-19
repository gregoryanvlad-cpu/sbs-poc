from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import asyncssh
from sqlalchemy import func, select

from app.core.config import settings
from app.db.models import LteVpnClient, Subscription
from app.db.session import session_scope

log = logging.getLogger(__name__)
ENV_PATH = "PATH=/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass
class _InboundRef:
    inbound: dict
    clients: list


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

    async def active_clients_count(self) -> int:
        async with session_scope() as session:
            now = datetime.now(timezone.utc)
            q = (
                select(func.count())
                .select_from(LteVpnClient)
                .join(Subscription, Subscription.tg_id == LteVpnClient.tg_id)
                .where(
                    LteVpnClient.is_enabled == True,
                    Subscription.is_active == True,
                    Subscription.end_at.is_not(None),
                    Subscription.end_at > now,
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
            row.cycle_anchor_end_at = subscription_end_at
            row.updated_at = datetime.now(timezone.utc)
            await session.flush()
            await session.commit()
            return row

    async def has_lte_access(self, tg_id: int, *, subscription_end_at: datetime | None, has_success_payment: bool) -> bool:
        if not subscription_end_at:
            return False
        if not has_success_payment:
            return True
        async with session_scope() as session:
            row = await session.get(LteVpnClient, tg_id)
            if not row or not row.cycle_anchor_end_at:
                return False
            try:
                current = subscription_end_at.astimezone(timezone.utc)
            except Exception:
                current = subscription_end_at
            try:
                anchor = row.cycle_anchor_end_at.astimezone(timezone.utc) if row.cycle_anchor_end_at else None
            except Exception:
                anchor = row.cycle_anchor_end_at
            return bool(anchor and current and anchor >= current)

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

    async def tail_access_log(self, lines: int = 300) -> str:
        return await self._run_output(f"tail -n {int(lines)} {self.access_log_path}", check=False)

    async def poll_new_connections(self) -> list[int]:
        if not settings.lte_enabled or not settings.lte_access_log_poll_enabled:
            return []
        raw = await self.tail_access_log()
        now = datetime.now(timezone.utc)
        connected: list[int] = []
        async with session_scope() as session:
            rows = (await session.execute(select(LteVpnClient))).scalars().all()
            by_email = {r.email: r for r in rows}
            for line in raw.splitlines():
                for email, row in by_email.items():
                    if email not in line:
                        continue
                    recent = row.notified_at and (now - row.notified_at).total_seconds() < 1800
                    if recent:
                        continue
                    row.notified_at = now
                    row.last_seen_at = now
                    row.updated_at = now
                    connected.append(int(row.tg_id))
            await session.commit()
        return connected


lte_vpn_service = LteVpnService()
