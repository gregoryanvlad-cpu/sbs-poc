from aiogram import Router, F
from aiogram.types import Message

from app.db.session import session_scope
from app.services.yandex.service import yandex_service
from app.db.models.user import User

router = Router()


@router.message(F.text & ~F.text.startswith("/"))
async def yandex_login_input(message: Message):
    tg_id = message.from_user.id
    login = message.text.strip()

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if not user or user.flow_state != "await_yandex_login":
            return

        # фиксируем логин
        user.flow_state = None
        user.flow_data = None

        res = await yandex_service.ensure_membership_after_payment(
            session=session,
            tg_id=tg_id,
            yandex_login=login,
        )

    if res.invite_link:
        await message.answer(
            "🟡 Яндекс\n\n"
            "Приглашение готово 👇\n"
            f"{res.invite_link}\n\n"
            "⚠️ Ссылка ограничена по времени."
        )
    else:
        await message.answer(res.message)
