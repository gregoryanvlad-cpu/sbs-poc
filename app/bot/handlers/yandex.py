from __future__ import annotations

import asyncio
import json
import re

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.db.session import session_scope
from app.db.models.user import User
from app.bot.keyboards import kb_main
from app.db.models.yandex_membership import YandexMembership
from app.services.yandex.service import yandex_service

router = Router()

# ✅ Важно: требуем хотя бы одну букву (a-z), чтобы НЕ ловить tg_id (цифры)
# Пример валидного логина: ivan.petrov, dereshchuk.lina, vladgin9
_YANDEX_LOGIN_RE_STRICT = re.compile(r"(?i)^(?=.*[a-z])[a-z0-9][a-z0-9._-]{1,63}$")


def _kb_open_invite(invite_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть приглашение", url=invite_link)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


# Реинвайт/TTL больше не используется: ссылки загружаются вручную и выдаются один раз.


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


@router.message(F.text.regexp(r"(?i)^(?=.*[a-z])[a-z0-9][a-z0-9._-]{1,63}$"))
async def on_yandex_login_input(message: Message) -> None:
    """
    Авто-инвайт:
    - nav.py выставляет user.flow_state = "await_yandex_login"
    - пользователь вводит логин сообщением
    - мы создаём membership + invite_link и отправляем ссылку

    ✅ Этот handler НЕ ловит tg_id (цифры), поэтому не мешает админскому FSM reset.
    """
    tg_id = message.from_user.id
    login = (message.text or "").strip().lstrip("@").strip()

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if not user or user.flow_state != "await_yandex_login":
            # Не наш сценарий — просто ничего не делаем.
            # (И это не мешает админскому reset, потому что tg_id не матчится по regexp.)
            return

        if not _YANDEX_LOGIN_RE_STRICT.match(login):
            await message.answer(
                "❌ Логин выглядит некорректно.\n\n"
                "Пример: <code>ivan.petrov</code>",
                parse_mode="HTML",
            )
            return

        await message.answer("⏳ Выдаю ссылку приглашения…")

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
        sent = await message.answer(
            "✅ Ссылка приглашения готова.\n\n"
            f"Логин: <code>{membership.yandex_login}</code>\n\n"
            "Нажми кнопку ниже и прими приглашение.\n"
            "Если не успел — ссылка всегда доступна в 🟡 Yandex Plus.",
            reply_markup=_kb_open_invite(membership.invite_link),
            parse_mode="HTML",
        )

        # Через минуту возвращаем карточку к главному меню, но ссылка останется в разделе Yandex Plus.
        async def _auto_back() -> None:
            try:
                await asyncio.sleep(60)
                await message.bot.edit_message_text(
                    chat_id=sent.chat.id,
                    message_id=sent.message_id,
                    text="Главное меню:",
                    reply_markup=kb_main(),
                )
            except Exception:
                pass

        asyncio.create_task(_auto_back())
    else:
        await message.answer(
            "✅ Логин сохранён, но ссылка приглашения не найдена.\n"
            "Открой 🟡 Yandex Plus ещё раз — я попробую выдать приглашение повторно.",
        )
