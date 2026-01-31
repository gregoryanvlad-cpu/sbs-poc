from __future__ import annotations

from pathlib import Path

import tempfile
import zipfile

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.types import FSInputFile

from sqlalchemy import select

from app.bot.auth import is_owner
from app.bot.keyboards import kb_admin_menu
from app.core.config import settings
from app.db.models.user import User
from app.db.models.yandex_account import YandexAccount
from app.db.session import session_scope
from app.services.yandex.provider import build_provider

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
        "🛠 <b>Админка</b>\n\n"
        "Здесь ты можешь подключать админские аккаунты Яндекса через cookies "
        "(<code>storage_state.json</code>).",
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
        "➕ <b>Добавление Yandex-аккаунта</b>\n\n"
        "Пришли сюда файлом <code>storage_state.json</code> (Playwright cookies).\n\n"
        "<b>Важно:</b>\n"
        "— Файл должен быть .json\n"
        "— Имя файла можно сделать как label (например <code>admin1_state.json</code>)\n",
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
        await message.answer("❌ Пришли файл .json (storage_state).", reply_markup=kb_admin_menu())
        return

    label = _safe_label_from_filename(doc.file_name)
    cookies_dir = Path(settings.yandex_cookies_dir)
    cookies_dir.mkdir(parents=True, exist_ok=True)

    saved_name = f"{label}.json"
    saved_path = cookies_dir / saved_name

    try:
        await message.bot.download(doc, destination=str(saved_path))
    except Exception:
        await message.answer("❌ Не смог скачать файл из Telegram. Повтори попытку.", reply_markup=kb_admin_menu())
        return

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
        "✅ <b>Yandex-аккаунт добавлен</b>\n\n"
        f"Label: <code>{label}</code>\n"
        f"Файл: <code>{saved_name}</code>\n"
        f"Путь: <code>{settings.yandex_cookies_dir}</code>\n\n"
        "Теперь можно нажать «🔍 Проверить Yandex аккаунт» (Playwright).",
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
            "📋 <b>Yandex аккаунты</b>\n\nПока пусто. Нажми «Добавить Yandex-аккаунт».",
            reply_markup=kb_admin_menu(),
            parse_mode="HTML",
        )
        await cb.answer()
        return

    lines = []
    for a in items:
        capacity = max(0, int(a.max_slots) - 1)
        lines.append(
            f"• <code>{a.label}</code> — {a.status} | slots: {a.used_slots}/{capacity} | plus_end: {a.plus_end_at or '—'}"
        )

    await cb.message.edit_text(
        "📋 <b>Yandex аккаунты</b>\n\n" + "\n".join(lines),
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.callback_query(lambda c: c.data == "admin:yandex:probe")
async def admin_yandex_probe(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await cb.answer("Проверяю аккаунт…", show_alert=False)

    async with session_scope() as session:
        q = (
            select(YandexAccount)
            .where(YandexAccount.status == "active")
            .order_by(YandexAccount.id.asc())
            .limit(1)
        )
        acc = (await session.execute(q)).scalar_one_or_none()

    if not acc:
        await cb.message.edit_text(
            "❌ Нет активных Yandex-аккаунтов. Сначала добавь cookies (storage_state.json).",
            reply_markup=kb_admin_menu(),
            parse_mode="HTML",
        )
        return

    path = str(Path(settings.yandex_cookies_dir) / str(acc.credentials_ref))
    provider = build_provider()

    try:
        snap = await provider.probe(storage_state_path=path)
    except Exception as e:
        await cb.message.edit_text(
            "❌ <b>Ошибка Playwright</b>\n\n"
            f"<code>{type(e).__name__}: {e}</code>\n\n"
            "Проверь:\n"
            "— cookies актуальны\n"
            "— volume /data/yandex доступен\n",
            reply_markup=kb_admin_menu(),
            parse_mode="HTML",
        )
        return

    debug_dir = (snap.raw_debug or {}).get("debug_dir")

    fam = snap.family
    if not fam:
        # ВАЖНО: если парс не удался — НЕ показываем фейковые слоты
        await cb.message.edit_text(
            "✅ <b>Yandex аккаунт</b>\n\n"
            "⚠️ <b>Семья:</b> не удалось стабильно прочитать страницу (возможен редирект/капча/не прогрузилась).\n"
            "Нажми «📦 Скачать последний debug» и посмотри скрин/HTML.\n\n"
            f"Debug: <code>{debug_dir or '—'}</code>",
            reply_markup=kb_admin_menu(),
            parse_mode="HTML",
        )
        return

    admins = ", ".join(fam.admins) if fam.admins else "—"
    guests = ", ".join(fam.guests) if fam.guests else "—"

    await cb.message.edit_text(
        "✅ <b>Yandex аккаунт</b>\n\n"
        f"Админ: <code>{admins}</code>\n"
        f"Гости: <code>{guests}</code>\n"
        f"Pending: <b>{fam.pending_count}</b>\n"
        f"Free slots: <b>{fam.free_slots}</b>\n\n"
        f"Debug: <code>{debug_dir or '—'}</code>",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )


def _pick_latest_dir(root: Path) -> Path | None:
    try:
        if not root.exists() or not root.is_dir():
            return None
        dirs = [p for p in root.iterdir() if p.is_dir()]
        if not dirs:
            return None
        dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return dirs[0]
    except Exception:
        return None


@router.callback_query(lambda c: c.data == "admin:yandex:debug:last")
async def admin_yandex_debug_last(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await cb.answer("Готовлю debug…", show_alert=False)

    async with session_scope() as session:
        q = (
            select(YandexAccount)
            .where(YandexAccount.status == "active")
            .order_by(YandexAccount.id.asc())
            .limit(1)
        )
        acc = (await session.execute(q)).scalar_one_or_none()

    if not acc:
        await cb.message.answer("❌ Нет активных Yandex-аккаунтов.", reply_markup=kb_admin_menu())
        return

    debug_root = Path(settings.yandex_cookies_dir) / "debug_out" / str(acc.label)
    latest_run = _pick_latest_dir(debug_root)

    if not latest_run:
        await cb.message.answer(
            "ℹ️ Debug папок пока нет.\n"
            "Сначала нажми «🔍 Проверить Yandex аккаунт» или создай инвайт.",
            reply_markup=kb_admin_menu(),
        )
        return

    # zip -> temp file
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            zip_path = Path(tmp.name)

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in latest_run.rglob("*"):
                if p.is_file():
                    zf.write(p, arcname=str(p.relative_to(latest_run.parent)))

        await cb.message.answer_document(
            document=FSInputFile(str(zip_path), filename=f"yandex_debug_{acc.label}_{latest_run.name}.zip"),
            caption=f"📦 Debug: <code>{latest_run}</code>",
            parse_mode="HTML",
        )
    except Exception:
        await cb.message.answer("❌ Не смог упаковать/отправить debug.", reply_markup=kb_admin_menu())


# ==============================
# ADMIN: FULL USER RESET (TEST)
# ==============================

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from app.services.admin.reset_user import AdminResetUserService
from app.services.admin.forgive_user import AdminForgiveUserService

_reset_service = AdminResetUserService()
_forgive_service = AdminForgiveUserService()


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
        "Пришли <code>tg_id</code> пользователя.\n"
        "⚠️ Будет удалено ВСЁ: подписка, VPN, Yandex, логин.\n"
        "Использовать ТОЛЬКО для тестов.",
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

    await msg.answer("⏳ Сбрасываю пользователя...")

    await _reset_service.reset_user(tg_id=tg_id)

    await state.clear()

    await msg.answer(
        f"✅ Пользователь <code>{tg_id}</code> полностью сброшен.\n"
        "Теперь он как новый.",
        parse_mode="HTML",
    )


# ==============================
# ADMIN: FORGIVE (remove strikes)
# ==============================

class AdminForgiveFSM(StatesGroup):
    waiting_tg_id = State()


@router.callback_query(lambda c: c.data == "admin:forgive:user")
async def admin_forgive_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()

    if not is_owner(cb.from_user.id):
        return

    await state.set_state(AdminForgiveFSM.waiting_tg_id)

    await cb.message.answer(
        "🧽 <b>Снять страйки / разблокировать Yandex</b>\n\n"
        "Пришли <code>tg_id</code> пользователя.\n"
        "Можно вводить и свой ID.",
        parse_mode="HTML",
    )


@router.message(AdminForgiveFSM.waiting_tg_id)
async def admin_forgive_confirm(msg: Message, state: FSMContext) -> None:
    if not is_owner(msg.from_user.id):
        return

    try:
        tg_id = int(msg.text.strip())
    except ValueError:
        await msg.answer("❌ tg_id должен быть числом")
        return

    ok = await _forgive_service.forgive_yandex(tg_id)
    await state.clear()

    if ok:
        await msg.answer(
            f"✅ Пользователь <code>{tg_id}</code> прощён\n"
            "Strikes = 0, reinvite разблокирован",
            parse_mode="HTML",
        )
    else:
        await msg.answer("ℹ️ У пользователя нет Yandex-записи")
