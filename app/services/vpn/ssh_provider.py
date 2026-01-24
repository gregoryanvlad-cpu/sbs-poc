import asyncssh
import logging
import os
import tempfile

log = logging.getLogger(__name__)

class WireGuardSSHProvider:
    def __init__(self, host: str, port: int, user: str, password: str | None = None,
                 interface: str = "wg0"):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.interface = interface

        self._key_path = None
        key = os.environ.get("WG_SSH_PRIVATE_KEY")
        if key:
            f = tempfile.NamedTemporaryFile(
                delete=False,
                mode="w",
                prefix="wgkey_",
                suffix=".key",
            )
            f.write(key)
            f.close()
            os.chmod(f.name, 0o600)
            self._key_path = f.name
            log.info("SSH private key written to temp file")

    async def _connect(self):
        return await asyncssh.connect(
            self.host,
            port=self.port,
            username=self.user,
            client_keys=[self._key_path] if self._key_path else None,
            password=None if self._key_path else self.password,
            known_hosts=None,
            connect_timeout=8,
            login_timeout=8,
        )

    async def _run(self, cmd: str) -> str:
        async with await self._connect() as conn:
            res = await conn.run(cmd, check=True, timeout=10)
            return res.stdout or ""

    async def add_peer(self, public_key: str, client_ip: str) -> None:
        await self._run(f"wg set {self.interface} peer {public_key} allowed-ips {client_ip}/32")

    async def remove_peer(self, public_key: str) -> None:
        await self._run(f"wg set {self.interface} peer {public_key} remove")
