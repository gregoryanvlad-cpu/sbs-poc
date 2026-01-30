from __future__ import annotations

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.db.session import session_scope
from app.db.models.user import User
from app.services.yandex.service import yandex_service

router = Router()


def _kb_open_invite(invite_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть приглашение", url=invite_link)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


@router.callback_query(F.data == "nav:yandex")
async def yandex_plus_handler(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if not user:
            await cb.answer()
            return

        # 1️⃣ Логин ещё не введён
        if not user.yandex_login:
            await cb.message.edit_text(
                "🟡 <b>Yandex Plus</b>\n\n"
                "Для подключения укажи логин Яндекс ID.\n"
                "Логин можно ввести <b>только один раз</b>.",
                reply_markup=_kb_back(),
                parse_mode="HTML",
            )
            await cb.answer()
            return

        # 2️⃣ Гарантируем наличие membership (авто-инвайт здесь!)
        membership = await yandex_service.ensure_membership_for_user(
            session=session,
            user_id=tg_id,
            yandex_login=user.yandex_login,
        )

        # 3️⃣ Awaiting join — показываем ссылку
        if membership.status == "awaiting_join":
            await cb.message.edit_text(
                "🟡 <b>Yandex Plus</b>\n\n"
                f"Логин: <code>{membership.yandex_login}</code>\n"
                "Статус: ⏳ <b>Ожидание вступления</b>\n\n"
                "Перейди по ссылке ниже, чтобы принять приглашение в семейную подписку.",
                reply_markup=_kb_open_invite(membership.invite_link),
                parse_mode="HTML",
            )
            await cb.answer()
            return

        # 4️⃣ Активный пользователь
        if membership.status == "active":
            await cb.message.edit_text(
                "🟡 <b>Yandex Plus</b>\n\n"
                f"Логин: <code>{membership.yandex_login}</code>\n"
                "Статус: ✅ <b>Подключён</b>\n\n"
                "Доступ к Яндекс Плюс активен.",
                reply_markup=_kb_back(),
                parse_mode="HTML",
            )
            await cb.answer()
            return

        # 5️⃣ Таймаут / удалён
        await cb.message.edit_text(
            "🟡 <b>Yandex Plus</b>\n\n"
            f"Логин: <code>{membership.yandex_login}</code>\n"
            "Статус: ⛔️ <b>Приглашение недоступно</b>\n\n"
            "Если доступно, новое приглашение будет выдано автоматически.",
            reply_markup=_kb_back(),
            parse_mode="HTML",
        )
        await cb.answer()
