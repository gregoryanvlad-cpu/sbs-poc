import asyncio
import base64
import json
import asyncssh
import logging
import os
from textwrap import dedent
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
        kwargs = {
            "host": self.host,
            "port": self.port,
            "username": self.user,
            "known_hosts": None,
            "connect_timeout": self.connect_timeout,
            "login_timeout": self.login_timeout,
        }

        if self.password:
            kwargs["password"] = self.password

        if self._key_obj:
            kwargs["client_keys"] = [self._key_obj]

        return await asyncssh.connect(**kwargs)

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



    async def _wg_quick_save(self) -> None:
        """Best-effort runtime-to-file sync for servers with SaveConfig=true."""
        try:
            await self._run(f"sudo wg-quick save {self.interface}")
        except Exception:
            log.warning("wg_quick_save_failed interface=%s", self.interface)

    async def _update_persisted_peer(self, public_key: str, client_ip: str | None) -> None:
        """Persist peer changes directly into /etc/wireguard/<iface>.conf.

        This keeps peer add/remove operations durable even on servers where
        SaveConfig=false and `wg-quick save` is unavailable.
        """
        payload = base64.b64encode(
            json.dumps(
                {
                    "iface": self.interface,
                    "public_key": public_key,
                    "client_ip": client_ip,
                }
            ).encode("utf-8")
        ).decode("ascii")
        script = dedent(
            f"""            from pathlib import Path
            import base64
            import json

            payload = json.loads(base64.b64decode({payload!r}).decode('utf-8'))
            iface = str(payload.get('iface') or 'wg0').strip() or 'wg0'
            public_key = str(payload.get('public_key') or '').strip()
            client_ip = payload.get('client_ip')
            if client_ip is not None:
                client_ip = str(client_ip).strip()

            path = Path(f'/etc/wireguard/{{iface}}.conf')
            try:
                text = path.read_text()
            except FileNotFoundError:
                text = ''

            parts = text.split('[Peer]')
            head = parts[0]
            peer_blocks = parts[1:]
            keep = []
            for raw in peer_blocks:
                block = raw.strip()
                if not block:
                    continue
                lines = [ln.rstrip() for ln in block.splitlines()]
                block_key = None
                for ln in lines:
                    if ln.strip().startswith('PublicKey') and '=' in ln:
                        block_key = ln.split('=', 1)[1].strip()
                        break
                if block_key == public_key:
                    continue
                keep.append('[Peer]\n' + '\n'.join(lines))

            result = head.rstrip() + '\n\n'
            if keep:
                result += '\n\n'.join(keep).rstrip() + '\n'
            if client_ip:
                block = f'[Peer]\nPublicKey = {{public_key}}\nAllowedIPs = {{client_ip}}/32\n'
                result = result.rstrip() + '\n\n' + block

            path.write_text(result.rstrip() + '\n')
            """
        )
        cmd = f"""python3 - <<"PY"
{script}
PY"""
        await self._run(cmd)

    async def _get_peer_ip_by_public_key(self, public_key: str) -> str | None:
        if not public_key:
            return None
        out = await self._run_output(f"{WG_BIN} show {self.interface} allowed-ips", check=False)
        if not out:
            return None
        want = public_key.strip()
        for ln in out.splitlines():
            parts = ln.strip().split()
            if len(parts) < 2:
                continue
            if parts[0].strip() != want:
                continue
            raw_ip = parts[1].split(',')[0].strip()
            return raw_ip.split('/')[0].strip() or None
        return None

    async def add_peer(self, public_key: str, client_ip: str, *, tg_id: int | None = None) -> None:
        await self._run(
            f"{WG_BIN} set {self.interface} peer {public_key} allowed-ips {client_ip}/32"
        )
        try:
            await self._update_persisted_peer(public_key, client_ip)
        except Exception:
            log.exception("wg_persist_add_peer_failed key=%s ip=%s", public_key, client_ip)
        # Best-effort: keep all users on the same 30 Mbit plan.
        try:
            await self.tc_apply_limit_for_ip(ip=client_ip, tg_id=int(tg_id or 0))
        except Exception:
            log.exception("wg_tc_apply_limit_failed ip=%s tg_id=%s", client_ip, tg_id)
        await self._wg_quick_save()

    async def remove_peer(self, public_key: str) -> None:
        peer_ip = None
        try:
            peer_ip = await self._get_peer_ip_by_public_key(public_key)
        except Exception:
            peer_ip = None
        try:
            await self._run(
                f"{WG_BIN} set {self.interface} peer {public_key} remove"
            )
        except Exception:
            log.warning("WG remove failed (ignored)")
        try:
            await self._update_persisted_peer(public_key, None)
        except Exception:
            log.exception("wg_persist_remove_peer_failed key=%s", public_key)
        try:
            if peer_ip:
                await self.tc_clear_limit_for_ip(ip=peer_ip)
        except Exception:
            log.exception("wg_tc_clear_limit_failed ip=%s", peer_ip)
        await self._wg_quick_save()


    async def list_peers(self) -> list[str]:
        out = await self._run_output(f"{WG_BIN} show {self.interface} peers", check=False)
        if not out:
            return []
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    async def get_total_peers(self) -> int:
        out = await self._run_output(f"{WG_BIN} show {self.interface} allowed-ips", check=False)
        if not out:
            return 0
        total = 0
        for ln in out.splitlines():
            parts = ln.strip().split()
            if len(parts) < 2:
                continue
            allowed = parts[1].strip()
            if not allowed or allowed == "(none)":
                continue
            total += 1
        return total

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

    async def get_peer_transfers(self) -> dict[str, dict[str, int]]:
        """Return transfer counters for peers.

        Output of `wg show <iface> transfer`:
          <pubkey>	<rx_bytes>	<tx_bytes>
        """
        out = await self._run_output(f"{WG_BIN} show {self.interface} transfer", check=False)
        res: dict[str, dict[str, int]] = {}
        if not out:
            return res
        for ln in out.splitlines():
            parts = ln.strip().split()
            if len(parts) < 3:
                continue
            key = parts[0].strip()
            try:
                rx = int(parts[1])
            except Exception:
                rx = 0
            try:
                tx = int(parts[2])
            except Exception:
                tx = 0
            if key:
                res[key] = {"rx_bytes": rx, "tx_bytes": tx}
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

    def _tc_class_for_ip(self, ip: str) -> int:
        """Map client IP to a stable 16-bit class minor like the NL1 server.

        For 10.66.A.B -> 0xAABB. This matches the working shape/filter pattern
        already present on the first server and keeps class IDs tc-safe.
        """
        try:
            parts = [int(x) for x in (ip or '').strip().split('.')]
            if len(parts) == 4:
                return ((parts[2] & 0xFF) << 8) | (parts[3] & 0xFF)
        except Exception:
            pass
        return 0x0999

    def _tc_ifb_dev(self) -> str:
        return f"ifb-{self.interface}"

    async def _tc_init(self) -> None:
        if not self._tc_enabled:
            return
        wg_dev = self.interface
        ifb_dev = self._tc_ifb_dev()
        parent = max(1, int(self._tc_parent_rate_mbit))
        cmd = (
            "sudo modprobe ifb || true; "
            f"sudo ip link add {ifb_dev} type ifb 2>/dev/null || true; "
            f"sudo ip link set dev {ifb_dev} up 2>/dev/null || true; "
            f"sudo tc qdisc add dev {wg_dev} root handle 1: htb default 999 r2q 10 2>/dev/null || true; "
            f"sudo tc class replace dev {wg_dev} parent 1: classid 1:1 htb rate {parent}mbit ceil {parent}mbit 2>/dev/null || true; "
            f"sudo tc qdisc add dev {wg_dev} handle ffff: ingress 2>/dev/null || true; "
            f"sudo tc filter replace dev {wg_dev} parent ffff: protocol ip u32 match u32 0 0 "
            f"action mirred egress redirect dev {ifb_dev} 2>/dev/null || true; "
            f"sudo tc qdisc add dev {ifb_dev} root handle 2: htb default 999 r2q 10 2>/dev/null || true; "
            f"sudo tc class replace dev {ifb_dev} parent 2: classid 2:1 htb rate {parent}mbit ceil {parent}mbit 2>/dev/null || true"
        )
        await self._run(cmd)

    async def tc_apply_limit_for_ip(self, *, ip: str, tg_id: int = 0) -> None:
        if not self._tc_enabled:
            return
        ip = (ip or "").strip()
        if not ip:
            return
        await self._tc_init()
        wg_dev = self.interface
        ifb_dev = self._tc_ifb_dev()
        rate = max(1, int(self._tc_rate_mbit))
        cls = self._tc_class_for_ip(ip)
        cls_hex = format(cls, 'x')
        cmd = (
            f"sudo tc filter del dev {wg_dev} parent 1: protocol ip prio {cls} 2>/dev/null || true; "
            f"sudo tc filter del dev {ifb_dev} parent 2: protocol ip prio {cls} 2>/dev/null || true; "
            f"sudo tc qdisc del dev {wg_dev} parent 1:{cls_hex} 2>/dev/null || true; "
            f"sudo tc qdisc del dev {ifb_dev} parent 2:{cls_hex} 2>/dev/null || true; "
            f"sudo tc class del dev {wg_dev} classid 1:{cls_hex} 2>/dev/null || true; "
            f"sudo tc class del dev {ifb_dev} classid 2:{cls_hex} 2>/dev/null || true; "
            f"sudo tc class replace dev {wg_dev} parent 1:1 classid 1:{cls_hex} htb rate {rate}mbit ceil {rate}mbit burst 1600b cburst 1600b 2>/dev/null || true; "
            f"sudo tc qdisc add dev {wg_dev} parent 1:{cls_hex} fq_codel 2>/dev/null || true; "
            f"sudo tc filter replace dev {wg_dev} protocol ip parent 1: prio {cls} u32 match ip dst {ip}/32 flowid 1:{cls_hex} 2>/dev/null || true; "
            f"sudo tc class replace dev {ifb_dev} parent 2:1 classid 2:{cls_hex} htb rate {rate}mbit ceil {rate}mbit burst 256Kb cburst 1593b 2>/dev/null || true; "
            f"sudo tc qdisc add dev {ifb_dev} parent 2:{cls_hex} fq_codel 2>/dev/null || true; "
            f"sudo tc filter replace dev {ifb_dev} protocol ip parent 2: prio {cls} u32 match ip src {ip}/32 flowid 2:{cls_hex} 2>/dev/null || true"
        )
        await self._run(cmd)

    async def tc_clear_limit_for_ip(self, *, ip: str) -> None:
        if not self._tc_enabled:
            return
        ip = (ip or "").strip()
        if not ip:
            return
        wg_dev = self.interface
        ifb_dev = self._tc_ifb_dev()
        cls = self._tc_class_for_ip(ip)
        cls_hex = format(cls, 'x')
        cmd = (
            f"sudo tc filter del dev {wg_dev} parent 1: protocol ip prio {cls} 2>/dev/null || true; "
            f"sudo tc filter del dev {ifb_dev} parent 2: protocol ip prio {cls} 2>/dev/null || true; "
            f"sudo tc qdisc del dev {wg_dev} parent 1:{cls_hex} 2>/dev/null || true; "
            f"sudo tc qdisc del dev {ifb_dev} parent 2:{cls_hex} 2>/dev/null || true; "
            f"sudo tc class del dev {wg_dev} classid 1:{cls_hex} 2>/dev/null || true; "
            f"sudo tc class del dev {ifb_dev} classid 2:{cls_hex} 2>/dev/null || true"
        )
        await self._run(cmd)
