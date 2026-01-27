import asyncio
import asyncssh
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


class WireGuardSSHProvider:
    def __init__(self, host: str, port: int, user: str, password: Optional[str], interface: str = "wg0"):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.interface = interface

        # Timeouts / retries (can be overridden via env)
        self.connect_timeout = int(os.environ.get("WG_SSH_CONNECT_TIMEOUT", "15"))
        self.login_timeout = int(os.environ.get("WG_SSH_LOGIN_TIMEOUT", "15"))
        self.cmd_timeout = int(os.environ.get("WG_SSH_CMD_TIMEOUT", "15"))
        self.retries = int(os.environ.get("WG_SSH_RETRIES", "3"))

        # Railway stores the key as multiline text in env. asyncssh treats plain strings in client_keys
        # as *filenames*, so we import the key into an object.
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

    async def _connect(self) -> asyncssh.SSHClientConnection:
        if self._key_obj is not None:
            log.info("SSH auth via private key (env)")
            return await asyncssh.connect(
                self.host,
                port=self.port,
                username=self.user,
                client_keys=[self._key_obj],
                known_hosts=None,
                connect_timeout=self.connect_timeout,
                login_timeout=self.login_timeout,
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
            connect_timeout=self.connect_timeout,
            login_timeout=self.login_timeout,
            keepalive_interval=15,
            keepalive_count_max=2,
        )

    async def _run(self, cmd: str) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                async with await self._connect() as conn:
                    res = await conn.run(cmd, check=True, timeout=self.cmd_timeout)
                    if res.stderr:
                        log.info("SSH stderr cmd=%s stderr=%s", cmd, res.stderr.strip())
                    return res.stdout or ""
            except Exception as e:
                last_exc = e
                log.warning("SSH cmd failed attempt=%s/%s cmd=%s err=%r", attempt, self.retries, cmd, e)
                # small exponential backoff
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
        assert last_exc is not None
        raise last_exc

    async def add_peer(self, public_key: str, client_ip: str) -> None:
        cmd = f"wg set {self.interface} peer {public_key} allowed-ips {client_ip}/32"
        log.info("WG add peer pub=%s ip=%s", public_key, client_ip)
        await self._run(cmd)

    async def remove_peer(self, public_key: str) -> None:
        cmd = f"wg set {self.interface} peer {public_key} remove"
        log.info("WG remove peer pub=%s", public_key)
        await self._run(cmd)
