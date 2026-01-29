import json

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.keyboards import kb_main
from app.core.config import settings
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
        q = select(YandexMembership).where(YandexMembership.tg_id == tg_id).order_by(YandexMembership.id.desc()).limit(1)
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

    if res.invite_link:
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
    else:
        await message.answer(res.message, reply_markup=kb_main())

    await message.answer("Главное меню:", reply_markup=kb_main())


@router.callback_query(lambda c: c.data == "yandex:reinvite")
async def yandex_reinvite(cb: CallbackQuery):
    tg_id = cb.from_user.id

    async with session_scope() as session:
        res = await yandex_service.reinvite(session, tg_id)
        await session.commit()

    if res.invite_link:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Открыть приглашение", url=res.invite_link)],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
            ]
        )
        await cb.message.edit_text(
            "🟡 *Yandex Plus*\n\n"
            "Новое приглашение готово 👇",
            reply_markup=kb,
            parse_mode="Markdown",
        )
    else:
        await cb.answer(res.message, show_alert=True)

    await cb.answer()
