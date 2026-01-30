from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright


PLUS_URL = "https://plus.yandex.ru/my"
FAMILY_URL = "https://id.yandex.ru/family"

_MONTHS_RU = (
    "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
)


def extract_next_charge(text: str) -> Optional[str]:
    """
    Стараемся вытащить дату следующего списания.
    На практике тексты могут отличаться, поэтому делаем несколько попыток.
    """
    # "Спишется 9 февраля"
    m = re.search(r"(Спишется\s+\d{1,2}\s+(" + _MONTHS_RU + r"))", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # "Следующий платёж ... 9 февраля"
    m2 = re.search(r"(Следующ(?:ий|ая)\s+плат[её]ж[^\n]*\d{1,2}\s+(" + _MONTHS_RU + r"))", text, re.IGNORECASE)
    if m2:
        return m2.group(1).strip()

    # "Оплачено до 9 февраля"
    m3 = re.search(r"(Оплачено\s+до\s+\d{1,2}\s+(" + _MONTHS_RU + r"))", text, re.IGNORECASE)
    if m3:
        return m3.group(1).strip()

    # Фолбек: первая строка со "Спишется"
    m4 = re.search(r"(Спишется[^\n]+)", text, re.IGNORECASE)
    return m4.group(1).strip() if m4 else None


def parse_family_min(text: str) -> dict:
    pending = len(re.findall(r"Ждём\s+ответ", text, flags=re.IGNORECASE))

    # Админ: "Админ • vladgin9" или "Админ · vladgin9"
    # Учитываем: пробелы, NBSP, разные "пули"
    admins = re.findall(r"Админ\s*[\u00A0 ]*[·•]\s*[\u00A0 ]*([a-zA-Z0-9._-]{2,128})", text)

    # Гости: ищем пары "Имя\nlogin"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    login_re = re.compile(r"^[a-z0-9][a-z0-9._-]{2,50}$", re.IGNORECASE)

    guests = []
    i = 0
    while i < len(lines) - 1:
        name = lines[i]
        maybe_login = lines[i + 1]

        # пропускаем служебные строки
        if re.search(
            r"^(Семейная\s+группа|Возможности\s+группы|Пригласить\s+близкого|Ждём\s+ответ)$",
            name,
            re.I,
        ):
            i += 1
            continue

        if login_re.match(maybe_login) and maybe_login not in admins:
            guests.append(maybe_login)
            i += 2
            continue

        i += 1

    guests = sorted(set(guests))

    used_slots = len(guests) + pending
    free_slots = max(0, 3 - used_slots)

    return {
        "admins": sorted(set(admins)),
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
            "export YANDEX_CREDENTIALS_REF='storage_state.json'"
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
            await page.wait_for_timeout(1500)

            plus_text = await page.inner_text("body")
            plus_html = await page.content()

            (debug_dir / "plus_body.txt").write_text(plus_text, encoding="utf-8")
            (debug_dir / "plus.html").write_text(plus_html, encoding="utf-8")
            await page.screenshot(path=str(debug_dir / "plus.png"), full_page=True)

            next_charge = extract_next_charge(plus_text)

            # FAMILY
            await page.goto(FAMILY_URL, wait_until="domcontentloaded", timeout=60_000)
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

            print(
                json.dumps(
                    {
                        "next_charge_text": next_charge,
                        "family": fam,
                        "debug_dir": str(debug_dir),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )

        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
