from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from aiogram import Bot

from sqlalchemy import select

from app.core.config import settings
from app.db.session import session_scope
from app.db.models.region_vpn_session import RegionVpnSession
from app.db.models.subscription import Subscription
from app.services.regionvpn.service import RegionVpnService

log = logging.getLogger(__name__)

# Xray access.log examples (reality/vless):
# 2026/02/15 22:03:06.330641 from 62.76.93.29:1155 accepted tcp:1.1.1.1:443 email: tg:896907140
# NOTE: Some builds may append extra fields after email, so we avoid anchoring to end-of-line.
_ACCESS_RE = re.compile(
    r"^(?P<dt>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)"
    r".*?\bfrom\s+(?P<ip>\d{1,3}(?:\.\d{1,3}){3}):\d+\s+accepted\b"
    r".*?\bemail:\s*(?P<email>\S+)"
)


def _parse_access_line(line: str) -> tuple[datetime, str, int] | None:
    m = _ACCESS_RE.match(line.strip())
    if not m:
        return None
    dt_s = m.group("dt")
    ip = m.group("ip")
    email = m.group("email")
    if not email.startswith("tg:"):
        return None
    try:
        tg_id = int(email.split(":", 1)[1])
    except Exception:
        return None
    try:
        dt = datetime.strptime(dt_s, "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return dt, ip, tg_id


async def region_session_guard_loop(bot: Bot) -> None:
    """Keeps exactly one active device per VPN-Region user.

    Policy: the most recently connected device becomes active.
    Previous device keeps the config, but its traffic is blackholed until it reconnects (and becomes the latest).
    """
    # Enabled only when VPN-Region is configured.
    if not getattr(settings, "region_ssh_host", ""):
        return
    if not getattr(settings, "region_session_guard_enabled", True):
        return

    period = max(2, int(getattr(settings, "region_session_guard_period_seconds", 5)))
    access_path = getattr(settings, "region_access_log_path", "/var/log/xray/access.log")

    svc = RegionVpnService(
        ssh_host=settings.region_ssh_host,
        ssh_port=settings.region_ssh_port,
        ssh_user=settings.region_ssh_user,
        ssh_password=settings.region_ssh_password,
        xray_config_path=settings.region_xray_config_path,
        xray_api_port=settings.region_xray_api_port,
        max_clients=settings.region_max_clients,
    )

    last_dt: datetime | None = None

    log.info("VPN-Region session guard started (period=%ss, log=%s)", period, access_path)

    while True:
        try:
            lines = await svc.tail_access_log(path=access_path, lines=250)

            latest: dict[int, tuple[datetime, str]] = {}
            for ln in lines:
                parsed = _parse_access_line(ln)
                if not parsed:
                    continue
                dt, ip, tg_id = parsed
                if last_dt and dt <= last_dt:
                    continue
                cur = latest.get(tg_id)
                if not cur or dt > cur[0]:
                    latest[tg_id] = (dt, ip)

            if latest:
                last_dt = max((dt for dt, _ in latest.values()), default=last_dt)

            switches: dict[int, str] = {}
            enabled_map: dict[int, bool] = {}
            notify: list[tuple[int, str, str]] = []  # (tg_id, old_ip, new_ip)

            async with session_scope() as s:
                # Subscriptions that are currently active
                now = datetime.utcnow()
                tg_ids = list(latest.keys())
                active_ids: set[int] = set()
                if tg_ids:
                    res = await s.execute(
                        select(Subscription.tg_id).where(
                            Subscription.tg_id.in_(tg_ids),
                            Subscription.is_active.is_(True),
                            Subscription.end_at > now,
                        )
                    )
                    active_ids = set(res.scalars().all())

                for tg_id, (dt, ip) in latest.items():
                    row = await s.get(RegionVpnSession, tg_id)
                    if not row:
                        row = RegionVpnSession(tg_id=tg_id)
                        s.add(row)

                    old_ip = (row.active_ip or "").strip() or None
                    row.last_seen_at = dt

                    # If subscription is not active, do not allow switching/activation.
                    if tg_id not in active_ids:
                        enabled_map[tg_id] = False
                        continue

                    enabled_map[tg_id] = True

                    if old_ip != ip:
                        row.active_ip = ip
                        row.last_switch_at = dt
                        switches[tg_id] = ip
                        if old_ip:
                            notify.append((tg_id, old_ip, ip))

                await s.commit()

            if enabled_map:
                # Make sure expired users are blocked and active users are unblocked
                await svc.apply_enabled_map(enabled_map)

            if switches:
                # Apply all routing changes in one restart (only for active subs)
                await svc.apply_active_ip_map({tg_id: ip for tg_id, ip in switches.items()})

                # Optional per-user bandwidth limit (tc/ifb) on the VPN-Region server.
                # This is best-effort: even if tc fails, routing policy still works.
                if getattr(settings, "region_tc_enabled", False):
                    for tg_id, ip in switches.items():
                        try:
                            await svc.tc_apply_limit_for_ip(tg_id=int(tg_id), ip=str(ip))
                        except Exception:
                            log.exception("Failed to apply tc limit for tg=%s ip=%s", tg_id, ip)

            for tg_id, old_ip, new_ip in notify:
                try:
                    text = (
                        "⚠️ *Обнаружено новое устройство*\n\n"
                        "Вы подключили *другое устройство* к *VPN-Region*.\n"
                        "Чтобы конфиг не \"шарили\", у нас действует правило: *только 1 устройство одновременно*.\n\n"
                        f"• Было активно: `{old_ip}`\n"
                        f"• Теперь активно: `{new_ip}`\n\n"
                        "✅ Новое устройство работает.\n"
                        "⛔️ На предыдущем устройстве интернет через VPN-Region перестанет работать.\n\n"
                        "_Если вы хотите вернуть доступ на первом устройстве — просто подключите VPN-Region там ещё раз._"
                    )
                    await bot.send_message(tg_id, text, parse_mode="Markdown")
                except Exception:
                    pass

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("VPN-Region session guard tick failed: %s", e)

        await asyncio.sleep(period)
