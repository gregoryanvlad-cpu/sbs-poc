import asyncssh
import logging
import os
from typing import Optional, Union, List

log = logging.getLogger(__name__)

class WireGuardSSHProvider:
    def __init__(self, host: str, port: int, user: str, password: Optional[str], interface: str = "wg0"):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.interface = interface

        # Railway stores the key as multiline text in env. asyncssh treats plain strings in client_keys
        # as filenames, so we must import the key object.
        self._key_obj = None
        key_text = os.environ.get("WG_SSH_PRIVATE_KEY")
        if key_text:
            key_text = key_text.strip()
            try:
                self._key_obj = asyncssh.import_private_key(key_text)
                log.info("SSH private key loaded from env (import_private_key)")
            except Exception as e:
                log.exception("Failed to import WG_SSH_PRIVATE_KEY from env: %s", e)
                self._key_obj = None

    async def _connect(self):
        if self._key_obj is not None:
            log.info("SSH auth via private key (env)")
            return await asyncssh.connect(
                self.host,
                port=self.port,
                username=self.user,
                client_keys=[self._key_obj],
                known_hosts=None,
                connect_timeout=8,
                login_timeout=8,
                keepalive_interval=15,
                keepalive_count_max=2,
            )

        log.info("SSH auth via password")
        return await asyncssh.connect(
            self.host,
            port=self.port,
            username=self.user,
            password=self.password,
            known_hosts=None,
            connect_timeout=8,
            login_timeout=8,
            keepalive_interval=15,
            keepalive_count_max=2,
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
