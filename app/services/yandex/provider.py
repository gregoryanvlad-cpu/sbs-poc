from __future__ import annotations

import re
import time
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
    next_charge_text: Optional[str]
    next_charge_date_raw: Optional[str]
    price_rub: Optional[int]
    family: Optional[YandexFamilySnapshot]
    raw_debug: Dict[str, Any]


# -----------------------------
# helpers
# -----------------------------

_MONTHS_RU = (
    "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
)

_LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$", re.IGNORECASE)

# ✅ ВАЖНО: принимаем ТОЛЬКО реальные invite-ссылки семьи
# Обычно они выглядят как:
# - https://id.yandex.ru/family/invite/<TOKEN>
# Иногда возможны варианты с параметрами, но токен должен быть
_INVITE_URL_RE = re.compile(
    r"https?://id\.yandex\.ru/family/invite/[^?\s\"']{6,}",
    re.IGNORECASE,
)


def _extract_next_charge(text: str) -> tuple[Optional[str], Optional[str]]:
    m = re.search(r"(Спишется\s+(\d{1,2})\s+(" + _MONTHS_RU + r"))", text, re.IGNORECASE)
    if m:
        return m.group(1).strip(), f"{m.group(2)} {m.group(3)}"

    m2 = re.search(r"(Оплачено\s+до\s+(\d{1,2})\s+(" + _MONTHS_RU + r"))", text, re.IGNORECASE)
    if m2:
        return m2.group(1).strip(), f"{m2.group(2)} {m2.group(3)}"

    m3 = re.search(
        r"(Следующ(?:ий|ая)\s+плат[её]ж.*?(\d{1,2})\s+(" + _MONTHS_RU + r"))",
        text,
        re.IGNORECASE,
    )
    if m3:
        return m3.group(0).strip(), f"{m3.group(2)} {m3.group(3)}"

    return None, None


def _extract_price_rub(text: str) -> Optional[int]:
    m = re.search(r"(\d{2,6})\s*[₽]", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _now_tag() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _find_invite_url_in_text(text: str) -> Optional[str]:
    """
    Ищем ТОЛЬКО корректный invite URL (family/invite/<token>).
    """
    if not text:
        return None
    m = _INVITE_URL_RE.search(text)
    if m:
        return m.group(0).strip()
    return None


def _parse_family_min(body_text: str) -> Tuple[List[str], List[str], int]:
    lines = [ln.strip() for ln in body_text.splitlines()]
    lines = [ln for ln in lines if ln]

    admins: List[str] = []
    guests: List[str] = []

    pending_count = 0
    for ln in lines:
        ll = ln.lower()
        if "ждём ответ" in ll or "ждем ответ" in ll or "ожидаем" in ll:
            pending_count += 1

    for ln in lines:
        m = re.search(r"Админ\s*•\s*([a-z0-9._-]{2,64})", ln, re.IGNORECASE)
        if m:
            login = m.group(1).strip()
            if _LOGIN_RE.match(login) and login not in admins:
                admins.append(login)

    for ln in lines:
        if ln.startswith("http://") or ln.startswith("https://"):
            continue
        if not _LOGIN_RE.match(ln):
            continue
        login = ln.lower()

        if login in [a.lower() for a in admins]:
            continue
        if login in ("yandex", "id", "family", "plus"):
            continue

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

    async def create_invite_link(self, *, storage_state_path: str) -> str:
        return "https://id.yandex.ru/family/invite/mocktoken123"

    async def cancel_pending_invite(self, *, storage_state_path: str, label: str | None = None, login: str | None = None) -> int:
        return 0


class PlaywrightYandexProvider:
    PLUS_URL = "https://plus.yandex.ru/my?from=yandexid&clientSource=yandexid&clientSubSource=main"
    FAMILY_URL = "https://id.yandex.ru/family"

    async def probe(self, *, storage_state_path: str) -> YandexPlusSnapshot:
        from playwright.async_api import async_playwright

        cookies_dir = Path(settings.yandex_cookies_dir)
        _ensure_dir(cookies_dir)

        storage_name = Path(storage_state_path).name.replace(".json", "")
        debug_dir = cookies_dir / "debug_out" / storage_name
        _ensure_dir(debug_dir)

        storage_state_path = str(Path(storage_state_path))

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=storage_state_path)
            page = await context.new_page()

            # PLUS
            await page.goto(self.PLUS_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1500)

            plus_body = await page.inner_text("body")
            plus_html = await page.content()

            await page.screenshot(path=str(debug_dir / "plus.png"), full_page=True)
            (debug_dir / "plus_body.txt").write_text(plus_body, encoding="utf-8")
            (debug_dir / "plus_html.html").write_text(plus_html, encoding="utf-8")

            next_line, next_date = _extract_next_charge(plus_body)
            price = _extract_price_rub(plus_body)

            # FAMILY
            await page.goto(self.FAMILY_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1500)

            family_body = await page.inner_text("body")
            family_html = await page.content()

            await page.screenshot(path=str(debug_dir / "family.png"), full_page=True)
            (debug_dir / "family_body.txt").write_text(family_body, encoding="utf-8")
            (debug_dir / "family_html.html").write_text(family_html, encoding="utf-8")

            admins, guests, pending_count = _parse_family_min(family_body)

            guest_capacity = 3
            used_slots = 1 + len(guests)
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

    async def create_invite_link(self, *, storage_state_path: str) -> str:
        """
        ✅ Возвращаем ТОЛЬКО реальную уникальную ссылку вида:
        https://id.yandex.ru/family/invite/<TOKEN>
        """
        from playwright.async_api import async_playwright

        cookies_dir = Path(settings.yandex_cookies_dir)
        _ensure_dir(cookies_dir)

        storage_name = Path(storage_state_path).name.replace(".json", "")
        debug_dir = cookies_dir / "debug_out" / storage_name / f"invite_{_now_tag()}"
        _ensure_dir(debug_dir)

        storage_state_path = str(Path(storage_state_path))

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)

            # ✅ Для чтения буфера обмена
            context = await browser.new_context(storage_state=storage_state_path)
            try:
                await context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://id.yandex.ru")
            except Exception:
                pass

            page = await context.new_page()

            await page.goto(self.FAMILY_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1200)
            await self._maybe_click_skip(page)

            await page.screenshot(path=str(debug_dir / "family_before_invite.png"), full_page=True)

            # 1) Нажимаем "Пригласить близкого"
            await self._click_invite(page)

            await page.wait_for_timeout(1200)
            await self._maybe_click_skip(page)

            await page.screenshot(path=str(debug_dir / "after_invite_click.png"), full_page=True)

            # 2) Достаём invite URL строго
            invite_link = await self._extract_invite_link_strict(page, debug_dir)

            # dump
            body = await page.inner_text("body")
            html = await page.content()
            (debug_dir / "body.txt").write_text(body, encoding="utf-8")
            (debug_dir / "html.html").write_text(html, encoding="utf-8")
            await page.screenshot(path=str(debug_dir / "final.png"), full_page=True)

            await context.close()
            await browser.close()

        if not invite_link:
            raise RuntimeError(f"Invite link not found (strict). Debug: {debug_dir}")

        return invite_link

    async def cancel_pending_invite(
        self,
        *,
        storage_state_path: str,
        label: str | None = None,
        login: str | None = None,
    ) -> int:
        from playwright.async_api import async_playwright

        cookies_dir = Path(settings.yandex_cookies_dir)
        _ensure_dir(cookies_dir)

        storage_name = Path(storage_state_path).name.replace(".json", "")
        debug_dir = cookies_dir / "debug_out" / storage_name / f"cancel_{_now_tag()}"
        _ensure_dir(debug_dir)

        storage_state_path = str(Path(storage_state_path))

        canceled = 0

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=storage_state_path)
            page = await context.new_page()

            await page.goto(self.FAMILY_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1200)
            await self._maybe_click_skip(page)

            await page.screenshot(path=str(debug_dir / "family_before_cancel.png"), full_page=True)

            # Ждём ответ -> ищем "Отменить"
            for _ in range(10):
                body_txt = (await page.inner_text("body")) or ""
                if not re.search(r"жд(ё|е)м ответ|ожидаем", body_txt, re.IGNORECASE):
                    break

                try:
                    btn = page.get_by_role("button", name=re.compile(r"Отмен", re.IGNORECASE)).first
                    await btn.click(timeout=10_000)
                    await page.wait_for_timeout(800)
                    await self._maybe_confirm_cancel(page)
                    canceled += 1
                    await page.wait_for_timeout(800)
                except Exception:
                    break

            # dump
            (debug_dir / "body.txt").write_text(await page.inner_text("body"), encoding="utf-8")
            (debug_dir / "html.html").write_text(await page.content(), encoding="utf-8")
            await page.screenshot(path=str(debug_dir / "after_cancel.png"), full_page=True)

            await context.close()
            await browser.close()

        return canceled

    # ---------- internal helpers ----------

    async def _maybe_click_skip(self, page) -> None:
        try:
            btn = page.get_by_role("button", name=re.compile(r"Пропустить", re.IGNORECASE))
            if await btn.count() > 0:
                await btn.first.click(timeout=3_000)
                await page.wait_for_timeout(600)
        except Exception:
            return

    async def _maybe_confirm_cancel(self, page) -> None:
        candidates = [
            re.compile(r"Да.*отмен", re.IGNORECASE),
            re.compile(r"Отменить приглашение", re.IGNORECASE),
            re.compile(r"Подтвердить", re.IGNORECASE),
        ]
        for rx in candidates:
            try:
                b = page.get_by_role("button", name=rx)
                if await b.count() > 0:
                    await b.first.click(timeout=3_000)
                    await page.wait_for_timeout(700)
                    return
            except Exception:
                continue

    async def _click_invite(self, page) -> None:
        # Более надёжно: пробуем кнопкой и текстом
        try:
            b = page.get_by_role("button", name=re.compile(r"Пригласить", re.IGNORECASE))
            if await b.count() > 0:
                await b.first.click(timeout=15_000)
                return
        except Exception:
            pass

        # fallback
        await page.get_by_text(re.compile(r"Пригласить близкого", re.IGNORECASE)).click(timeout=15_000)

    async def _extract_invite_link_strict(self, page, debug_dir: Path) -> Optional[str]:
        """
        ✅ Строгая логика:
        1) ищем invite URL в value input/textarea (часто там лежит)
        2) жмём "Скопировать" и читаем clipboard
        3) ищем invite URL в body/html по строгому regex
        """
        # 1) input/textarea value
        try:
            # любые input/textarea, где value содержит id.yandex.ru/family/invite/
            inp = page.locator('input, textarea')
            cnt = await inp.count()
            for i in range(min(cnt, 30)):
                node = inp.nth(i)
                try:
                    val = await node.input_value()
                except Exception:
                    try:
                        val = await node.get_attribute("value")
                    except Exception:
                        val = None

                url = _find_invite_url_in_text(val or "")
                if url:
                    return url
        except Exception:
            pass

        # 2) Нажать "Скопировать" и прочитать буфер
        try:
            copy_btn = page.get_by_role("button", name=re.compile(r"Скопир", re.IGNORECASE))
            if await copy_btn.count() > 0:
                await copy_btn.first.click(timeout=10_000)
                await page.wait_for_timeout(300)

                # читаем clipboard
                clip = await page.evaluate(
                    "() => navigator.clipboard && navigator.clipboard.readText ? navigator.clipboard.readText() : ''"
                )
                url = _find_invite_url_in_text(clip or "")
                if url:
                    return url
        except Exception:
            pass

        # 3) строгий поиск в body/html
        try:
            body = await page.inner_text("body")
            url = _find_invite_url_in_text(body)
            if url:
                return url
        except Exception:
            body = ""

        try:
            html = await page.content()
            url = _find_invite_url_in_text(html)
            if url:
                return url
        except Exception:
            html = ""

        # debug dump для диагностики “почему не нашли”
        try:
            (debug_dir / "strict_body.txt").write_text(body or "", encoding="utf-8")
            (debug_dir / "strict_html.html").write_text(html or "", encoding="utf-8")
            await page.screenshot(path=str(debug_dir / "strict_not_found.png"), full_page=True)
        except Exception:
            pass

        return None


def build_provider():
    v = (settings.yandex_provider or "mock").strip().lower()
    if v in ("playwright", "pw", "chromium", "1", "true", "yes", "on"):
        return PlaywrightYandexProvider()
    return MockYandexProvider()
