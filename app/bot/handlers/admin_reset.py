from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from app.core.config import settings
from app.services.admin.reset_user import AdminResetUserService

router = Router()
_reset_service = AdminResetUserService()


class AdminResetFSM(StatesGroup):
    waiting_tg_id = State()


@router.callback_query(F.data == "admin:reset:user")
async def admin_reset_start(cb: CallbackQuery, state: FSMContext):
    # КРИТИЧНО: сразу закрываем callback
    await cb.answer()

    if cb.from_user.id != settings.owner_tg_id:
        return

    await state.set_state(AdminResetFSM.waiting_tg_id)

    await cb.message.answer(
        "🧨 <b>Полный сброс пользователя</b>\n\n"
        "Отправь <code>tg_id</code> пользователя.\n"
        "⚠️ Будет удалено ВСЁ (VPN, Yandex, подписка).",
        parse_mode="HTML",
    )


@router.message(AdminResetFSM.waiting_tg_id)
async def admin_reset_confirm(msg: Message, state: FSMContext):
    if msg.from_user.id != settings.owner_tg_id:
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
