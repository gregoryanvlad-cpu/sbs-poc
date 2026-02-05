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
from app.services.referrals.service import referral_service

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
    """
    Legacy cleanup: раньше тут были подсказки/скрины для ввода логина.
    Сейчас логин не вводим, но чистилка остаётся безопасной.
    """
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
            ref_code = await referral_service.ensure_ref_code(session, cb.from_user.id)
            active_refs = await referral_service.count_active_referrals(session, cb.from_user.id)
            bal_av, bal_pend, bal_paid = await referral_service.get_balances(session, tg_id=cb.from_user.id)
            inviter_id = await referral_service.get_inviter_tg_id(session, tg_id=cb.from_user.id)

            q = (
                select(Payment)
                .where(Payment.tg_id == cb.from_user.id)
                .order_by(Payment.id.desc())
                .limit(5)
            )
            res = await session.execute(q)
            payments = list(res.scalars().all())

        pay_lines = [f"• {p.amount} {p.currency} / {p.provider} / {p.status}" for p in payments]
        pay_text = "\n".join(pay_lines) if pay_lines else "• оплат пока нет"

        inviter_line = (
            f"— Вас пригласил: <code>{inviter_id}</code>\n" if inviter_id else "— Вы пришли: <b>самостоятельно</b>\n"
        )

        # Новый статус Yandex: без логинов, показываем семью/слот/наличие ссылки.
        if ym and ym.invite_link:
            y_text = (
                f"— Семья: <code>{getattr(ym, 'account_label', '—') or '—'}</code>\n"
                f"— Слот: <b>{getattr(ym, 'slot_index', '—') or '—'}</b>\n"
                "— Приглашение: ✅ есть"
            )
        else:
            y_text = "— Приглашение: ❌ не выдано"

        text = (
            "👤 <b>Личный кабинет</b>\n\n"
            f"🆔 ID: <code>{cb.from_user.id}</code>\n\n"
            f"💳 Подписка: {'активна ✅' if _is_sub_active(sub.end_at) else 'не активна ❌'}\n"
            f"📅 До: {fmt_dt(sub.end_at)}\n"
            f"⏳ Осталось: {days_left(sub.end_at)} дн.\n\n"
            "🟡 <b>Yandex Plus</b>\n"
            f"{y_text}\n\n"
            "🧾 <b>Последние оплаты</b>\n"
            f"{pay_text}"
            "\n\n👥 <b>Рефералы</b>\n"
            f"{inviter_line}"
            f"— Активных: <b>{active_refs}</b>\n"
            f"— Баланс: <b>{bal_av} ₽</b> (ожидание {bal_pend} ₽)\n"
            "— Реферал засчитывается после первой оплаты другом.\n"
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

    if where == "referrals":
        async with session_scope() as session:
            user = await session.get(User, cb.from_user.id)
            if not user:
                user = await ensure_user(session, cb.from_user.id)
                await session.commit()
            code = await referral_service.ensure_ref_code(session, user)

            active_cnt = await referral_service.count_active_referrals(session, cb.from_user.id)
            pending_sum, avail_sum = await referral_service.get_balance(session, cb.from_user.id)
            pct = await referral_service.current_percent(session, cb.from_user.id)
            inviter_id = await referral_service.get_inviter_tg_id(session, tg_id=cb.from_user.id)
            refs = await referral_service.list_referrals_summary(session, tg_id=cb.from_user.id, limit=15)

            # bot username (optional)
            bot_username = getattr(settings, "bot_username", None)
            deep_link = (
                f"https://t.me/{bot_username}?start=ref_{code}"
                if bot_username
                else f"/start ref_{code}"
            )

            inviter_line = (
                f"— Вас пригласил: <code>{inviter_id}</code>\n\n" if inviter_id else "— Вы пришли: <b>самостоятельно</b>\n\n"
            )

            refs_lines = []
            for r in refs:
                dt = r.get("activated_at")
                dt_s = fmt_dt(dt) if dt else "—"
                refs_lines.append(
                    f"• <code>{r['referred_tg_id']}</code> — всего <b>{r['total']} ₽</b> "
                    f"(доступно {r['available']} / ожид. {r['pending']} / выплач. {r['paid']}) — активирован {dt_s}"
                )

            refs_block = "\n".join(refs_lines) if refs_lines else "— Пока нет активных рефералов (засчитаются после первой оплаты)"

            text = (
                "👥 <b>Реферальная программа</b>\n\n"
                "Реферал засчитывается <b>после первой оплаты</b> вашим другом.\n"
                + inviter_line
                + f"Ваша ссылка:\n<code>{deep_link}</code>\n\n"
                + f"Активных рефералов: <b>{active_cnt}</b>\n"
                + f"Ваш текущий уровень: <b>{pct}%</b>\n\n"
                + f"Баланс (ожидает): <b>{pending_sum} ₽</b>\n"
                + f"Баланс (доступно): <b>{avail_sum} ₽</b>\n"
                + f"Минимум на вывод: <b>{int(getattr(settings, 'referral_min_payout_rub', 50) or 50)} ₽</b>\n\n"
                + "<b>Ваши активные рефералы</b>\n"
                + refs_block
            )

        buttons = []
        if bot_username:
            buttons.append([InlineKeyboardButton(text="📣 Поделиться ссылкой", url=f"https://t.me/share/url?url={deep_link}")])
        buttons.append([InlineKeyboardButton(text="💸 Вывести", callback_data="ref:withdraw")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:cabinet")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        try:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
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

        buttons: list[list[InlineKeyboardButton]] = []

        # Если ссылка уже есть — показываем кнопку открыть.
        if ym and ym.invite_link:
            buttons.append([InlineKeyboardButton(text="🔗 Открыть приглашение", url=ym.invite_link)])
            # Главное — ссылка всегда доступна здесь.
            info = (
                "🟡 <b>Yandex Plus</b>\n\n"
                "✅ Приглашение уже выдано и доступно по кнопке ниже.\n\n"
                f"Семья: <code>{getattr(ym, 'account_label', '—') or '—'}</code>\n"
                f"Слот: <b>{getattr(ym, 'slot_index', '—') or '—'}</b>\n\n"
                "Если ты не успел перейти — просто открой приглашение отсюда."
            )
        else:
            # Ссылки ещё не было — выдаём по кнопке.
            buttons.append([InlineKeyboardButton(text="Получить приглашение", callback_data="yandex:issue")])
            info = (
                "🟡 <b>Yandex Plus</b>\n\n"
                "Нажми кнопку ниже — я выдам тебе приглашение в семейную подписку.\n"
                "После выдачи ссылка останется в этом разделе."
            )

        buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        try:
            await cb.message.edit_text(info, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass

        await cb.answer()
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

        # process referral earnings (first payment activates referral)
        pay = await session.scalar(
            select(Payment)
            .where(Payment.tg_id == tg_id)
            .order_by(Payment.id.desc())
            .limit(1)
        )
        if pay:
            await referral_service.on_successful_payment(session, pay)

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


from app.bot.keyboards import faq_kb
from aiogram import Router, F
from aiogram.types import CallbackQuery

router = Router()

ABOUT_TEXT = (
    "ℹ️ О сервисе\n\n"
    "Сервис предоставляет платные услуги по технической настройке и сопровождению "
    "доступа к цифровым сервисам.\n\n"
    "Для оказания услуг используются серверы, размещённые в Нидерландах — "
    "юрисдикции с одной из наиболее развитых телекоммуникационных инфраструктур. "
    "Используемые серверные мощности относятся к категории высоконагруженных "
    "и дорогостоящих решений.\n\n"
    "Исполнитель ориентирован на качество услуг и сохранение деловой репутации. "
    "Сервис не продаёт и не распространяет подписки или контент третьих лиц."
)

OFFER_TEXT = (
    "📄 Публичная оферта\n\n"
    "Используя сервис, вы принимаете условия публичной оферты "
    "на возмездное оказание услуг по технической поддержке и настройке цифровых сервисов.\n\n"
    "Полный текст оферты регулирует условия оказания услуг, ответственность сторон, "
    "порядок оплаты, возвратов и обработки персональных данных."
)

@router.callback_query(F.data == "faq_about")
async def faq_about(cb: CallbackQuery):
    await cb.message.answer(ABOUT_TEXT)
    await cb.answer()

@router.callback_query(F.data == "faq_offer")
async def faq_offer(cb: CallbackQuery):
    await cb.message.answer(OFFER_TEXT)
    await cb.answer()
