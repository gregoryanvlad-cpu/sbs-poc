from __future__ import annotations

from pathlib import Path

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


def _safe_label_from_filename(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = base.replace(".json", "").strip()
    base = "".join(ch for ch in base if ch.isalnum() or ch in ("-", "_"))[:64]
    return base or "yandex_admin"


@router.callback_query(lambda c: c.data == "admin:menu")
async def admin_menu(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await cb.message.edit_text(
        (
            "<b>🛠 Админка</b>\n\n"
            "Здесь ты можешь подключать админские аккаунты Яндекса "
            "через cookies (<code>storage_state.json</code>)."
        ),
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
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
        (
            "<b>➕ Добавление Yandex-аккаунта</b>\n\n"
            "Пришли файлом <code>storage_state.json</code> "
            "(cookies из Playwright).\n\n"
            "<b>Важно:</b>\n"
            "• файл должен быть <code>.json</code>\n"
            "• имя файла используется как label аккаунта\n"
        ),
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(F.document)
async def admin_receive_state_file(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return

    async with session_scope() as session:
        user = await session.get(User, message.from_user.id)
        if not user or user.flow_state != "await_admin_yandex_state":
            return

    doc = message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".json"):
        await message.answer(
            "❌ Пришли файл <code>.json</code> (storage_state).",
            reply_markup=kb_admin_menu(),
            parse_mode="HTML",
        )
        return

    label = _safe_label_from_filename(doc.file_name)
    cookies_dir = Path(settings.yandex_cookies_dir)
    cookies_dir.mkdir(parents=True, exist_ok=True)

    saved_name = f"{label}.json"
    saved_path = cookies_dir / saved_name

    await message.bot.download(doc, destination=str(saved_path))

    async with session_scope() as session:
        q = select(YandexAccount).where(YandexAccount.label == label).limit(1)
        res = await session.execute(q)
        acc = res.scalar_one_or_none()

        if not acc:
            acc = YandexAccount(
                label=label,
                status="active",
                max_slots=4,
                used_slots=0,
                credentials_ref=saved_name,
            )
            session.add(acc)
        else:
            acc.credentials_ref = saved_name
            acc.status = "active"

        user = await session.get(User, message.from_user.id)
        if user:
            user.flow_state = None
            user.flow_data = None

        await session.commit()

    await message.answer(
        (
            "✅ <b>Yandex-аккаунт добавлен</b>\n\n"
            f"Label: <code>{label}</code>\n"
            f"Файл: <code>{saved_name}</code>\n\n"
            "Аккаунт готов к использованию."
        ),
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
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
            "<b>📋 Yandex аккаунты</b>\n\nПока пусто.",
            reply_markup=kb_admin_menu(),
            parse_mode="HTML",
        )
        await cb.answer()
        return

    lines = []
    for a in items:
        capacity = max(0, int(a.max_slots) - 1)
        lines.append(
            f"• <code>{a.label}</code> — {a.status} | "
            f"slots: {a.used_slots}/{capacity}"
        )

    await cb.message.edit_text(
        "<b>📋 Yandex аккаунты</b>\n\n" + "\n".join(lines),
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()
