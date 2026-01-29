import json

from aiogram import Router, F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.keyboards import kb_main
from app.db.models.user import User
from app.db.models.yandex_membership import YandexMembership
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

        # если логин уже зафиксирован — не даём менять
        q = (
            select(YandexMembership)
            .where(YandexMembership.tg_id == tg_id)
            .order_by(YandexMembership.id.desc())
            .limit(1)
        )
        res = await session.execute(q)
        ym = res.scalar_one_or_none()
        if ym and ym.yandex_login:
            user.flow_state = None
            user.flow_data = None
            await session.commit()
            await message.answer(
                f"🟡 Yandex Plus уже подключается/подключён.\nЛогин: {ym.yandex_login}",
                reply_markup=kb_main(),
            )
            return

        # удаляем подсказочные сообщения
        try:
            if user.flow_data:
                data = json.loads(user.flow_data)
                for msg_id in data.get("hint_msg_ids", []):
                    try:
                        await message.bot.delete_message(chat_id=message.chat.id, message_id=msg_id)
                    except Exception:
                        pass
        except Exception:
            pass

        user.flow_state = None
        user.flow_data = None

        res = await yandex_service.ensure_membership_after_payment(
            session=session,
            tg_id=tg_id,
            yandex_login=login,
        )
        await session.commit()

    # ✅ УСПЕХ: даём кнопку-ссылку и меню в одном сообщении
    if getattr(res, "invite_link", None):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Открыть приглашение", url=res.invite_link)],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
            ]
        )
        await message.answer(
            "🟡 *Yandex Plus*\n\n"
            "Приглашение готово 👇",
            reply_markup=kb,
            parse_mode="Markdown",
        )
        return

    # ✅ ОШИБКА: одно сообщение (без второго “Главное меню”)
    await message.answer(getattr(res, "message", "⚠️ Не удалось выдать приглашение."), reply_markup=kb_main())
