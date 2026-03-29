from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardRemove, FSInputFile
from pathlib import Path

from app.bot.keyboards import kb_main
from app.db.session import session_scope
from sqlalchemy import select, func
from app.db.models import User, Payment, Subscription, Referral
from app.repo import ensure_user, is_trial_available
from app.services.referrals.service import referral_service

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    tg_id = message.from_user.id

    payload = None
    try:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2:
            payload = parts[1].strip()
    except Exception:
        payload = None

    show_trial = False
    async with session_scope() as session:
        u = message.from_user
        existing_user = await session.get(User, int(tg_id))
        await ensure_user(
            session,
            tg_id,
            username=u.username,
            first_name=u.first_name,
            last_name=u.last_name,
        )

        # Привязка по рефералке допустима только на самом первом входе.
        # Если пользователь уже существовал в БД, не назначаем реферера повторно.
        if payload and payload.startswith("ref_"):
            code = payload.split("ref_", 1)[1].strip()
            if existing_user is None:
                if code:
                    await referral_service.attach_pending_referrer(
                        session,
                        referred_tg_id=tg_id,
                        ref_code=code,
                    )
            else:
                # Самовосстановление после старого бага: если зрелому пользователю
                # ошибочно проставили pending-реферала, убираем только pending-метадату,
                # не трогая уже оформленные referral-записи.
                has_referral_row = bool(await session.scalar(select(Referral.referred_tg_id).where(Referral.referred_tg_id == int(tg_id)).limit(1)))
                payments_cnt = int(await session.scalar(select(func.count()).select_from(Payment).where(Payment.tg_id == int(tg_id))) or 0)
                has_sub = bool(await session.get(Subscription, int(tg_id)))
                if getattr(existing_user, 'referred_by_tg_id', None) and not has_referral_row and (payments_cnt > 0 or has_sub):
                    existing_user.referred_by_tg_id = None
                    if hasattr(existing_user, 'referred_at'):
                        existing_user.referred_at = None

        await referral_service.ensure_ref_code(session, tg_id)
        show_trial = await is_trial_available(session, tg_id)
        await session.commit()

    # Greeting (обычный текст, без рамки/код-блока)
    text = (
        "<b>Добро пожаловать</b> 👋\n"
        "<i>Этот бот — ваш центр управления сервисами:</i>\n\n"
        "• Безопасный VPN\n"
        "• Обход глушилок региона\n"
        "• Обход замедления Telegram\n"
        "• Yandex Plus\n\n"
        "<b>Всего 199 ₽ в месяц</b>\n\n"
        "<i>По вопросам сотрудничества:</i> @sbsmanager_bot"
    )

    welcome_image = Path(__file__).resolve().parents[2] / "content" / "welcome-start.jpg"

    if welcome_image.exists():
        await message.answer_photo(
            FSInputFile(str(welcome_image)),
            caption=text,
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="HTML",
        )
    else:
        await message.answer(text, reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")

    from app.bot.handlers.nav import _build_home_text

    await message.answer(
        await _build_home_text(),
        reply_markup=kb_main(show_trial=show_trial),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
