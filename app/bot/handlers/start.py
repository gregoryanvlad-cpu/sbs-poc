from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardRemove

from app.bot.keyboards import kb_main
from app.db.session import session_scope
from app.repo import ensure_user
from app.services.referrals.service import referral_service

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    tg_id = message.from_user.id

    # /start <payload>
    payload = None
    try:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2:
            payload = parts[1].strip()
    except Exception:
        payload = None

    async with session_scope() as session:
        await ensure_user(session, tg_id)

        # Referral payload format:
        #   ref_<CODE>
        # Where CODE is referrer's stable ref_code.
        if payload and payload.startswith("ref_"):
            code = payload.split("ref_", 1)[1].strip()
            if code:
                await referral_service.attach_pending_referrer(session, referred_tg_id=tg_id, ref_code=code)

        # ensure user has their own ref_code
        await referral_service.ensure_ref_code(session, tg_id)
        await session.commit()

    text = (
        "Привет!\n\n"
        "Здесь ты можешь управлять подпиской, VPN и бонусом (в разработке)."
    )
    await message.answer(text, reply_markup=ReplyKeyboardRemove())
    await message.answer("Главное меню:", reply_markup=kb_main())
