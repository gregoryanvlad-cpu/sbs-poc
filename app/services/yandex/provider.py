from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict, Any

from app.core.config import settings


# -----------------------------
# DTO
# -----------------------------

@dataclass(frozen=True)
class YandexPlusSnapshot:
    next_charge_text: Optional[str]          # например: "Спишется 9 февраля"
    next_charge_date_raw: Optional[str]      # если сможем вытащить дату как текст ("9 февраля")
    price_rub: Optional[int]                 # если найдём (например 449)
    members: List[str]                       # имена участников семьи (без админа, если сможем)
    raw_debug: Dict[str, Any]                # любые отладочные поля


# -----------------------------
# helpers
# -----------------------------

_MONTHS_RU = (
    "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
)

def _extract_next_charge(text: str) -> tuple[Optional[str], Optional[str]]:
    """
    Возвращает:
      - full line: "Спишется 9 февраля"
      - date part: "9 февраля"
    """
    m = re.search(r"(Спишется\s+(\d{1,2})\s+(" + _MONTHS_RU + r"))", text, re.IGNORECASE)
    if m:
        return m.group(1).strip(), f"{m.group(2)} {m.group(3)}"
    # альтернативы
    m2 = re.search(r"(Оплачено\s+до\s+(\d{1,2})\s+(" + _MONTHS_RU + r"))", text, re.IGNORECASE)
    if m2:
        return m2.group(1).strip(), f"{m2.group(2)} {m2.group(3)}"
    m3 = re.search(r"(Следующ(?:ий|ая)\s+плат[её]ж.*?(\d{1,2})\s+(" + _MONTHS_RU + r"))", text, re.IGNORECASE)
    if m3:
        # line целиком может быть длинной, оставим коротко:
        return m3.group(0).strip(), f"{m3.group(2)} {m3.group(3)}"
    return None, None


def _extract_price_rub(text: str) -> Optional[int]:
    # встречается: "449 ₽" или "449₽"
    m = re.search(r"(\d{2,6})\s*[₽]", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


# -----------------------------
# Providers
# -----------------------------

class MockYandexProvider:
    """
    Нужен, чтобы:
      1) импорты не падали
      2) можно было гонять систему без Playwright
    """

    async def probe(self, *, storage_state_path: str) -> YandexPlusSnapshot:
        # В mock можем вернуть заглушки
        return YandexPlusSnapshot(
            next_charge_text="Спишется 9 февраля",
            next_charge_date_raw="9 февраля",
            price_rub=449,
            members=[],
            raw_debug={"provider": "mock", "storage_state_path": storage_state_path},
        )


class PlaywrightYandexProvider:
    """
    Серверный вариант: читает storage_state.json и парсит страницу Яндекс Плюса.
    """

    PLUS_URL = "https://plus.yandex.ru/my?from=yandexid&clientSource=yandexid&clientSubSource=main"

    async def probe(self, *, storage_state_path: str) -> YandexPlusSnapshot:
        # ленивый импорт, чтобы mock не тащил playwright
        from playwright.async_api import async_playwright

        cookies_dir = Path(settings.yandex_cookies_dir)
        _ensure_dir(cookies_dir)

        debug_dir = cookies_dir / "debug_out"
        _ensure_dir(debug_dir)

        # Иногда storage_state_path приходит относительным — нормализуем
        storage_state_path = str(Path(storage_state_path))

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=storage_state_path)
            page = await context.new_page()

            await page.goto(self.PLUS_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1500)

            body_text = await page.inner_text("body")
            html = await page.content()

            # debug
            await page.screenshot(path=str(debug_dir / "page.png"), full_page=True)
            (debug_dir / "page_body.txt").write_text(body_text, encoding="utf-8")
            (debug_dir / "page_html.html").write_text(html, encoding="utf-8")

            next_line, next_date = _extract_next_charge(body_text)
            price = _extract_price_rub(body_text)

            # ⚠️ У Яндекса состав семьи часто грузится отдельно / может быть на другой странице.
            # Поэтому здесь members = [] (пока). Дальше расширим отдельным шагом парсингом family-страницы.
            members: List[str] = []

            await context.close()
            await browser.close()

        return YandexPlusSnapshot(
            next_charge_text=next_line,
            next_charge_date_raw=next_date,
            price_rub=price,
            members=members,
            raw_debug={
                "provider": "playwright",
                "debug_dir": str(debug_dir),
                "url": self.PLUS_URL,
            },
        )


def build_provider():
    """
    Возвращает провайдера по settings.yandex_provider.
    Поддерживаем также '1' на всякий случай, но правильно — 'playwright'.
    """
    v = (settings.yandex_provider or "mock").strip().lower()

    if v in ("playwright", "pw", "chromium", "1", "true", "yes", "on"):
        return PlaywrightYandexProvider()

    return MockYandexProvider()
