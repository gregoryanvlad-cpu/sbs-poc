from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.bot.auth import is_owner
from app.bot.keyboards import kb_admin_menu
from app.core.config import settings
from app.db.models.user import User
from app.db.models.yandex_account import YandexAccount
from app.db.session import session_scope

router = Router()

PLUS_URL = "https://plus.yandex.ru/my?from=yandexid&clientSource=yandexid&clientSubSource=main"
FAMILY_URL = "https://id.yandex.ru/family"


def _safe_label_from_filename(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = base.replace(".json", "").strip()
    base = "".join(ch for ch in base if ch.isalnum() or ch in ("-", "_"))[:64]
    return base or "yandex_admin"


async def _probe_yandex_account(storage_state_path: str) -> dict:
    """
    Server-side Playwright probe:
    - plus: find "Спишется ..." (or alternatives)
    - family: count pending "Ждём ответ", estimate members logins
    Returns dict with:
      plus_line, used_slots_guests, pending_count
    """
    from playwright.async_api import async_playwright  # lazy import

    result = {
        "plus_line": None,          # type: Optional[str]
        "used_slots_guests": 0,
        "pending_count": 0,
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=storage_state_path)
        page = await context.new_page()

        # --- PLUS ---
        await page.goto(PLUS_URL, wait_until="networkidle", timeout=120_000)
        await page.wait_for_timeout(1500)
        plus_text = await page.inner_text("body")

        # main
        m = re.search(r"(Спишется\s+[^\n]+)", plus_text, flags=re.I)
        if m:
            result["plus_line"] = m.group(1).strip()
        else:
            # alternatives
            m2 = re.search(r"(Оплачено\s+до\s+[^\n]+)", plus_text, flags=re.I)
            if m2:
                result["plus_line"] = m2.group(1).strip()
            else:
                m3 = re.search(r"(Следующ(ий|ая)\s+плат(ёж|еж)[^\n]+)", plus_text, flags=re.I)
                if m3:
                    result["plus_line"] = m3.group(1).strip()

        # --- FAMILY ---
        await page.goto(FAMILY_URL, wait_until="networkidle", timeout=120_000)
        await page.wait_for_timeout(1500)
        fam_text = await page.inner_text("body")

        # pending invites
        result["pending_count"] = len(re.findall(r"Ждём\s+ответ", fam_text, flags=re.I))

        # rough member logins extraction
        # We take anything resembling a yandex login token in the text.
        login_re = re.compile(r"\b[a-z0-9][a-z0-9._-]{2,127}\b", re.I)
        tokens = login_re.findall(fam_text)

        # Filter obvious UI words that may match regex (rare, but safe)
        blacklist = set([
            "yandex", "plus", "start", "amedia", "bank", "t-bank", "history", "settings"
        ])
        tokens = [t for t in tokens if t.lower() not in blacklist]

        # Deduplicate while preserving order
        seen = set()
        logins = []
        for t in tokens:
            if t.lower() in seen:
                continue
            seen.add(t.lower())
            logins.append(t)

        # In most cases, members_total = len(actual logins shown under names).
        # We keep it conservative: if nothing parsed, assume at least admin exists.
        members_total = len(logins) if logins else 1
        result["used_slots_guests"] = max(0, members_total - 1)

        await context.close()
        await browser.close()

    return result


@router.callback_query(lambda c: c.data == "admin:menu")
async def admin_menu(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await cb.message.edit_text(
        "🛠 *Админка*\n\n"
        "Здесь ты подключаешь админские Yandex-аккаунты через cookies (storage_state.json).\n"
        "После загрузки файл автоматически проверится (Plus + Family) и обновит слоты.",
        reply_markup=kb_admin_menu(),
        parse_mode="Markdown",
    )
    await cb.answer()


@router.callback_query(lambda c: c.data == "admin:yandex:add")
async def admin_yandex_add(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    async with session_scope() as session:
        user = await session.get(User, cb.from_user.id)
        if user:
            user.flow_state = "await_admin_yandex_state"
            user.flow_data = None
            await session.commit()

    await cb.message.edit_text(
        "➕ *Добавление Yandex-аккаунта*\n\n"
        "Пришли сюда файлом `storage_state.json` (Playwright cookies).\n\n"
        "Совет:\n"
        "— Можно назвать файл как `admin1.json`, `admin2.json` и т.п.\n"
        "— label возьмём из имени файла.",
        reply_markup=kb_admin_menu(),
        parse_mode="Markdown",
    )
    await cb.answer()


@router.message(F.document)
async def admin_receive_state_file(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return

    # check flow state
    async with session_scope() as session:
        user = await session.get(User, message.from_user.id)
        if not user or user.flow_state != "await_admin_yandex_state":
            return

    doc = message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".json"):
        await message.answer("❌ Пришли файл .json (storage_state).", reply_markup=kb_admin_menu())
        return

    label = _safe_label_from_filename(doc.file_name)
    cookies_dir = Path(settings.yandex_cookies_dir)
    cookies_dir.mkdir(parents=True, exist_ok=True)

    saved_name = f"{label}.json"
    saved_path = cookies_dir / saved_name

    # download from telegram
    try:
        await message.bot.download(doc, destination=str(saved_path))
    except Exception:
        await message.answer("❌ Не смог скачать файл из Telegram. Повтори попытку.", reply_markup=kb_admin_menu())
        return

    # upsert account
    async with session_scope() as session:
        q = select(YandexAccount).where(YandexAccount.label == label).limit(1)
        res = await session.execute(q)
        acc = res.scalar_one_or_none()

        if not acc:
            acc = YandexAccount(
                label=label,
                status="active",
                max_slots=4,      # админ + 3 гостя
                used_slots=0,     # guests used
                credentials_ref=saved_name,
            )
            session.add(acc)
        else:
            acc.credentials_ref = saved_name
            acc.status = "active"
            acc.max_slots = 4

        # clear flow
        user = await session.get(User, message.from_user.id)
        if user:
            user.flow_state = None
            user.flow_data = None

        await session.commit()

    # probe via playwright (server-side)
    await message.answer("⏳ Проверяю аккаунт (Plus + Family)... Это займёт ~5–15 секунд.")

    try:
        probe = await _probe_yandex_account(str(saved_path))

        plus_line = probe.get("plus_line")
        used_slots = int(probe.get("used_slots_guests") or 0)
        pending = int(probe.get("pending_count") or 0)

        # save to DB
        async with session_scope() as session:
            q = select(YandexAccount).where(YandexAccount.label == label).limit(1)
            res = await session.execute(q)
            acc = res.scalar_one_or_none()
            if acc:
                acc.used_slots = used_slots
                acc.max_slots = 4
                # Если plus_line не найден — считаем disabled (cookies невалидны или страница не та)
                acc.status = "active" if plus_line else "disabled"
                await session.commit()

        await message.answer(
            "✅ *Yandex-аккаунт добавлен и проверен*\n\n"
            f"Label: `{label}`\n"
            f"Cookies: `{saved_name}`\n"
            f"Plus: `{plus_line or 'не найдено'}`\n"
            f"Slots (гости): `{used_slots}/3`\n"
            f"Pending: `{pending}`\n\n"
            "Дальше бот сможет выбирать этот аккаунт для выдачи инвайтов.",
            reply_markup=kb_admin_menu(),
            parse_mode="Markdown",
        )

    except Exception as e:
        await message.answer(
            "⚠️ Cookies сохранены, но проверка Playwright упала.\n\n"
            f"Ошибка: `{type(e).__name__}: {e}`\n\n"
            "Проверь, что на сервере установлен Playwright + Chromium.",
            reply_markup=kb_admin_menu(),
            parse_mode="Markdown",
        )


@router.callback_query(lambda c: c.data == "admin:yandex:list")
async def admin_yandex_list(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    async with session_scope() as session:
        q = select(YandexAccount).order_by(YandexAccount.id.asc())
        res = await session.execute(q)
        items = list(res.scalars().all())

    if not items:
        await cb.message.edit_text(
            "📋 *Yandex аккаунты*\n\nПока пусто. Нажми «Добавить Yandex-аккаунт».",
            reply_markup=kb_admin_menu(),
            parse_mode="Markdown",
        )
        await cb.answer()
        return

    lines = []
    for a in items:
        capacity = max(0, int(a.max_slots) - 1)  # минус админ
        lines.append(
            f"• `{a.label}` — {a.status} | slots: {a.used_slots}/{capacity} | plus_end: {a.plus_end_at or '—'}"
        )

    await cb.message.edit_text(
        "📋 *Yandex аккаунты*\n\n" + "\n".join(lines),
        reply_markup=kb_admin_menu(),
        parse_mode="Markdown",
    )
    await cb.answer()
