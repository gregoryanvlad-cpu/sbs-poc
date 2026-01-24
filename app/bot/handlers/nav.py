from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone

import qrcode
from aiogram import Router
from aiogram.types import CallbackQuery, BufferedInputFile
from dateutil.relativedelta import relativedelta

from app.bot.keyboards import (
    kb_back_home,
    kb_cabinet,
    kb_confirm_reset,
    kb_main,
    kb_pay,
    kb_vpn,
)
from app.bot.ui import days_left, fmt_dt, utcnow
from app.core.config import settings
from app.db.session import session_scope
from app.repo import extend_subscription, get_subscription
from app.services.vpn.service import vpn_service

router = Router()


# ===== helpers =====

def _is_sub_active(sub_end_at: datetime | None) -> bool:
    if not sub_end_at:
        return False
    if sub_end_at.tzinfo is None:
        sub_end_at = sub_end_at.replace(tzinfo=timezone.utc)
    return sub_end_at > utcnow()


# ===== navigation =====

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
        await cb.message.edit_text(
            "❓ FAQ\n\n"
            "— Как оплатить? В разделе «Оплата»\n"
            "— Как получить VPN? Раздел «VPN»",
            reply_markup=kb_back_home(),
        )
        await cb.answer()
        return

    if where == "support":
        await cb.message.edit_text(
            "🛠 Поддержка\n\nНапиши сюда: @support",
            reply_markup=kb_back_home(),
        )
        await cb.answer()
        return

    await cb.answer("Неизвестный раздел")


# ===== payment (mock) =====

@router.callback_query(lambda c: c.data and c.data.startswith("pay:mock:"))
async def on_mock_pay(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id

    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        now = utcnow()
        base = sub.end_at if sub.end_at and sub.end_at > now else now
        new_end = base + relativedelta(months=settings.period_months)

        await extend_subscription(
            session,
            tg_id,
            months=settings.period_months,
            days_legacy=settings.period_days,
        )

        sub.end_at = new_end
        sub.is_active = True
        sub.status = "active"

        await session.commit()

    await cb.answer("Оплата успешна")
    await cb.message.edit_text(
        f"✅ Оплата успешна\n\nПодписка до: {fmt_dt(new_end)}",
        reply_markup=kb_main(),
    )


# ===== VPN =====

@router.callback_query(lambda c: c.data == "vpn:guide")
async def on_vpn_guide(cb: CallbackQuery) -> None:
    await cb.message.edit_text(
        "📖 Инструкция\n\n"
        "1) Нажми «Отправить конфиг + QR»\n"
        "2) Импортируй в WireGuard\n"
        f"3) Конфиг удалится через {settings.auto_delete_seconds} сек.",
        reply_markup=kb_vpn(),
    )
    await cb.answer()


@router.callback_query(lambda c: c.data == "vpn:reset:confirm")
async def on_vpn_reset_confirm(cb: CallbackQuery) -> None:
    await cb.message.edit_text(
        "♻️ Сбросить VPN?\nСтарый конфиг перестанет работать.",
        reply_markup=kb_confirm_reset(),
    )
    await cb.answer()


@router.callback_query(lambda c: c.data == "vpn:reset")
async def on_vpn_reset(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id

    async with session_scope() as session:
        await vpn_service.rotate_peer(session, tg_id, reason="manual_reset")
        await session.commit()

    await cb.answer("VPN сброшен")
    await cb.message.edit_text(
        "♻️ VPN сброшен. Получи новый конфиг.",
        reply_markup=kb_vpn(),
    )


# ===== VPN CONFIG (ВАЖНОЕ МЕСТО) =====

@router.callback_query(lambda c: c.data == "vpn:bundle")
async def on_vpn_bundle(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id

    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Подписка не активна", show_alert=True)
            return

        try:
            peer = await vpn_service.ensure_peer(session, tg_id)
            await session.commit()
        except Exception:
            # SSH может упасть — мы НЕ ломаем UX
            await cb.answer(
                "⚠️ VPN сервер временно недоступен.\n"
                "Конфиг сгенерирован, попробуй подключиться позже.",
                show_alert=True,
            )

            peer = {
                "client_private_key_plain": vpn_service.gen_keys()[0]
                if hasattr(vpn_service, "gen_keys")
                else None,
                "client_ip": vpn_service._alloc_ip(tg_id),
            }

    if not peer.get("client_private_key_plain"):
        await cb.answer("❌ Не удалось сгенерировать ключ", show_alert=True)
        return

    conf_text = vpn_service.build_wg_conf(peer, user_label=str(tg_id))

    qr_img = qrcode.make(conf_text)
    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    buf.seek(0)

    conf_file = BufferedInputFile(conf_text.encode(), filename="wg.conf")
    qr_file = BufferedInputFile(buf.getvalue(), filename="wg.png")

    msg_conf = await cb.message.answer_document(
        document=conf_file,
        caption=f"WireGuard конфиг. Удалится через {settings.auto_delete_seconds} сек.",
    )
    msg_qr = await cb.message.answer_photo(
        photo=qr_file,
        caption="QR для WireGuard",
    )

    await cb.answer()

    async def _cleanup():
        await asyncio.sleep(settings.auto_delete_seconds)
        for m in (msg_conf, msg_qr):
            try:
                await m.delete()
            except Exception:
                pass
        try:
            await cb.message.edit_text("Главное меню:", reply_markup=kb_main())
        except Exception:
            pass

    asyncio.create_task(_cleanup())
