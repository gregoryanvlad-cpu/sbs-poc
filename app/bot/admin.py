from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot.auth import is_owner
from app.bot.keyboards import kb_admin_menu
from app.core.config import settings
from app.db.models.user import User
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_invite_slot import YandexInviteSlot
from app.db.session import session_scope
from app.services.admin.forgive_user import AdminForgiveUserService
from app.services.admin.reset_user import AdminResetUserService

router = Router()


def _parse_date_utc(s: str) -> datetime:
    """Parse YYYY-MM-DD into 23:59:59 UTC."""
    s = (s or "").strip()
    dt = datetime.strptime(s, "%Y-%m-%d")
    return dt.replace(tzinfo=timezone.utc, hour=23, minute=59, second=59)


class AdminYandexFSM(StatesGroup):
    waiting_account = State()  # label + date
    waiting_slots = State()  # label + 3 links


@router.callback_query(lambda c: c.data == "admin:menu")
async def admin_menu(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await cb.message.edit_text(
        "🛠 <b>Админка</b>\n\n"
        "Yandex Plus теперь работает в <b>ручном режиме</b>:\n"
        "— ты добавляешь аккаунт и дату окончания Plus\n"
        "— загружаешь 3 готовые ссылки-приглашения (1 аккаунт = 3 слота)\n"
        "— бот выдаёт ссылки пользователям и автоматически делает ротацию\n\n"
        "⚠️ Исключение пользователей из семьи делается вручную.\n",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.callback_query(lambda c: c.data == "admin:yandex:add")
async def admin_yandex_add(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.set_state(AdminYandexFSM.waiting_account)
    await cb.message.edit_text(
        "➕ <b>Добавление Yandex-аккаунта</b>\n\n"
        "Отправь одним сообщением:\n"
        "<code>LABEL YYYY-MM-DD</code>\n\n"
        "Пример: <code>YA_ACC_1 2026-03-28</code>\n\n"
        "Дата — это до какого числа активна подписка Plus на этом аккаунте (введи вручную).",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(AdminYandexFSM.waiting_account)
async def admin_yandex_add_msg(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    text = (message.text or "").strip()
    try:
        label, date_str = text.split(maxsplit=1)
        label = label.strip()[:64]
        plus_end_at = _parse_date_utc(date_str)
    except Exception:
        await message.answer(
            "❌ Формат неверный. Нужно: <code>LABEL YYYY-MM-DD</code>\n"
            "Пример: <code>YA_ACC_1 2026-03-28</code>",
            parse_mode="HTML",
        )
        return

    async with session_scope() as session:
        acc = await session.scalar(select(YandexAccount).where(YandexAccount.label == label).limit(1))
        if not acc:
            acc = YandexAccount(
                label=label,
                status="active",
                max_slots=4,
                used_slots=0,
            )
            session.add(acc)
            await session.flush()
        acc.plus_end_at = plus_end_at
        acc.status = "active"
        acc.last_probe_error = None

        # clear owner flow state (if any)
        user = await session.get(User, message.from_user.id)
        if user:
            user.flow_state = None
            user.flow_data = None

        await session.commit()

    await state.clear()
    await message.answer(
        "✅ <b>Аккаунт добавлен</b>\n\n"
        f"Label: <code>{label}</code>\n"
        f"Plus до: <code>{plus_end_at.date().isoformat()}</code>\n\n"
        "Теперь нажми «🔗 Загрузить 3 ссылки» и добавь 3 приглашения для этого аккаунта.",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.callback_query(lambda c: c.data == "admin:yandex:slots:add")
async def admin_yandex_slots_add(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    await state.set_state(AdminYandexFSM.waiting_slots)
    await cb.message.edit_text(
        "🔗 <b>Загрузка 3 ссылок</b>\n\n"
        "Отправь одним сообщением в 4 строки:\n\n"
        "<code>LABEL</code>\n"
        "<code>LINK_SLOT_1</code>\n"
        "<code>LINK_SLOT_2</code>\n"
        "<code>LINK_SLOT_3</code>\n\n"
        "Важно: ссылки должны быть уже созданные в Yandex Family и уникальные.",
        reply_markup=kb_admin_menu(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(AdminYandexFSM.waiting_slots)
async def admin_yandex_slots_add_msg(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return

    lines = [ln.strip() for ln in (message.text or "").splitlines() if ln.strip()]
    if len(lines) != 4:
        await message.answer(
            "❌ Нужно 4 строки: LABEL + 3 ссылки.",
            reply_markup=kb_admin_menu(),
        )
        return

    label, link1, link2, link3 = lines

    async with session_scope() as session:
        acc = await session.scalar(select(YandexAccount).where(YandexAccount.label == label).limit(1))
        if not acc:
            await message.answer(
                "❌ Аккаунт не найден. Сначала добавь аккаунт кнопкой «➕ Добавить Yandex-аккаунт».",
                reply_markup=kb_admin_menu(),
            )
            return

        # upsert 3 slots
        links = [link1, link2, link3]
        for idx, link in enumerate(links, start=1):
            slot = await session.scalar(
                select(YandexInviteSlot)
                .where(
                    YandexInviteSlot.yandex_account_id == acc.id,
                    YandexInviteSlot.slot_index == idx,
                )
                .limit(1)
            )
            if not slot:
                slot = YandexInviteSlot(
                    yandex_account_id=acc.id,
                    slot_index=idx,
                    invite_link=link,
                    status="free",
                )
                session.add(slot)
            else:
                # if slot already issued/burned, do NOT overwrite (S1). allow overwrite only if still free.
                if (slot.status or "free") != "free":
                    continue
                slot.invite_link = link

        await session.commit()

    await state.clear()
    await message.answer(
        "✅ <b>Ссылки загружены</b>\n\n"
        f"Аккаунт: <code>{label}</code>\n"
        "Слоты 1..3 готовы к выдаче.",
        parse_mode="HTML",
        reply_markup=kb_admin_menu(),
    )


@router.callback_query(lambda c: c.data == "admin:yandex:list")
async def admin_yandex_list(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return

    async with session_scope() as session:
        accounts = (
            await session.scalars(select(YandexAccount).order_by(YandexAccount.id.asc()))
        ).all()

        if not accounts:
            await cb.message.edit_text(
                "📋 <b>Yandex аккаунты</b>\n\nПока пусто. Нажми «➕ Добавить Yandex-аккаунт».",
                reply_markup=kb_admin_menu(),
                parse_mode="HTML",
            )
            await cb.answer()
            return

        lines = ["📋 <b>Yandex аккаунты / слоты</b>\n"]
        for acc in accounts:
            free_cnt = await session.scalar(
                select(func.count(YandexInviteSlot.id)).where(
                    YandexInviteSlot.yandex_account_id == acc.id,
                    YandexInviteSlot.status == "free",
                )
            )
            issued_cnt = await session.scalar(
                select(func.count(YandexInviteSlot.id)).where(
                    YandexInviteSlot.yandex_account_id == acc.id,
                    YandexInviteSlot.status != "free",
                )
            )
            lines.append(
                f"• <code>{acc.label}</code> — {acc.status} | Plus до: <code>{(acc.plus_end_at.date().isoformat() if acc.plus_end_at else '—')}</code> | "
                f"slots free/issued: <b>{int(free_cnt or 0)}</b>/<b>{int(issued_cnt or 0)}</b>"
            )

        await cb.message.edit_text(
            "\n".join(lines),
            reply_markup=kb_admin_menu(),
            parse_mode="HTML",
        )
        await cb.answer()


# ==============================
# ADMIN: FULL USER RESET (TEST)
# ==============================


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
