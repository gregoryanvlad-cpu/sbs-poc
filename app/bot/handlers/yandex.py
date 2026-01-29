import json

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import kb_main, kb_yandex_login_confirm
from app.bot.ui import utcnow
from app.db.models import Subscription, User
from app.db.session import session_scope
from app.services.yandex.service import yandex_service

router = Router()


def _is_sub_active(sub_end_at):
    if not sub_end_at:
        return False
    return sub_end_at > utcnow()


@router.message(F.text & ~F.text.startswith("/"))
async def yandex_login_input(message: Message):
    tg_id = message.from_user.id
    login = message.text.strip()

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        sub = await session.get(Subscription, tg_id)

        if not user or user.flow_state != "await_yandex_login":
            return

        if not sub or not _is_sub_active(sub.end_at):
            user.flow_state = None
            user.flow_data = None
            await session.commit()
            await message.answer("❌ Подписка не активна.", reply_markup=kb_main())
            return

        # удалить картинку-подсказку
        try:
            data = json.loads(user.flow_data or "{}")
            msg_id = data.get("hint_msg_id")
            if msg_id:
                try:
                    await message.bot.delete_message(message.chat.id, msg_id)
                except Exception:
                    pass
        except Exception:
            pass

        # Переходим на подтверждение
        user.flow_state = "await_yandex_login_confirm"
        user.flow_data = json.dumps({"login": login})
        await session.commit()

    await message.answer(
        f"🟡 *Yandex Plus*\n\nВы ввели логин: `{login}`\n\nПодтверждаете?",
        reply_markup=kb_yandex_login_confirm(),
        parse_mode="Markdown",
    )


@router.callback_query(lambda c: c.data in ("yandex:login:confirm", "yandex:login:retry"))
async def yandex_login_confirm(cb: CallbackQuery):
    tg_id = cb.from_user.id

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        sub = await session.get(Subscription, tg_id)

        if not user or user.flow_state != "await_yandex_login_confirm":
            await cb.answer()
            return

        if cb.data == "yandex:login:retry":
            user.flow_state = "await_yandex_login"
            user.flow_data = None
            await session.commit()
            await cb.message.edit_text("Ок. Введите логин ещё раз сообщением ниже.")
            await cb.answer()
            return

        data = json.loads(user.flow_data or "{}")
        login = data.get("login")

        if not login or not sub or not _is_sub_active(sub.end_at):
            user.flow_state = None
            user.flow_data = None
            await session.commit()
            await cb.message.edit_text("❌ Ошибка. Попробуйте снова.")
            await cb.answer()
            await cb.message.answer("Главное меню:", reply_markup=kb_main())
            return

        # ВАЖНО: логин фиксируем НЕ в User, а в yandex_memberships внутри сервиса
        user.flow_state = None
        user.flow_data = None

        res = await yandex_service.ensure_membership_after_payment(
            session=session,
            tg_id=tg_id,
            yandex_login=login,
        )
        await session.commit()

    await cb.message.edit_text(f"✅ Логин подтверждён: `{login}`", parse_mode="Markdown")
    await cb.answer()

    if getattr(res, "invite_link", None):
        await cb.message.answer(
            "🟡 *Yandex Plus*\n\nПриглашение готово 👇\n"
            f"{res.invite_link}\n\n"
            "⚠️ Ссылка ограничена по времени.",
            parse_mode="Markdown",
        )
    else:
        await cb.message.answer(getattr(res, "message", "⚠️ Не удалось выдать приглашение."))

    await cb.message.answer("Главное меню:", reply_markup=kb_main())
