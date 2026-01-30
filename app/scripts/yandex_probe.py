from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import select

from app.db.models.yandex_account import YandexAccount
from app.db.session import session_scope
from app.services.yandex.provider import build_provider


async def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Yandex Plus + Family using Playwright cookies")
    parser.add_argument("--label", help="YandexAccount.label to probe (default: first account)", default=None)
    args = parser.parse_args()

    provider = build_provider()

    async with session_scope() as session:
        if args.label:
            q = select(YandexAccount).where(YandexAccount.label == args.label).limit(1)
        else:
            q = select(YandexAccount).order_by(YandexAccount.id.asc()).limit(1)

        res = await session.execute(q)
        acc = res.scalar_one_or_none()

        if not acc:
            raise SystemExit("No yandex account found in DB. Add one via admin panel first.")

        if not acc.credentials_ref:
            raise SystemExit(f"Account {acc.label} has no credentials_ref (storage_state file).")

        snap = await provider.probe(credentials_ref=acc.credentials_ref)

        print(
            json.dumps(
                {
                    "next_charge_text": snap.next_charge_text,
                    "next_charge_date_raw": snap.next_charge_date_raw,
                    "price_rub": snap.price_rub,
                    "family": {
                        "members": [
                            {"name": m.name, "login": m.login, "role": m.role, "status": m.status}
                            for m in (snap.family.members if snap.family else [])
                        ],
                        "pending_count": (snap.family.pending_count if snap.family else None),
                        "used_slots": (snap.family.used_slots if snap.family else None),
                        "free_slots": (snap.family.free_slots if snap.family else None),
                    }
                    if snap.family
                    else None,
                    "raw_debug": snap.raw_debug,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
