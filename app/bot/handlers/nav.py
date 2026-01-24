from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone

import qrcode
from aiogram import Router
from aiogram.types import CallbackQuery, BufferedInputFile
from dateutil.relativedelta import relativedelta

from app.bot.keyboards import kb_back_home, kb_cabinet, kb_confirm_reset, kb_main, kb_pay, kb_vpn
from app.bot.ui import days_left, fmt_dt, utcnow
from app.core.config import settings
from app.db.session import session_scope
from app.repo import extend_subscription, get_subscription, set_subscription_expired
from app.services.vpn.service import vpn_service


router = Router()


def _is_sub_active(sub_end_at: datetime | None) -> bool:
    if not sub_end_at:
        return False
    if sub_end_at.tzinfo is None:
        sub_end_at = sub_end_at.replace(tzinfo=timezone.utc)
    return sub_end_at > utcnow()


@router.callback_query(lambda c: c.data and c.data.startswith("nav:"))
async def on_nav(cb: CallbackQuery) -> None:
    where = cb.data.split(":", 1)[1]
    if where == "home":
        await cb.message.edit_text("Главное меню:", reply_markup=kb_main())
        await cb.answer()
        return

    if where == "cabinet":
        async with session_scope() as session:
            sub = await get_subscription(session, cb.from_user.id)
        text = (
            "👤 Личный кабинет\n\n"
            f"Статус: {'активна' if _is_sub_active(sub.end_at) else 'нет подписки'}\n"
            f"До: {fmt_dt(sub.end_at)}\n"
            f"Осталось: {days_left(sub.end_at)} дн."
        )
        await cb.message.edit_text(text, reply_markup=kb_cabinet())
        await cb.answer()
        return

    if where == "pay":
        await cb.message.edit_text(
            f"💳 Оплата\n\nТариф: {settings.price_rub} ₽ / {settings.period_months} мес.",
            reply_markup=kb_pay(),
        )
        await cb.answer()
        return

    if where == "vpn":
        await cb.message.edit_text("🌍 VPN", reply_markup=kb_vpn())
        await cb.answer()
        return

    if where == "faq":
        text = "❓ FAQ\n\n— Как оплатить? В разделе ‘Оплата’ (пока mock)\n— Как получить VPN? Раздел ‘VPN’."
        await cb.message.edit_text(text, reply_markup=kb_back_home())
        await cb.answer()
        return

    if where == "support":
        await cb.message.edit_text("🛠 Поддержка\n\nНапиши сюда: @support (заглушка)", reply_markup=kb_back_home())
        await cb.answer()
        return

    await cb.answer("Неизвестный раздел", show_alert=False)


@router.callback_query(lambda c: c.data and c.data.startswith("pay:mock:"))
async def on_mock_pay(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        now = utcnow()
        base = sub.end_at if sub.end_at and sub.end_at > now else now
        new_end = base + relativedelta(months=settings.period_months)
        await extend_subscription(session, tg_id, months=settings.period_months, days_legacy=settings.period_days)
        sub.end_at = new_end
        sub.is_active = True
        sub.status = "active"
        await vpn_service.ensure_peer(session, tg_id)
        await session.commit()

    await cb.answer("Оплата успешна")
    await cb.message.edit_text(
        f"✅ Оплата успешна\n\nПодписка до: {fmt_dt(new_end)}",
        reply_markup=kb_main(),
    )


@router.callback_query(lambda c: c.data == "vpn:guide")
async def on_vpn_guide(cb: CallbackQuery) -> None:
    text = (
        "📖 Инструкция\n\n"
        "1) Нажми ‘Отправить конфиг + QR’\n"
        "2) Импортируй в WireGuard\n"
        f"3) Конфиг удалится через {settings.auto_delete_seconds} сек."
    )
    await cb.message.edit_text(text, reply_markup=kb_vpn())
    await cb.answer()


@router.callback_query(lambda c: c.data == "vpn:reset:confirm")
async def on_vpn_reset_confirm(cb: CallbackQuery) -> None:
    await cb.message.edit_text("♻️ Сбросить VPN?\nСтарый конфиг перестанет работать.", reply_markup=kb_confirm_reset())
    await cb.answer()


@router.callback_query(lambda c: c.data == "vpn:reset")
async def on_vpn_reset(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    async with session_scope() as session:
        await vpn_service.rotate_peer(session, tg_id, reason="manual_reset")
        await session.commit()
    await cb.answer("Сброшено")
    await cb.message.edit_text("♻️ VPN сброшен. Получи новый конфиг в разделе VPN.", reply_markup=kb_vpn())


@router.callback_query(lambda c: c.data == "vpn:bundle")
async def on_vpn_bundle(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id

    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Подписка не активна", show_alert=True)
            return

    # ✅ МГНОВЕННЫЙ ответ Telegram
    await cb.answer("⏳ Генерирую конфиг, подожди…")

    # ✅ Вся тяжёлая логика — в фоне
    asyncio.create_task(_generate_and_send_vpn(cb, tg_id))

async def _generate_and_send_vpn(cb: CallbackQuery, tg_id: int):
    try:
        async with session_scope() as session:
            peer = await vpn_service.ensure_peer(session, tg_id)
            await session.commit()

        conf_text = vpn_service.build_wg_conf(peer, user_label=str(tg_id))

        conf_file = BufferedInputFile(
            conf_text.encode("utf-8"),
            filename="wg.conf",
        )

        await cb.message.answer_document(
            document=conf_file,
            caption="✅ WireGuard конфиг",
        )

    except Exception as e:
        await cb.message.answer(
            f"❌ Ошибка при создании VPN:\n{type(e).__name__}: {e}"
        )

