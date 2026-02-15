from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Tuple

import asyncssh

log = logging.getLogger(__name__)

ENV_PATH = "PATH=/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass
class _InboundRef:
    inbound: dict
    clients: list


class RegionVpnService:
    """Async SSH helper for managing Xray VLESS+Reality clients.

    This service is deliberately defensive: if the remote server/config is not
    reachable or required env vars are missing, the caller should handle the
    raised error and show a friendly message.

    The bot identifies a client by email `tg:<telegram_id>`.
    """

    def __init__(
        self,
        *,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        ssh_password: Optional[str],
        xray_config_path: str = "/usr/local/etc/xray/config.json",
        xray_api_port: int = 10085,
        max_clients: int = 40,
    ):
        self.ssh_host = (ssh_host or "").strip()
        self.ssh_port = int(ssh_port or 22)
        self.ssh_user = (ssh_user or "root").strip()
        self.ssh_password = ssh_password
        self.xray_config_path = (xray_config_path or "/usr/local/etc/xray/config.json").strip()
        self.xray_api_port = int(xray_api_port or 10085)
        self.max_clients = int(max_clients or 40)

        self.connect_timeout = 15
        self.login_timeout = 15
        self.cmd_timeout = 15
        self.retries = 2

        self._key_obj = None
        key_b64 = (os.environ.get("REGION_SSH_PRIVATE_KEY_B64") or "").strip()
        if key_b64:
            try:
                key_text = base64.b64decode(key_b64.encode()).decode()
                self._key_obj = asyncssh.import_private_key(key_text.strip())
                log.info("[regionvpn] SSH key loaded (base64)")
            except Exception:
                log.warning("[regionvpn] Failed to load REGION_SSH_PRIVATE_KEY_B64")

    def _validate(self) -> None:
        if not self.ssh_host:
            raise RuntimeError("region_not_configured")

    async def _connect(self) -> asyncssh.SSHClientConnection:
        self._validate()
        return await asyncssh.connect(
            self.ssh_host,
            port=self.ssh_port,
            username=self.ssh_user,
            password=self.ssh_password if self._key_obj is None else None,
            client_keys=[self._key_obj] if self._key_obj is not None else None,
            known_hosts=None,
            connect_timeout=self.connect_timeout,
            login_timeout=self.login_timeout,
        )

    async def _run_output(self, cmd: str, *, check: bool = True) -> str:
        last: Optional[Exception] = None
        for _ in range(self.retries):
            try:
                async with await self._connect() as conn:
                    full_cmd = f"{ENV_PATH} {cmd}"
                    result = await conn.run(full_cmd, timeout=self.cmd_timeout, check=check)
                    if result.stderr:
                        log.warning("[regionvpn] SSH stderr: %s", (result.stderr or "").strip())
                    return (result.stdout or "").strip()
            except Exception as e:
                last = e
                await asyncio.sleep(0.5)
        raise last  # type: ignore[misc]

    async def _run(self, cmd: str) -> None:
        await self._run_output(cmd, check=True)

    async def _read_xray_config(self) -> dict:
        out = await self._run_output(f"cat {self.xray_config_path}")
        if not out:
            raise RuntimeError("xray_config_empty")
        try:
            return json.loads(out)
        except Exception as e:
            raise RuntimeError("xray_config_invalid") from e

    async def _write_xray_config(self, cfg: dict) -> None:
        text = json.dumps(cfg, ensure_ascii=False, indent=2)
        marker = "__XRAYCFG__"
        tmp_path = "/tmp/xray_config_new.json"
        # Write file, then atomically install over config path.
        cmd = (
            f"cat > {tmp_path} <<'{marker}'\n"
            f"{text}\n"
            f"{marker}\n"
            f"sudo install -m 644 {tmp_path} {self.xray_config_path}"
        )
        await self._run(cmd)

    def _find_vless_inbound(self, cfg: dict) -> _InboundRef:
        inbounds = cfg.get("inbounds") or []
        if not isinstance(inbounds, list):
            raise RuntimeError("xray_no_inbounds")
        for ib in inbounds:
            if not isinstance(ib, dict):
                continue
            if (ib.get("protocol") or "").lower() != "vless":
                continue
            settings = ib.get("settings")
            if not isinstance(settings, dict):
                continue
            clients = settings.get("clients")
            if clients is None:
                # If settings exist but clients missing - create.
                settings["clients"] = []
                clients = settings["clients"]
            if isinstance(clients, list):
                return _InboundRef(inbound=ib, clients=clients)
        raise RuntimeError("xray_vless_inbound_not_found")

    def build_vless_url(self, client_uuid: str) -> str:
        host = (os.environ.get("REGION_VLESS_HOST") or self.ssh_host).strip()
        port = (os.environ.get("REGION_VLESS_PORT") or "443").strip()
        sni = (os.environ.get("REGION_VLESS_SNI") or "max.ru").strip()
        fp = (os.environ.get("REGION_VLESS_FP") or "chrome").strip()
        pbk = (os.environ.get("REGION_VLESS_PBK") or "").strip()
        sid = (os.environ.get("REGION_VLESS_SID") or "").strip()
        flow = (os.environ.get("REGION_VLESS_FLOW") or "xtls-rprx-vision").strip()
        name = (os.environ.get("REGION_VLESS_NAME") or "VPN Region").strip()

        # Minimal URL encoding for fragment.
        frag = name.replace(" ", "%20")

        params = {
            "encryption": "none",
            "flow": flow,
            "security": "reality",
            "sni": sni,
            "fp": fp,
            "type": "tcp",
        }
        if pbk:
            params["pbk"] = pbk
        if sid:
            params["sid"] = sid

        query = "&".join([f"{k}={v}" for k, v in params.items() if v is not None and v != ""])  # noqa: E501
        return f"vless://{client_uuid}@{host}:{port}?{query}#{frag}"

    async def active_clients_count(self) -> int:
        cfg = await self._read_xray_config()
        ref = self._find_vless_inbound(cfg)
        return len(ref.clients)

    async def ensure_client(self, tg_id: int) -> str:
        """Ensure a client exists in Xray and return vless:// share url.

        Raises RuntimeError("server_overloaded") when client limit reached.
        """

        email_variants = {f"tg:{tg_id}", str(tg_id)}

        cfg = await self._read_xray_config()
        ref = self._find_vless_inbound(cfg)

        existing: Optional[dict] = None
        for c in ref.clients:
            if not isinstance(c, dict):
                continue
            if str(c.get("email") or "") in email_variants:
                existing = c
                break

        if existing is None:
            if len(ref.clients) >= self.max_clients:
                raise RuntimeError("server_overloaded")

            new_uuid = str(uuid.uuid4())
            flow = (os.environ.get("REGION_VLESS_FLOW") or "xtls-rprx-vision").strip()

            client = {"id": new_uuid, "email": f"tg:{tg_id}"}
            if flow:
                client["flow"] = flow

            ref.clients.append(client)
            await self._write_xray_config(cfg)

            # Restart xray to apply changes (simple & reliable).
            # If you later enable xray api for dynamic updates, we can switch.
            await self._run("sudo systemctl restart xray")

            return self.build_vless_url(new_uuid)

        client_uuid = str(existing.get("id") or "").strip()
        if not client_uuid:
            # Bad config entry; repair.
            client_uuid = str(uuid.uuid4())
            existing["id"] = client_uuid
            await self._write_xray_config(cfg)
            await self._run("sudo systemctl restart xray")

        return self.build_vless_url(client_uuid)

    async def revoke_client(self, tg_id: int) -> bool:
        """Remove a client from Xray config by tg_id.

        The bot identifies a client by email `tg:<telegram_id>`.
        Returns True if a client was removed, False if nothing matched.
        """

        email_variants = {f"tg:{tg_id}", str(tg_id)}

        cfg = await self._read_xray_config()
        ref = self._find_vless_inbound(cfg)

        before = len(ref.clients)
        ref.clients[:] = [
            c
            for c in ref.clients
            if not (isinstance(c, dict) and str(c.get("email") or "") in email_variants)
        ]
        removed = len(ref.clients) != before

        if removed:
            await self._write_xray_config(cfg)
            await self._run("sudo systemctl restart xray")

        return removed

    async def list_clients(self) -> list[dict]:
        """Return a best-effort list of configured VLESS clients.

        NOTE: This is not "active" sessions, just provisioned clients.
        """
        cfg = await self._read_xray_config()
        ref = self._find_vless_inbound(cfg)
        out: list[dict] = []
        for c in ref.clients:
            if isinstance(c, dict):
                out.append({
                    "id": str(c.get("id") or ""),
                    "email": str(c.get("email") or ""),
                    "flow": str(c.get("flow") or ""),
                })
        return out

    async def get_user_traffic_bytes(self, tg_id: int) -> Optional[Tuple[int, int]]:
        """Best-effort traffic stats (up, down).

        This requires Xray API/stats to be enabled. If not available, returns None.
        We keep it optional to avoid breaking the bot.
        """
        # Not implemented in this minimal module.
        return None
