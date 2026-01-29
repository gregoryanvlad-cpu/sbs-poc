import json

from aiogram import Router, F
from aiogram.types import Message

from app.bot.keyboards import kb_main
from app.db.models.user import User
from app.db.session import session_scope
from app.services.yandex.service import yandex_service

router = Router()


@router.message(F.text & ~F.text.startswith("/"))
async def yandex_login_input(message: Message):
    tg_id = message.from_user.id
    login = message.text.strip()

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if not user or user.flow_state != "await_yandex_login":
            return

        # удаляем картинку-подсказку, если она была
        try:
            if user.flow_data:
                data = json.loads(user.flow_data)
                msg_id = data.get("yandex_hint_msg_id")
                if msg_id:
                    try:
                        await message.bot.delete_message(chat_id=message.chat.id, message_id=msg_id)
                    except Exception:
                        pass
        except Exception:
            pass

        # фиксируем логин / чистим состояние
        user.flow_state = None
        user.flow_data = None

        res = await yandex_service.ensure_membership_after_payment(
            session=session,
            tg_id=tg_id,
            yandex_login=login,
        )
        await session.commit()

    if res.invite_link:
        await message.answer(
            "🟡 *Yandex Plus*\n\n"
            "Приглашение готово 👇\n"
            f"{res.invite_link}\n\n"
            "⚠️ Ссылка ограничена по времени.",
            parse_mode="Markdown",
        )
    else:
        await message.answer(res.message)

    # возвращаем пользователя в меню
    await message.answer("Главное меню:", reply_markup=kb_main())
