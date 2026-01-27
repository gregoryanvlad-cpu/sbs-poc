from __future__ import annotations

import base64
import ipaddress
import logging
import os
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from sqlalchemy import select
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
        self.provider = WireGuardSSHProvider(
            host=os.environ["WG_SSH_HOST"],
            port=int(os.environ.get("WG_SSH_PORT", "22")),
            user=os.environ["WG_SSH_USER"],
            password=os.environ.get("WG_SSH_PASSWORD"),
            interface=os.environ.get("VPN_INTERFACE", "wg0"),
        )
        self.server_pub = os.environ["VPN_SERVER_PUBLIC_KEY"]
        self.endpoint = os.environ["VPN_ENDPOINT"]
        self.dns = os.environ.get("VPN_DNS", "1.1.1.1")

    def _alloc_ip(self, tg_id: int) -> str:
        # Deterministic allocation: stable IP per tg_id until rotate.
        # Reserve first addresses in subnet (network + 0/1 are special; start from +2).
        host = (tg_id % 65000) + 2
        return str(VPN_NET.network_address + host)

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
            "tg_id": row.tg_id,
            "client_ip": row.client_ip,
            "public_key": row.client_public_key,
            "client_private_key_enc": row.client_private_key_enc,
            "client_private_key_plain": priv_plain,
        }

    async def ensure_peer(self, session: AsyncSession, tg_id: int) -> Dict[str, Any]:
        """Return existing active peer or create/apply a new one."""
        active = await self._get_active_peer(session, tg_id)
        if active:
            return self._row_to_peer_dict(active)

        last = await self._get_last_peer(session, tg_id)
        if last and not last.is_active:
            # Reactivate the SAME peer (same keys/IP) by re-applying on server
            log.info("vpn_restore_peer tg_id=%s peer_id=%s", tg_id, last.id)
            await self.provider.add_peer(last.client_public_key, last.client_ip)
            last.is_active = True
            last.revoked_at = None
            last.rotation_reason = None
            await session.flush()
            return self._row_to_peer_dict(last)

        client_ip = self._alloc_ip(tg_id)
        client_priv, client_pub = gen_keys()

        log.info("vpn_create_peer tg_id=%s ip=%s", tg_id, client_ip)
        await self.provider.add_peer(client_pub, client_ip)

        row = VpnPeer(
            tg_id=tg_id,
            client_public_key=client_pub,
            client_private_key_enc=crypto.encrypt(client_priv),
            client_ip=client_ip,
            is_active=True,
            revoked_at=None,
            rotation_reason=None,
        )
        session.add(row)
        await session.flush()

        return {
            "tg_id": tg_id,
            "client_ip": client_ip,
            "public_key": client_pub,
            "client_private_key_enc": row.client_private_key_enc,
            "client_private_key_plain": client_priv,
        }

    async def rotate_peer(self, session: AsyncSession, tg_id: int, reason: str = "manual_reset") -> Dict[str, Any]:
        """Manual reset: best-effort remove old peer then create/apply new peer."""
        active = await self._get_active_peer(session, tg_id)

        if active:
            try:
                await self.provider.remove_peer(active.client_public_key)
            except Exception:
                # Best-effort remove: even if SSH hiccups, we still continue to create new peer
                log.exception("vpn_remove_old_peer_failed tg_id=%s peer_id=%s", tg_id, active.id)

            active.is_active = False
            active.revoked_at = utcnow()
            active.rotation_reason = reason
            await session.flush()

        client_ip = self._alloc_ip(tg_id)
        client_priv, client_pub = gen_keys()

        log.info("vpn_rotate_create_peer tg_id=%s ip=%s", tg_id, client_ip)
        await self.provider.add_peer(client_pub, client_ip)

        row = VpnPeer(
            tg_id=tg_id,
            client_public_key=client_pub,
            client_private_key_enc=crypto.encrypt(client_priv),
            client_ip=client_ip,
            is_active=True,
            revoked_at=None,
            rotation_reason=None,
        )
        session.add(row)
        await session.flush()

        return {
            "tg_id": tg_id,
            "client_ip": client_ip,
            "public_key": client_pub,
            "client_private_key_enc": row.client_private_key_enc,
            "client_private_key_plain": client_priv,
        }

    def build_wg_conf(self, peer: Dict[str, Any], user_label: Optional[str] = None) -> str:
        priv = peer.get("client_private_key_plain")
        if not priv:
            raise RuntimeError("Missing client_private_key_plain in peer dict.")

        return (
            "[Interface]\n"
            f"PrivateKey = {priv}\n"
            f"Address = {peer['client_ip']}/32\n"
            f"DNS = {self.dns}\n\n"
            "[Peer]\n"
            f"PublicKey = {self.server_pub}\n"
            f"Endpoint = {self.endpoint}\n"
            "AllowedIPs = 0.0.0.0/0\n"
            "PersistentKeepalive = 25\n"
        )


vpn_service = VPNService()
