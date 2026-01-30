# app/services/yandex/provider.py
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, date, timezone
from typing import Iterable

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


PLUS_URL = "https://plus.yandex.ru/my?from=yandexid&clientSource=yandexid&clientSubSource=main"
FAMILY_URL = "https://id.yandex.ru/family"


# ----------------------------
# Models returned by provider
# ----------------------------

@dataclass
class YandexFamilyMember:
    name: str
    login: str | None = None
    role: str | None = None  # "admin" | "member" | None


@dataclass
class YandexAccountSnapshot:
    # PLUS
    next_charge_text: str | None = None          # e.g. "Спишется 9 февраля"
    next_charge_date: date | None = None         # parsed date if possible

    # FAMILY
    members: list[YandexFamilyMember] | None = None
    pending_invites: int = 0                     # count of "Ждём ответ"

    # derived
    used_slots: int = 0                          # members (excluding admin) + pending
    capacity_slots: int = 3                      # you said: max 3 invites (admin excluded)


# ----------------------------
# Parsing helpers
# ----------------------------

_RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

_LOGIN_RE = re.compile(r"^[a-zA-Z0-9._-]{3,64}$")


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def parse_ru_day_month(text: str) -> date | None:
    """
    Parses strings like:
      "Спишется 9 февраля"
      "Спишется 9\u00a0февраля"
    Returns a date with inferred year (current/next).
    """
    if not text:
        return None

    s = " ".join(text.replace("\u00a0", " ").split())
    m = re.search(r"(\d{1,2})\s+([а-яА-Я]+)", s)
    if not m:
        return None

    day = int(m.group(1))
    month_name = m.group(2).lower()
    month = _RU_MONTHS.get(month_name)
    if not month:
        return None

    today = _utc_today()
    year = today.year

    # If the month already passed this year, assume next year (simple safe inference)
    if month < today.month - 1:
        year += 1

    try:
        return date(year, month, day)
    except ValueError:
        return None


def _dedupe_members(items: Iterable[YandexFamilyMember]) -> list[YandexFamilyMember]:
    seen: set[tuple[str, str | None]] = set()
    out: list[YandexFamilyMember] = []
    for it in items:
        key = (it.name.strip(), (it.login or "").strip() or None)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _extract_members_from_text(body_text: str) -> tuple[list[YandexFamilyMember], int]:
    """
    Fallback extractor from body innerText.
    Very tolerant: tries to find patterns:
      Name\nlogin
    Also counts "Ждём ответ" as pending invite(s).
    """
    if not body_text:
        return ([], 0)

    txt = body_text.replace("\u00a0", " ")
    pending = len(re.findall(r"Жд[её]м\s+ответ", txt, flags=re.I))

    # Pattern: line with Name, next line with login (ascii)
    # We also allow Cyrillic names with spaces/dots
    candidates: list[YandexFamilyMember] = []
    rx = re.compile(r"(?m)^(?P<name>[^\n]{2,60})\n(?P<login>[a-zA-Z0-9._-]{3,64})$")
    for m in rx.finditer(txt):
        name = m.group("name").strip()
        login = m.group("login").strip()

        # Filter obvious UI words
        if any(w in name.lower() for w in ("семей", "приглас", "возможн", "настрой", "удалить", "безопас", "поддерж")):
            continue
        if not _LOGIN_RE.match(login):
            continue

        candidates.append(YandexFamilyMember(name=name, login=login))

    # Try to detect admin role from nearby "Админ"
    # (simple heuristic: if "Админ" is in the same line area)
    # We'll just keep role None here; you can enrich later if needed.

    return (_dedupe_members(candidates), pending)


def _extract_next_charge_text_from_text(body_text: str) -> str | None:
    if not body_text:
        return None

    txt = body_text.replace("\u00a0", " ")
    # primary
    m = re.search(r"(Спишется\s+\d{1,2}\s+[а-яА-Я]+)", txt, flags=re.I)
    if m:
        # normalize to original case "Спишется ..."
        s = m.group(1)
        s = " ".join(s.split())
        # Ensure first letter uppercase like UI
        if s.lower().startswith("спишется"):
            s = "Спишется" + s[len("спишется") :]
        return s

    # alternates
    m2 = re.search(r"(Оплачено\s+до\s+\d{1,2}\s+[а-яА-Я]+)", txt, flags=re.I)
    if m2:
        return " ".join(m2.group(1).split())

    m3 = re.search(r"(Следующ\w*\s+плат[её]ж[^\n]{0,40})", txt, flags=re.I)
    if m3:
        return " ".join(m3.group(1).split())

    return None


# ----------------------------
# Playwright provider
# ----------------------------

async def probe_yandex_account(storage_state_path: str, *, timeout_ms: int = 25000) -> YandexAccountSnapshot:
    """
    Server-side probe.
    Uses Playwright + storage_state.json to:
      - read next charge date/text from PLUS_URL
      - read family members + pending invites from FAMILY_URL

    Returns best-effort snapshot; never throws unless Playwright itself can't start.
    """
    snap = YandexAccountSnapshot(members=[])

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=storage_state_path)

        page = await context.new_page()

        # 1) PLUS: next charge
        try:
            await page.goto(PLUS_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(1500)  # allow UI to paint
            body_text = await page.locator("body").inner_text()
            next_text = _extract_next_charge_text_from_text(body_text)
            snap.next_charge_text = next_text
            snap.next_charge_date = parse_ru_day_month(next_text or "")
        except PlaywrightTimeoutError:
            # leave empty
            pass
        except Exception:
            pass

        # 2) FAMILY: members + pending
        try:
            await page.goto(FAMILY_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(1500)

            body_text = await page.locator("body").inner_text()

            members, pending = _extract_members_from_text(body_text)
            snap.members = members
            snap.pending_invites = pending

            # used slots: all members excluding admin (we try to detect admin by role; if unknown, assume first is admin)
            used_members = 0
            if members:
                # Heuristic: the first in list is usually admin; but if we can detect role later — refine.
                used_members = max(0, len(members) - 1)
            snap.used_slots = used_members + pending
        except PlaywrightTimeoutError:
            pass
        except Exception:
            pass

        await context.close()
        await browser.close()

    return snap
