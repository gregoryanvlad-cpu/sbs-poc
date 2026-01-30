from __future__ import annotations

from pathlib import Path

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from sqlalchemy import select

from app.bot.auth import is_owner
from app.bot.keyboards import kb_admin_menu
from app.core.config import settings
from app.db.models.user import User
from app.db.models.yandex_account import YandexAccount
from app.db.session import session_scope
from app.services.yandex.provider import build_provider
from app.services.admin.reset_user import AdminResetUserService

router = Router()
_reset_service = AdminResetUserService()


# =========================
# HELPERS
# =========================

def _safe_label_from_filename(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = base.replace(".json", "").strip()
    base = "".join(ch for ch in base if ch.isalnum() or ch in ("-", "_"))[:64]
    return base or "yandex_admin"


# =========================
# ADMIN MENU
# =========================

@router.callback_query(lambda c: c.data == "admin:menu")
async def admin_menu(cb: CallbackQuery) -> None:
    await cb.answer()

    if not is_owner(cb.from_user.id):
        return

    await cb.message.edit_text(
        "🛠 <b>Админка</b>",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )


# =========================
# ADD YANDEX ACCOUNT
# =========================

@router.callback_query(lambda c: c.data == "admin:yandex:add")
async def admin_yandex_add(cb: CallbackQuery) -> None:
    await cb.answer()

    if not is_owner(cb.from_user.id):
        return

    async with session_scope() as session:
        user = await session.get(User, cb.from_user.id)
        if user:
            user.flow_state = "await_admin_yandex_state"
            user.flow_data = None
            await session.commit()

    await cb.message.edit_text(
        "➕ <b>Добавление Yandex-аккаунта</b>\n\n"
        "Пришли файлом <code>storage_state.json</code> (Playwright cookies).",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )


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
        await message.answer("❌ Пришли файл .json", reply_markup=kb_admin_menu())
        return

    label = _safe_label_from_filename(doc.file_name)
    cookies_dir = Path(settings.yandex_cookies_dir)
    cookies_dir.mkdir(parents=True, exist_ok=True)

    saved_name = f"{label}.json"
    saved_path = cookies_dir / saved_name

    await message.bot.download(doc, destination=str(saved_path))

    async with session_scope() as session:
        q = select(YandexAccount).where(YandexAccount.label == label).limit(1)
        acc = (await session.execute(q)).scalar_one_or_none()

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
        f"✅ <b>Yandex-аккаунт добавлен</b>\n\n"
        f"Label: <code>{label}</code>",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )


# =========================
# LIST YANDEX ACCOUNTS
# =========================

@router.callback_query(lambda c: c.data == "admin:yandex:list")
async def admin_yandex_list(cb: CallbackQuery) -> None:
    await cb.answer()

    if not is_owner(cb.from_user.id):
        return

    async with session_scope() as session:
        items = (await session.execute(select(YandexAccount))).scalars().all()

    if not items:
        await cb.message.edit_text(
            "📋 Пусто",
            reply_markup=kb_admin_menu(),
            parse_mode="HTML",
        )
        return

    text = "📋 <b>Yandex аккаунты</b>\n\n"
    for a in items:
        text += (
            f"• <code>{a.label}</code> | "
            f"{a.used_slots}/{a.max_slots - 1} | "
            f"{a.status}\n"
        )

    await cb.message.edit_text(text, reply_markup=kb_admin_menu(), parse_mode="HTML")


# =========================
# PROBE YANDEX ACCOUNT
# =========================

@router.callback_query(lambda c: c.data == "admin:yandex:probe")
async def admin_yandex_probe(cb: CallbackQuery) -> None:
    await cb.answer("Проверяю…")

    if not is_owner(cb.from_user.id):
        return

    async with session_scope() as session:
        acc = (
            await session.execute(
                select(YandexAccount)
                .where(YandexAccount.status == "active")
                .order_by(YandexAccount.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if not acc:
        await cb.message.edit_text("❌ Нет активных аккаунтов", reply_markup=kb_admin_menu())
        return

    provider = build_provider()
    path = str(Path(settings.yandex_cookies_dir) / acc.credentials_ref)

    snap = await provider.probe(storage_state_path=path)
    fam = snap.family

    await cb.message.edit_text(
        "✅ <b>Yandex аккаунт</b>\n\n"
        f"Админ: <code>{', '.join(fam.admins)}</code>\n"
        f"Гости: <code>{', '.join(fam.guests)}</code>\n"
        f"Pending: <b>{fam.pending_count}</b>\n"
        f"Free slots: <b>{fam.free_slots}</b>",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )


# =========================
# FULL USER RESET (TEST)
# =========================

class AdminResetFSM(StatesGroup):
    waiting_tg_id = State()


@router.callback_query(lambda c: c.data == "admin:reset:user")
async def admin_reset_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()

    if not is_owner(cb.from_user.id):
        return

    await state.set_state(AdminResetFSM.waiting_tg_id)

    await cb.message.answer(
        "🧨 <b>Полный сброс пользователя</b>\n\n"
        "Пришли <code>tg_id</code>.\n"
        "⚠️ Будет удалено ВСЁ.",
        parse_mode="HTML",
    )


@router.message(AdminResetFSM.waiting_tg_id)
async def admin_reset_confirm(msg: Message, state: FSMContext) -> None:
    if not is_owner(msg.from_user.id):
        return

    try:
        tg_id = int(msg.text.strip())
    except ValueError:
        await msg.answer("❌ tg_id должен быть числом")
        return

    await msg.answer("⏳ Сбрасываю пользователя…")
    await _reset_service.reset_user(tg_id=tg_id)
    await state.clear()

    await msg.answer(
        f"✅ Пользователь <code>{tg_id}</code> полностью сброшен",
        parse_mode="HTML",
    )
