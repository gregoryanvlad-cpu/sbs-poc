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
    def __init__(self, host: str, port: int, user: str, password: Optional[str], interface: str = "wg0", tc_dev: Optional[str] = None, tc_parent_rate_mbit: Optional[int] = None):
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

        # Best-effort per-user bandwidth limit for WireGuard users.
        # Enabled by default so paid users and trial users have identical conditions.
        self._tc_enabled = str(os.environ.get("WG_TC_ENABLED", os.environ.get("VPN_TC_ENABLED", "1"))).strip().lower() not in {"0", "false", "no", "off"}
        self._tc_dev = (tc_dev or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or "eth0").strip() or "eth0"
        try:
            self._tc_rate_mbit = int((os.environ.get("WG_TC_RATE_MBIT") or os.environ.get("VPN_TC_RATE_MBIT") or "30").strip() or "30")
        except Exception:
            self._tc_rate_mbit = 30
        try:
            if tc_parent_rate_mbit is None:
                tc_parent_rate_mbit = int((os.environ.get("WG_TC_PARENT_RATE_MBIT") or os.environ.get("VPN_TC_PARENT_RATE_MBIT") or "1000").strip() or "1000")
            self._tc_parent_rate_mbit = max(int(tc_parent_rate_mbit), self._tc_rate_mbit)
        except Exception:
            self._tc_parent_rate_mbit = max(self._tc_rate_mbit, 1000)

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

    async def _run_output(self, cmd: str, *, check: bool = True) -> str:
        """Run a command over SSH and return stdout.

        If check=False, a non-zero exit code won't raise; stderr will be logged.
        """
        last = None
        for _ in range(self.retries):
            try:
                async with await self._connect() as conn:
                    full_cmd = f"{ENV_PATH} {cmd}"
                    try:
                        result = await conn.run(full_cmd, timeout=self.cmd_timeout, check=check)
                    except asyncssh.ProcessError as e:
                        # Surface stderr to logs (helps debug remote env differences).
                        if getattr(e, "stderr", None):
                            log.warning("SSH stderr: %s", str(e.stderr).strip())
                        raise

                    if result.stderr:
                        log.warning("SSH stderr: %s", result.stderr.strip())
                    if not check and getattr(result, "exit_status", 0) != 0:
                        log.warning("SSH non-zero exit status %s for cmd: %s", result.exit_status, cmd)
                    return (result.stdout or "").strip()
            except Exception as e:
                last = e
                await asyncio.sleep(0.5)
        raise last

    async def add_peer(self, public_key: str, client_ip: str, *, tg_id: int | None = None) -> None:
        await self._run(
            f"{WG_BIN} set {self.interface} peer {public_key} allowed-ips {client_ip}/32"
        )
        # Best-effort: keep all users on the same 30 Mbit plan.
        try:
            await self.tc_apply_limit_for_ip(ip=client_ip, tg_id=int(tg_id or 0))
        except Exception:
            log.exception("wg_tc_apply_limit_failed ip=%s tg_id=%s", client_ip, tg_id)

    async def remove_peer(self, public_key: str) -> None:
        try:
            await self._run(
                f"{WG_BIN} set {self.interface} peer {public_key} remove"
            )
        except Exception:
            log.warning("WG remove failed (ignored)")


    async def list_peers(self) -> list[str]:
        out = await self._run_output(f"{WG_BIN} show {self.interface} peers", check=False)
        if not out:
            return []
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    async def get_total_peers(self) -> int:
        out = await self._run_output(f"{WG_BIN} show {self.interface} peers")
        if not out:
            return 0
        return len([ln for ln in out.splitlines() if ln.strip()])

    async def get_active_peers(self, window_seconds: int = 180) -> int:
        # count peers with recent handshake
        cmd = (
            f"{WG_BIN} show {self.interface} latest-handshakes | "
            f"awk -v now=$(date +%s) -v w={int(window_seconds)} '$2>0 && (now-$2)<w {{c++}} END{{print c+0}}'"
        )
        out = await self._run_output(cmd)
        try:
            return int(out.strip()) if out else 0
        except Exception:
            return 0

    async def get_latest_handshakes(self) -> dict[str, int]:
        """Return latest handshake timestamps for peers.

        Output of `wg show <iface> latest-handshakes`:
          <pubkey>\t<unix_ts>
        where unix_ts can be 0 if never.
        """
        out = await self._run_output(f"{WG_BIN} show {self.interface} latest-handshakes", check=False)
        res: dict[str, int] = {}
        if not out:
            return res
        for ln in out.splitlines():
            parts = ln.strip().split()
            if len(parts) < 2:
                continue
            key = parts[0].strip()
            try:
                ts = int(parts[1])
            except Exception:
                ts = 0
            if key:
                res[key] = ts
        return res

    async def get_peer_endpoints(self) -> dict[str, str]:
        """Return endpoints for peers.

        Output of `wg show <iface> endpoints`:
          <pubkey>\t<ip:port>
        where endpoint can be "(none)".
        """
        out = await self._run_output(f"{WG_BIN} show {self.interface} endpoints", check=False)
        res: dict[str, str] = {}
        if not out:
            return res
        for ln in out.splitlines():
            parts = ln.strip().split()
            if len(parts) < 2:
                continue
            key = parts[0].strip()
            ep = parts[1].strip()
            if key:
                res[key] = ep
        return res

    async def has_peer(self, public_key: str) -> bool:
        """Check if a peer exists on the interface (best-effort)."""
        if not public_key:
            return False
        try:
            out = await self._run_output(f"{WG_BIN} show {self.interface} peers", check=False)
        except Exception:
            return False
        if not out:
            return False
        return public_key.strip() in {ln.strip() for ln in out.splitlines() if ln.strip()}

    async def get_peer_latest_handshake(self, public_key: str) -> int:
        """Return latest handshake unix ts for a specific peer (0 if never/unknown)."""
        if not public_key:
            return 0
        try:
            hs = await self.get_latest_handshakes()
        except Exception:
            return 0
        return int(hs.get(public_key, 0) or 0)

    async def get_cpu_load_percent(self, sample_seconds: int = 1) -> float:
        """Compute CPU usage percent using /proc/stat deltas.

        We intentionally do the math on *our* side instead of a remote shell
        expression: on some minimal images, shell arithmetic/awk variants can
        behave differently and produce "0" even when there is load.
        """

        def _parse_cpu_line(line: str) -> Optional[list[int]]:
            # Example: cpu  4705 0 4310 136239 52 0 103 0 0 0
            parts = line.strip().split()
            if len(parts) < 5 or parts[0] != "cpu":
                return None
            try:
                return [int(x) for x in parts[1:]]
            except Exception:
                return None

        s = max(1, int(sample_seconds))

        # Snapshot #1
        out1 = await self._run_output("head -n1 /proc/stat", check=False)
        v1 = _parse_cpu_line(out1)
        if not v1:
            return 0.0

        await asyncio.sleep(s)

        # Snapshot #2
        out2 = await self._run_output("head -n1 /proc/stat", check=False)
        v2 = _parse_cpu_line(out2)
        if not v2:
            return 0.0

        # Fields order (Linux): user nice system idle iowait irq softirq steal guest guest_nice
        # We treat idle = idle + iowait (if present).
        idle1 = v1[3] + (v1[4] if len(v1) > 4 else 0)
        idle2 = v2[3] + (v2[4] if len(v2) > 4 else 0)
        total1 = sum(v1)
        total2 = sum(v2)

        dt = total2 - total1
        didle = idle2 - idle1
        if dt <= 0:
            return 0.0

        usage = (dt - didle) / dt * 100.0
        if usage < 0:
            usage = 0.0
        if usage > 100:
            usage = 100.0
        return round(usage, 1)

    def _tc_class_for_tg(self, tg_id: int) -> int:
        base = 10
        span = 65000 - base
        return base + (int(tg_id) % span)

    async def _tc_init(self) -> None:
        if not self._tc_enabled:
            return
        dev = self._tc_dev
        parent = max(1, int(self._tc_parent_rate_mbit))
        cmd = (
            "sudo modprobe ifb || true; "
            "sudo ip link add ifb0 type ifb 2>/dev/null || true; "
            "sudo ip link set dev ifb0 up 2>/dev/null || true; "
            f"sudo tc qdisc add dev {dev} handle ffff: ingress 2>/dev/null || true; "
            f"sudo tc filter add dev {dev} parent ffff: protocol ip u32 match u32 0 0 "
            "action mirred egress redirect dev ifb0 2>/dev/null || true; "
            f"sudo tc qdisc add dev {dev} root handle 1: htb default 999 r2q 10 2>/dev/null || true; "
            f"sudo tc class add dev {dev} parent 1: classid 1:1 htb rate {parent}mbit ceil {parent}mbit 2>/dev/null || true; "
            "sudo tc qdisc add dev ifb0 root handle 2: htb default 999 r2q 10 2>/dev/null || true; "
            f"sudo tc class add dev ifb0 parent 2: classid 2:1 htb rate {parent}mbit ceil {parent}mbit 2>/dev/null || true"
        )
        await self._run(cmd)

    async def tc_apply_limit_for_ip(self, *, ip: str, tg_id: int = 0) -> None:
        if not self._tc_enabled:
            return
        ip = (ip or "").strip()
        if not ip:
            return
        await self._tc_init()
        dev = self._tc_dev
        rate = max(1, int(self._tc_rate_mbit))
        cls = self._tc_class_for_tg(int(tg_id))
        cmd = (
            f"sudo tc filter del dev {dev} parent 1: protocol ip prio {cls} 2>/dev/null || true; "
            f"sudo tc filter del dev ifb0 parent 2: protocol ip prio {cls} 2>/dev/null || true; "
            f"sudo tc class del dev {dev} classid 1:{cls} 2>/dev/null || true; "
            f"sudo tc class del dev ifb0 classid 2:{cls} 2>/dev/null || true; "
            f"sudo tc class add dev {dev} parent 1:1 classid 1:{cls} htb rate {rate}mbit ceil {rate}mbit 2>/dev/null || true; "
            f"sudo tc filter add dev {dev} protocol ip parent 1: prio {cls} u32 match ip dst {ip}/32 flowid 1:{cls} 2>/dev/null || true; "
            "sudo ip link show ifb0 >/dev/null 2>&1 || true; "
            f"sudo tc class add dev ifb0 parent 2:1 classid 2:{cls} htb rate {rate}mbit ceil {rate}mbit 2>/dev/null || true; "
            f"sudo tc filter add dev ifb0 protocol ip parent 2: prio {cls} u32 match ip src {ip}/32 flowid 2:{cls} 2>/dev/null || true"
        )
        await self._run(cmd)
