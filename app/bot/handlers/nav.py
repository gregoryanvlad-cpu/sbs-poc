from __future__ import annotations

import asyncio
import io
import json
from datetime import datetime, timezone

import qrcode
from aiogram import Router
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from dateutil.relativedelta import relativedelta
from sqlalchemy import select

from app.bot.auth import is_owner
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
from app.db.models import Payment, User
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.repo import extend_subscription, get_subscription
from app.services.vpn.service import vpn_service

router = Router()


def _is_sub_active(sub_end_at: datetime | None) -> bool:
    if not sub_end_at:
        return False
    if sub_end_at.tzinfo is None:
        sub_end_at = sub_end_at.replace(tzinfo=timezone.utc)
    return sub_end_at > utcnow()


async def _get_yandex_membership(session, tg_id: int) -> YandexMembership | None:
    q = (
        select(YandexMembership)
        .where(YandexMembership.tg_id == tg_id)
        .order_by(YandexMembership.id.desc())
        .limit(1)
    )
    res = await session.execute(q)
    return res.scalar_one_or_none()


async def _cleanup_flow_messages_for_user(bot, chat_id: int, tg_id: int) -> None:
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

        user.flow_state = None
        user.flow_data = None
        await session.commit()


@router.callback_query(lambda c: c.data and c.data.startswith("nav:"))
async def on_nav(cb: CallbackQuery) -> None:
    where = cb.data.split(":", 1)[1]

    if where == "home":
        await _cleanup_flow_messages_for_user(cb.bot, cb.message.chat.id, cb.from_user.id)
        try:
            await cb.message.edit_text("Главное меню:", reply_markup=kb_main())
        except Exception:
            pass
        await cb.answer()
        return

    if where == "cabinet":
        async with session_scope() as session:
            sub = await get_subscription(session, cb.from_user.id)
            ym = await _get_yandex_membership(session, cb.from_user.id)

            q = (
                select(Payment)
                .where(Payment.tg_id == cb.from_user.id)
                .order_by(Payment.id.desc())
                .limit(5)
            )
            res = await session.execute(q)
            payments = list(res.scalars().all())

        y_status = ym.status if ym else "не подключено"
        y_login = ym.yandex_login if (ym and ym.yandex_login) else "—"

        pay_lines = [f"• {p.amount} {p.currency} / {p.provider} / {p.status}" for p in payments]
        pay_text = "\n".join(pay_lines) if pay_lines else "• оплат пока нет"

        text = (
            "👤 <b>Личный кабинет</b>\n\n"
            f"🆔 ID: <code>{cb.from_user.id}</code>\n\n"
            f"💳 Подписка: {'активна ✅' if _is_sub_active(sub.end_at) else 'не активна ❌'}\n"
            f"📅 До: {fmt_dt(sub.end_at)}\n"
            f"⏳ Осталось: {days_left(sub.end_at)} дн.\n\n"
            "🟡 <b>Yandex Plus</b>\n"
            f"— Статус: <b>{y_status}</b>\n"
            f"— Логин: <code>{y_login}</code>\n\n"
            "🧾 <b>Последние оплаты</b>\n"
            f"{pay_text}"
        )
        try:
            await cb.message.edit_text(
                text,
                reply_markup=kb_cabinet(is_owner=is_owner(cb.from_user.id)),
                parse_mode="HTML",
            )
        except Exception:
            pass
        await cb.answer()
        return

    if where == "pay":
        try:
            await cb.message.edit_text(
                f"💳 Оплата\n\nТариф: {settings.price_rub} ₽ / {settings.period_months} мес.",
                reply_markup=kb_pay(),
            )
        except Exception:
            pass
        await cb.answer()
        return

    if where == "vpn":
        try:
            await cb.message.edit_text("🌍 VPN", reply_markup=kb_vpn())
        except Exception:
            pass
        await cb.answer()
        return

    if where == "yandex":
        async with session_scope() as session:
            sub = await get_subscription(session, cb.from_user.id)
            ym = await _get_yandex_membership(session, cb.from_user.id)

        if not _is_sub_active(sub.end_at):
            await cb.answer("Подписка не активна. Оплатите доступ.", show_alert=True)
            return

        # Если membership уже есть — показываем статус + ссылку, если ожидает вступления
        if ym and ym.yandex_login:
            buttons = []
            if ym.status in ("awaiting_join", "pending") and ym.invite_link:
                buttons.append([InlineKeyboardButton(text="🔗 Открыть приглашение", url=ym.invite_link)])
            buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])

            kb = InlineKeyboardMarkup(inline_keyboard=buttons)

            await cb.message.edit_text(
                "🟡 <b>Yandex Plus</b>\n\n"
                f"Ваш логин: <code>{ym.yandex_login}</code>\n"
                f"Статус: <b>{ym.status}</b>\n\n"
                "Логин подтверждён и не может быть изменён.",
                reply_markup=kb,
                parse_mode="HTML",
            )
            await cb.answer()
            return

        # переходим в режим ожидания логина (ввод сообщением)
        async with session_scope() as session:
            user = await session.get(User, cb.from_user.id)
            if user:
                user.flow_state = "await_yandex_login"
                user.flow_data = None
                await session.commit()

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔎 Посмотреть свой логин", url="https://id.yandex.ru")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
            ]
        )

        await cb.message.edit_text(
            "🟡 <b>Yandex Plus</b>\n\n"
            "Введите ваш логин Yandex ID.\n"
            "⚠️ После подтверждения изменить логин нельзя.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await cb.answer()

        # ✅ возвращаем подсказку-картинку (как было)
        photo = FSInputFile("app/bot/assets/yandex_login_hint.jpg")
        hint_msg = await cb.message.answer_photo(photo=photo)
        prompt_msg = await cb.message.answer("👇 Введите логин сообщением ниже")

        async with session_scope() as session:
            user = await session.get(User, cb.from_user.id)
            if user:
                user.flow_data = json.dumps({"hint_msg_ids": [hint_msg.message_id, prompt_msg.message_id]})
                await session.commit()

        return

    if where == "faq":
        text = (
            "❓ FAQ\n\n"
            "— Как оплатить? В разделе «Оплата»\n"
            "— Как получить VPN? Раздел «VPN»"
        )
        try:
            await cb.message.edit_text(text, reply_markup=kb_back_home())
        except Exception:
            pass
        await cb.answer()
        return

    if where == "support":
        try:
            await cb.message.edit_text(
                "🛠 Поддержка\n\nНапиши сюда: @support (заглушка)",
                reply_markup=kb_back_home(),
            )
        except Exception:
            pass
        await cb.answer()
        return

    await cb.answer("Неизвестный раздел")


@router.callback_query(lambda c: c.data and c.data.startswith("pay:mock"))
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

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
    )

    await cb.message.edit_text(
        "✅ <b>Оплата прошла успешно!</b>\n\n"
        "Для подключения перейдите в разделы:\n"
        "— 🟡 <b>Yandex Plus</b>\n"
        "— 🌍 <b>VPN</b>\n\n"
        "Спасибо, что выбрали наш сервис 💛",
        reply_markup=kb,
        parse_mode="HTML",
    )
    return


@router.callback_query(lambda c: c.data == "vpn:guide")
async def on_vpn_guide(cb: CallbackQuery) -> None:
    text = (
        "📖 Инструкция\n\n"
        "1) Нажми «Отправить конфиг + QR»\n"
        "2) Импортируй в WireGuard\n"
        f"3) Конфиг удалится через {settings.auto_delete_seconds} сек."
    )
    await cb.message.edit_text(text, reply_markup=kb_vpn())
    await cb.answer()


@router.callback_query(lambda c: c.data == "vpn:reset:confirm")
async def on_vpn_reset_confirm(cb: CallbackQuery) -> None:
    # ✅ FIX: запрет экрана reset_confirm без активной подписки
    async with session_scope() as session:
        sub = await get_subscription(session, cb.from_user.id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Подписка не активна", show_alert=True)
            return

    await cb.message.edit_text(
        "♻️ Сбросить VPN?\nСтарый конфиг перестанет работать.",
        reply_markup=kb_confirm_reset(),
    )
    await cb.answer()


@router.callback_query(lambda c: c.data == "vpn:reset")
async def on_vpn_reset(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    chat_id = cb.message.chat.id

    # ✅ FIX: запрет сброса VPN без активной подписки
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Подписка не активна", show_alert=True)
            return

    await cb.answer("Сбрасываю…")
    await cb.message.edit_text(
        "🔄 Сбрасываю VPN и готовлю новый конфиг…\n"
        "Это займёт несколько секунд.",
        reply_markup=kb_vpn(),
    )

    async def _do_reset_and_send():
        try:
            async with session_scope() as session:
                peer = await vpn_service.rotate_peer(session, tg_id, reason="manual_reset")
                await session.commit()

            conf_text = vpn_service.build_wg_conf(peer, user_label=str(tg_id))

            qr_img = qrcode.make(conf_text)
            buf = io.BytesIO()
            qr_img.save(buf, format="PNG")
            buf.seek(0)

            conf_file = BufferedInputFile(
                conf_text.encode(),
                filename=f"SBS_{tg_id}_{datetime.now().strftime('%d-%m-%Y')}.conf",
            )
            qr_file = BufferedInputFile(buf.getvalue(), filename="wg.png")

            msg_conf = await cb.bot.send_document(
                chat_id=chat_id,
                document=conf_file,
                caption=f"WireGuard конфиг (после сброса). Будет удалён через {settings.auto_delete_seconds} сек.",
            )
            msg_qr = await cb.bot.send_photo(
                chat_id=chat_id,
                photo=qr_file,
                caption="QR для WireGuard (после сброса)",
            )

            async def _cleanup():
                await asyncio.sleep(settings.auto_delete_seconds)
                for m in (msg_conf, msg_qr):
                    try:
                        await cb.bot.delete_message(chat_id=chat_id, message_id=m.message_id)
                    except Exception:
                        pass

            asyncio.create_task(_cleanup())

        except Exception:
            try:
                await cb.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ Не удалось сбросить VPN из-за временной ошибки. Попробуй ещё раз через минуту.",
                )
            except Exception:
                pass

    asyncio.create_task(_do_reset_and_send())


@router.callback_query(lambda c: c.data == "vpn:bundle")
async def on_vpn_bundle(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id

    # ✅ FIX: запрет выдачи VPN без активной подписки
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Подписка не активна", show_alert=True)
            return

        try:
            peer = await vpn_service.ensure_peer(session, tg_id)
            await session.commit()
        except Exception:
            await cb.answer(
                "⚠️ VPN сервер временно недоступен.\n"
                "Попробуй ещё раз через минуту.",
                show_alert=True,
            )
            return

    conf_text = vpn_service.build_wg_conf(peer, user_label=str(tg_id))

    qr_img = qrcode.make(conf_text)
    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    buf.seek(0)

    conf_file = BufferedInputFile(
        conf_text.encode(),
        filename=f"SBS_{tg_id}_{datetime.now().strftime('%d-%m-%Y')}.conf",
    )
    qr_file = BufferedInputFile(buf.getvalue(), filename="wg.png")

    msg_conf = await cb.message.answer_document(
        document=conf_file,
        caption=f"WireGuard конфиг. Будет удалён через {settings.auto_delete_seconds} сек.",
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
