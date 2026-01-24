import asyncssh
import logging
import os

log = logging.getLogger(__name__)

class WireGuardSSHProvider:
    def __init__(self, host: str, port: int, user: str, password: str | None = None,
                 interface: str = "wg0"):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.interface = interface
        self.private_key = os.environ.get("WG_SSH_PRIVATE_KEY")

    async def _connect(self):
        if self.private_key:
            log.info("SSH auth via private key")
            return await asyncssh.connect(
                self.host,
                port=self.port,
                username=self.user,
                client_keys=[self.private_key],
                known_hosts=None,
                connect_timeout=8,
                login_timeout=8,
            )
        else:
            log.info("SSH auth via password")
            return await asyncssh.connect(
                self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                known_hosts=None,
                connect_timeout=8,
                login_timeout=8,
            )

    async def _run(self, cmd: str) -> str:
        async with await self._connect() as conn:
            res = await conn.run(cmd, check=True, timeout=10)
            return res.stdout or ""

    async def add_peer(self, public_key: str, client_ip: str) -> None:
        cmd = f"wg set {self.interface} peer {public_key} allowed-ips {client_ip}/32"
        log.info("WG add peer pub=%s ip=%s", public_key, client_ip)
        await self._run(cmd)

    async def remove_peer(self, public_key: str) -> None:
        cmd = f"wg set {self.interface} peer {public_key} remove"
        log.info("WG remove peer pub=%s", public_key)
        await self._run(cmd)
