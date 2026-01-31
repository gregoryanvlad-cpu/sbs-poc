from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Protocol

from playwright.async_api import async_playwright, Page

from app.core.config import settings

PLUS_URL = "https://plus.yandex.ru/my"
FAMILY_URL = "https://id.yandex.ru/family"

_MONTHS_RU = (
    "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
)

INVITE_RE = re.compile(r"https://id\.yandex\.ru/family/invite\?invite-id=[a-f0-9-]{8,}", re.I)
LOGIN_LOWER_RE = re.compile(r"\b([a-z0-9][a-z0-9._-]{1,63})\b")


# ==========================
# Debug storage management
# ==========================

_DEBUG_KEEP_LAST_PER_ACCOUNT = 20
_DEBUG_MAX_TOTAL_MB = 250


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except Exception:
                pass
    except Exception:
        pass
    return total


def _safe_rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _prune_debug_root(root: Path) -> None:
    """
    Чистим debug_out, чтобы volume не забивался.
    1) На каждый аккаунт оставляем N последних папок.
    2) Если общий размер > лимита — удаляем самые старые до нормального размера.
    """
    try:
        if not root.exists() or not root.is_dir():
            return
    except Exception:
        return

    # 1) keep last per account
    try:
        for acc_dir in root.iterdir():
            try:
                if not acc_dir.is_dir():
                    continue
                runs = [p for p in acc_dir.iterdir() if p.is_dir()]
                runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                for old in runs[_DEBUG_KEEP_LAST_PER_ACCOUNT:]:
                    _safe_rmtree(old)
            except Exception:
                continue
    except Exception:
        pass

    # 2) cap total size
    try:
        limit = int(_DEBUG_MAX_TOTAL_MB) * 1024 * 1024
        total = _dir_size_bytes(root)
        if total <= limit:
            return

        all_runs: list[Path] = []
        for acc_dir in root.iterdir():
            if acc_dir.is_dir():
                for run in acc_dir.iterdir():
                    if run.is_dir():
                        all_runs.append(run)

        all_runs.sort(key=lambda p: p.stat().st_mtime)  # старые -> новые

        for run in all_runs:
            if total <= limit:
                break
            before = _dir_size_bytes(run)
            _safe_rmtree(run)
            total = max(0, total - before)
    except Exception:
        pass


# ==========================
# Data models
# ==========================

@dataclass
class YandexFamilySnapshot:
    admins: list[str]
    guests: list[str]
    pending_count: int
    used_slots: int
    free_slots: int


@dataclass
class YandexProbeSnapshot:
    next_charge_text: Optional[str]
    family: Optional[YandexFamilySnapshot]
    raw_debug: dict[str, Any]


class YandexProvider(Protocol):
    async def probe(self, *, storage_state_path: str) -> YandexProbeSnapshot: ...
    async def create_invite_link(self, *, storage_state_path: str, debug_dir_name: str = "invite", strict: bool = True) -> str: ...
    async def cancel_pending_invite(self, *, storage_state_path: str, debug_dir_name: str = "cancel_pending") -> bool: ...
    async def remove_guest(self, *, storage_state_path: str, guest_login: str) -> bool: ...


def _now_tag() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def _debug_root() -> Path:
    base = Path(settings.yandex_cookies_dir or "/data/yandex")
    return base / "debug_out"


async def _save_debug(page: Page, out_dir: Path, prefix: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        html = await page.content()
        (out_dir / f"{prefix}_page.html").write_text(html, encoding="utf-8")
    except Exception:
        pass

    try:
        body = await page.locator("body").inner_text()
        (out_dir / f"{prefix}_body.txt").write_text(body, encoding="utf-8")
    except Exception:
        pass

    try:
        await page.screenshot(path=str(out_dir / f"{prefix}.png"), full_page=True)
    except Exception:
        pass


def extract_next_charge(text: str) -> Optional[str]:
    if not text:
        return None

    m = re.search(rf"(Спишется\s+\d{{1,2}}\s+(?:{_MONTHS_RU}))", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    m2 = re.search(
        rf"((?:Следующ(?:ий|ая)\s+плат[её]ж)[^\n]*\d{{1,2}}\s+(?:{_MONTHS_RU}))",
        text,
        re.IGNORECASE,
    )
    if m2:
        return m2.group(1).strip()

    return None


def parse_family_min(text: str) -> YandexFamilySnapshot:
    admins: list[str] = []
    guests: list[str] = []
    pending_count = 0

    if not text:
        return YandexFamilySnapshot(admins=[], guests=[], pending_count=0, used_slots=0, free_slots=3)

    pending_count = len(re.findall(r"Жд[её]м\s+ответ", text, flags=re.IGNORECASE))

    for m in re.finditer(r"Админ\s*[•·]\s*([a-z0-9][a-z0-9._-]{1,63})", text, re.IGNORECASE):
        admins.append(m.group(1).lower())

    candidates = set(LOGIN_LOWER_RE.findall(text or ""))

    blacklist = {
        "yandex", "id", "family", "plus",
        "login", "admin", "pending", "invite",
        "https", "http", "ru", "com", "org", "www",
        "mailto", "support", "help", "account", "settings", "profile",
        "oauth", "token", "clientsource", "from",
        "skip", "share", "copy", "button", "link", "open", "close",
        "ok", "cancel",
    }

    filtered: list[str] = []
    for c in candidates:
        if c in blacklist:
            continue
        if c.isdigit():
            continue
        if len(c) < 3:
            continue
        if c in admins:
            continue
        filtered.append(c)

    filtered.sort()
    guests = filtered

    used_slots = (1 if admins else 0) + len(guests)
    free_slots = 4 - used_slots - pending_count
    if free_slots < 0:
        free_slots = 0

    return YandexFamilySnapshot(
        admins=admins,
        guests=guests,
        pending_count=pending_count,
        used_slots=used_slots,
        free_slots=free_slots,
    )


async def _goto(page: Page, url: str, out_dir: Path, prefix: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_load_state("networkidle", timeout=60_000)
    await _save_debug(page, out_dir, prefix)


async def _click_by_text(page: Page, text: str, out_dir: Path, prefix: str) -> bool:
    loc = page.get_by_text(text, exact=False)
    try:
        await loc.first.wait_for(state="visible", timeout=10_000)
        await loc.first.click()
        await page.wait_for_load_state("networkidle", timeout=30_000)
        await _save_debug(page, out_dir, prefix)
        return True
    except Exception:
        return False


async def _extract_invite_from_page(page: Page) -> Optional[str]:
    try:
        body = await page.locator("body").inner_text()
        m = INVITE_RE.search(body or "")
        if m:
            return m.group(0)
    except Exception:
        pass

    try:
        html = await page.content()
        m = INVITE_RE.search(html or "")
        if m:
            return m.group(0)
    except Exception:
        pass

    return None


async def _click_invite_button_strict(page: Page, out_dir: Path) -> bool:
    try:
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)
    except Exception:
        pass

    patterns = [
        re.compile(r"Пригласить близкого", re.I),
        re.compile(r"Пригласить", re.I),
        re.compile(r"Добавить.*сем(ь|ю)ю", re.I),
    ]

    candidates = []
    for pat in patterns:
        candidates.append(page.get_by_role("button", name=pat))
        candidates.append(page.get_by_role("link", name=pat))

    candidates.append(page.locator("text=Пригласить близкого"))
    candidates.append(page.locator("text=Пригласить"))

    for loc in candidates:
        try:
            if await loc.count() == 0:
                continue
            el = loc.first
            try:
                await el.scroll_into_view_if_needed(timeout=2_000)
                await page.wait_for_timeout(150)
            except Exception:
                pass

            await el.click(timeout=3_000)
            await page.wait_for_load_state("networkidle", timeout=30_000)
            await _save_debug(page, out_dir, "click_invite_OK")
            return True
        except Exception:
            continue

    await _save_debug(page, out_dir, "invite_button_NOT_FOUND")
    return False


async def _click_confirm_remove(page: Page) -> bool:
    patterns = [
        re.compile(r"Исключить", re.I),
        re.compile(r"Удалить", re.I),
        re.compile(r"Подтвердить", re.I),
    ]
    for pat in patterns:
        try:
            btn = page.get_by_role("button", name=pat)
            if await btn.count() > 0:
                await btn.first.click(timeout=3_000)
                await page.wait_for_load_state("networkidle", timeout=20_000)
                return True
        except Exception:
            continue
    return False


class PlaywrightYandexProvider:
    async def probe(self, *, storage_state_path: str) -> YandexProbeSnapshot:
        root = _debug_root()
        _prune_debug_root(root)

        debug_dir = root / Path(storage_state_path).stem / f"probe_{_now_tag()}"
        debug_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                storage_state=storage_state_path,
                viewport={"width": 1280, "height": 720},
            )
            page = await context.new_page()

            next_charge_text: Optional[str] = None
            family_snap: Optional[YandexFamilySnapshot] = None

            try:
                await _goto(page, PLUS_URL, debug_dir, "plus")
                body = await page.locator("body").inner_text()
                next_charge_text = extract_next_charge(body or "")
            except Exception:
                await _save_debug(page, debug_dir, "plus_error")

            try:
                await _goto(page, FAMILY_URL, debug_dir, "family")
                body = await page.locator("body").inner_text()
                family_snap = parse_family_min(body or "")
            except Exception:
                await _save_debug(page, debug_dir, "family_error")

            await context.close()
            await browser.close()

        return YandexProbeSnapshot(
            next_charge_text=next_charge_text,
            family=family_snap,
            raw_debug={"debug_dir": str(debug_dir)},
        )

    async def cancel_pending_invite(self, *, storage_state_path: str, debug_dir_name: str = "cancel_pending") -> bool:
        root = _debug_root()
        _prune_debug_root(root)

        debug_dir = root / Path(storage_state_path).stem / f"{debug_dir_name}_{_now_tag()}"
        debug_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                storage_state=storage_state_path,
                viewport={"width": 1280, "height": 720},
            )
            page = await context.new_page()

            await _goto(page, FAMILY_URL, debug_dir, "family_open")

            pending_loc = page.get_by_text("Ждём ответ", exact=False)
            try:
                await pending_loc.first.wait_for(state="visible", timeout=5_000)
                await pending_loc.first.click()
                await _save_debug(page, debug_dir, "pending_opened")
            except Exception:
                await _save_debug(page, debug_dir, "no_pending")
                await context.close()
                await browser.close()
                return False

            cancelled = await _click_by_text(page, "Отменить приглашение", debug_dir, "cancel_clicked")
            await context.close()
            await browser.close()
            return bool(cancelled)

    async def create_invite_link(
        self,
        *,
        storage_state_path: str,
        debug_dir_name: str = "invite",
        strict: bool = True,
    ) -> str:
        root = _debug_root()
        _prune_debug_root(root)

        debug_dir = root / Path(storage_state_path).stem / f"{debug_dir_name}_{_now_tag()}"
        debug_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                storage_state=storage_state_path,
                viewport={"width": 1280, "height": 720},
            )
            page = await context.new_page()

            await _goto(page, FAMILY_URL, debug_dir, "family_open")

            # если уже есть pending — отменяем
            try:
                pending = page.get_by_text("Ждём ответ", exact=False)
                if await pending.count() > 0:
                    await pending.first.click()
                    await _save_debug(page, debug_dir, "pending_modal_opened")
                    await _click_by_text(page, "Отменить приглашение", debug_dir, "pending_cancelled")
                    await _goto(page, FAMILY_URL, debug_dir, "family_after_cancel")
            except Exception:
                pass

            ok = await _click_invite_button_strict(page, debug_dir)
            if not ok:
                await context.close()
                await browser.close()
                if strict:
                    raise RuntimeError(f"Invite button not found. Debug: {debug_dir}")
                return ""

            await _click_by_text(page, "Пропустить", debug_dir, "relation_skipped")

            share_clicked = await _click_by_text(page, "Поделиться ссылкой", debug_dir, "share_clicked")
            if not share_clicked:
                await _save_debug(page, debug_dir, "share_button_not_found")
                await context.close()
                await browser.close()
                if strict:
                    raise RuntimeError(f"Share button not found. Debug: {debug_dir}")
                return ""

            invite_link: Optional[str] = None
            for _ in range(10):
                invite_link = await _extract_invite_from_page(page)
                if invite_link:
                    break
                try:
                    await page.wait_for_timeout(800)
                except Exception:
                    pass

            await _save_debug(page, debug_dir, "invite_final")

            await context.close()
            await browser.close()

        if not invite_link:
            if strict:
                raise RuntimeError(f"Invite link not found (strict). Debug: {debug_dir}")
            return ""
        return invite_link

    async def remove_guest(self, *, storage_state_path: str, guest_login: str) -> bool:
        guest_login = (guest_login or "").strip().lstrip("@").lower()
        if not guest_login:
            return False

        root = _debug_root()
        _prune_debug_root(root)

        debug_dir = root / Path(storage_state_path).stem / f"kick_{guest_login}_{_now_tag()}"
        debug_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                storage_state=storage_state_path,
                viewport={"width": 1280, "height": 720},
            )
            page = await context.new_page()

            await _goto(page, FAMILY_URL, debug_dir, "family_open")

            clicked_card = False
            try:
                loc = page.get_by_text(guest_login, exact=False)
                if await loc.count() > 0:
                    await loc.first.scroll_into_view_if_needed(timeout=5_000)
                    await loc.first.click(timeout=5_000)
                    await _save_debug(page, debug_dir, "guest_card_opened")
                    clicked_card = True
            except Exception:
                clicked_card = False

            if not clicked_card:
                await _save_debug(page, debug_dir, "guest_card_not_found")
                await context.close()
                await browser.close()
                return False

            removed = await _click_by_text(page, "Исключить из семьи", debug_dir, "click_remove")
            if not removed:
                removed = await _click_by_text(page, "Исключить", debug_dir, "click_remove_2")

            if not removed:
                await _save_debug(page, debug_dir, "remove_button_not_found")
                await context.close()
                await browser.close()
                return False

            await _click_confirm_remove(page)
            await _save_debug(page, debug_dir, "remove_confirmed")

            await context.close()
            await browser.close()

        return True


# ======================================================
# ✅ ВОТ ЭТОГО НЕ ХВАТАЛО: build_provider()
# ======================================================

def build_provider() -> YandexProvider:
    """
    Фабрика провайдера. Сейчас используем Playwright.
    Важно: эту функцию импортирует app.services.yandex.service
    """
    provider_name = (getattr(settings, "yandex_provider", None) or "playwright").lower()
    if provider_name == "playwright":
        return PlaywrightYandexProvider()
    # на будущее — если добавишь другой провайдер
    return PlaywrightYandexProvider()
