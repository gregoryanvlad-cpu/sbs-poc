import json

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.bot.keyboards import kb_main, kb_yandex_login_confirm
from app.db.models import Subscription, User
from app.db.session import session_scope
from app.services.yandex.service import yandex_service
from app.bot.ui import utcnow

router = Router()


def _is_sub_active(sub_end_at):
    if not sub_end_at:
        return False
    try:
        return sub_end_at > utcnow()
    except Exception:
        return False


@router.message(F.text & ~F.text.startswith("/"))
async def yandex_login_input(message: Message):
    tg_id = message.from_user.id
    login = message.text.strip()

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if not user or user.flow_state != "await_yandex_login":
            return

        # защита: если подписка не активна — не принимаем логин
        sub = await session.get(Subscription, tg_id)
        if not sub or not _is_sub_active(sub.end_at):
            user.flow_state = None
            user.flow_data = None
            await session.commit()
            await message.answer(
                "❌ Подписка не активна.\n\nОплатите доступ в разделе «Оплата».",
                reply_markup=kb_main(),
            )
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

        # сохраняем введённый логин во временное состояние (для подтверждения)
        user.flow_state = "await_yandex_login_confirm"
        user.flow_data = json.dumps({"pending_yandex_login": login})
        await session.commit()

    await message.answer(
        "🟡 *Yandex Plus*\n\n"
        f"Вы ввели логин: `{login}`\n\n"
        "Подтверждаете?",
        reply_markup=kb_yandex_login_confirm(),
        parse_mode="Markdown",
    )


@router.callback_query(lambda c: c.data in ("yandex:login:confirm", "yandex:login:retry"))
async def yandex_login_confirm(cb: CallbackQuery):
    tg_id = cb.from_user.id
    action = cb.data

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if not user or user.flow_state not in ("await_yandex_login_confirm", "await_yandex_login"):
            await cb.answer("Нет активного шага.", show_alert=True)
            return

        # retry: снова ждём ввод логина
        if action == "yandex:login:retry":
            user.flow_state = "await_yandex_login"
            # flow_data очищаем, чтобы не мешало
            user.flow_data = None
            await session.commit()
            await cb.message.edit_text(
                "🟡 *Yandex Plus*\n\n"
                "Ок. Введите логин *Yandex ID* ещё раз сообщением ниже.",
                parse_mode="Markdown",
            )
            await cb.answer()
            return

        # confirm: достаем pending login
        try:
            data = json.loads(user.flow_data or "{}")
            login = data.get("pending_yandex_login")
        except Exception:
            login = None

        if not login:
            user.flow_state = "await_yandex_login"
            user.flow_data = None
            await session.commit()
            await cb.message.edit_text("⚠️ Не удалось прочитать логин. Введите его ещё раз.")
            await cb.answer()
            return

        # проверяем подписку ещё раз
        sub = await session.get(Subscription, tg_id)
        if not sub or not _is_sub_active(sub.end_at):
            user.flow_state = None
            user.flow_data = None
            await session.commit()
            await cb.message.edit_text("❌ Подписка не активна. Оплатите доступ в разделе «Оплата».")
            await cb.answer()
            await cb.message.answer("Главное меню:", reply_markup=kb_main())
            return

        # фиксируем логин (теперь он окончательный для пользователя)
        # (если в модели User есть поле yandex_login — запишем. Если нет — пропустим.)
        if hasattr(user, "yandex_login"):
            setattr(user, "yandex_login", login)

        user.flow_state = None
        user.flow_data = None

        # запускаем бизнес-логику выдачи инвайта/создания membership
        res = await yandex_service.ensure_membership_after_payment(
            session=session,
            tg_id=tg_id,
            yandex_login=login,
        )
        await session.commit()

    # UI: сначала подтверждение, затем результат, затем меню
    try:
        await cb.message.edit_text(
            f"✅ Логин подтверждён: `{login}`",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    await cb.answer()

    if getattr(res, "invite_link", None):
        await cb.message.answer(
            "🟡 *Yandex Plus*\n\n"
            "Приглашение готово 👇\n"
            f"{res.invite_link}\n\n"
            "⚠️ Ссылка ограничена по времени.",
            parse_mode="Markdown",
        )
    else:
        await cb.message.answer(getattr(res, "message", "⚠️ Не удалось выдать приглашение."))

    await cb.message.answer("Главное меню:", reply_markup=kb_main())
