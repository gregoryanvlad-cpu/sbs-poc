from __future__ import annotations

import os
import re
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

from playwright.async_api import async_playwright, Page

from app.core.config import settings

PLUS_URL = "https://plus.yandex.ru/my"
FAMILY_URL = "https://id.yandex.ru/family"

_MONTHS_RU = (
    "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
)

INVITE_RE = re.compile(r"https://id\.yandex\.ru/family/invite\?invite-id=[a-f0-9-]{8,}", re.I)


# -----------------------------
# Data objects
# -----------------------------
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


# -----------------------------
# Helpers
# -----------------------------
def _now_tag() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def _debug_root() -> Path:
    # Обычно settings.yandex_cookies_dir == "/data/yandex"
    base = Path(settings.yandex_cookies_dir or "/data/yandex")
    return base / "debug_out"


async def _save_debug(page: Page, out_dir: Path, prefix: str) -> None:
    """
    Сохраняем: html, body.txt, screenshot
    """
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
    """
    Достаём из страницы Plus текст типа:
      - "Спишется 9 февраля"
      - "Следующий платёж ... 9 февраля"
    """
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
    """
    Минимальный парсер состава семьи из body-текста.
    На твоих скринах это строки формата:
      "Админ • vladgin9"
      "dereshchuk.lina"
      "Ждём ответ" (pending)
    """
    admins: list[str] = []
    guests: list[str] = []
    pending_count = 0

    if not text:
        return YandexFamilySnapshot(admins=[], guests=[], pending_count=0, used_slots=0, free_slots=3)

    # pending
    pending_count = len(re.findall(r"Жд[её]м\s+ответ", text, flags=re.IGNORECASE))

    # admin logins: "Админ • login"
    for m in re.finditer(r"Админ\s*[•·]\s*([a-z0-9][a-z0-9._-]{1,63})", text, re.IGNORECASE):
        admins.append(m.group(1))

    # guest logins: берём похожие на логин строки, но стараемся не схватить мусор
    candidates = set(re.findall(r"\b([a-z0-9][a-z0-9._-]{1,63})\b", text, re.IGNORECASE))
    # выкинем "admin" и прочее
    blacklist = {
        "yandex", "id", "family", "plus", "login", "admin", "pending", "invite", "https", "http"
    }
    candidates = {c for c in candidates if c.lower() not in blacklist}

    # админов исключаем из гостей
    for c in sorted(candidates):
        if c in admins:
            continue
        # грубый фильтр: логин на скрине имеет точку — но не всегда; оставим как есть
        guests.append(c)

    # used_slots: админ + guests (pending не входит в guests, но занимает слот)
    used_slots = 0
    if admins:
        used_slots += 1
    used_slots += len(guests)

    # free_slots: всего 4 (админ+3). Свободные = 4 - used_slots - pending_count
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
    # 1) из текста body
    try:
        body = await page.locator("body").inner_text()
        m = INVITE_RE.search(body or "")
        if m:
            return m.group(0)
    except Exception:
        pass

    # 2) из html
    try:
        html = await page.content()
        m = INVITE_RE.search(html or "")
        if m:
            return m.group(0)
    except Exception:
        pass

    return None


# -----------------------------
# Provider
# -----------------------------
class PlaywrightYandexProvider:
    """
    Headless Playwright provider.
    Работает с storage_state.json (cookies) и вытаскивает:
      - состав семьи
      - next charge в Plus
      - invite link из family-модалки
    """

    async def probe(self, *, storage_state_path: str) -> YandexProbeSnapshot:
        debug_dir = _debug_root() / Path(storage_state_path).stem / f"probe_{_now_tag()}"
        debug_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = await browser.new_context(storage_state=storage_state_path, viewport={"width": 1280, "height": 720})
            page = await context.new_page()

            next_charge_text: Optional[str] = None
            family_snap: Optional[YandexFamilySnapshot] = None

            # PLUS
            try:
                await _goto(page, PLUS_URL, debug_dir, "plus")
                body = await page.locator("body").inner_text()
                next_charge_text = extract_next_charge(body or "")
            except Exception:
                await _save_debug(page, debug_dir, "plus_error")

            # FAMILY
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

    async def list_family_logins(self, *, storage_state_path: str, debug_dir_name: str = "family_list") -> YandexFamilySnapshot:
        debug_dir = _debug_root() / Path(storage_state_path).stem / f"{debug_dir_name}_{_now_tag()}"
        debug_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(storage_state=storage_state_path, viewport={"width": 1280, "height": 720})
            page = await context.new_page()

            await _goto(page, FAMILY_URL, debug_dir, "family")
            body = await page.locator("body").inner_text()
            snap = parse_family_min(body or "")

            await context.close()
            await browser.close()

        return snap

    async def cancel_pending_invite(self, *, storage_state_path: str, debug_dir_name: str = "cancel_pending") -> bool:
        """
        Отменяет приглашение в статусе "Ждём ответ".
        Возвращает True если отмена была выполнена, иначе False.
        """
        debug_dir = _debug_root() / Path(storage_state_path).stem / f"{debug_dir_name}_{_now_tag()}"
        debug_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(storage_state=storage_state_path, viewport={"width": 1280, "height": 720})
            page = await context.new_page()

            await _goto(page, FAMILY_URL, debug_dir, "family_open")

            # Ищем блок "Ждём ответ"
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

            # В модалке есть кнопка "Отменить приглашение"
            cancelled = await _click_by_text(page, "Отменить приглашение", debug_dir, "cancel_clicked")
            if not cancelled:
                # Иногда кнопка может быть ниже/не влезла, пробуем ещё раз через locator по роли
                try:
                    btn = page.get_by_role("button", name=re.compile("Отменить", re.I))
                    await btn.first.click()
                    await _save_debug(page, debug_dir, "cancel_clicked_2")
                    cancelled = True
                except Exception:
                    cancelled = False

            await context.close()
            await browser.close()

        return cancelled

    async def create_invite_link(
        self,
        *,
        storage_state_path: str,
        debug_dir_name: str = "invite",
        strict: bool = True,
    ) -> str:
        """
        Создаёт приглашение через https://id.yandex.ru/family
        и возвращает ПРАВИЛЬНУЮ ссылку вида:
          https://id.yandex.ru/family/invite?invite-id=...
        """
        debug_dir = _debug_root() / Path(storage_state_path).stem / f"{debug_dir_name}_{_now_tag()}"
        debug_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(storage_state=storage_state_path, viewport={"width": 1280, "height": 720})
            page = await context.new_page()

            await _goto(page, FAMILY_URL, debug_dir, "family_open")

            # На всякий случай: если уже есть pending — отменяем (чтобы не блокировать слот)
            try:
                pending = page.get_by_text("Ждём ответ", exact=False)
                if await pending.count() > 0:
                    await pending.first.click()
                    await _save_debug(page, debug_dir, "pending_modal_opened")
                    await _click_by_text(page, "Отменить приглашение", debug_dir, "pending_cancelled")
                    # возвращаемся на family
                    await _goto(page, FAMILY_URL, debug_dir, "family_after_cancel")
            except Exception:
                pass

            # 1) "Пригласить близкого"
            ok = await _click_by_text(page, "Пригласить близкого", debug_dir, "click_invite_close_person")
            if not ok:
                # fallback: иногда это просто "+" / кнопка рядом
                try:
                    plus_btn = page.get_by_role("button", name=re.compile(r"приглас", re.I))
                    await plus_btn.first.click()
                    await _save_debug(page, debug_dir, "click_invite_fallback")
                    ok = True
                except Exception:
                    ok = False

            if not ok:
                await _save_debug(page, debug_dir, "invite_button_not_found")
                await context.close()
                await browser.close()
                if strict:
                    raise RuntimeError(f"Invite button not found. Debug: {debug_dir}")
                return ""

            # 2) Может вылезти модалка "Кто этот человек для вас?" -> жмём "Пропустить"
            # (у тебя на скрине она есть)
            await _click_by_text(page, "Пропустить", debug_dir, "relation_skipped")

            # 3) Модалка "Приглашение в семейную группу" -> "Поделиться ссылкой"
            share_clicked = await _click_by_text(page, "Поделиться ссылкой", debug_dir, "share_clicked")
            if not share_clicked:
                # иногда кнопка может быть в другом месте
                try:
                    btn = page.get_by_role("button", name=re.compile("Поделиться", re.I))
                    await btn.first.click()
                    await _save_debug(page, debug_dir, "share_clicked_2")
                    share_clicked = True
                except Exception:
                    share_clicked = False

            if not share_clicked:
                await _save_debug(page, debug_dir, "share_button_not_found")
                await context.close()
                await browser.close()
                if strict:
                    raise RuntimeError(f"Share button not found. Debug: {debug_dir}")
                return ""

            # 4) Ждём появления ссылки в модалке
            invite_link: Optional[str] = None
            for i in range(10):
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


# -----------------------------
# Factory
# -----------------------------
def build_provider() -> PlaywrightYandexProvider:
    # сейчас у нас один рабочий провайдер
    return PlaywrightYandexProvider()
