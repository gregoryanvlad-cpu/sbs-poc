from __future__ import annotations

import asyncio
import os
import json
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright


PLUS_URL = "https://plus.yandex.ru/my"
FAMILY_URL = "https://id.yandex.ru/family"


def extract_next_charge(text: str) -> Optional[str]:
    import re
    m = re.search(r"(Спишется[^\n]+)", text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def parse_family_min(text: str) -> dict:
    import re

    pending = len(re.findall(r"Ждём\s+ответ", text, flags=re.IGNORECASE))

    # Админ: "Админ · login"
    admins = re.findall(r"Админ\s*·\s*([a-zA-Z0-9._-]{2,128})", text)

    # Гости: имя + логин (очень MVP)
    # Ищем любые логины, которые похожи на yandex login
    logins = set(re.findall(r"\b[a-z0-9][a-z0-9._-]{2,50}\b", text, flags=re.IGNORECASE))

    # фильтруем мусорные слова (минимально)
    blacklist = {
        "семейная", "группа", "возможности", "пригласить", "близкого", "ждём", "ответ",
        "админ", "удалить", "из", "семейной", "исключить", "приглашение", "ссылкой"
    }
    guests = sorted([x for x in logins if x.lower() not in blacklist and x not in admins])

    # used slots: guest_count + pending
    used_slots = len(guests) + pending
    free_slots = max(0, 3 - used_slots)

    return {
        "admins": admins,
        "guests": guests,
        "pending_count": pending,
        "used_slots": used_slots,
        "free_slots": free_slots,
    }


async def main() -> None:
    cookies_dir = os.getenv("YANDEX_COOKIES_DIR", str(Path.cwd() / "yandex_cookies"))
    credentials_ref = os.getenv("YANDEX_CREDENTIALS_REF")

    if not credentials_ref:
        raise SystemExit(
            "Set env YANDEX_CREDENTIALS_REF to your storage_state filename, e.g.\n"
            "export YANDEX_CREDENTIALS_REF='storage_state.json' (or 'myacc.json')"
        )

    storage_state_path = Path(cookies_dir) / credentials_ref
    if not storage_state_path.exists():
        raise SystemExit(f"storage_state not found: {storage_state_path}")

    debug_dir = Path(cookies_dir) / "debug_local" / Path(credentials_ref).stem
    debug_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=str(storage_state_path))
        page = await context.new_page()

        try:
            # PLUS
            await page.goto(PLUS_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1200)
            plus_text = await page.inner_text("body")
            plus_html = await page.content()
            (debug_dir / "plus_body.txt").write_text(plus_text, encoding="utf-8")
            (debug_dir / "plus.html").write_text(plus_html, encoding="utf-8")
            await page.screenshot(path=str(debug_dir / "plus.png"), full_page=True)

            next_charge = extract_next_charge(plus_text)

            # FAMILY
            await page.goto(FAMILY_URL, wait_until="domcontentloaded", timeout=60_000)
            # якорь (по твоим скринам)
            try:
                await page.get_by_text("Семейная группа").wait_for(timeout=20_000)
            except Exception:
                pass

            await page.wait_for_timeout(800)
            fam_text = await page.inner_text("body")
            fam_html = await page.content()
            (debug_dir / "family_body.txt").write_text(fam_text, encoding="utf-8")
            (debug_dir / "family.html").write_text(fam_html, encoding="utf-8")
            await page.screenshot(path=str(debug_dir / "family.png"), full_page=True)

            fam = parse_family_min(fam_text)

            print(json.dumps({
                "next_charge_text": next_charge,
                "family": fam,
                "debug_dir": str(debug_dir),
            }, ensure_ascii=False, indent=2))

        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
