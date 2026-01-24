from __future__ import annotations

import os
import base64
import ipaddress
from typing import Optional, Dict, Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives import serialization

from app.services.vpn.ssh_provider import WireGuardSSHProvider

VPN_NET = ipaddress.ip_network(os.environ.get("VPN_CLIENT_NET", "10.66.0.0/16"))

def _b64encode_raw(b: bytes) -> str:
    return base64.b64encode(b).decode("utf-8")

def gen_keys() -> tuple[str, str]:
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

def encrypt(secret: str, data: str) -> str:
    return Fernet(secret.encode("utf-8")).encrypt(data.encode("utf-8")).decode("utf-8")

class VPNService:
    def __init__(self) -> None:
        # password is optional now (when using WG_SSH_PRIVATE_KEY)
        self.provider = WireGuardSSHProvider(
            host=os.environ["WG_SSH_HOST"],
            port=int(os.environ.get("WG_SSH_PORT", "22")),
            user=os.environ["WG_SSH_USER"],
            password=os.environ.get("WG_SSH_PASSWORD"),  # may be None
            interface=os.environ.get("VPN_INTERFACE", "wg0"),
        )
        self.server_pub = os.environ["VPN_SERVER_PUBLIC_KEY"]
        self.endpoint = os.environ["VPN_ENDPOINT"]
        self.enc_secret = os.environ["VPN_KEY_ENC_SECRET"]
        self.dns = os.environ.get("VPN_DNS", "1.1.1.1")

    def _alloc_ip(self, tg_id: int) -> str:
        host = (tg_id % 65000) + 2
        return str(VPN_NET.network_address + host)

    async def ensure_peer(self, session, tg_id: int) -> Dict[str, Any]:
        client_ip = self._alloc_ip(tg_id)
        return await self.create_peer(tg_id, client_ip)

    async def rotate_peer(self, session, tg_id: int, reason: str = "manual_reset") -> Dict[str, Any]:
        client_ip = self._alloc_ip(tg_id)
        return await self.create_peer(tg_id, client_ip)

    async def create_peer(self, tg_id: int, client_ip: str) -> Dict[str, Any]:
        client_priv, client_pub = gen_keys()
        await self.provider.add_peer(client_pub, client_ip)
        return {
            "tg_id": tg_id,
            "client_ip": client_ip,
            "public_key": client_pub,
            "client_private_key_enc": encrypt(self.enc_secret, client_priv),
            "client_private_key_plain": client_priv,
        }

    def build_wg_conf(self, peer: Dict[str, Any], user_label: Optional[str] = None) -> str:
        priv = peer.get("client_private_key_plain")
        if not priv:
            raise RuntimeError("Missing client private key in peer dict (client_private_key_plain).")

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
