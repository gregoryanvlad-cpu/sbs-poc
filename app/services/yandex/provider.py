from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from playwright.async_api import async_playwright, Page

from app.core.config import settings

PLUS_URL = "https://plus.yandex.ru/my"

# запасной URL (тот, что ты прислал) — иногда у Яндекса разные редиректы/компоненты
PLUS_URL_ALT = (
    "https://plus.yandex.ru/my?"
    "utm_source=plushome&utm_medium=main_button&lk_ret_path=https%3A%2F%2Fplus.yandex.ru%2F&"
    "utm_campaign=menu&source=yandex_serp_menu_zaloginplus&state=zaloginplus&origin=serp_desktop_plus"
)

FAMILY_URL = "https://id.yandex.ru/family"

_MONTHS_RU = (
    "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
)

_MONTH_NUM_RU = {
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


def parse_plus_end_at(next_charge_text: str | None, *, now: datetime | None = None) -> Optional[datetime]:
    """Parse 'Спишется 9 февраля' into a timezone-aware datetime (UTC).

    Yandex UI typically shows the *charge date* (billing day). We treat Plus as active
    through the end of that calendar day, so returned datetime is 23:59:59 UTC.

    If year is not present in the text, we infer it: current year, or next year if the
    date has already passed.
    """
    if not next_charge_text:
        return None

    now = now or datetime.now(timezone.utc)
    text = " ".join(str(next_charge_text).strip().split())

    m = re.search(r"Спишется\s+(\d{1,2})\s+([А-Яа-я]+)(?:\s+(\d{4}))?", text, re.IGNORECASE)
    if not m:
        return None

    day = int(m.group(1))
    month_name = m.group(2).lower()
    year_str = m.group(3)

    month = _MONTH_NUM_RU.get(month_name)
    if not month:
        return None

    if year_str:
        year = int(year_str)
    else:
        year = now.year
        candidate = datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)
        if candidate < now:
            year += 1

    try:
        return datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)
    except Exception:
        return None


INVITE_RE = re.compile(r"https://id\.yandex\.ru/family/invite\?invite-id=[a-f0-9-]{8,}", re.I)
LOGIN_LOWER_RE = re.compile(r"\b([a-z0-9][a-z0-9._-]{1,63})\b")

_BAD_FAMILY_MARKERS_RE = re.compile(
    r"(войти|войдите|вход|подтвердите|captcha|капча|не\s+удалось\s+загрузить|ошибка|error|"
    r"попробуйте\s+позже|временно\s+недоступно|something\s+went\s+wrong)",
    re.I,
)

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
    plus_end_at: Optional[datetime]
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


async def _read_body_text(page: Page) -> str:
    try:
        return await page.locator("body").inner_text()
    except Exception:
        return ""


# --------------------------
# NEXT CHARGE extraction
# --------------------------

_NEXT_CHARGE_RE = re.compile(
    rf"(Спишется\s+\d{{1,2}}\s+(?:{_MONTHS_RU})(?:\s+\d{{4}})?)",
    re.I,
)

_CAPTCHA_MARKERS_RE = re.compile(
    r"(captcha|капча|подтвердите|robot|робот|я\s+не\s+робот|пройдите\s+проверку)",
    re.I,
)

async def _find_next_charge_in_frames(page: Page) -> Optional[str]:
    """
    Ищем 'Спишется ...' во всех frames (иногда Яндекс рендерит куски в iframe).
    """
    for fr in page.frames:
        try:
            # 1) locator text= regex
            loc = fr.locator("text=/Спишется\\s+\\d{1,2}\\s+(" + _MONTHS_RU + ")(\\s+\\d{4})?/i")
            if await loc.count() > 0:
                txt = (await loc.first.inner_text()).strip()
                m = _NEXT_CHARGE_RE.search(txt)
                return m.group(1).strip() if m else txt
        except Exception:
            pass

        try:
            # 2) body inner text regex
            body = await fr.locator("body").inner_text()
            m = _NEXT_CHARGE_RE.search(body or "")
            if m:
                return m.group(1).strip()
        except Exception:
            pass

        try:
            # 3) html regex
            html = await fr.content()
            m = _NEXT_CHARGE_RE.search(html or "")
            if m:
                return m.group(1).strip()
        except Exception:
            pass

    return None


async def _extract_next_charge_strict(page: Page, out_dir: Path, *, timeout_ms: int = 20_000) -> Optional[str]:
    """
    Строго пытаемся получить 'Спишется ...' с ожиданием.
    Возвращаем строку или None (если реально не нашли/капча/не тот экран).
    """
    # Быстрый детект капчи
    try:
        body = await _read_body_text(page)
        if _CAPTCHA_MARKERS_RE.search(body or ""):
            await _save_debug(page, out_dir, "plus_captcha_detected")
            return None
    except Exception:
        pass

    # 1) Ждём появления текста по locator (если SPA поздно дорисовывает)
    try:
        loc = page.locator("text=/Спишется\\s+\\d{1,2}\\s+(" + _MONTHS_RU + ")(\\s+\\d{4})?/i")
        await loc.first.wait_for(state="visible", timeout=timeout_ms)
        txt = (await loc.first.inner_text()).strip()
        m = _NEXT_CHARGE_RE.search(txt)
        return m.group(1).strip() if m else txt
    except Exception:
        pass

    # 2) Ищем в frames + regex
    txt2 = await _find_next_charge_in_frames(page)
    if txt2:
        return txt2

    # 3) Последняя попытка — прочитать body/html текущей страницы (на случай, если элемент не "visible")
    try:
        body = await _read_body_text(page)
        m = _NEXT_CHARGE_RE.search(body or "")
        if m:
            return m.group(1).strip()
    except Exception:
        pass

    try:
        html = await page.content()
        m = _NEXT_CHARGE_RE.search(html or "")
        if m:
            return m.group(1).strip()
    except Exception:
        pass

    await _save_debug(page, out_dir, "plus_next_charge_NOT_FOUND")
    return None


# --------------------------
# Family parsing
# --------------------------

def _looks_like_bad_family_page(body: str) -> bool:
    if not body:
        return True
    if len(body.strip()) < 120:
        return True
    if _BAD_FAMILY_MARKERS_RE.search(body):
        return True
    return False


def parse_family_min(text: str) -> Optional[YandexFamilySnapshot]:
    """
    Возвращает None, если по тексту видно что страница не та / не прогрузилась,
    чтобы не показывать "4 свободных" и т.п.
    """
    if not text:
        return None

    if _looks_like_bad_family_page(text):
        return None

    admins: list[str] = []
    guests: list[str] = []

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
        c = c.lower().strip()
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
    if len(filtered) > 3:
        return None

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


async def _goto_family_with_retry(page: Page, out_dir: Path) -> str:
    last_body = ""
    for attempt in range(1, 4):
        prefix = f"family_try{attempt}"
        try:
            await _goto(page, FAMILY_URL, out_dir, prefix)
        except Exception:
            await _save_debug(page, out_dir, f"{prefix}_goto_error")

        try:
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(250)
            except Exception:
                pass

            body = await _read_body_text(page)
            last_body = body or ""
            await _save_debug(page, out_dir, f"{prefix}_after_scroll")
            if not _looks_like_bad_family_page(last_body):
                return last_body
        except Exception:
            await _save_debug(page, out_dir, f"{prefix}_read_error")

        try:
            await page.wait_for_timeout(700)
        except Exception:
            pass

        try:
            await page.reload(wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_load_state("networkidle", timeout=60_000)
            await _save_debug(page, out_dir, f"{prefix}_reloaded")
        except Exception:
            await _save_debug(page, out_dir, f"{prefix}_reload_error")

    return last_body


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

        next_charge_text: Optional[str] = None
        plus_end_at: Optional[datetime] = None
        family_snap: Optional[YandexFamilySnapshot] = None
        raw_debug: dict[str, Any] = {"debug_dir": str(debug_dir)}

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                storage_state=storage_state_path,
                viewport={"width": 1280, "height": 720},
                locale="ru-RU",
            )
            page = await context.new_page()

            # -------- PLUS: строгий поиск "Спишется ..."
            plus_ok = False
            for attempt in range(1, 4):
                try:
                    url = PLUS_URL if attempt < 3 else PLUS_URL_ALT
                    await _goto(page, url, debug_dir, f"plus_try{attempt}")
                    # даём SPA дорисовать (иногда "Спишется" появляется позже networkidle)
                    await page.wait_for_timeout(1200)
                    next_charge_text = await _extract_next_charge_strict(page, debug_dir, timeout_ms=20_000)

                    raw_debug["plus_attempt"] = attempt
                    raw_debug["plus_url"] = url
                    raw_debug["next_charge_text"] = next_charge_text

                    if next_charge_text:
                        plus_end_at = parse_plus_end_at(next_charge_text, now=datetime.now(timezone.utc))
                        raw_debug["plus_end_at"] = plus_end_at.isoformat() if plus_end_at else None
                        plus_ok = bool(plus_end_at)
                        if plus_ok:
                            break
                except Exception as e:
                    raw_debug[f"plus_try{attempt}_error"] = str(e)
                    await _save_debug(page, debug_dir, f"plus_try{attempt}_error")

                # reload между попытками
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=60_000)
                    await page.wait_for_load_state("networkidle", timeout=60_000)
                    await _save_debug(page, debug_dir, f"plus_try{attempt}_reloaded")
                except Exception:
                    pass

            raw_debug["plus_ok"] = plus_ok

            # -------- FAMILY (устойчиво)
            try:
                body = await _goto_family_with_retry(page, debug_dir)
                family_snap = parse_family_min(body or "")
                if family_snap is None:
                    await _save_debug(page, debug_dir, "family_parse_failed")
            except Exception as e:
                raw_debug["family_error"] = str(e)
                await _save_debug(page, debug_dir, "family_error")
                family_snap = None

            await context.close()
            await browser.close()

        return YandexProbeSnapshot(
            next_charge_text=next_charge_text,
            plus_end_at=plus_end_at,
            family=family_snap,
            raw_debug=raw_debug,
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
                locale="ru-RU",
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
                locale="ru-RU",
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
                locale="ru-RU",
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


def build_provider() -> YandexProvider:
    provider_name = (getattr(settings, "yandex_provider", None) or "playwright").lower()
    if provider_name == "playwright":
        return PlaywrightYandexProvider()
    return PlaywrightYandexProvider()
