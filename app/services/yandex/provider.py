from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from app.core.config import settings

# Playwright is optional at runtime for local dev.
# We import inside PlaywrightYandexProvider to avoid import errors in environments without browsers.


# -----------------------------
# DTO
# -----------------------------

@dataclass(frozen=True)
class FamilyMember:
    name: str
    login: str
    role: str  # "admin" | "guest"
    status: str  # "active" | "pending" (pending here means "invite pending card", not a user)


@dataclass(frozen=True)
class FamilySnapshot:
    members: List[FamilyMember]
    pending_count: int
    used_slots: int
    free_slots: int
    max_guest_slots: int = 3


@dataclass(frozen=True)
class YandexPlusSnapshot:
    # Plus (/my)
    next_charge_text: Optional[str]          # например: "Спишется 9 февраля"
    next_charge_date_raw: Optional[str]      # если сможем вытащить дату как текст ("9 февраля")
    price_rub: Optional[int]                 # если найдём (например 449)

    # Family (/family)
    family: Optional[FamilySnapshot]

    # Debug / misc
    raw_debug: Dict[str, Any]


# -----------------------------
# helpers: PLUS
# -----------------------------

_MONTHS_RU = (
    "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
)

def _extract_next_charge(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Возвращает:
      - full line: "Спишется 9 февраля"
      - date part: "9 февраля"
    """
    m = re.search(r"(Спишется\s+(\d{1,2})\s+(" + _MONTHS_RU + r"))", text, re.IGNORECASE)
    if m:
        return m.group(1).strip(), f"{m.group(2)} {m.group(3)}"

    m2 = re.search(r"(Оплачено\s+до\s+(\d{1,2})\s+(" + _MONTHS_RU + r"))", text, re.IGNORECASE)
    if m2:
        return m2.group(1).strip(), f"{m2.group(2)} {m2.group(3)}"

    m3 = re.search(r"(Следующ(?:ий|ая)\s+плат[её]ж.*?(\d{1,2})\s+(" + _MONTHS_RU + r"))", text, re.IGNORECASE)
    if m3:
        return m3.group(0).strip(), f"{m3.group(2)} {m3.group(3)}"

    # last resort: any "Спишется ..." line
    m4 = re.search(r"(Спишется\s+[^\n]+)", text, re.IGNORECASE)
    if m4:
        return m4.group(1).strip(), None

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


# -----------------------------
# helpers: FAMILY
# -----------------------------

def parse_family_members(body_text: str) -> Dict[str, Any]:
    """
    Парсим список участников по тексту body (устойчиво для старта).
    По твоим скринам карточки выглядят так:

      Vlad Grigoryan
      Админ · vladgin9

      Ангелина Д.
      dereshchuk.lina

      Ждём ответ  (это pending-инвайт, не пользователь)

    Возвращаем:
      - members: [{name, login, role, status}]
      - pending_count: int
    """
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
    pending_count = 0

    for ln in lines:
        if re.search(r"Ждём\s+ответ", ln, flags=re.I):
            pending_count += 1

    login_re = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$", re.I)

    members: List[Dict[str, str]] = []

    i = 0
    while i < len(lines) - 1:
        name = lines[i]
        next_line = lines[i + 1]

        # admin marker line: "Админ · vladgin9"
        m_admin = re.search(r"Админ\s*·\s*([a-z0-9._-]{2,128})", next_line, flags=re.I)
        if m_admin:
            login = m_admin.group(1).strip()
            members.append({"name": name, "login": login, "role": "admin", "status": "active"})
            i += 2
            continue

        # guest pattern: "<Name>\n<login>"
        login = next_line
        if (
            len(name) >= 2
            and not re.search(
                r"^(Семейная\s+группа|Возможности\s+группы|Пригласить\s+близкого|Ждём\s+ответ)$",
                name,
                flags=re.I,
            )
            and login_re.match(login)
        ):
            members.append({"name": name, "login": login, "role": "guest", "status": "active"})
            i += 2
            continue

        i += 1

    # uniq by login
    uniq: Dict[str, Dict[str, str]] = {}
    for m in members:
        uniq[m["login"]] = m
    members = list(uniq.values())

    return {"members": members, "pending_count": pending_count}


def _family_snapshot_from_dict(d: Dict[str, Any]) -> FamilySnapshot:
    members = [
        FamilyMember(
            name=m.get("name", "").strip(),
            login=m.get("login", "").strip(),
            role=m.get("role", "guest"),
            status=m.get("status", "active"),
        )
        for m in d.get("members", [])
        if m.get("login")
    ]
    pending_count = int(d.get("pending_count", 0) or 0)

    # used slots = active guests + pending invites
    active_guests = sum(1 for m in members if m.role == "guest" and m.status == "active")
    used_slots = active_guests + pending_count
    free_slots = max(0, 3 - used_slots)

    return FamilySnapshot(
        members=members,
        pending_count=pending_count,
        used_slots=used_slots,
        free_slots=free_slots,
        max_guest_slots=3,
    )


# -----------------------------
# Provider interface
# -----------------------------

class BaseYandexProvider:
    async def probe(self, *, credentials_ref: str) -> YandexPlusSnapshot:
        raise NotImplementedError

    async def create_invite_link(self, *, credentials_ref: str) -> str:
        raise NotImplementedError

    async def cancel_pending_invite(self, *, credentials_ref: str) -> bool:
        raise NotImplementedError

    async def remove_member(self, *, credentials_ref: str, member_login: str) -> None:
        raise NotImplementedError


class MockYandexProvider(BaseYandexProvider):
    """
    Заглушка, чтобы:
      1) импорты не падали
      2) можно было гонять систему без Playwright
    """

    async def probe(self, *, credentials_ref: str) -> YandexPlusSnapshot:
        return YandexPlusSnapshot(
            next_charge_text="Спишется 9 февраля",
            next_charge_date_raw="9 февраля",
            price_rub=449,
            family=FamilySnapshot(members=[], pending_count=0, used_slots=0, free_slots=3),
            raw_debug={"provider": "mock", "credentials_ref": credentials_ref},
        )

    async def create_invite_link(self, *, credentials_ref: str) -> str:
        return "https://id.yandex.ru/family/invite?invite-id=MOCK"

    async def cancel_pending_invite(self, *, credentials_ref: str) -> bool:
        return True

    async def remove_member(self, *, credentials_ref: str, member_login: str) -> None:
        return None


class PlaywrightYandexProvider(BaseYandexProvider):
    # URL /my лучше оставить как у вас уже проверено на сервере
    PLUS_URL = "https://plus.yandex.ru/my"
    FAMILY_URL = "https://id.yandex.ru/family"

    def _paths(self, credentials_ref: str) -> Dict[str, Path]:
        cookies_dir = Path(settings.yandex_cookies_dir)
        cookies_dir.mkdir(parents=True, exist_ok=True)

        storage_state_path = cookies_dir / credentials_ref

        # debug folder per account (by file stem)
        debug_dir = cookies_dir / "debug" / Path(credentials_ref).stem
        debug_dir.mkdir(parents=True, exist_ok=True)

        return {"storage_state_path": storage_state_path, "debug_dir": debug_dir}

    async def probe(self, *, credentials_ref: str) -> YandexPlusSnapshot:
        paths = self._paths(credentials_ref)
        storage_state_path = paths["storage_state_path"]
        debug_dir = paths["debug_dir"]

        if not storage_state_path.exists():
            raise FileNotFoundError(f"storage_state not found: {storage_state_path}")

        from playwright.async_api import async_playwright  # lazy import

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(storage_state_path))
            page = await context.new_page()

            try:
                # -------- PLUS (/my)
                await page.goto(self.PLUS_URL, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(1500)

                plus_body_text = await page.inner_text("body")
                plus_html = await page.content()

                await page.screenshot(path=str(debug_dir / "plus_page.png"), full_page=True)
                (debug_dir / "plus_body.txt").write_text(plus_body_text, encoding="utf-8")
                (debug_dir / "plus.html").write_text(plus_html, encoding="utf-8")

                next_charge_text, next_charge_date_raw = _extract_next_charge(plus_body_text)
                price_rub = _extract_price_rub(plus_body_text)

                # -------- FAMILY (/family)
                await page.goto(self.FAMILY_URL, wait_until="domcontentloaded", timeout=60_000)
                # якорь со скрина
                try:
                    await page.get_by_text("Семейная группа").wait_for(timeout=20_000)
                except Exception:
                    # иногда текст может быть в другом виде, не фейлим тут — просто подождём чуть-чуть
                    await page.wait_for_timeout(2000)

                await page.wait_for_timeout(800)

                family_body_text = await page.inner_text("body")
                family_html = await page.content()

                await page.screenshot(path=str(debug_dir / "family_page.png"), full_page=True)
                (debug_dir / "family_body.txt").write_text(family_body_text, encoding="utf-8")
                (debug_dir / "family.html").write_text(family_html, encoding="utf-8")

                fam_dict = parse_family_members(family_body_text)
                family = _family_snapshot_from_dict(fam_dict)

                return YandexPlusSnapshot(
                    next_charge_text=next_charge_text,
                    next_charge_date_raw=next_charge_date_raw,
                    price_rub=price_rub,
                    family=family,
                    raw_debug={
                        "provider": "playwright",
                        "credentials_ref": credentials_ref,
                        "debug_dir": str(debug_dir),
                    },
                )
            finally:
                await context.close()
                await browser.close()

    async def create_invite_link(self, *, credentials_ref: str) -> str:
        """MVP: создаём инвайт-ссылку через UI (как на твоих скринах)."""
        paths = self._paths(credentials_ref)
        storage_state_path = paths["storage_state_path"]
        debug_dir = paths["debug_dir"]

        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(storage_state_path))
            page = await context.new_page()

            try:
                await page.goto(self.FAMILY_URL, wait_until="domcontentloaded", timeout=60_000)
                await page.get_by_text("Семейная группа").wait_for(timeout=20_000)

                await page.get_by_text("Пригласить близкого").click(timeout=20_000)

                # модалка "Кто этот человек для вас?" -> "Пропустить" (может быть/может не быть)
                try:
                    await page.get_by_text("Кто этот человек для вас?").wait_for(timeout=5_000)
                    await page.get_by_text("Пропустить").click(timeout=10_000)
                except Exception:
                    pass

                await page.get_by_text("Приглашение в семейную группу").wait_for(timeout=20_000)
                await page.get_by_text("Поделиться ссылкой").click(timeout=20_000)

                body = await page.inner_text("body")
                m = re.search(r"(https://id\.yandex\.ru/family/invite\?[^\s]+)", body)
                if not m:
                    await page.screenshot(path=str(debug_dir / "invite_error.png"), full_page=True)
                    (debug_dir / "invite_error_body.txt").write_text(body, encoding="utf-8")
                    raise RuntimeError("Invite link not found in modal body text")

                invite = m.group(1).strip()

                await page.screenshot(path=str(debug_dir / "invite_ok.png"), full_page=True)
                return invite
            finally:
                await context.close()
                await browser.close()

    async def cancel_pending_invite(self, *, credentials_ref: str) -> bool:
        paths = self._paths(credentials_ref)
        storage_state_path = paths["storage_state_path"]
        debug_dir = paths["debug_dir"]

        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(storage_state_path))
            page = await context.new_page()

            try:
                await page.goto(self.FAMILY_URL, wait_until="domcontentloaded", timeout=60_000)
                await page.get_by_text("Семейная группа").wait_for(timeout=20_000)

                pending = page.get_by_text("Ждём ответ")
                if await pending.count() == 0:
                    return False

                await pending.first.click(timeout=20_000)
                await page.get_by_text("Ждём ответ на приглашение").wait_for(timeout=20_000)
                await page.get_by_text("Отменить приглашение").click(timeout=20_000)

                await page.screenshot(path=str(debug_dir / "cancel_invite_ok.png"), full_page=True)
                return True
            finally:
                await context.close()
                await browser.close()

    async def remove_member(self, *, credentials_ref: str, member_login: str) -> None:
        paths = self._paths(credentials_ref)
        storage_state_path = paths["storage_state_path"]
        debug_dir = paths["debug_dir"]

        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(storage_state_path))
            page = await context.new_page()

            try:
                await page.goto(self.FAMILY_URL, wait_until="domcontentloaded", timeout=60_000)
                await page.get_by_text("Семейная группа").wait_for(timeout=20_000)

                await page.get_by_text(member_login).click(timeout=20_000)
                await page.get_by_text("Удалить из семейной группы").click(timeout=20_000)
                await page.get_by_text("Исключить").click(timeout=20_000)

                await page.screenshot(path=str(debug_dir / "remove_member_ok.png"), full_page=True)
            finally:
                await context.close()
                await browser.close()


def build_provider() -> BaseYandexProvider:
    """
    Возвращает провайдера по settings.yandex_provider.
    Поддерживаем также '1' на всякий случай, но правильно — 'playwright'.
    """
    v = (settings.yandex_provider or "mock").strip().lower()

    if v in ("playwright", "pw", "chromium", "1", "true", "yes", "on"):
        return PlaywrightYandexProvider()

    return MockYandexProvider()
