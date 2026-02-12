from __future__ import annotations

import json
import re
from datetime import timedelta

from aiogram import Router, F
from aiogram.exceptions import SkipHandler
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.bot.ui import utcnow
from app.db.models.user import User
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.repo import get_subscription
from app.services.yandex.provider import build_provider

router = Router()

_LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$", re.IGNORECASE)

# TTL для инвайта (сколько держим слот, если пользователь не вступил)
INVITE_TTL_MINUTES = 15


def _kb_confirm_login() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data="yandex:login:confirm")],
            [InlineKeyboardButton(text="✏️ Исправить", callback_data="yandex:login:edit")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _kb_open_invite(invite_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть приглашение", url=invite_link)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


async def _cleanup_hint_messages(bot, chat_id: int, tg_id: int) -> None:
    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if not user or not user.flow_data:
            return
        try:
            data = json.loads(user.flow_data)
            for msg_id in data.get("hint_msg_ids", []):
                try:
                    await bot.delete_message(chat_id, msg_id)
                except Exception:
                    pass
        except Exception:
            pass


async def _is_sub_active(tg_id: int) -> bool:
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not sub or not sub.end_at:
            return False
        return sub.end_at > utcnow()


@router.message(F.text)
async def on_yandex_login_input(msg: Message) -> None:
    tg_id = msg.from_user.id

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        # Важно: этот хендлер повешен на F.text (ловит все текстовые сообщения).
        # Если мы просто `return`, aiogram считает апдейт обработанным и
        # не даёт другим хендлерам (в т.ч. FSM в админке) его обработать.
        # Поэтому в нерелевантных случаях делаем SkipHandler.
        if not user or user.flow_state != "await_yandex_login":
            raise SkipHandler

        login = (msg.text or "").strip()
        login = login.replace("@", "").strip()

        if not _LOGIN_RE.match(login):
            await msg.answer("❌ Логин выглядит некорректно. Пример: <code>ivan.petrov</code>", parse_mode="HTML")
            return

        user.flow_state = "await_yandex_login_confirm"
        user.flow_data = json.dumps({"login": login, **(json.loads(user.flow_data) if user.flow_data else {})})
        await session.commit()

    await msg.answer(
        "Подтверди логин:\n\n"
        f"🔑 <code>{login}</code>\n\n"
        "⚠️ После подтверждения изменить логин нельзя.",
        reply_markup=_kb_confirm_login(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "yandex:login:edit")
async def on_yandex_login_edit(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    await cb.answer()

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if not user:
            return
        user.flow_state = "await_yandex_login"
        # flow_data оставляем (там hint_msg_ids)
        await session.commit()

    await cb.message.answer("Ок, введи логин ещё раз сообщением ниже 👇")


@router.callback_query(F.data == "yandex:login:confirm")
async def on_yandex_login_confirm(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    await cb.answer()

    # Защита: без активной подписки не выдаём инвайт
    if not await _is_sub_active(tg_id):
        await cb.message.answer("⛔️ Подписка не активна. Сначала оплати доступ.")
        return

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if not user or user.flow_state != "await_yandex_login_confirm" or not user.flow_data:
            await cb.message.answer("⚠️ Сессия ввода логина устарела. Зайди в 🟡 Yandex Plus и попробуй снова.")
            return

        data = json.loads(user.flow_data)
        login = (data.get("login") or "").strip()
        if not login:
            await cb.message.answer("⚠️ Не вижу логин. Зайди в 🟡 Yandex Plus и попробуй снова.")
            return

        # Если уже есть активная membership — просто покажем её
        existing = await session.scalar(
            select(YandexMembership)
            .where(
                YandexMembership.tg_id == tg_id,
                YandexMembership.status.in_(["awaiting_join", "pending", "active"]),
            )
            .order_by(YandexMembership.id.desc())
            .limit(1)
        )
        if existing:
            # сбрасываем flow
            user.flow_state = None
            user.flow_data = None
            await session.commit()

            await _cleanup_hint_messages(cb.bot, cb.message.chat.id, tg_id)

            if existing.invite_link and existing.status in ("awaiting_join", "pending"):
                await cb.message.answer(
                    "У тебя уже есть активное приглашение ✅\n\n"
                    f"Логин: <code>{existing.yandex_login}</code>",
                    reply_markup=_kb_open_invite(existing.invite_link),
                    parse_mode="HTML",
                )
            else:
                await cb.message.answer(
                    "У тебя уже есть подключение/заявка ✅\n\n"
                    f"Логин: <code>{existing.yandex_login}</code>\n"
                    f"Статус: <b>{existing.status}</b>",
                    parse_mode="HTML",
                )
            return

        # Берём первый активный аккаунт (пока 1)
        acc = await session.scalar(
            select(YandexAccount)
            .where(YandexAccount.status == "active")
            .order_by(YandexAccount.id.asc())
            .limit(1)
        )
        if not acc:
            await cb.message.answer("❌ Нет активного Yandex-аккаунта (cookies).")
            return

        provider = build_provider()

        # credentials_ref хранится как имя файла, полный путь = settings.yandex_cookies_dir / credentials_ref
        from app.core.config import settings
        full_state_path = f"{settings.yandex_cookies_dir}/{acc.credentials_ref}"

        # Создаём invite (Playwright)
        try:
            invite_link = await provider.create_invite_link(storage_state_path=full_state_path)
        except Exception as e:
            msg_text = (
                "❌ Не получилось создать приглашение.\n\n"
                f"<code>{type(e).__name__}: {e}</code>\n\n"
            )
            if "Invite daily limit reached" in str(e):
                msg_text += (
                    "Превышено дневное ограничение на количество создаваемых приглашений.\n"
                    "Пожалуйста, попробуйте сгенерировать приглашение позже."
                )
            else:
                msg_text += "Попробуй ещё раз через минуту."

            await cb.message.answer(msg_text, parse_mode="HTML")
            return

        now = utcnow()
        membership = YandexMembership(
            tg_id=tg_id,
            yandex_account_id=acc.id,
            yandex_login=login,
            status="awaiting_join",
            invite_link=invite_link,
            invite_issued_at=now,
            invite_expires_at=now + timedelta(minutes=INVITE_TTL_MINUTES),
            reinvite_used=0,
        )
        session.add(membership)

        # сбрасываем flow
        user.flow_state = None
        user.flow_data = None

        await session.commit()

    await _cleanup_hint_messages(cb.bot, cb.message.chat.id, tg_id)

    await cb.message.answer(
        "✅ Логин подтверждён!\n\n"
        f"Логин: <code>{login}</code>\n"
        "Статус: ⏳ <b>Ожидание вступления</b>\n\n"
        "Нажми кнопку ниже и прими приглашение в семейную подписку.",
        reply_markup=_kb_open_invite(invite_link),
        parse_mode="HTML",
    )
