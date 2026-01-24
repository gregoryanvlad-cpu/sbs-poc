
import os
import ipaddress
import base64
from cryptography.fernet import Fernet
from app.services.vpn.ssh_provider import WireGuardSSHProvider
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives import serialization

VPN_NET = ipaddress.ip_network("10.66.0.0/16")

def gen_keys():
    private = x25519.X25519PrivateKey.generate()
    public = private.public_key()
    priv_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption()
    )
    pub_bytes = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
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

    async def create_peer(self, user_id: int, client_ip: str):
        priv, pub = gen_keys()
        await self.provider.add_peer(pub, client_ip)

        enc_priv = encrypt(self.enc_secret, priv)

        config = f"""[Interface]
PrivateKey = {priv}
Address = {client_ip}/32
DNS = 1.1.1.1

[Peer]
PublicKey = {self.server_pub}
Endpoint = {self.endpoint}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""

        return {
            "public_key": pub,
            "private_key_enc": enc_priv,
            "config": config,
        }
