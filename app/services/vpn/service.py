import os
import ipaddress
import base64
from typing import Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives import serialization

from app.services.vpn.ssh_provider import WireGuardSSHProvider

# VPN network
VPN_NET = ipaddress.ip_network("10.66.0.0/16")


def gen_keys():
    private = x25519.X25519PrivateKey.generate()
    public = private.public_key()

    priv_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    return (
        base64.b64encode(priv_bytes).decode(),
        base64.b64encode(pub_bytes).decode(),
    )


def encrypt(secret: str, data: str) -> str:
    f = Fernet(secret.encode())
    return f.encrypt(data.encode()).decode()


class VPNService:
    def __init__(self):
        self.provider = WireGuardSSHProvider(
            host=os.environ["WG_SSH_HOST"],
            port=int(os.environ.get("WG_SSH_PORT", 22)),
            user=os.environ["WG_SSH_USER"],
            password=os.environ["WG_SSH_PASSWORD"],
            interface=os.environ.get("VPN_INTERFACE", "wg0"),
        )

        self.server_pub = os.environ["VPN_SERVER_PUBLIC_KEY"]
        self.endpoint = os.environ["VPN_ENDPOINT"]
        self.enc_secret = os.environ["VPN_KEY_ENC_SECRET"]

    # 🔹 минимальный IPAM: выдаём следующий IP
    def _alloc_ip(self, user_id: int) -> str:
        # простая и детерминированная схема
        host = (user_id % 60000) + 2
        return str(VPN_NET.network_address + host)

    async def ensure_peer(self, session, user_id: int):
        """
        Если peer уже есть — вернуть его
        Если нет — создать
        """
        # ⚠️ В MVP просто создаём каждый раз новый peer
        client_ip = self._alloc_ip(user_id)
        peer = await self.create_peer(user_id, client_ip)
        return peer

    async def rotate_peer(self, session, user_id: int, reason: str = "manual"):
        """
        Для MVP: просто создаём новый peer
        (старый будет неиспользуем)
        """
        client_ip = self._alloc_ip(user_id)
        peer = await self.create_peer(user_id, client_ip)
        return peer

    async def create_peer(self, user_id: int, client_ip: str):
        priv, pub = gen_keys()

        # добавляем peer на сервер
        await self.provider.add_peer(pub, client_ip)

        enc_priv = encrypt(self.enc_secret, priv)

        return {
            "public_key": pub,
            "private_key_enc": enc_priv,
            "private_key": priv,
            "client_ip": client_ip,
        }

    def build_wg_conf(self, peer: dict, user_label: Optional[str] = None) -> str:
        return f"""[Interface]
PrivateKey = {peer["private_key"]}
Address = {peer["client_ip"]}/32
DNS = 1.1.1.1

[Peer]
PublicKey = {self.server_pub}
Endpoint = {self.endpoint}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""


# ✅ ВОТ ЭТА СТРОКА — САМОЕ ГЛАВНОЕ
# singleton, который импортируется в nav.py
vpn_service = VPNService()
