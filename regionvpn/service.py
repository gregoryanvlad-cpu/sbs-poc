from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

import asyncssh

log = logging.getLogger(__name__)


ENV_PATH = "PATH=/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass
class _ClientInfo:
    user_key: str
    client_id: str


class RegionVpnService:
    """Manage VPN-Region clients in Xray config (VLESS + Reality).

    This is a minimal implementation to keep the existing project logic intact:
    - Ensures a per-user client exists in `/usr/local/etc/xray/config.json`.
    - Enforces a max clients limit ("slots").
    - Returns a `vless://` share link suitable for Happ.

    Advanced features (traffic stats, multi-device kick) are implemented later
    and intentionally not required for boot stability.
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
    ) -> None:
        self.ssh_host = ssh_host
        self.ssh_port = int(ssh_port or 22)
        self.ssh_user = ssh_user or "root"
        self.ssh_password = ssh_password
        self.xray_config_path = xray_config_path
        self.xray_api_port = int(xray_api_port or 10085)
        self.max_clients = int(max_clients or 40)

        self._connect_timeout = 15
        self._login_timeout = 15
        self._cmd_timeout = 20
        self._retries = 2

        self._key_obj = None
        key_b64 = os.environ.get("REGION_SSH_PRIVATE_KEY_B64")
        if key_b64:
            try:
                key_text = base64.b64decode(key_b64.encode()).decode()
                self._key_obj = asyncssh.import_private_key(key_text.strip())
                log.info("Region SSH key loaded (base64)")
            except Exception:
                log.exception("Failed to load REGION_SSH_PRIVATE_KEY_B64")

    # ------------------------
    # SSH helpers
    # ------------------------
    async def _connect(self) -> asyncssh.SSHClientConnection:
        return await asyncssh.connect(
            self.ssh_host,
            port=self.ssh_port,
            username=self.ssh_user,
            password=self.ssh_password,
            client_keys=[self._key_obj] if self._key_obj else None,
            known_hosts=None,
            connect_timeout=self._connect_timeout,
            login_timeout=self._login_timeout,
        )

    async def _run_output(self, cmd: str, *, check: bool = True) -> str:
        last = None
        for _ in range(self._retries):
            try:
                async with await self._connect() as conn:
                    full_cmd = f"{ENV_PATH} {cmd}"
                    result = await conn.run(full_cmd, timeout=self._cmd_timeout, check=check)
                    if result.stderr:
                        log.warning("Region SSH stderr: %s", result.stderr.strip())
                    return (result.stdout or "").strip()
            except Exception as e:
                last = e
                await asyncio.sleep(0.5)
        raise last

    async def _run(self, cmd: str, *, check: bool = True) -> None:
        await self._run_output(cmd, check=check)

    # ------------------------
    # Public API
    # ------------------------
    async def ensure_client(self, tg_id: int) -> str:
        """Ensure the client exists on Xray and return the share link."""

        if not self.ssh_host:
            # Region not configured yet; callers should display "Подключается..."
            raise RuntimeError("server_overloaded")

        email = self._user_email(tg_id)

        cfg = await self._read_xray_config()
        inbound, clients = self._find_vless_inbound(cfg)
        if inbound is None or clients is None:
            log.error("VLESS inbound not found in Xray config")
            raise RuntimeError("server_overloaded")

        existing = None
        for c in clients:
            if str(c.get("email") or "").strip() == email:
                existing = c
                break

        if existing is None:
            if len(clients) >= self.max_clients:
                raise RuntimeError("server_overloaded")

            new_id = str(uuid.uuid4())
            client_obj = {"id": new_id, "email": email}
            flow = (os.environ.get("REGION_VLESS_FLOW") or "").strip()
            if flow:
                client_obj["flow"] = flow
            clients.append(client_obj)

            # Write back config and reload Xray.
            await self._write_xray_config(cfg)
            await self._reload_xray()
            client_id = new_id
        else:
            client_id = str(existing.get("id") or existing.get("uuid") or "").strip()
            if not client_id:
                # corrupted entry; regenerate id
                client_id = str(uuid.uuid4())
                existing["id"] = client_id
                await self._write_xray_config(cfg)
                await self._reload_xray()

        return self._build_vless_url(client_id)

    async def get_user_traffic_bytes(self, tg_id: int) -> Optional[Tuple[int, int]]:
        """Best-effort traffic stats (up, down) in bytes.

        In the current project this is optional. If stats aren't enabled on the
        server, we return None and issuance proceeds.
        """

        # Not implemented yet (requires Xray API/StatsService + auth + parsing).
        return None

    # ------------------------
    # Internal helpers
    # ------------------------
    @staticmethod
    def _user_email(tg_id: int) -> str:
        return f"tg_{int(tg_id)}"

    async def _read_xray_config(self) -> dict:
        out = await self._run_output(f"cat {self.xray_config_path}")
        return json.loads(out) if out else {}

    async def _write_xray_config(self, cfg: dict) -> None:
        data = json.dumps(cfg, ensure_ascii=False, indent=2)
        b64 = base64.b64encode(data.encode()).decode()
        cmd = (
            f"python3 -c \"import base64; p='{self.xray_config_path}'; "
            f"open(p,'wb').write(base64.b64decode('{b64}'))\""
        )
        await self._run(cmd)

    def _find_vless_inbound(self, cfg: dict) -> tuple[Optional[dict], Optional[list]]:
        """Return (inbound_dict, clients_list) for the first vless inbound."""
        inbounds = cfg.get("inbounds")
        if not isinstance(inbounds, list):
            return None, None
        for inbound in inbounds:
            if not isinstance(inbound, dict):
                continue
            if str(inbound.get("protocol") or "").lower() != "vless":
                continue
            settings = inbound.get("settings")
            if not isinstance(settings, dict):
                continue
            clients = settings.get("clients")
            if isinstance(clients, list):
                return inbound, clients
        return None, None

    async def _reload_xray(self) -> None:
        # Prefer reload; fallback to restart.
        await self._run("systemctl reload xray", check=False)
        # Some unit files don't support reload. Restart if not active.
        out = await self._run_output("systemctl is-active xray", check=False)
        if out.strip() != "active":
            await self._run("systemctl restart xray", check=False)

    def _build_vless_url(self, client_uuid: str) -> str:
        host = (os.environ.get("REGION_VLESS_HOST") or "").strip() or self.ssh_host
        port = (os.environ.get("REGION_VLESS_PORT") or "443").strip()
        sni = (os.environ.get("REGION_VLESS_SNI") or "").strip()
        fp = (os.environ.get("REGION_VLESS_FP") or "chrome").strip()
        pbk = (os.environ.get("REGION_VLESS_PBK") or "").strip()
        sid = (os.environ.get("REGION_VLESS_SID") or "").strip()
        flow = (os.environ.get("REGION_VLESS_FLOW") or "").strip()
        name = (os.environ.get("REGION_VLESS_NAME") or "VPN Region").strip()

        # Build query string.
        q = {
            "encryption": "none",
            "security": "reality",
            "type": "tcp",
        }
        if flow:
            q["flow"] = flow
        if sni:
            q["sni"] = sni
        if fp:
            q["fp"] = fp
        if pbk:
            q["pbk"] = pbk
        if sid:
            q["sid"] = sid

        # Stable order for readability.
        parts = [f"{k}={self._url_escape(v)}" for k, v in q.items() if v is not None and v != ""]
        query = "&".join(parts)
        # Fragment (name) should be URL-encoded lightly (spaces -> %20).
        frag = self._url_escape(name)

        return f"vless://{client_uuid}@{host}:{port}?{query}#{frag}"

    @staticmethod
    def _url_escape(s: str) -> str:
        # A tiny safe encoder (avoids adding new deps). Enough for our params.
        return (
            str(s)
            .replace("%", "%25")
            .replace(" ", "%20")
            .replace("#", "%23")
            .replace("?", "%3F")
            .replace("&", "%26")
        )
