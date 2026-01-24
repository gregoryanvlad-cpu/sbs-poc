from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardRemove

from app.bot.keyboards import kb_main
from app.db.session import session_scope
from app.repo import ensure_user

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    tg_id = message.from_user.id
    async with session_scope() as session:
        await ensure_user(session, tg_id)
        await session.commit()

    text = (
        "Привет!\n\n"
        "Здесь ты можешь управлять подпиской, VPN и бонусом (в разработке)."
    )
    await message.answer(text, reply_markup=ReplyKeyboardRemove())
    await message.answer("Главное меню:", reply_markup=kb_main())
