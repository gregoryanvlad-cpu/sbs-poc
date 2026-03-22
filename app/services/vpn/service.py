from __future__ import annotations

import base64
import ipaddress
import logging
import os
import json
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from sqlalchemy import select, text, func, literal
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.vpn_peer import VpnPeer
from app.repo import utcnow
from app.services.vpn import crypto
from app.services.vpn.ssh_provider import WireGuardSSHProvider

log = logging.getLogger(__name__)

VPN_NET = ipaddress.ip_network(os.environ.get("VPN_CLIENT_NET", "10.66.0.0/16"))


def _b64encode_raw(b: bytes) -> str:
    return base64.b64encode(b).decode("utf-8")


def gen_keys() -> tuple[str, str]:
    """Generate WireGuard keypair (X25519) in base64 Raw format."""
    priv = x25519.X25519PrivateKey.generate()
    pub = priv.public_key()

    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64encode_raw(priv_bytes), _b64encode_raw(pub_bytes)


class VPNService:
    def __init__(self) -> None:
        # IMPORTANT: password is OPTIONAL
        pwd = os.environ.get("WG_SSH_PASSWORD")
        if pwd is not None and pwd.strip() == "":
            pwd = None

        self.provider = WireGuardSSHProvider(
            host=os.environ["WG_SSH_HOST"],
            port=int(os.environ.get("WG_SSH_PORT", "22")),
            user=os.environ["WG_SSH_USER"],
            password=pwd,
            interface=os.environ.get("VPN_INTERFACE", "wg0"),
            tc_dev=os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV"),
            tc_parent_rate_mbit=int((os.environ.get("WG_TC_PARENT_RATE_MBIT") or os.environ.get("VPN_TC_PARENT_RATE_MBIT") or "1000").strip() or "1000"),
        )

        self.server_pub = os.environ["VPN_SERVER_PUBLIC_KEY"]
        self.endpoint = os.environ["VPN_ENDPOINT"]
        self.dns = os.environ.get("VPN_DNS", "1.1.1.1")

        # Cache of providers for multi-location servers.
        # Keyed by (host, port, user, interface, tc_dev, tc_parent_rate_mbit).
        self._providers: dict[Tuple[str, int, str, str, str, int], WireGuardSSHProvider] = {}

    def _provider_for(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str | None,
        interface: str,
        tc_dev: str | None = None,
        tc_parent_rate_mbit: int | None = None,
    ) -> WireGuardSSHProvider:
        """Get a cached SSH provider for a given server."""
        tc_dev_norm = (tc_dev or "").strip()
        parent_rate = int(tc_parent_rate_mbit or 1000)
        key = (host, int(port), user, interface, tc_dev_norm, parent_rate)
        p = self._providers.get(key)
        if p is None:
            p = WireGuardSSHProvider(host=host, port=int(port), user=user, password=password, interface=interface, tc_dev=tc_dev_norm or None, tc_parent_rate_mbit=parent_rate)
            self._providers[key] = p
        return p

    def _alloc_ip(self, tg_id: int) -> str:
        # Stable IP per tg_id until rotate (manual reset).
        host = (tg_id % 65000) + 2
        return str(VPN_NET.network_address + host)

    async def _alloc_ip_unique(self, session: AsyncSession, *, tg_id: int) -> str:
        """Allocate a unique client IP inside VPN_NET.

        Base IP stays stable for the first peer (same as _alloc_ip). For
        additional peers (admin-only feature), we probe for a free IP derived
        from tg_id and an incrementing offset, ensuring global uniqueness.
        """

        base = ipaddress.ip_address(self._alloc_ip(tg_id))

        # Fast path: if base IP isn't used, keep it.
        exists = await session.execute(select(VpnPeer.id).where(VpnPeer.client_ip == str(base)).limit(1))
        if exists.scalar_one_or_none() is None:
            return str(base)

        # Probe deterministic offsets until we find a free IP.
        # We intentionally avoid the first few addresses and stay within the /16.
        for off in range(1, 5000):
            cand_int = int(base) + off
            cand = ipaddress.ip_address(cand_int)
            if cand not in VPN_NET:
                break
            exists = await session.execute(select(VpnPeer.id).where(VpnPeer.client_ip == str(cand)).limit(1))
            if exists.scalar_one_or_none() is None:
                return str(cand)

        # Last resort: scan forward from network base.
        start = int(VPN_NET.network_address) + 100
        end = int(VPN_NET.broadcast_address) - 1
        for cand_int in range(start, min(end, start + 20000)):
            cand = ipaddress.ip_address(cand_int)
            exists = await session.execute(select(VpnPeer.id).where(VpnPeer.client_ip == str(cand)).limit(1))
            if exists.scalar_one_or_none() is None:
                return str(cand)

        raise RuntimeError("No free VPN client IPs available")

    def _load_vpn_servers(self) -> list[dict]:
        raw = (os.environ.get("VPN_SERVERS_JSON") or os.environ.get("VPN_SERVERS") or "").strip()
        out: list[dict] = []
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and "servers" in data:
                    data = data["servers"]
                if isinstance(data, list):
                    out = [x for x in data if isinstance(x, dict)]
            except Exception:
                out = []
        if out:
            return out
        code = (os.environ.get("VPN_CODE") or "NL").upper()
        return [{
            "code": code,
            "name": os.environ.get("VPN_NAME") or code,
            "host": os.environ.get("WG_SSH_HOST"),
            "port": int(os.environ.get("WG_SSH_PORT", "22") or 22),
            "user": os.environ.get("WG_SSH_USER"),
            "password": os.environ.get("WG_SSH_PASSWORD"),
            "interface": os.environ.get("VPN_INTERFACE", "wg0"),
            "tc_dev": os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV"),
            "tc_parent_rate_mbit": int((os.environ.get("WG_TC_PARENT_RATE_MBIT") or os.environ.get("VPN_TC_PARENT_RATE_MBIT") or "1000").strip() or "1000"),
            "server_public_key": os.environ.get("VPN_SERVER_PUBLIC_KEY"),
            "endpoint": os.environ.get("VPN_ENDPOINT"),
            "dns": os.environ.get("VPN_DNS", "1.1.1.1"),
            "max_active": int(os.environ.get("VPN_MAX_ACTIVE", "40") or 40),
        }]

    def _server_aliases(self, servers: list[dict], code: str) -> set[str]:
        code_u = str(code or "").strip().upper()
        if not code_u:
            return set()
        aliases = {code_u, code_u.replace(" ", "")}
        ordinal = None
        for idx, s in enumerate(servers, start=1):
            if str(s.get("code") or "").strip().upper() == code_u:
                ordinal = idx
                break
        if ordinal is None:
            if code_u.startswith("NL") and code_u[2:].isdigit():
                ordinal = int(code_u[2:])
            elif code_u == "NL":
                ordinal = 1
            else:
                digits = "".join(ch for ch in code_u if ch.isdigit())
                if digits:
                    try:
                        ordinal = int(digits)
                    except Exception:
                        ordinal = None
        if ordinal is not None:
            aliases.update({f"SERVER{ordinal}", f"SERVER #{ordinal}", f"NL{ordinal}"})
            if ordinal == 1:
                aliases.add("NL")
        if code_u in {"NL", "NL1"}:
            aliases.update({"NL", "NL1", "SERVER1", "SERVER #1"})
        if code_u == "NL2":
            aliases.update({"SERVER2", "SERVER #2"})
        return {a for a in aliases if a}

    def _server_capacity(self, server: dict | None) -> int:
        try:
            if server and server.get("max_active") is not None:
                return max(1, int(server.get("max_active")))
            return max(1, int(os.environ.get("VPN_MAX_ACTIVE", "40") or 40))
        except Exception:
            return 40

    async def _vpn_seats_by_server(self, session: AsyncSession) -> dict[str, int]:
        servers = self._load_vpn_servers()
        default_code = (os.environ.get("VPN_CODE") or "NL").upper()
        canonical_for_alias: dict[str, str] = {}
        result: dict[str, int] = {}
        for s in servers:
            code = str(s.get("code") or default_code).upper()
            result[code] = 0
            for alias in self._server_aliases(servers, code):
                canonical_for_alias[str(alias).upper()] = code
            canonical_for_alias.setdefault(code, code)

        code_expr = func.coalesce(func.upper(VpnPeer.server_code), default_code)
        q = (
            select(
                code_expr.label("code"),
                func.count(VpnPeer.id).label("cnt"),
            )
            .where(VpnPeer.is_active == True)  # noqa: E712
            .group_by(code_expr)
        )
        res = await session.execute(q)
        for raw_code, cnt in res.all():
            raw = str(raw_code or default_code).upper()
            canonical = canonical_for_alias.get(raw, raw)
            result[canonical] = int(result.get(canonical, 0)) + int(cnt or 0)

        for s in servers:
            code = str(s.get("code") or default_code).upper()
            host = str(s.get("host") or "").strip()
            user = str(s.get("user") or "").strip()
            if not host or not user:
                continue
            try:
                st = await self.get_server_status_for(
                    host=host,
                    port=int(s.get("port") or 22),
                    user=user,
                    password=s.get("password"),
                    interface=str(s.get("interface") or os.environ.get("VPN_INTERFACE", "wg0")),
                )
                if st.get("ok") and st.get("total_peers") is not None:
                    result[code] = max(int(result.get(code, 0)), int(st.get("total_peers") or 0))
            except Exception:
                pass
        if not servers:
            result.setdefault(default_code, 0)
        return result

    async def _pick_server_for_extra_peer(self, session: AsyncSession, *, inherited_code: str | None = None) -> dict:
        servers = self._load_vpn_servers()
        if not servers:
            raise RuntimeError("No VPN servers configured")
        used = await self._vpn_seats_by_server(session)

        def can_use(server: dict) -> bool:
            code = str(server.get("code") or "").upper()
            seats = int(used.get(code, 0))
            cap = self._server_capacity(server)
            return seats < cap

        if inherited_code:
            inherited_code = inherited_code.upper()
            for s in servers:
                if str(s.get("code") or "").upper() == inherited_code and can_use(s):
                    return s

        for s in servers:
            if can_use(s):
                return s
        raise RuntimeError("All VPN servers are full")

    async def create_extra_peer(self, session: AsyncSession, tg_id: int) -> Dict[str, Any]:
        """Create an additional active peer for the same tg_id.

        Intended for admin-only usage (multiple devices for the same admin).
        Does not deactivate existing peers.
        In multi-location mode, the extra peer is created on the current server
        only if that server still has free capacity; otherwise the next server
        with free seats is used.
        """
        try:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": int(tg_id)})
        except Exception:
            pass

        client_ip = await self._alloc_ip_unique(session, tg_id=tg_id)
        client_priv, client_pub = gen_keys()

        inherited_code = None
        try:
            active = await self._get_active_peer(session, tg_id)
            if active and getattr(active, "server_code", None):
                inherited_code = str(active.server_code).upper()
        except Exception:
            inherited_code = None
        if not inherited_code:
            inherited_code = (os.environ.get("VPN_CODE") or "NL").upper()

        server = await self._pick_server_for_extra_peer(session, inherited_code=inherited_code)
        server_code = str(server.get("code") or inherited_code or os.environ.get("VPN_CODE") or "NL").upper()
        provider = self._provider_for(
            host=str(server.get("host") or os.environ.get("WG_SSH_HOST") or ""),
            port=int(server.get("port") or 22),
            user=str(server.get("user") or os.environ.get("WG_SSH_USER") or ""),
            password=server.get("password"),
            interface=str(server.get("interface") or os.environ.get("VPN_INTERFACE", "wg0")),
            tc_dev=str(server.get("tc_dev") or server.get("wg_tc_dev") or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or ""),
            tc_parent_rate_mbit=int(server.get("tc_parent_rate_mbit") or server.get("wg_tc_parent_rate_mbit") or os.environ.get("WG_TC_PARENT_RATE_MBIT") or os.environ.get("VPN_TC_PARENT_RATE_MBIT") or 1000),
        )

        log.info("vpn_create_extra_peer tg_id=%s ip=%s server=%s", tg_id, client_ip, server_code)
        await provider.add_peer(client_pub, client_ip, tg_id=tg_id)

        row = VpnPeer(
            tg_id=tg_id,
            client_public_key=client_pub,
            client_private_key_enc=crypto.encrypt(client_priv),
            client_ip=client_ip,
            server_code=server_code,
            is_active=True,
            revoked_at=None,
            rotation_reason=None,
        )
        session.add(row)
        await session.flush()
        return {
            "peer_id": row.id,
            "tg_id": tg_id,
            "client_ip": client_ip,
            "public_key": client_pub,
            "client_private_key_enc": row.client_private_key_enc,
            "client_private_key_plain": client_priv,
            "server_code": server_code,
            "server_public_key": str(server.get("server_public_key") or self.server_pub),
            "endpoint": str(server.get("endpoint") or self.endpoint),
            "dns": str(server.get("dns") or self.dns),
            "host": str(server.get("host") or ""),
            "port": int(server.get("port") or 22),
            "user": str(server.get("user") or ""),
            "password": server.get("password"),
            "interface": str(server.get("interface") or os.environ.get("VPN_INTERFACE", "wg0")),
            "tc_dev": str(server.get("tc_dev") or server.get("wg_tc_dev") or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or ""),
        }

    async def _get_active_peer(self, session: AsyncSession, tg_id: int) -> Optional[VpnPeer]:
        q = (
            select(VpnPeer)
            .where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True)  # noqa: E712
            .order_by(VpnPeer.id.desc())
            .limit(1)
        )
        res = await session.execute(q)
        return res.scalar_one_or_none()

    async def _get_last_peer(self, session: AsyncSession, tg_id: int) -> Optional[VpnPeer]:
        q = select(VpnPeer).where(VpnPeer.tg_id == tg_id).order_by(VpnPeer.id.desc()).limit(1)
        res = await session.execute(q)
        return res.scalar_one_or_none()

    def _row_to_peer_dict(self, row: VpnPeer) -> Dict[str, Any]:
        priv_plain = crypto.decrypt(row.client_private_key_enc)
        return {
            "peer_id": row.id,
            "tg_id": row.tg_id,
            "client_ip": row.client_ip,
            "public_key": row.client_public_key,
            "client_private_key_enc": row.client_private_key_enc,
            "client_private_key_plain": priv_plain,
        }

    async def ensure_peer(self, session: AsyncSession, tg_id: int) -> Dict[str, Any]:
        """Return existing active peer or create/apply a new one."""

        # Prevent duplicate peer creation on rapid double-clicks / concurrent requests
        # (including across multiple app replicas). PostgreSQL advisory lock is scoped
        # to the current transaction.
        try:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": int(tg_id)})
        except Exception:
            # Non-Postgres / no permissions — best effort.
            pass

        active = await self._get_active_peer(session, tg_id)
        if active:
            return self._row_to_peer_dict(active)

        last = await self._get_last_peer(session, tg_id)
        if last and not last.is_active:
            # Restore only if the peer is eligible. After a grace window we prefer
            # issuing a new peer to avoid keeping stale records forever.
            eligible = True
            if (last.rotation_reason or "") == "expired_purged":
                eligible = False
            if (last.rotation_reason or "") in {"expired", "subscription_expired"}:
                if last.revoked_at is None:
                    eligible = False
                else:
                    cutoff = utcnow() - timedelta(hours=24)
                    eligible = last.revoked_at >= cutoff

            if eligible:
                log.info("vpn_restore_peer tg_id=%s peer_id=%s", tg_id, last.id)
                await self.provider.add_peer(last.client_public_key, last.client_ip, tg_id=tg_id)
                last.is_active = True
                last.revoked_at = None
                last.rotation_reason = None
                await session.flush()
                return self._row_to_peer_dict(last)

        client_ip = self._alloc_ip(tg_id)
        client_priv, client_pub = gen_keys()

        log.info("vpn_create_peer tg_id=%s ip=%s", tg_id, client_ip)
        await self.provider.add_peer(client_pub, client_ip, tg_id=tg_id)

        row = VpnPeer(
            tg_id=tg_id,
            client_public_key=client_pub,
            client_private_key_enc=crypto.encrypt(client_priv),
            client_ip=client_ip,
            server_code=(os.environ.get("VPN_CODE") or "NL").upper(),
            is_active=True,
            revoked_at=None,
            rotation_reason=None,
        )
        session.add(row)
        await session.flush()

        return {
            "peer_id": row.id,
            "tg_id": tg_id,
            "client_ip": client_ip,
            "public_key": client_pub,
            "client_private_key_enc": row.client_private_key_enc,
            "client_private_key_plain": client_priv,
        }

    async def ensure_peer_for_server(
        self,
        session: AsyncSession,
        tg_id: int,
        *,
        server_code: str,
        host: str,
        port: int,
        user: str,
        password: str | None,
        interface: str,
        tc_dev: str | None = None,
    ) -> Dict[str, Any]:
        """Create/restore an active peer for a specific server.

        This is an additive feature for multi-location deployments. It does not
        change existing logic for single-server usage.
        """

        try:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": int(tg_id)})
        except Exception:
            pass

        q = (
            select(VpnPeer)
            .where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True, VpnPeer.server_code == server_code)  # noqa: E712
            .order_by(VpnPeer.id.desc())
            .limit(1)
        )
        res = await session.execute(q)
        active = res.scalar_one_or_none()
        if active:
            return self._row_to_peer_dict(active)

        provider = self._provider_for(host=host, port=port, user=user, password=password, interface=interface, tc_dev=tc_dev)

        # Try restore last inactive for this server
        q = (
            select(VpnPeer)
            .where(VpnPeer.tg_id == tg_id, VpnPeer.server_code == server_code)
            .order_by(VpnPeer.id.desc())
            .limit(1)
        )
        res = await session.execute(q)
        last = res.scalar_one_or_none()
        if last and not last.is_active:
            eligible = True
            if (last.rotation_reason or "") == "expired_purged":
                eligible = False
            if (last.rotation_reason or "") in {"expired", "subscription_expired"}:
                if last.revoked_at is None:
                    eligible = False
                else:
                    cutoff = utcnow() - timedelta(hours=24)
                    eligible = last.revoked_at >= cutoff

            if eligible:
                log.info("vpn_restore_peer tg_id=%s peer_id=%s server=%s", tg_id, last.id, server_code)
                await provider.add_peer(last.client_public_key, last.client_ip, tg_id=tg_id)
                last.is_active = True
                last.revoked_at = None
                last.rotation_reason = None
                await session.flush()
                return self._row_to_peer_dict(last)

        # No eligible peer to restore on this server — create a fresh peer.
        client_ip = self._alloc_ip(tg_id)
        client_priv, client_pub = gen_keys()

        log.info("vpn_create_peer tg_id=%s ip=%s server=%s", tg_id, client_ip, server_code)
        await provider.add_peer(client_pub, client_ip, tg_id=tg_id)

        row = VpnPeer(
            tg_id=tg_id,
            client_public_key=client_pub,
            client_private_key_enc=crypto.encrypt(client_priv),
            client_ip=client_ip,
            server_code=server_code,
            is_active=True,
            revoked_at=None,
            rotation_reason=None,
        )
        session.add(row)
        await session.flush()

        return {
            "peer_id": row.id,
            "tg_id": tg_id,
            "client_ip": client_ip,
            "public_key": client_pub,
            "client_private_key_enc": row.client_private_key_enc,
            "client_private_key_plain": client_priv,
        }

    async def restore_expired_peers(self, session: AsyncSession, tg_id: int, *, grace_hours: int = 24) -> int:
        """Re-enable peers disabled due to subscription expiration.

        Called after a successful payment so the existing config starts working
        again without requiring a new config.
        """

        cutoff = utcnow() - timedelta(hours=int(grace_hours))

        q = (
            select(VpnPeer)
            .where(
                VpnPeer.tg_id == tg_id,
                VpnPeer.is_active == False,  # noqa: E712
                VpnPeer.rotation_reason.in_(["expired", "subscription_expired"]),
                VpnPeer.revoked_at.is_not(None),
                VpnPeer.revoked_at >= cutoff,
            )
            .order_by(VpnPeer.id.asc())
        )
        rows = list((await session.execute(q)).scalars().all())
        if not rows:
            return 0

        # Load multi-location servers mapping once (best-effort).
        servers_by_code: dict[str, dict] = {}
        try:
            import json

            servers_json = (os.environ.get("VPN_SERVERS_JSON") or os.environ.get("VPN_SERVERS") or "").strip()
            servers: list[dict] = []
            if servers_json:
                v = json.loads(servers_json)
                if isinstance(v, list):
                    servers = [x for x in v if isinstance(x, dict)]
            for s in servers:
                code = str(s.get("code") or "").upper()
                if code:
                    servers_by_code[code] = s
        except Exception:
            servers_by_code = {}

        restored = 0
        for p in rows:
            try:
                code = (p.server_code or "").upper() or None
                if code and code in servers_by_code:
                    srv = servers_by_code[code]
                    provider = self._provider_for(
                        host=str(srv.get("host")),
                        port=int(srv.get("port") or 22),
                        user=str(srv.get("user")),
                        password=srv.get("password"),
                        interface=str(srv.get("interface") or os.environ.get("VPN_INTERFACE", "wg0")),
                        tc_dev=str(srv.get("tc_dev") or srv.get("wg_tc_dev") or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or ""),
                        tc_parent_rate_mbit=int(srv.get("tc_parent_rate_mbit") or srv.get("wg_tc_parent_rate_mbit") or os.environ.get("WG_TC_PARENT_RATE_MBIT") or os.environ.get("VPN_TC_PARENT_RATE_MBIT") or 1000),
                    )
                    await provider.add_peer(p.client_public_key, p.client_ip, tg_id=tg_id)
                else:
                    await self.provider.add_peer(p.client_public_key, p.client_ip, tg_id=tg_id)
                p.is_active = True
                p.revoked_at = None
                p.rotation_reason = None
                restored += 1
            except Exception:
                log.exception("vpn_restore_expired_peer_failed tg_id=%s peer_id=%s", tg_id, getattr(p, "id", None))

        await session.flush()
        return restored

    async def ensure_rate_limit(self, *, tg_id: int, ip: str) -> None:
        """Best-effort apply the standard WG per-user rate limit on the default server."""
        await self.provider.tc_apply_limit_for_ip(ip=ip, tg_id=tg_id)

    async def ensure_rate_limit_for_server(
        self,
        *,
        tg_id: int,
        ip: str,
        host: str,
        port: int,
        user: str,
        password: str | None,
        interface: str,
        tc_dev: str | None = None,
        tc_parent_rate_mbit: int | None = None,
    ) -> None:
        provider = self._provider_for(host=host, port=port, user=user, password=password, interface=interface, tc_dev=tc_dev, tc_parent_rate_mbit=tc_parent_rate_mbit)
        await provider.tc_apply_limit_for_ip(ip=ip, tg_id=tg_id)

    async def remove_peer_for_server(
        self,
        *,
        public_key: str,
        host: str,
        port: int,
        user: str,
        password: str | None,
        interface: str,
        tc_dev: str | None = None,
        tc_parent_rate_mbit: int | None = None,
    ) -> None:
        provider = self._provider_for(host=host, port=port, user=user, password=password, interface=interface, tc_dev=tc_dev, tc_parent_rate_mbit=tc_parent_rate_mbit)
        await provider.remove_peer(public_key)

    async def get_peer_handshake_for_server(
        self,
        *,
        public_key: str,
        host: str,
        port: int,
        user: str,
        password: str | None,
        interface: str,
        tc_dev: str | None = None,
        tc_parent_rate_mbit: int | None = None,
    ) -> int:
        provider = self._provider_for(host=host, port=port, user=user, password=password, interface=interface, tc_dev=tc_dev, tc_parent_rate_mbit=tc_parent_rate_mbit)
        return await provider.get_peer_latest_handshake(public_key)

    async def rotate_peer(self, session: AsyncSession, tg_id: int, reason: str = "manual_reset") -> Dict[str, Any]:
        """Manual reset: best-effort remove old peer then create/apply new peer."""

        # Serialize resets per user (see ensure_peer).
        try:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": int(tg_id)})
        except Exception:
            pass

        active = await self._get_active_peer(session, tg_id)

        if active:
            try:
                await self.provider.remove_peer(active.client_public_key)
            except Exception:
                log.exception("vpn_remove_old_peer_failed tg_id=%s peer_id=%s", tg_id, active.id)

            active.is_active = False
            active.revoked_at = utcnow()
            active.rotation_reason = reason
            await session.flush()

        client_ip = self._alloc_ip(tg_id)
        client_priv, client_pub = gen_keys()

        log.info("vpn_rotate_create_peer tg_id=%s ip=%s", tg_id, client_ip)
        await self.provider.add_peer(client_pub, client_ip, tg_id=tg_id)

        row = VpnPeer(
            tg_id=tg_id,
            client_public_key=client_pub,
            client_private_key_enc=crypto.encrypt(client_priv),
            client_ip=client_ip,
            server_code=None,
            is_active=True,
            revoked_at=None,
            rotation_reason=None,
        )
        session.add(row)
        await session.flush()

        return {
            "peer_id": row.id,
            "tg_id": tg_id,
            "client_ip": client_ip,
            "public_key": client_pub,
            "client_private_key_enc": row.client_private_key_enc,
            "client_private_key_plain": client_priv,
        }

    def build_wg_conf(
        self,
        peer: Dict[str, Any],
        user_label: Optional[str] = None,
        *,
        server_public_key: str | None = None,
        endpoint: str | None = None,
        dns: str | None = None,
    ) -> str:
        priv = peer.get("client_private_key_plain")
        if not priv:
            raise RuntimeError("Missing client_private_key_plain in peer dict.")

        server_pub = server_public_key or self.server_pub
        endpoint_v = endpoint or self.endpoint
        dns_v = dns or self.dns

        return (
            "[Interface]\n"
            f"PrivateKey = {priv}\n"
            f"Address = {peer['client_ip']}/32\n"
            f"DNS = {dns_v}\n\n"
            "[Peer]\n"
            f"PublicKey = {server_pub}\n"
            f"Endpoint = {endpoint_v}\n"
            "AllowedIPs = 0.0.0.0/0\n"
            "PersistentKeepalive = 25\n"
        )

    async def get_server_status(self) -> Dict[str, Any]:
        """Best-effort server status for UI (no hard dependency).

        Returns:
          {
            ok: bool,
            cpu_load_percent: float|None,
            active_peers: int|None,
            total_peers: int|None,
          }
        """
        try:
            cpu = await self.provider.get_cpu_load_percent(sample_seconds=1)
            active = await self.provider.get_active_peers(window_seconds=180)
            total = await self.provider.get_total_peers()
            return {
                "ok": True,
                "cpu_load_percent": cpu,
                "active_peers": active,
                "total_peers": total,
            }
        except Exception as e:
            log.warning("vpn_status_unavailable: %s", e)
            return {
                "ok": False,
                "cpu_load_percent": None,
                "active_peers": None,
                "total_peers": None,
            }

    async def get_server_status_for(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str | None,
        interface: str,
    ) -> Dict[str, Any]:
        """Best-effort status for an arbitrary WireGuard SSH server.

        This does NOT change existing logic; it is only used for UI to show multi-server load.
        """
        try:
            provider = WireGuardSSHProvider(
                host=host,
                port=int(port),
                user=user,
                password=password,
                interface=interface,
                tc_dev=None,
            )
            cpu = await provider.get_cpu_load_percent(sample_seconds=1)
            active = await provider.get_active_peers(window_seconds=180)
            total = await provider.get_total_peers()
            return {
                "ok": True,
                "cpu_load_percent": cpu,
                "active_peers": active,
                "total_peers": total,
            }
        except Exception as e:
            log.warning("vpn_status_unavailable(%s): %s", host, e)
            return {
                "ok": False,
                "cpu_load_percent": None,
                "active_peers": None,
                "total_peers": None,
            }


    async def get_recent_peer_handshakes(self, window_seconds: int = 180) -> list[dict[str, Any]]:
        """List recent peers by latest handshake across all configured servers.

        Returns a list of dicts:
          {public_key, handshake_ts, age_seconds, server_code}
        for peers with handshake within window_seconds.

        This is used by admin UI to map active peers to user profiles.
        """
        servers = self._load_vpn_servers()
        now_ts = int(utcnow().timestamp())
        w = max(1, int(window_seconds))

        merged: dict[str, dict[str, Any]] = {}

        for server in servers:
            code = str(server.get("code") or os.environ.get("VPN_CODE") or "NL").strip().upper()
            host = str(server.get("host") or os.environ.get("WG_SSH_HOST") or "").strip()
            user = str(server.get("user") or os.environ.get("WG_SSH_USER") or "").strip()
            if not host or not user:
                continue
            try:
                provider = self._provider_for(
                    host=host,
                    port=int(server.get("port") or os.environ.get("WG_SSH_PORT", "22") or 22),
                    user=user,
                    password=server.get("password") if server.get("password") is not None else os.environ.get("WG_SSH_PASSWORD"),
                    interface=str(server.get("interface") or os.environ.get("VPN_INTERFACE") or "wg0"),
                    tc_dev=str(server.get("tc_dev") or server.get("wg_tc_dev") or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or ""),
                    tc_parent_rate_mbit=int(server.get("tc_parent_rate_mbit") or os.environ.get("WG_TC_PARENT_RATE_MBIT") or os.environ.get("VPN_TC_PARENT_RATE_MBIT") or 1000),
                )
                hs = await provider.get_latest_handshakes()
            except Exception as e:
                log.warning("vpn_recent_handshakes_unavailable(%s): %s", code or host, e)
                continue

            for k, ts in hs.items():
                if not ts or ts <= 0:
                    continue
                age = now_ts - int(ts)
                if age < 0:
                    age = 0
                if age > w:
                    continue
                item = {
                    "public_key": k,
                    "handshake_ts": int(ts),
                    "age_seconds": int(age),
                    "server_code": code,
                }
                prev = merged.get(k)
                if prev is None or int(item["handshake_ts"]) > int(prev.get("handshake_ts", 0) or 0):
                    merged[k] = item

        items = list(merged.values())
        items.sort(key=lambda x: x.get("handshake_ts", 0), reverse=True)
        return items

    async def get_used_peer_stats(self) -> list[dict[str, Any]]:
        """List peers that have evidence of real VPN usage across all configured servers.

        A peer is considered "used" when at least one of these is true on any server:
          - latest handshake timestamp > 0
          - received bytes > 0
          - sent bytes > 0
        """
        servers = self._load_vpn_servers()
        now_ts = int(utcnow().timestamp())
        merged: dict[str, dict[str, Any]] = {}

        for server in servers:
            code = str(server.get("code") or os.environ.get("VPN_CODE") or "NL").strip().upper()
            host = str(server.get("host") or os.environ.get("WG_SSH_HOST") or "").strip()
            user = str(server.get("user") or os.environ.get("WG_SSH_USER") or "").strip()
            if not host or not user:
                continue
            try:
                provider = self._provider_for(
                    host=host,
                    port=int(server.get("port") or os.environ.get("WG_SSH_PORT", "22") or 22),
                    user=user,
                    password=server.get("password") if server.get("password") is not None else os.environ.get("WG_SSH_PASSWORD"),
                    interface=str(server.get("interface") or os.environ.get("VPN_INTERFACE") or "wg0"),
                    tc_dev=str(server.get("tc_dev") or server.get("wg_tc_dev") or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or ""),
                    tc_parent_rate_mbit=int(server.get("tc_parent_rate_mbit") or os.environ.get("WG_TC_PARENT_RATE_MBIT") or os.environ.get("VPN_TC_PARENT_RATE_MBIT") or 1000),
                )
                hs = await provider.get_latest_handshakes()
                transfers = await provider.get_peer_transfers()
            except Exception as e:
                log.warning("vpn_used_peer_stats_unavailable(%s): %s", code or host, e)
                continue

            keys = set(hs.keys()) | set(transfers.keys())
            for key in keys:
                hts = int(hs.get(key, 0) or 0)
                tr = transfers.get(key) or {}
                rx = int(tr.get("rx_bytes", 0) or 0)
                tx = int(tr.get("tx_bytes", 0) or 0)
                total = rx + tx
                if hts <= 0 and total <= 0:
                    continue
                age = None
                if hts > 0:
                    age = now_ts - hts
                    if age < 0:
                        age = 0
                item = {
                    "public_key": key,
                    "server_code": code,
                    "handshake_ts": hts,
                    "age_seconds": age,
                    "rx_bytes": rx,
                    "tx_bytes": tx,
                    "total_bytes": total,
                }
                prev = merged.get(key)
                if prev is None:
                    merged[key] = item
                    continue
                prev_hs = int(prev.get("handshake_ts", 0) or 0)
                prev_total = int(prev.get("total_bytes", 0) or 0)
                if hts > prev_hs or (hts == prev_hs and total > prev_total):
                    merged[key] = item

        items = list(merged.values())
        items.sort(key=lambda x: (int(x.get("handshake_ts", 0) or 0), int(x.get("total_bytes", 0) or 0)), reverse=True)
        return items


vpn_service = VPNService()
