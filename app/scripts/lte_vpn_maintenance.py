from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

try:  # aiogram versions before CopyTextButton do not have this type
    from aiogram.types import CopyTextButton
except Exception:  # pragma: no cover
    CopyTextButton = None  # type: ignore[assignment]

from app.core.config import settings
from app.db.session import init_engine
from app.services.lte_vpn.service import lte_vpn_service

log = logging.getLogger(__name__)


def _init_db() -> None:
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set")
    init_engine(db_url)


async def sync_active(*, prune_stale: bool, limit: int | None) -> None:
    stats = await lte_vpn_service.sync_active_clients_to_remote(
        prune_stale_lte=bool(prune_stale),
        limit=limit,
    )
    print("LTE remote sync complete")
    for key, value in stats.items():
        print(f"{key}: {value}")


async def print_active_links(*, limit: int | None) -> None:
    rows = await lte_vpn_service.active_client_rows(limit=limit)
    print(f"active_local: {len(rows)}")
    for row in rows:
        end = row.cycle_anchor_end_at
        if end and end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        until = end.isoformat() if end else ""
        url = lte_vpn_service.build_vless_url(str(row.uuid), tg_id=int(row.tg_id))
        print(f"tg_id={row.tg_id} email={row.email} until={until} url={url}")


async def notify_active_links(*, dry_run: bool, limit: int | None, sync_first: bool) -> None:
    if sync_first:
        await sync_active(prune_stale=True, limit=limit)

    rows = await lte_vpn_service.active_client_rows(limit=limit)
    print(f"active_local: {len(rows)}")
    if dry_run:
        for row in rows:
            url = lte_vpn_service.build_vless_url(str(row.uuid), tg_id=int(row.tg_id))
            print(f"DRY tg_id={row.tg_id} url={url}")
        print("Dry-run only. Re-run with --yes to send messages.")
        return

    bot = Bot(token=settings.bot_token)
    sent = 0
    failed = 0
    try:
        for row in rows:
            tg_id = int(row.tg_id)
            url = lte_vpn_service.build_vless_url(str(row.uuid), tg_id=tg_id)
            rows_kb: list[list[InlineKeyboardButton]] = []
            if CopyTextButton is not None and 1 <= len(url) <= 256:
                try:
                    rows_kb.append([
                        InlineKeyboardButton(
                            text="📋 Скопировать новую ссылку VPN LTE",
                            copy_text=CopyTextButton(text=url),  # type: ignore[arg-type]
                        )
                    ])
                except Exception:
                    pass
            rows_kb.append([InlineKeyboardButton(text="📶 Открыть VPN LTE", callback_data="vpn:lte")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows_kb)
            text = (
                "📶 <b>VPN LTE обновлён</b>\n\n"
                "Мы перенесли LTE-сервер. Старый конфиг может перестать работать.\n"
                "Скопируйте новую ссылку и импортируйте её в Happ Plus через <b>+</b> → <b>Из буфера</b>.\n\n"
                "🔗 <b>Новая ссылка</b>\n"
                f"<code>{url}</code>\n\n"
                "<tg-spoiler>ℹ️ Сервера VPN LTE обновляются раз в 2 месяца. Если подключение перестало работать, откройте раздел VPN LTE и скопируйте актуальную ссылку.</tg-spoiler>"
            )
            try:
                await bot.send_message(tg_id, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
                sent += 1
                print(f"SENT tg_id={tg_id}")
            except Exception as exc:
                failed += 1
                print(f"FAILED tg_id={tg_id} error={exc!r}")
    finally:
        await bot.session.close()
    print(f"DONE notify-active-links sent={sent} failed={failed}")


def main() -> None:
    parser = argparse.ArgumentParser(description="VPN LTE maintenance helpers")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync-active", help="Sync all active LTE clients from DB to the current Xray server")
    p_sync.add_argument("--limit", type=int, default=None, help="Remote capacity limit. Defaults to LTE_MAX_CLIENTS")
    p_sync.add_argument(
        "--keep-stale-remote",
        action="store_true",
        help="Keep old *@lte remote clients not active in DB. By default they are pruned.",
    )

    p_links = sub.add_parser("print-active-links", help="Print current VLESS links for active LTE clients")
    p_links.add_argument("--limit", type=int, default=None)

    p_notify = sub.add_parser("notify-active-links", help="Send the current LTE VLESS link to every active LTE user")
    p_notify.add_argument("--limit", type=int, default=None)
    p_notify.add_argument("--yes", action="store_true", help="Actually send Telegram messages. Default is dry-run.")
    p_notify.add_argument("--no-sync-first", action="store_true", help="Do not sync remote Xray before sending links")

    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    _init_db()

    if args.cmd == "sync-active":
        asyncio.run(sync_active(prune_stale=not bool(args.keep_stale_remote), limit=args.limit))
    elif args.cmd == "print-active-links":
        asyncio.run(print_active_links(limit=args.limit))
    elif args.cmd == "notify-active-links":
        asyncio.run(notify_active_links(dry_run=not bool(args.yes), limit=args.limit, sync_first=not bool(args.no_sync_first)))


if __name__ == "__main__":
    main()
