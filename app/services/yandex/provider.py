from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from app.core.config import settings


# -----------------------------
# DTO
# -----------------------------

@dataclass(frozen=True)
class YandexFamilySnapshot:
    admins: List[str]
    guests: List[str]
    pending_count: int
    used_slots: int       # admin + guests (без pending)
    free_slots: int       # гостевые слоты: 3 - guests - pending
    raw_debug: Dict[str, Any]


@dataclass(frozen=True)
class YandexPlusSnapshot:
    next_charge_text: Optional[str]          # например: "Спишется 9 февраля"
    next_charge_date_raw: Optional[str]      # например: "9 февраля"
    price_rub: Optional[int]                 # если найдём (например 449)
    family: Optional[YandexFamilySnapshot]   # состав семьи / pending / слоты
    raw_debug: Dict[str, Any]                # любые отладочные поля


# -----------------------------
# helpers
# -----------------------------

_MONTHS_RU = (
    "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
)

_LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$", re.IGNORECASE)


def _extract_next_charge(text: str) -> tuple[Optional[str], Optional[str]]:
    """
    Возвращает:
      - full line: "Спишется 9 февраля"
      - date part: "9 февраля"
    """
    # "Спишется 9 февраля"
    m = re.search(r"(Спишется\s+(\d{1,2})\s+(" + _MONTHS_RU + r"))", text, re.IGNORECASE)
    if m:
        return m.group(1).strip(), f"{m.group(2)} {m.group(3)}"

    # "Оплачено до 9 февраля"
    m2 = re.search(r"(Оплачено\s+до\s+(\d{1,2})\s+(" + _MONTHS_RU + r"))", text, re.IGNORECASE)
    if m2:
        return m2.group(1).strip(), f"{m2.group(2)} {m2.group(3)}"

    # "Следующий платеж ... 9 февраля"
    m3 = re.search(
        r"(Следующ(?:ий|ая)\s+плат[её]ж.*?(\d{1,2})\s+(" + _MONTHS_RU + r"))",
        text,
        re.IGNORECASE,
    )
    if m3:
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


def _parse_family_min(body_text: str) -> Tuple[List[str], List[str], int]:
    """
    Минимальный парсинг страницы https://id.yandex.ru/family по ТЕКСТУ body.

    Что достаём:
      - admin login (из строки вида "Админ • vladgin9" либо рядом)
      - guest logins (видим в карточках участников)
      - pending_count (по "Ждём ответ"/"Ждем ответ")

    ВАЖНО:
    - делаем максимально терпимым к верстке: работаем по строкам.
    """
    lines = [ln.strip() for ln in body_text.splitlines()]
    lines = [ln for ln in lines if ln]

    admins: List[str] = []
    guests: List[str] = []

    # pending
    pending_count = 0
    for ln in lines:
        if "ждём ответ" in ln.lower() or "ждем ответ" in ln.lower():
            pending_count += 1

    # admin из "Админ • login"
    for ln in lines:
        m = re.search(r"Админ\s*•\s*([a-z0-9._-]{2,64})", ln, re.IGNORECASE)
        if m:
            login = m.group(1).strip()
            if _LOGIN_RE.match(login) and login not in admins:
                admins.append(login)

    # гости: ищем строки, которые выглядят как логин (и не url)
    # но не дублируем админа
    for i, ln in enumerate(lines):
        if ln.startswith("http://") or ln.startswith("https://"):
            continue
        if not _LOGIN_RE.match(ln):
            continue
        login = ln.lower()

        # если это админ — пропускаем
        if login in [a.lower() for a in admins]:
            continue

        # ещё один хак: иногда рядом бывают системные слова — отфильтруем
        if login in ("yandex", "id", "family", "plus"):
            continue

        # ок — это кандидат в гостя
        if login not in [g.lower() for g in guests]:
            guests.append(ln)

    return admins, guests, pending_count


# -----------------------------
# Providers
# -----------------------------

class MockYandexProvider:
    async def probe(self, *, storage_state_path: str) -> YandexPlusSnapshot:
        fam = YandexFamilySnapshot(
            admins=["admin_mock"],
            guests=["guest_mock"],
            pending_count=0,
            used_slots=2,
            free_slots=2,
            raw_debug={"provider": "mock"},
        )
        return YandexPlusSnapshot(
            next_charge_text="Спишется 9 февраля",
            next_charge_date_raw="9 февраля",
            price_rub=449,
            family=fam,
            raw_debug={"provider": "mock", "storage_state_path": storage_state_path},
        )


class PlaywrightYandexProvider:
    """
    Серверный вариант: читает storage_state.json и парсит:
      - plus: https://plus.yandex.ru/my
      - family: https://id.yandex.ru/family
    """

    PLUS_URL = "https://plus.yandex.ru/my?from=yandexid&clientSource=yandexid&clientSubSource=main"
    FAMILY_URL = "https://id.yandex.ru/family"

    async def probe(self, *, storage_state_path: str) -> YandexPlusSnapshot:
        # ленивый импорт, чтобы mock не тащил playwright
        from playwright.async_api import async_playwright

        cookies_dir = Path(settings.yandex_cookies_dir)
        _ensure_dir(cookies_dir)

        # debug складываем в отдельную папку, чтобы всегда можно было открыть логи
        storage_name = Path(storage_state_path).name.replace(".json", "")
        debug_dir = cookies_dir / "debug_out" / storage_name
        _ensure_dir(debug_dir)

        storage_state_path = str(Path(storage_state_path))

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=storage_state_path)
            page = await context.new_page()

            # -------- PLUS ----------
            await page.goto(self.PLUS_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1500)

            plus_body = await page.inner_text("body")
            plus_html = await page.content()

            await page.screenshot(path=str(debug_dir / "plus.png"), full_page=True)
            (debug_dir / "plus_body.txt").write_text(plus_body, encoding="utf-8")
            (debug_dir / "plus_html.html").write_text(plus_html, encoding="utf-8")

            next_line, next_date = _extract_next_charge(plus_body)
            price = _extract_price_rub(plus_body)

            # -------- FAMILY ----------
            await page.goto(self.FAMILY_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1500)

            family_body = await page.inner_text("body")
            family_html = await page.content()

            await page.screenshot(path=str(debug_dir / "family.png"), full_page=True)
            (debug_dir / "family_body.txt").write_text(family_body, encoding="utf-8")
            (debug_dir / "family_html.html").write_text(family_html, encoding="utf-8")

            admins, guests, pending_count = _parse_family_min(family_body)

            # слоты: у Яндекс Семьи 1 админ + 3 гостя
            guest_capacity = 3
            used_slots = 1 + len(guests) if admins else (1 + len(guests))  # админ считается всегда
            free_slots = max(0, guest_capacity - len(guests) - pending_count)

            family = YandexFamilySnapshot(
                admins=admins,
                guests=guests,
                pending_count=pending_count,
                used_slots=used_slots,
                free_slots=free_slots,
                raw_debug={"url": self.FAMILY_URL},
            )

            await context.close()
            await browser.close()

        return YandexPlusSnapshot(
            next_charge_text=next_line,
            next_charge_date_raw=next_date,
            price_rub=price,
            family=family,
            raw_debug={
                "provider": "playwright",
                "debug_dir": str(debug_dir),
                "plus_url": self.PLUS_URL,
                "family_url": self.FAMILY_URL,
            },
        )


def build_provider():
    """
    Возвращает провайдера по settings.yandex_provider.
    """
    v = (settings.yandex_provider or "mock").strip().lower()

    if v in ("playwright", "pw", "chromium", "1", "true", "yes", "on"):
        return PlaywrightYandexProvider()

    return MockYandexProvider()
