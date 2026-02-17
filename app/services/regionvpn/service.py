from __future__ import annotations

import asyncio
import base64
import hashlib
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

        # tc/ifb rate limit parameters (optional)
        # IMPORTANT:
        # The project-level Settings default REGION_TC_ENABLED to True.
        # Earlier versions of this service treated a missing env as "disabled",
        # which caused "tc" limits to never apply unless the env var was explicitly
        # set. Here we make the default consistent with Settings:
        # - REGION_TC_ENABLED is missing/empty  -> enabled
        # - REGION_TC_ENABLED is set to falsy   -> disabled
        # - REGION_TC_ENABLED is set to truthy  -> enabled
        _tc_env = os.getenv("REGION_TC_ENABLED")
        if _tc_env is None or not _tc_env.strip():
            self._tc_enabled = True
        else:
            self._tc_enabled = _tc_env.strip().lower() in ("1", "true", "yes", "y", "on")
        try:
            self._tc_rate_mbit = int(os.getenv("REGION_TC_RATE_MBIT", "25").strip() or "25")
        except Exception:
            self._tc_rate_mbit = 25
        self._tc_dev = (os.getenv("REGION_TC_DEV", "eth0") or "eth0").strip() or "eth0"

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

    async def _restart_xray(self) -> None:
        # Use systemd restart; keep it centralized so other methods can call it.
        await self._run("sudo systemctl restart xray")

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

    @staticmethod
    def _client_exists(clients: list[dict], *, email: str) -> bool:
        for c in clients:
            if isinstance(c, dict) and c.get("email") == email:
                return True
        return False

    def build_vless_url(self, client_uuid: str) -> str:
        host = (os.environ.get("REGION_VLESS_HOST") or self.ssh_host).strip()
        port = (os.environ.get("REGION_VLESS_PORT") or "443").strip()
        sni = (os.environ.get("REGION_VLESS_SNI") or "max.ru").strip()
        fp = (os.environ.get("REGION_VLESS_FP") or "chrome").strip()
        pbk = (os.environ.get("REGION_VLESS_PBK") or "").strip()
        sid = (os.environ.get("REGION_VLESS_SID") or "").strip()
        flow = (os.environ.get("REGION_VLESS_FLOW") or "xtls-rprx-vision").strip()
        name = (os.environ.get("REGION_VLESS_NAME") or "VPN Region").strip()

        # Optional anti-DPI extras (Reality PQ signature + path obfuscation)
        mldsa65_verify = (
            os.environ.get("REALITY_MLDSA65_VERIFY")
            or os.environ.get("MLDSA65_VERIFY")
            or os.environ.get("REGION_MLDSA65_VERIFY")
            or ""
        ).strip()
        spider_x_base = (
            os.environ.get("SPIDER_X")
            or os.environ.get("SPIDERX")
            or os.environ.get("REALITY_SPIDER_X")
            or ""
        ).strip()

        # Minimal URL encoding for fragment.
        frag = self._url_escape(name)

        # Stable per-user "random" (so the link doesn't change every time you request it)
        rand5 = hashlib.sha256(client_uuid.encode("utf-8")).hexdigest()[:5]
        spider_x = ""
        if spider_x_base:
            if "{rand}" in spider_x_base:
                spider_x = spider_x_base.replace("{rand}", rand5)
            elif spider_x_base.endswith("="):
                spider_x = spider_x_base + rand5
            else:
                spider_x = spider_x_base

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
        if mldsa65_verify:
            # IMPORTANT: exact key name expected by clients.
            params["mldsa65Verify"] = mldsa65_verify
        if spider_x:
            params["spiderX"] = spider_x

        query = "&".join(
            [
                f"{k}={self._url_escape(v)}"
                for k, v in params.items()
                if v is not None and v != ""
            ]
        )
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

        before_clients = len(ref.clients)
        ref.clients[:] = [
            c
            for c in ref.clients
            if not (isinstance(c, dict) and str(c.get("email") or "") in email_variants)
        ]
        removed = len(ref.clients) != before_clients

        # Also remove any routing policy rules for this user (single-device enforcement).
        rules_removed = False
        try:
            inbound_tag = self._get_region_inbound_tag(ref.inbound)
            rules = self._ensure_routing_rules_list(cfg)
            before_rules = len(rules)
            for email in email_variants:
                self._remove_regionvpn_rules_for_user(rules, inbound_tag=inbound_tag, email=email)
            rules_removed = len(rules) != before_rules
        except Exception:
            pass

        if removed or rules_removed:
            await self._write_xray_config(cfg)
            await self._restart_xray()

        # Best-effort: clear traffic shaping for this tg_id.
        try:
            await self.tc_clear_limit_for_ip(tg_id=tg_id)
        except Exception:
            pass

        return removed


    async def set_client_enabled(self, tg_id: int, enabled: bool) -> bool:
        """Enable/disable a client **without** changing its UUID.

        We do this by editing Xray routing rules for the user (email `tg:<id>`):
        - enabled=True  -> remove any "blocked" rule for the user (no traffic rules will
          be enforced here; the session guard may later add allow/deny rules based on IP)
        - enabled=False -> remove user-specific allow/deny rules and add a rule that
          sends everything to the `blocked` outbound (blackhole).

        Returns True if config changed.
        """

        email = f"tg:{tg_id}"
        cfg = await self._get_config()

        inbound_tag = self._get_region_inbound_tag(cfg)
        inbound = self._get_inbound_by_tag(cfg, inbound_tag)
        if not inbound:
            return False

        # If client doesn't exist - nothing to toggle.
        if not self._client_exists(inbound, email=email):
            return False

        changed = False
        routing = cfg.setdefault("routing", {})
        rules: list[dict] = routing.setdefault("rules", [])

        # remove any previous rules for this user (allow/deny/block)
        before = len(rules)
        rules[:] = [
            r
            for r in rules
            if not (
                r.get("type") == "field"
                and email in (r.get("user") or [])
                and inbound_tag in (r.get("inboundTag") or [])
                and r.get("outboundTag") in {"regionvpn", "blocked"}
            )
        ]
        if len(rules) != before:
            changed = True

        # ensure base outbounds exist for block rule
        self._ensure_regionvpn_outbounds(cfg)

        if not enabled:
            block_rule = {
                "type": "field",
                "inboundTag": [inbound_tag],
                "user": [email],
                "outboundTag": "blocked",
            }
            # put at the top to take precedence
            rules.insert(0, block_rule)
            changed = True

        if changed:
            await self._write_config(cfg)
            await self._restart_xray()

        return changed


    async def disable_client(self, tg_id: int) -> bool:
        return await self.set_client_enabled(tg_id=tg_id, enabled=False)


    async def enable_client(self, tg_id: int) -> bool:
        return await self.set_client_enabled(tg_id=tg_id, enabled=True)


    async def apply_enabled_map(self, enabled_map: dict[int, bool]) -> bool:
        """Batch enable/disable changes in a single Xray restart."""

        if not enabled_map:
            return False

        cfg = await self._get_config()
        inbound_tag = self._get_region_inbound_tag(cfg)
        inbound = self._get_inbound_by_tag(cfg, inbound_tag)
        if not inbound:
            return False

        self._ensure_regionvpn_outbounds(cfg)

        routing = cfg.setdefault("routing", {})
        rules: list[dict] = routing.setdefault("rules", [])

        changed = False
        for tg_id, enabled in enabled_map.items():
            email = f"tg:{tg_id}"
            if not self._client_exists(inbound, email=email):
                continue

            before = len(rules)
            rules[:] = [
                r
                for r in rules
                if not (
                    r.get("type") == "field"
                    and email in (r.get("user") or [])
                    and inbound_tag in (r.get("inboundTag") or [])
                    and r.get("outboundTag") in {"regionvpn", "blocked"}
                )
            ]
            if len(rules) != before:
                changed = True

            if not enabled:
                rules.insert(
                    0,
                    {
                        "type": "field",
                        "inboundTag": [inbound_tag],
                        "user": [email],
                        "outboundTag": "blocked",
                    },
                )
                changed = True

        if changed:
            await self._write_config(cfg)
            await self._restart_xray()

        return changed


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

    async def tail_access_log(self, *, path: str, lines: int = 200) -> list[str]:
        """Read last N lines from Xray access log via SSH."""
        cmd = f"sudo -n tail -n {int(lines)} {path} || tail -n {int(lines)} {path}"
        try:
            text = (await self._run_output(cmd, check=False) or "").strip()
        except Exception:
            return []
        if not text:
            return []
        return text.splitlines()

    # ------------------------------
    # Single-device enforcement
    # ------------------------------

    def _ensure_region_inbound_tag(self, inbound: dict) -> None:
        # Use a stable inboundTag so routing rules can target only VPN-Region traffic.
        if not inbound.get("tag"):
            inbound["tag"] = "regionvpn"
        elif inbound.get("tag") != "regionvpn":
            # Do NOT override a custom tag set by user; instead keep it and use it.
            # But we still need a tag for our rules: store it for later.
            pass

    def _get_region_inbound_tag(self, inbound: dict) -> str:
        tag = (inbound.get("tag") or "").strip()
        return tag or "regionvpn"

    def _ensure_outbound_tag(self, cfg: dict, *, tag: str, protocol: str) -> None:
        outbounds = cfg.get("outbounds")
        if not isinstance(outbounds, list):
            outbounds = []
            cfg["outbounds"] = outbounds
        for ob in outbounds:
            if isinstance(ob, dict) and ob.get("tag") == tag:
                return
        outbounds.append({"protocol": protocol, "tag": tag})

    def _ensure_routing_rules_list(self, cfg: dict) -> list[dict]:
        routing = cfg.get("routing")
        if not isinstance(routing, dict):
            routing = {}
            cfg["routing"] = routing
        rules = routing.get("rules")
        if not isinstance(rules, list):
            rules = []
            routing["rules"] = rules
        return rules  # type: ignore[return-value]

    def _email_for_tg(self, tg_id: int) -> str:
        return f"tg:{int(tg_id)}"

    def _is_regionvpn_rule_for_user(self, rule: dict, *, inbound_tag: str, email: str) -> bool:
        if not isinstance(rule, dict):
            return False
        if rule.get("type") != "field":
            return False
        it = rule.get("inboundTag")
        if isinstance(it, list) and inbound_tag in it:
            pass
        else:
            return False
        user = rule.get("user")
        if not (isinstance(user, list) and email in user):
            return False
        # Only touch rules that route to direct/blocked and optionally have source.
        ot = rule.get("outboundTag")
        if ot not in ("direct", "blocked"):
            return False
        return True

    def _remove_regionvpn_rules_for_user(self, rules: list[dict], *, inbound_tag: str, email: str) -> None:
        kept: list[dict] = []
        for r in rules:
            if self._is_regionvpn_rule_for_user(r, inbound_tag=inbound_tag, email=email):
                continue
            kept.append(r)
        rules[:] = kept

    def _upsert_regionvpn_rules_for_user(
        self,
        rules: list[dict],
        *,
        inbound_tag: str,
        email: str,
        active_ip: str | None,
    ) -> None:
        """Upsert 2 routing rules:

        1) Allow traffic for (user=email AND source=active_ip) -> direct
        2) Everything else for that user -> blocked (blackhole)

        IMPORTANT: We intentionally do NOT block connection handshake itself. The client can connect,
        but if it's not the active IP, all its outbound traffic is blackholed. This allows us to
        detect a new device via access.log and then switch "active_ip" quickly.
        """
        self._remove_regionvpn_rules_for_user(rules, inbound_tag=inbound_tag, email=email)

        if active_ip:
            allow_rule = {
                "type": "field",
                "inboundTag": [inbound_tag],
                "user": [email],
                "source": [f"{active_ip}/32"],
                "outboundTag": "direct",
            }
            rules.insert(0, allow_rule)

            deny_rule = {
                "type": "field",
                "inboundTag": [inbound_tag],
                "user": [email],
                "outboundTag": "blocked",
            }
            rules.insert(1, deny_rule)
        else:
            # No active IP yet -> do not restrict (first connection will be discovered in logs).
            return

    async def apply_active_ip_map(self, active_ip_by_tg: dict[int, str | None]) -> None:
        """Apply/refresh routing rules for multiple users in a single config update + restart."""
        if not active_ip_by_tg:
            return

        cfg = await self._read_xray_config()
        ref = self._find_vless_inbound(cfg)
        self._ensure_region_inbound_tag(ref.inbound)
        inbound_tag = self._get_region_inbound_tag(ref.inbound)

        # Make sure required outbounds exist
        self._ensure_outbound_tag(cfg, tag="direct", protocol="freedom")
        self._ensure_outbound_tag(cfg, tag="blocked", protocol="blackhole")

        rules = self._ensure_routing_rules_list(cfg)

        for tg_id, ip in active_ip_by_tg.items():
            email = self._email_for_tg(int(tg_id))
            self._upsert_regionvpn_rules_for_user(rules, inbound_tag=inbound_tag, email=email, active_ip=ip)

        await self._write_xray_config(cfg)
        await self._restart_xray()

    # ------------------------------
    # tc/ifb per-IP rate limits (optional)
    # ------------------------------

    def _tc_class_for_tg(self, tg_id: int) -> int:
        """Stable tc class (minor id) for a Telegram user id.

        We keep it within 10..65000 to be safe.
        """
        base = 10
        span = 65000 - base
        return base + (int(tg_id) % span)

    async def _tc_init(self) -> None:
        """Ensure base tc qdiscs exist (eth0 + ifb0 ingress redirect)."""
        dev = self._tc_dev
        # Create ifb0 and redirect ingress -> ifb0
        cmd = (
            "sudo modprobe ifb || true; "
            "sudo ip link add ifb0 type ifb 2>/dev/null || true; "
            "sudo ip link set dev ifb0 up; "
            f"sudo tc qdisc add dev {dev} handle ffff: ingress 2>/dev/null || true; "
            f"sudo tc filter add dev {dev} parent ffff: protocol ip u32 match u32 0 0 "
            "action mirred egress redirect dev ifb0 2>/dev/null || true; "
            # Root HTB on egress
            f"sudo tc qdisc add dev {dev} root handle 1: htb default 999 r2q 10 2>/dev/null || true; "
            f"sudo tc class add dev {dev} parent 1: classid 1:1 htb rate 1gbit ceil 1gbit 2>/dev/null || true; "
            # Root HTB on ifb0 (ingress shaped)
            "sudo tc qdisc add dev ifb0 root handle 2: htb default 999 r2q 10 2>/dev/null || true; "
            "sudo tc class add dev ifb0 parent 2: classid 2:1 htb rate 1gbit ceil 1gbit 2>/dev/null || true"
        )
        await self._run(cmd)

    async def tc_apply_limit_for_ip(self, *, tg_id: int, ip: str) -> None:
        """(Best-effort) apply 25mbit per-IP limit for both directions."""
        if not self._tc_enabled:
            return
        ip = (ip or "").strip()
        if not ip:
            return
        await self._tc_init()
        dev = self._tc_dev
        rate = max(1, int(self._tc_rate_mbit))
        cls = self._tc_class_for_tg(int(tg_id))

        # Delete any previous rules for this class, then add fresh rules for this IP.
        # We delete by classid first (ignore errors) and then add filters.
        cmd = (
            f"sudo tc filter del dev {dev} parent 1: protocol ip prio {cls} 2>/dev/null || true; "
            f"sudo tc filter del dev ifb0 parent 2: protocol ip prio {cls} 2>/dev/null || true; "
            f"sudo tc class del dev {dev} classid 1:{cls} 2>/dev/null || true; "
            f"sudo tc class del dev ifb0 classid 2:{cls} 2>/dev/null || true; "
            f"sudo tc class add dev {dev} parent 1:1 classid 1:{cls} htb rate {rate}mbit ceil {rate}mbit 2>/dev/null || true; "
            f"sudo tc filter add dev {dev} protocol ip parent 1: prio {cls} u32 match ip dst {ip}/32 flowid 1:{cls} 2>/dev/null || true; "
            f"sudo tc class add dev ifb0 parent 2:1 classid 2:{cls} htb rate {rate}mbit ceil {rate}mbit 2>/dev/null || true; "
            f"sudo tc filter add dev ifb0 protocol ip parent 2: prio {cls} u32 match ip src {ip}/32 flowid 2:{cls} 2>/dev/null || true"
        )
        await self._run(cmd)

    async def tc_clear_limit_for_ip(self, *, tg_id: int, ip: str | None = None) -> None:
        """Remove tc class for the tg_id (and any best-effort filters)."""
        if not self._tc_enabled:
            return
        dev = self._tc_dev
        cls = self._tc_class_for_tg(int(tg_id))
        cmd = (
            f"sudo tc filter del dev {dev} parent 1: protocol ip prio {cls} 2>/dev/null || true; "
            f"sudo tc filter del dev ifb0 parent 2: protocol ip prio {cls} 2>/dev/null || true; "
            f"sudo tc class del dev {dev} classid 1:{cls} 2>/dev/null || true; "
            f"sudo tc class del dev ifb0 classid 2:{cls} 2>/dev/null || true"
        )
        await self._run(cmd)

    async def clear_user_policy(self, tg_id: int) -> None:
        """Remove routing rules for the given user (no restriction)."""
        cfg = await self._read_xray_config()
        ref = self._find_vless_inbound(cfg)
        self._ensure_region_inbound_tag(ref.inbound)
        inbound_tag = self._get_region_inbound_tag(ref.inbound)
        rules = self._ensure_routing_rules_list(cfg)
        email = self._email_for_tg(int(tg_id))
        self._remove_regionvpn_rules_for_user(rules, inbound_tag=inbound_tag, email=email)
        await self._write_xray_config(cfg)
        await self._restart_xray()

    async def get_user_traffic_bytes(self, tg_id: int) -> Optional[Tuple[int, int]]:
        """Best-effort traffic stats (up, down).

        This requires Xray API/stats to be enabled. If not available, returns None.
        We keep it optional to avoid breaking the bot.
        """
        # Not implemented in this minimal module.
        return None
