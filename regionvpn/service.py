from __future__ import annotations

import base64
import json
import os
import uuid
import re
from dataclasses import dataclass
from typing import Any

from app.services.vpn.ssh_provider import WireGuardSSHProvider


@dataclass(frozen=True)
class RegionVlessParams:
    host: str
    port: int
    sni: str
    pbk: str
    sid: str
    fp: str
    flow: str


class RegionVpnService:
    """
    VLESS+Reality (Xray) "Region" VPN service.

    - Issues a per-user VLESS share-link (unique UUID per tg_id, stored in Xray config as client.email = "tg_<id>")
    - Enforces max slots by counting clients in the VLESS inbound (best-effort).
    - Quota enforcement is best-effort and only checked on issuance (requires Xray API+Stats to be enabled).
    """

    def __init__(
        self,
        *,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        ssh_password: str | None,
        xray_config_path: str = "/usr/local/etc/xray/config.json",
        xray_api_port: int = 10085,
        max_clients: int = 40,
    ) -> None:
        self._ssh = WireGuardSSHProvider(
            host=ssh_host,
            port=ssh_port,
            user=ssh_user,
            password=ssh_password,
            interface=os.environ.get("WG_INTERFACE", "wg0"),
        )
        self.xray_config_path = xray_config_path
        self.xray_api_port = xray_api_port
        self.max_clients = max_clients

        self.params = RegionVlessParams(
            host=os.environ.get("REGION_VLESS_HOST", ssh_host),
            port=int(os.environ.get("REGION_VLESS_PORT", "443")),
            sni=os.environ.get("REGION_VLESS_SNI", "max.ru"),
            pbk=os.environ.get("REGION_VLESS_PBK", ""),
            sid=os.environ.get("REGION_VLESS_SID", ""),
            fp=os.environ.get("REGION_VLESS_FP", "chrome"),
            flow=os.environ.get("REGION_VLESS_FLOW", "xtls-rprx-vision"),
        )

    async def _read_xray_config(self) -> dict[str, Any]:
        raw = await self._ssh._run_output(f"cat {self.xray_config_path}", check=True)
        return json.loads(raw)

    async def _write_xray_config(self, cfg: dict[str, Any]) -> None:
        blob = json.dumps(cfg, ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode("utf-8")
        b64 = base64.b64encode(blob).decode()
        # Write atomically via temp file.
        cmd = (
            "python3 - <<'PY'\n"
            "import base64,os,sys,tempfile\n"
            f"path={self.xray_config_path!r}\n"
            f"data=base64.b64decode({b64!r})\n"
            "d=os.path.dirname(path)\n"
            "fd, tmp = tempfile.mkstemp(prefix='.xray.', dir=d)\n"
            "os.write(fd, data)\n"
            "os.close(fd)\n"
            "os.replace(tmp, path)\n"
            "print('ok')\n"
            "PY"
        )
        await self._ssh._run_output(cmd, check=True)
        # Restart xray to apply new clients (best-effort).
        await self._ssh._run_output("systemctl restart xray", check=False)

    def _find_vless_inbound(self, cfg: dict[str, Any]) -> dict[str, Any] | None:
        inbounds = cfg.get("inbounds") or []
        for ib in inbounds:
            if (ib.get("protocol") or "").lower() != "vless":
                continue
            settings = ib.get("settings") or {}
            if isinstance(settings, dict) and "clients" in settings:
                return ib
        return None

    async def ensure_client(self, tg_id: int) -> str:
        """
        Ensure a VLESS client exists for this user (by email = "tg_<id>").
        Returns share-link vless://...
        """
        cfg = await self._read_xray_config()
        ib = self._find_vless_inbound(cfg)
        if not ib:
            raise RuntimeError("VLESS inbound not found in Xray config")

        settings = ib.setdefault("settings", {})
        clients = settings.setdefault("clients", [])
        if not isinstance(clients, list):
            raise RuntimeError("Xray VLESS clients must be a list")

        email = f"tg_{tg_id}"

        # Existing?
        for c in clients:
            if isinstance(c, dict) and (c.get("email") == email):
                cid = c.get("id")
                if cid:
                    return self.build_share_link(str(cid))

        # Slot limit
        if self.max_clients and len([c for c in clients if isinstance(c, dict)]) >= self.max_clients:
            raise RuntimeError("server_overloaded")

        new_id = str(uuid.uuid4())
        clients.append({"id": new_id, "flow": self.params.flow, "email": email})
        await self._write_xray_config(cfg)
        return self.build_share_link(new_id)

    def build_share_link(self, client_uuid: str) -> str:
        # IMPORTANT: Telegram URL button should be plain vless://...
        p = self.params
        # sid can be empty; include only if set
        sid_part = f"&sid={p.sid}" if p.sid else ""
        pbk_part = f"&pbk={p.pbk}" if p.pbk else ""
        # Keep a stable name for UI
        name = os.environ.get("REGION_VLESS_NAME", "VPN Region")
        return (
            f"vless://{client_uuid}@{p.host}:{p.port}"
            f"?encryption=none&flow={p.flow}&security=reality&sni={p.sni}"
            f"&fp={p.fp}{pbk_part}{sid_part}&type=tcp#{name}"
        )

    async def get_user_traffic_bytes(self, tg_id: int) -> tuple[int, int] | None:
        """
        Best-effort traffic counters from Xray StatsService.
        Returns (uplink_bytes, downlink_bytes) or None if not available.
        """
        email = f"tg_{tg_id}"
        # Standard xray api command (requires API+Stats enabled in config):
        # xray api statsquery --server=127.0.0.1:10085 -pattern "user>>tg_123>>"
        cmd = (
            f"/usr/local/bin/xray api statsquery --server=127.0.0.1:{self.xray_api_port} "
            f"-pattern \"user>>{email}>>\""
        )
        out = await self._ssh._run_output(cmd, check=False)
        if not out:
            return None
        up = down = 0
        # Parse lines like: "user>>tg_123>>traffic>>uplink: 12345"
        for ln in out.splitlines():
            ln = ln.strip()
            m = None
            if "uplink" in ln:
                m = re.search(r":\s*([0-9]+)\s*$", ln)
                if m:
                    up = int(m.group(1))
            elif "downlink" in ln:
                m = re.search(r":\s*([0-9]+)\s*$", ln)
                if m:
                    down = int(m.group(1))
        return up, down
