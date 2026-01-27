import asyncio
import base64
import asyncssh
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

WG_BIN = "/usr/bin/wg"
ENV_PATH = "PATH=/usr/sbin:/usr/bin:/sbin:/bin"


class WireGuardSSHProvider:
    def __init__(self, host: str, port: int, user: str, password: Optional[str], interface: str = "wg0"):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.interface = interface

        self.connect_timeout = 15
        self.login_timeout = 15
        self.cmd_timeout = 10
        self.retries = 2

        self._key_obj = None

        key_b64 = os.environ.get("WG_SSH_PRIVATE_KEY_B64")
        if key_b64:
            key_text = base64.b64decode(key_b64.encode()).decode()
            self._key_obj = asyncssh.import_private_key(key_text.strip())
            log.info("SSH key loaded (base64)")

    async def _connect(self) -> asyncssh.SSHClientConnection:
        return await asyncssh.connect(
            self.host,
            port=self.port,
            username=self.user,
            client_keys=[self._key_obj],
            known_hosts=None,
            connect_timeout=self.connect_timeout,
            login_timeout=self.login_timeout,
        )

    async def _run(self, cmd: str) -> None:
        last = None
        for _ in range(self.retries):
            try:
                async with await self._connect() as conn:
                    full_cmd = f"{ENV_PATH} {cmd}"
                    result = await conn.run(full_cmd, timeout=self.cmd_timeout, check=True)
                    if result.stderr:
                        log.warning("SSH stderr: %s", result.stderr.strip())
                    return
            except Exception as e:
                last = e
                await asyncio.sleep(0.5)
        raise last

    async def add_peer(self, public_key: str, client_ip: str) -> None:
        await self._run(
            f"{WG_BIN} set {self.interface} peer {public_key} allowed-ips {client_ip}/32"
        )

    async def remove_peer(self, public_key: str) -> None:
        try:
            await self._run(
                f"{WG_BIN} set {self.interface} peer {public_key} remove"
            )
        except Exception:
            log.warning("WG remove failed (ignored)")
