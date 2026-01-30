from __future__ import annotations

import json
import re

from aiogram import Router, F
from aiogram.dispatcher.event.handler import SkipHandler
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.db.session import session_scope
from app.db.models.user import User
from app.services.yandex.service import yandex_service

router = Router()

_LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$", re.IGNORECASE)


def _kb_open_invite(invite_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть приглашение", url=invite_link)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


async def _cleanup_hint_messages(bot, chat_id: int, user: User) -> None:
    """
    Удаляем подсказочные сообщения (скрин/текст), если их ID были сохранены в user.flow_data:
    {"hint_msg_ids": [...]}.
    """
    if not user.flow_data:
        return
    try:
        data = json.loads(user.flow_data)
        ids = data.get("hint_msg_ids") or []
        for mid in ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=int(mid))
            except Exception:
                pass
    except Exception:
        pass


@router.message(F.text)
async def on_yandex_login_input(message: Message) -> None:
    """
    Авто-инвайт:
    - nav.py выставляет user.flow_state = "await_yandex_login"
    - пользователь вводит логин сообщением
    - мы создаём membership + invite_link и отправляем ссылку

    ВАЖНО:
    Если сообщение не относится к нашему flow — пропускаем дальше через SkipHandler,
    чтобы работали админские FSM и другие сценарии.
    """
    tg_id = message.from_user.id
    text = (message.text or "").strip()

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if not user or user.flow_state != "await_yandex_login":
            raise SkipHandler

        login = text.strip().lstrip("@").strip()
        if not _LOGIN_RE.match(login):
            await message.answer(
                "❌ Логин выглядит некорректно.\n\n"
                "Пример: <code>ivan.petrov</code>",
                parse_mode="HTML",
            )
            return

        await message.answer("⏳ Создаю приглашение в семейную подписку…")

        try:
            membership = await yandex_service.ensure_membership_for_user(
                session=session,
                tg_id=tg_id,
                yandex_login=login,
            )
        except Exception as e:
            await message.answer(
                "❌ Не получилось создать приглашение.\n\n"
                f"<code>{type(e).__name__}: {e}</code>\n\n"
                "Попробуй ещё раз через минуту.",
                parse_mode="HTML",
            )
            return

        # чистим подсказки и flow
        await _cleanup_hint_messages(message.bot, message.chat.id, user)
        user.flow_state = None
        user.flow_data = None
        await session.commit()

    # отправляем ссылку
    if membership.invite_link:
        await message.answer(
            "✅ Логин принят.\n\n"
            f"Логин: <code>{membership.yandex_login}</code>\n"
            "Статус: ⏳ <b>Ожидание вступления</b>\n\n"
            "Нажми кнопку ниже и прими приглашение:",
            reply_markup=_kb_open_invite(membership.invite_link),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "✅ Логин сохранён, но ссылка приглашения не найдена.\n"
            "Открой 🟡 Yandex Plus ещё раз — я попробую выдать приглашение повторно.",
        )
