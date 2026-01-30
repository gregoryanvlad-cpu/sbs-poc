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


def _find_first_url(text: str) -> Optional[str]:
    """
    Ищем ссылку приглашения максимально терпимо к формату:
    - https://id.yandex.ru/family/invite/...
    - https://id.yandex.ru/family/invite?...
    - https://yandex.ru/... (иногда)
    """
    if not text:
        return None

    # сначала наиболее вероятные варианты
    patterns = [
        r"https?://id\.yandex\.ru/family/[^\s\"']+",
        r"https?://id\.yandex\.(ru|com)/[^\s\"']+invite[^\s\"']*",
        r"https?://[^\s\"']+invite[^\s\"']*",
        r"https?://[^\s\"']+",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(0).strip()

    return None


def _parse_family_min(body_text: str) -> Tuple[List[str], List[str], int]:
    """
    Минимальный парсинг страницы https://id.yandex.ru/family по ТЕКСТУ body.

    Что достаём:
      - admin login (из строки вида "Админ • vladgin9" либо рядом)
      - guest logins (видим в карточках участников)
      - pending_count (по "Ждём ответ"/"Ждем ответ")

    Делаем максимально терпимым к верстке: работаем по строкам.
    """
    lines = [ln.strip() for ln in body_text.splitlines()]
    lines = [ln for ln in lines if ln]

    admins: List[str] = []
    guests: List[str] = []

    # pending: на странице может быть "Ждём ответ", "Ждем ответ", "Ожидаем"
    pending_count = 0
    for ln in lines:
        ll = ln.lower()
        if "ждём ответ" in ll or "ждем ответ" in ll or "ожидаем" in ll:
            pending_count += 1

    # admin из "Админ • login"
    for ln in lines:
        m = re.search(r"Админ\s*•\s*([a-z0-9._-]{2,64})", ln, re.IGNORECASE)
        if m:
            login = m.group(1).strip()
            if _LOGIN_RE.match(login) and login not in admins:
                admins.append(login)

    # гости: ищем строки-логины, исключая админа
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
        return "https://id.yandex.ru/family/invite/mock"

    async def cancel_pending_invite(self, *, storage_state_path: str, label: str | None = None, login: str | None = None) -> int:
        return 0


class PlaywrightYandexProvider:
    """
    Серверный вариант: читает storage_state.json и парсит:
      - plus: https://plus.yandex.ru/my
      - family: https://id.yandex.ru/family

    Также умеет:
      - create_invite_link(): создать приглашение в семье и вернуть ссылку
      - cancel_pending_invite(): отменить pending-приглашения
    """

    PLUS_URL = "https://plus.yandex.ru/my?from=yandexid&clientSource=yandexid&clientSubSource=main"
    FAMILY_URL = "https://id.yandex.ru/family"

    # ---------- Public API ----------

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
        Создать invite-link в Яндекс Семье и вернуть URL.

        ВАЖНО:
        - Яндекс иногда показывает "Пропустить" (плашка/онбординг).
        - Ссылка может появляться как текст, как href, или в "поделиться" блоке.
        - Мы сохраняем скрины/текст/HTML для отладки.
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
            context = await browser.new_context(storage_state=storage_state_path)
            page = await context.new_page()

            await page.goto(self.FAMILY_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1200)

            # на всякий
            await self._maybe_click_skip(page, debug_dir)

            await page.screenshot(path=str(debug_dir / "family_before_invite.png"), full_page=True)

            # 1) нажимаем "Пригласить близкого"
            # делаем максимально терпимо: роль/button, текстовые варианты
            invite_btn = None
            try:
                invite_btn = page.get_by_role("button", name=re.compile(r"Пригласить", re.IGNORECASE))
                if await invite_btn.count() > 0:
                    await invite_btn.first.click(timeout=15_000)
            except Exception:
                invite_btn = None

            if invite_btn is None:
                try:
                    await page.get_by_text(re.compile(r"Пригласить близкого", re.IGNORECASE)).click(timeout=15_000)
                except Exception:
                    # фоллбек: ищем по содержимому страницы любой clickable элемент
                    await page.locator("text=Пригласить").first.click(timeout=15_000)

            await page.wait_for_timeout(1200)

            # иногда после клика снова появляется "Пропустить"
            await self._maybe_click_skip(page, debug_dir)

            await page.screenshot(path=str(debug_dir / "after_invite_click.png"), full_page=True)

            # 2) пытаемся получить ссылку несколькими способами
            invite_link = await self._extract_invite_link(page, debug_dir)

            # финальный дебаг-дамп
            body = await page.inner_text("body")
            html = await page.content()
            (debug_dir / "body.txt").write_text(body, encoding="utf-8")
            (debug_dir / "html.html").write_text(html, encoding="utf-8")
            await page.screenshot(path=str(debug_dir / "final.png"), full_page=True)

            await context.close()
            await browser.close()

        if not invite_link:
            raise RuntimeError(f"Invite link not found. Debug: {debug_dir}")

        return invite_link

    async def cancel_pending_invite(
        self,
        *,
        storage_state_path: str,
        label: str | None = None,
        login: str | None = None,
    ) -> int:
        """
        Отменяет pending-приглашения в семье.

        Возвращает количество отменённых приглашений.
        """
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

            await self._maybe_click_skip(page, debug_dir)

            await page.screenshot(path=str(debug_dir / "family_before_cancel.png"), full_page=True)

            # Идея: на странице может быть несколько pending карточек.
            # Мы пытаемся найти кнопки "Отменить" в контексте "Ждём ответ/Ожидаем"
            # и нажать их все по очереди.
            try:
                # сперва найдём все потенциальные "Отменить"
                cancel_buttons = page.get_by_role("button", name=re.compile(r"Отмен", re.IGNORECASE))
                count = await cancel_buttons.count()
            except Exception:
                count = 0

            if count == 0:
                # фоллбек по тексту
                cancel_buttons = page.locator("text=/Отмен(ить|а)/i")
                count = await cancel_buttons.count()

            # если кнопок нет — попробуем по карточкам с "Ждём ответ"
            if count == 0:
                await self._dump_page(page, debug_dir, "no_cancel_buttons")
                await context.close()
                await browser.close()
                return 0

            # кликаем все кнопки "Отменить" по одной (пересчитывая каждый раз)
            for _ in range(10):
                # проверим есть ли "ждём ответ" вообще (иначе можно случайно отменить что-то другое)
                body_txt = (await page.inner_text("body")) or ""
                if not re.search(r"жд(ё|е)м ответ|ожидаем", body_txt, re.IGNORECASE):
                    break

                try:
                    btn = page.get_by_role("button", name=re.compile(r"Отмен", re.IGNORECASE)).first
                    await btn.click(timeout=10_000)
                    await page.wait_for_timeout(800)

                    # часто Яндекс просит подтверждение в диалоге
                    await self._maybe_confirm_cancel(page, debug_dir)

                    canceled += 1
                    await page.wait_for_timeout(800)
                except Exception:
                    break

            await self._dump_page(page, debug_dir, "after_cancel")

            await context.close()
            await browser.close()

        return canceled

    # ---------- internal helpers ----------

    async def _maybe_click_skip(self, page, debug_dir: Path) -> None:
        """
        Иногда Яндекс показывает онбординг с кнопкой "Пропустить".
        """
        try:
            btn = page.get_by_role("button", name=re.compile(r"Пропустить", re.IGNORECASE))
            if await btn.count() > 0:
                await btn.first.click(timeout=3_000)
                await page.wait_for_timeout(600)
                await page.screenshot(path=str(debug_dir / "clicked_skip.png"), full_page=True)
        except Exception:
            return

    async def _maybe_confirm_cancel(self, page, debug_dir: Path) -> None:
        """
        Иногда после "Отменить" появляется подтверждение:
        - "Отменить приглашение"
        - "Да, отменить"
        - "Подтвердить"
        """
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
                    await page.screenshot(path=str(debug_dir / "confirm_cancel.png"), full_page=True)
                    return
            except Exception:
                continue

    async def _extract_invite_link(self, page, debug_dir: Path) -> Optional[str]:
        """
        Достаём invite-link максимально устойчиво:
        1) ищем href с family/invite
        2) ищем видимый текст с http(s)
        3) ищем в HTML regex
        4) пытаемся нажать "Поделиться" / "Скопировать" и снова искать
        """
        # 1) href
        try:
            a = page.locator('a[href*="family"]').first
            if await a.count() > 0:
                href = await a.get_attribute("href")
                if href and "invite" in href:
                    return href
        except Exception:
            pass

        # 2) по body
        try:
            body = await page.inner_text("body")
            url = _find_first_url(body)
            if url and "invite" in url:
                return url
        except Exception:
            body = ""

        # 3) по HTML
        try:
            html = await page.content()
            url = _find_first_url(html)
            if url and "invite" in url:
                return url
        except Exception:
            html = ""

        # 4) попытка раскрыть "Поделиться" / "Скопировать"
        for rx in [
            re.compile(r"Поделиться", re.IGNORECASE),
            re.compile(r"Скопировать", re.IGNORECASE),
            re.compile(r"Ссылка", re.IGNORECASE),
        ]:
            try:
                btn = page.get_by_role("button", name=rx)
                if await btn.count() > 0:
                    await btn.first.click(timeout=5_000)
                    await page.wait_for_timeout(700)
                    await page.screenshot(path=str(debug_dir / f"clicked_{rx.pattern}.png"), full_page=True)

                    body2 = await page.inner_text("body")
                    url2 = _find_first_url(body2) or _find_first_url(await page.content())
                    if url2 and "invite" in url2:
                        return url2
            except Exception:
                continue

        # финальный фоллбек: ищем любой URL вообще, если вдруг invite без слова invite (редко)
        url_any = _find_first_url(body or "") or _find_first_url(html or "")
        return url_any

    async def _dump_page(self, page, debug_dir: Path, tag: str) -> None:
        try:
            body = await page.inner_text("body")
            html = await page.content()
            (debug_dir / f"{tag}_body.txt").write_text(body, encoding="utf-8")
            (debug_dir / f"{tag}_html.html").write_text(html, encoding="utf-8")
            await page.screenshot(path=str(debug_dir / f"{tag}.png"), full_page=True)
        except Exception:
            return


def build_provider():
    """
    Возвращает провайдера по settings.yandex_provider.
    """
    v = (settings.yandex_provider or "mock").strip().lower()

    if v in ("playwright", "pw", "chromium", "1", "true", "yes", "on"):
        return PlaywrightYandexProvider()

    return MockYandexProvider()
