
import asyncssh
import asyncio
import logging

log = logging.getLogger(__name__)

class WireGuardSSHProvider:
    def __init__(self, host, port, user, password, interface="wg0"):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.interface = interface

    async def _run(self, cmd: str):
        async with asyncssh.connect(
            self.host,
            port=self.port,
            username=self.user,
            password=self.password,
            known_hosts=None,
        ) as conn:
            result = await conn.run(cmd, check=True)
            return result.stdout

    async def add_peer(self, public_key: str, client_ip: str):
        cmd = f"wg set {self.interface} peer {public_key} allowed-ips {client_ip}/32"
        log.info("WG add peer %s %s", public_key, client_ip)
        return await self._run(cmd)

    async def remove_peer(self, public_key: str):
        cmd = f"wg set {self.interface} peer {public_key} remove"
        log.info("WG remove peer %s", public_key)
        return await self._run(cmd)
