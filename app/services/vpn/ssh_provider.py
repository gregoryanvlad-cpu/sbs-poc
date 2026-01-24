import asyncssh
import logging

log = logging.getLogger(__name__)

class WireGuardSSHProvider:
    def __init__(self, host: str, port: int, user: str, password: str, interface: str = "wg0"):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.interface = interface

    async def _run(self, cmd: str) -> str:
        async with asyncssh.connect(
            self.host,
            port=self.port,
            username=self.user,
            password=self.password,
            known_hosts=None,
        ) as conn:
            res = await conn.run(cmd, check=True)
            return res.stdout or ""

    async def add_peer(self, public_key: str, client_ip: str) -> None:
        cmd = f"wg set {self.interface} peer {public_key} allowed-ips {client_ip}/32"
        log.info("WG add peer pub=%s ip=%s", public_key, client_ip)
        await self._run(cmd)

    async def remove_peer(self, public_key: str) -> None:
        cmd = f"wg set {self.interface} peer {public_key} remove"
        log.info("WG remove peer pub=%s", public_key)
        await self._run(cmd)
