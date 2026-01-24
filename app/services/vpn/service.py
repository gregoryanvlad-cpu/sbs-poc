from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption, PublicFormat
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import VpnPeer
from app.repo import get_active_peer, deactivate_peers
from app.services.vpn.crypto import decrypt, encrypt

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PeerData:
    tg_id: int
    public_key: str
    private_key: str
    client_ip: str


def _wg_keypair() -> tuple[str, str]:
    """Generate WireGuard X25519 keypair.

    WireGuard uses raw 32-byte keys base64-encoded.
    """
    priv = X25519PrivateKey.generate()
    pub = priv.public_key()
    priv_raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_raw = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(priv_raw).decode("ascii"), base64.b64encode(pub_raw).decode("ascii")


def _stable_ip(tg_id: int) -> str:
    # 10.66.0.0/16; skip .0 and .255 by using 1..254 for host
    n = tg_id % (254 * 256)
    o2 = n // 254
    o3 = (n % 254) + 1
    return f"10.66.{o2}.{o3}"


class VpnService:
    async def ensure_peer(self, session: AsyncSession, tg_id: int) -> PeerData:
        peer = await get_active_peer(session, tg_id)
        if peer:
            return PeerData(tg_id=tg_id, public_key=peer.client_public_key, private_key=decrypt(peer.client_private_key_enc), client_ip=peer.client_ip)

        priv, pub = _wg_keypair()
        client_ip = _stable_ip(tg_id)
        peer = VpnPeer(
            tg_id=tg_id,
            client_public_key=pub,
            client_private_key_enc=encrypt(priv),
            client_ip=client_ip,
            is_active=True,
        )
        session.add(peer)
        await session.flush()

        log.info("vpn_peer_created", extra={"tg_id": tg_id})
        return PeerData(tg_id=tg_id, public_key=pub, private_key=priv, client_ip=client_ip)

    async def rotate_peer(self, session: AsyncSession, tg_id: int, *, reason: str) -> PeerData:
        await deactivate_peers(session, tg_id, reason=reason)
        priv, pub = _wg_keypair()
        client_ip = _stable_ip(tg_id)
        peer = VpnPeer(
            tg_id=tg_id,
            client_public_key=pub,
            client_private_key_enc=encrypt(priv),
            client_ip=client_ip,
            is_active=True,
            rotation_reason=reason,
        )
        session.add(peer)
        await session.flush()
        log.info("vpn_peer_rotated", extra={"tg_id": tg_id, "reason": reason})
        return PeerData(tg_id=tg_id, public_key=pub, private_key=priv, client_ip=client_ip)

    def build_wg_conf(self, peer: PeerData, *, user_label: str) -> str:
        return (
            f"[Interface]\n"
            f"PrivateKey = {peer.private_key}\n"
            f"Address = {peer.client_ip}/32\n"
            f"DNS = {settings.vpn_dns}\n\n"
            f"[Peer]\n"
            f"PublicKey = {settings.vpn_server_public_key}\n"
            f"AllowedIPs = {settings.vpn_allowed_ips}\n"
            f"Endpoint = {settings.vpn_endpoint}\n"
            f"PersistentKeepalive = 25\n"
        )


vpn_service = VpnService()
