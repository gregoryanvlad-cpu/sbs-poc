from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import Iterable

from sqlalchemy import select

from app.db.models.vpn_peer import VpnPeer
from app.db.session import init_engine, session_scope
from app.services.vpn.service import VPNService

log = logging.getLogger(__name__)


def _server_by_code(vpn: VPNService, code: str) -> dict:
    code_u = str(code or '').upper()
    for srv in vpn._load_vpn_servers():
        if str(srv.get('code') or '').upper() == code_u:
            return srv
    raise RuntimeError(f'Server code not found: {code_u}')


def _provider_from_server(vpn: VPNService, srv: dict):
    return vpn._provider_for(
        host=str(srv.get('host') or ''),
        port=int(srv.get('port') or 22),
        user=str(srv.get('user') or ''),
        password=srv.get('password'),
        interface=str(srv.get('interface') or os.environ.get('VPN_INTERFACE', 'wg0')),
        tc_dev=str(srv.get('tc_dev') or srv.get('wg_tc_dev') or os.environ.get('WG_TC_DEV') or os.environ.get('VPN_TC_DEV') or ''),
        tc_parent_rate_mbit=int(srv.get('tc_parent_rate_mbit') or srv.get('wg_tc_parent_rate_mbit') or os.environ.get('WG_TC_PARENT_RATE_MBIT') or os.environ.get('VPN_TC_PARENT_RATE_MBIT') or 1000),
    )


async def apply_limits_existing(server_code: str | None = None) -> None:
    vpn = VPNService()
    async with session_scope() as session:
        q = select(VpnPeer).where(VpnPeer.is_active == True)  # noqa: E712
        if server_code:
            q = q.where(VpnPeer.server_code == str(server_code).upper())
        rows = list((await session.execute(q.order_by(VpnPeer.id.asc()))).scalars().all())

    by_code: dict[str, list[VpnPeer]] = {}
    for row in rows:
        code = str(getattr(row, 'server_code', '') or '').upper() or (os.environ.get('VPN_CODE') or 'NL').upper()
        by_code.setdefault(code, []).append(row)

    total = 0
    for code, items in by_code.items():
        srv = _server_by_code(vpn, code)
        for row in items:
            await vpn.ensure_rate_limit_for_server(
                tg_id=int(row.tg_id),
                ip=str(row.client_ip),
                host=str(srv.get('host') or ''),
                port=int(srv.get('port') or 22),
                user=str(srv.get('user') or ''),
                password=srv.get('password'),
                interface=str(srv.get('interface') or os.environ.get('VPN_INTERFACE', 'wg0')),
                tc_dev=str(srv.get('tc_dev') or srv.get('wg_tc_dev') or os.environ.get('WG_TC_DEV') or os.environ.get('VPN_TC_DEV') or ''),
                tc_parent_rate_mbit=int(srv.get('tc_parent_rate_mbit') or srv.get('wg_tc_parent_rate_mbit') or os.environ.get('WG_TC_PARENT_RATE_MBIT') or os.environ.get('VPN_TC_PARENT_RATE_MBIT') or 1000),
            )
            total += 1
            print(f'LIMIT OK server={code} tg_id={row.tg_id} ip={row.client_ip}')
    print(f'DONE apply-limits total={total}')


async def prune_orphans(server_code: str, yes: bool = False) -> None:
    vpn = VPNService()
    code = str(server_code).upper()
    srv = _server_by_code(vpn, code)
    provider = _provider_from_server(vpn, srv)

    async with session_scope() as session:
        rows = list((await session.execute(
            select(VpnPeer.client_public_key).where(VpnPeer.is_active == True, VpnPeer.server_code == code)  # noqa: E712
        )).scalars().all())
    db_keys = {str(x).strip() for x in rows if str(x).strip()}
    remote_keys = set(await provider.list_peers())
    orphans = sorted(remote_keys - db_keys)

    print(f'server={code} remote={len(remote_keys)} db_active={len(db_keys)} orphan={len(orphans)}')
    for key in orphans:
        print(f'ORPHAN {key}')

    if not yes:
        print('Dry-run only. Re-run with --yes to remove listed peers from server.')
        return

    removed = 0
    for key in orphans:
        await provider.remove_peer(key)
        removed += 1
        print(f'REMOVED {key}')
    print(f'DONE prune-orphans removed={removed}')


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='cmd', required=True)

    p1 = sub.add_parser('apply-limits-existing')
    p1.add_argument('--server', dest='server_code', default=None)

    p2 = sub.add_parser('prune-orphans')
    p2.add_argument('--server', dest='server_code', required=True)
    p2.add_argument('--yes', action='store_true')

    args = parser.parse_args()

    db_url = os.environ.get('DATABASE_URL') or os.environ.get('DATABASE_PUBLIC_URL')
    if not db_url:
        raise RuntimeError('DATABASE_URL is not set')
    init_engine(db_url)

    if args.cmd == 'apply-limits-existing':
        asyncio.run(apply_limits_existing(server_code=args.server_code))
    elif args.cmd == 'prune-orphans':
        asyncio.run(prune_orphans(server_code=args.server_code, yes=bool(args.yes)))


if __name__ == '__main__':
    main()
