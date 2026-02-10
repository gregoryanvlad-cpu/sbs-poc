from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from datetime import datetime, timezone

import qrcode
from aiogram import Router
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from dateutil.relativedelta import relativedelta
from sqlalchemy import select

from app.bot.auth import is_owner
from app.bot.keyboards import (
    kb_back_home,
    kb_back_faq,
    kb_cabinet,
    kb_confirm_reset,
    kb_faq,
    kb_main,
    kb_pay,
    kb_vpn,
    kb_vpn_guide_platforms,
    kb_vpn_guide_back,
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

# Store message ids of iOS guide screenshots to delete on Back
VPN_BUNDLE_COUNTER: dict[int, tuple[str, int]] = {}

IOS_GUIDE_MEDIA: dict[int, list[int]] = {}


def _today_key() -> str:
    """Return current date key used for per-day counters (UTC)."""
    return datetime.now(timezone.utc).date().isoformat()


def _next_vpn_bundle_filename(tg_id: int) -> str:
    """Generate a unique filename for today's *downloads*.

    NOTE: The peer/config itself must stay the same until user presses
    "Сбросить VPN". We only change the filename so clients that cache by name
    (esp. iOS) can re-import.

    Format: SBS_<tg_id>_<N>.conf where N starts from 1 each day.
    """
    today = _today_key()
    prev = VPN_BUNDLE_COUNTER.get(tg_id)
    if not prev or prev[0] != today:
        n = 1
    else:
        n = prev[1] + 1
    VPN_BUNDLE_COUNTER[tg_id] = (today, n)
    return f"SBS_{tg_id}_{n}.conf"


def _reset_vpn_bundle_counter(tg_id: int) -> None:
    """Reset per-day bundle filename counter for the user.

    Called on VPN reset and on full user reset.
    """
    # Start numbering from 1 after reset (on next выдача).
    VPN_BUNDLE_COUNTER.pop(tg_id, None)



async def _safe_cb_answer(cb: CallbackQuery) -> None:
    """Best-effort callback answer (avoid 'query is too old' noise)."""
    try:
        await cb.answer()
    except Exception:
        pass


def _load_wg_instructions() -> dict:
    """Load device-specific WireGuard instructions from instructions.json.

    Best-effort: if file missing or invalid, return an empty dict.
    """
    try:
        # instructions.json is stored at project root
        root = Path(__file__).resolve().parents[3]
        p = root / "instructions.json"
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fmt_instruction_block(lines: list[str]) -> str:
    if not lines:
        return "—"
    return "\n".join(lines)


async def _build_home_text() -> str:
    """Main menu text with best-effort VPN status."""
    line = "🌍 VPN: статус недоступен"
    try:
        st = await asyncio.wait_for(vpn_service.get_server_status(), timeout=4)
        if st.get("ok"):
            cpu = st.get("cpu_load_percent")
            act = st.get("active_peers")
            tot = st.get("total_peers")
            if cpu is not None and act is not None and tot is not None:
                cpu_str = f"{cpu:.1f}%" if cpu >= 0.1 else ("&lt;0.1%" if cpu > 0 else "0.0%")
                line = f"🌍 Нагрузка на VPN сейчас составляет: <b>{cpu_str}</b>"
    except Exception:
        pass

    return "🏠 <b>Главное меню</b>\n" + line


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
    # Answer ASAP for *all* nav callbacks to avoid Telegram callback timeouts.
    # Some branches do DB/SSH/network work and can take a few seconds.
    await _safe_cb_answer(cb)

    where = cb.data.split(":", 1)[1]

    if where == "home":
        # Home text may wait on VPN status; callback already answered above.
        await _cleanup_flow_messages_for_user(cb.bot, cb.message.chat.id, cb.from_user.id)
        try:
            await cb.message.edit_text(await _build_home_text(), reply_markup=kb_main(), parse_mode="HTML")
        except Exception:
            pass
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
                f"— № Места: <b>{getattr(ym, 'slot_index', '—') or '—'}</b>\n"
                "— Приглашение: ✅ есть"
            )
        else:
            y_text = "— Приглашение: ❌ не выдано"

        text = (
            "👤 <b>Личный кабинет</b>\n\n"
            f"🆔 ID: <code>{cb.from_user.id}</code>\n\n"
            f"💳 Подписка: {'активна ✅' if _is_sub_active(sub.end_at) else 'не активна ❌'}\n"
            f"📅 Активна до: {fmt_dt(sub.end_at)}\n"
            "🟡 <b>Yandex Plus</b>\n"
            f"{y_text}\n\n"
            "🧾 <b>Последние оплаты</b>\n"
            f"{pay_text}"
            "\n\n👥 <b>Рефералы</b>\n"
            f"{inviter_line}"
            f"— Активных: <b>{active_refs}</b>\n"
            f"— Баланс: <b>{bal_av} ₽</b> (В холде: {bal_pend} ₽)\n"
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
        await _safe_cb_answer(cb)
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
        await _safe_cb_answer(cb)
        return

    if where == "pay":
        try:
            await cb.message.edit_text(
                f"💳 Оплата\n\nТариф: {settings.price_rub} ₽ / {settings.period_months} мес.",
                reply_markup=kb_pay(),
            )
        except Exception:
            pass
        await _safe_cb_answer(cb)
        return

    if where == "vpn":
        try:
            await cb.message.edit_text("🌍 VPN", reply_markup=kb_vpn())
        except Exception:
            pass
        await _safe_cb_answer(cb)
        return

    if where == "yandex":
        async with session_scope() as session:
            sub = await get_subscription(session, cb.from_user.id)
            ym = await _get_yandex_membership(session, cb.from_user.id)

        
        if not _is_sub_active(sub.end_at):
            try:
                await cb.message.edit_text(
                    "🟡 <b>Yandex Plus</b>\n\n"
                    "🚫 Подписка не активна. Чтобы открыть доступ — оплати подписку в разделе «Оплата».",
                    reply_markup=kb_back_home(),
                    parse_mode="HTML",
                )
            except Exception:
                pass
            await _safe_cb_answer(cb)
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
                "Нажмите кнопку ниже — вам будет выслано приглашение в семейную подписку.\n"
                "После выдачи ссылка останется в этом разделе."
            )

        buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        try:
            await cb.message.edit_text(info, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass

        await _safe_cb_answer(cb)
        return

    if where == "faq":
        text = (
            "❓ FAQ\n\n"
            "Выберите раздел ниже.\n"
        )
        try:
            await cb.message.edit_text(text, reply_markup=kb_faq())
        except Exception:
            try:
                await cb.message.answer(text, reply_markup=kb_faq())
            except Exception:
                pass
        await _safe_cb_answer(cb)
        return

    if where == "support":
        try:
            await cb.message.edit_text(
                "🛠 Поддержка\n\n"
                "По всем вопросам пиши сюда: @sbsmanager_bot\n\n"
                "Контакты для связи:\n"
                "sbs@sertera.group",
                reply_markup=kb_back_home(),
            )
        except Exception:
            pass
        await _safe_cb_answer(cb)
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
        "Теперь вам доступны следующие разделы:\n"
        "— 🟡 <b>Yandex Plus</b>\n"
        "— 🌍 <b>VPN</b>\n\n"
        "Спасибо, что выбрали наш сервис 💛",
        reply_markup=kb,
        parse_mode="HTML",
    )
    return


@router.callback_query(lambda c: c.data == "vpn:guide")
async def on_vpn_guide(cb: CallbackQuery) -> None:

    # cleanup iOS guide screenshots if they were sent previously
    ids = IOS_GUIDE_MEDIA.pop(cb.from_user.id, [])
    for mid in ids:
        try:
            await cb.bot.delete_message(chat_id=cb.message.chat.id, message_id=mid)
        except Exception:
            pass
    text = (
        "📖 <b>Инструкция по подключению WireGuard</b>\n\n"
        "1) Нажмите «📦 Отправить конфиг + QR»\n"
        "2) Импортируйте конфигурацию (.conf) в приложение WireGuard\n"
        f"3) Конфиг будет удалён автоматически через <b>{settings.auto_delete_seconds} сек.</b>\n\n"
        "Выберите устройство, чтобы открыть инструкцию:"
    )
    await cb.message.edit_text(text, reply_markup=kb_vpn_guide_platforms(), parse_mode="HTML")
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data and c.data.startswith("vpn:howto:"))
async def on_vpn_howto(cb: CallbackQuery) -> None:
    platform = cb.data.split(":", 2)[2]

    if platform == "ios":
        text = (
            "🍎 <b>iPhone / iPad — подключение WireGuard</b>\n\n"
            "1) Установите WireGuard из App Store\n"
            "2) В боте нажмите «📦 Отправить конфиг + QR»\n"
            "3) Откройте .conf и импортируйте в WireGuard\n\n"
            "Ниже придёт подробная инструкция со скриншотами."
        )
        await cb.message.edit_text(text, reply_markup=kb_vpn_guide_back(), parse_mode="HTML")

        # Send screenshots as album (will be removed on Back)
        base = Path(__file__).resolve().parents[1] / "assets" / "ios_wg"
        files = [
            base / "01_appstore.jpg",
            base / "02_bot_menu.jpg",
            base / "03_conf_message.jpg",
            base / "04_open_share.jpg",
            base / "05_share_sheet.jpg",
            base / "06_choose_wg.jpg",
            base / "07_enable.jpg",
        ]
        media = []
        for fp in files:
            if fp.exists():
                media.append(InputMediaPhoto(media=FSInputFile(str(fp))))
        sent_ids: list[int] = []
        if media:
            try:
                msgs = await cb.bot.send_media_group(chat_id=cb.message.chat.id, media=media)
                sent_ids = [m.message_id for m in msgs]
            except Exception:
                # fallback: send one by one
                for fp in files:
                    if not fp.exists():
                        continue
                    try:
                        mmsg = await cb.bot.send_photo(chat_id=cb.message.chat.id, photo=FSInputFile(str(fp)))
                        sent_ids.append(mmsg.message_id)
                    except Exception:
                        pass

        if sent_ids:
            IOS_GUIDE_MEDIA[cb.from_user.id] = sent_ids

        await _safe_cb_answer(cb)
        return

    instructions = _load_wg_instructions()
    lines = instructions.get(platform, [])

    if platform != "ios" and not lines:
        lines = [
            "Инструкция для этого устройства будет добавлена позже.",
            "Пока используйте импорт .conf в приложении WireGuard.",
        ]

    # Fallback for linux (often missing in json)
    if platform == "linux" and not lines:
        lines = [
            "1) Установите WireGuard (Ubuntu/Debian): <code>sudo apt update && sudo apt install wireguard</code>",
            "2) Скопируйте конфиг в <code>/etc/wireguard/wg0.conf</code>",
            "3) Запустите: <code>sudo wg-quick up wg0</code>",
            "4) Остановить: <code>sudo wg-quick down wg0</code>",
        ]

    title_map = {
        "android": "📱 Android",
        "ios": "🍎 iPhone / iPad",
        "windows": "💻 Windows",
        "macos": "🍏 macOS",
        "linux": "🐧 Linux",
    }
    title = title_map.get(platform, platform)

    text = (
        f"{title} — <b>подключение WireGuard</b>\n\n"
        f"{_fmt_instruction_block(lines)}\n\n"
        "Если что-то не подключается — попробуйте «♻️ Сбросить VPN» в меню VPN."
    )

    await cb.message.edit_text(text, reply_markup=kb_vpn_guide_back(), parse_mode="HTML")
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "vpn:reset:confirm")
async def on_vpn_reset_confirm(cb: CallbackQuery) -> None:
    # ✅ FIX: запрет экрана reset_confirm без активной подписки
    async with session_scope() as session:
        sub = await get_subscription(session, cb.from_user.id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Для доступа необходимо оплатить подписку!", show_alert=True)
            return

    await cb.message.edit_text(
        "♻️ Сбросить VPN?\n ВНИМАНИЕ: Старый конфиг перестанет работать.",
        reply_markup=kb_confirm_reset(),
    )
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "vpn:reset")
async def on_vpn_reset(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    _reset_vpn_bundle_counter(tg_id)
    chat_id = cb.message.chat.id

    # ✅ FIX: запрет сброса VPN без активной подписки
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Для доступа необходимо оплатить подписку!", show_alert=True)
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
                filename=f"SBS_{tg_id}.conf",
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
            await cb.answer("Для доступа необходимо оплатить подписку!", show_alert=True)
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
        # Keep the same active config content, but use a unique filename on each выдача
        # (helps iOS/Android caches and matches expected behaviour).
        filename=_next_vpn_bundle_filename(tg_id),
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

    await _safe_cb_answer(cb)

    async def _cleanup():
        await asyncio.sleep(settings.auto_delete_seconds)
        for m in (msg_conf, msg_qr):
            try:
                await m.delete()
            except Exception:
                pass
        try:
            await cb.message.edit_text(await _build_home_text(), reply_markup=kb_main(), parse_mode="HTML")
        except Exception:
            pass

    asyncio.create_task(_cleanup())


# --- FAQ: About / Offer ---

FAQ_ABOUT_TEXT = 'ℹ️ О сервисе\n\nСервис предоставляет платные услуги по технической настройке и сопровождению доступа к цифровым сервисам, включая настройку защищённого соединения и консультационную поддержку.\n\nДля оказания услуг используются серверные мощности, размещённые в Нидерландах. Используемая инфраструктура относится к категории высоконагруженных и дорогостоящих решений, что позволяет обеспечивать стабильную работу и предсказуемые технические параметры.\n\nИсполнитель ориентирован на качество оказания услуг и сохранение деловой репутации.\n\nСервис не является правообладателем контента, подписок или функционала сторонних сервисов и не осуществляет их продажу или распространение. Все действия Исполнителя ограничиваются технической поддержкой и организацией доступа к сервисам третьих лиц на условиях их правообладателей.'

FAQ_OFFER_TEXT = 'ПУБЛИЧНАЯ ОФЕРТА\nна возмездное оказание услуг по технической поддержке и настройке цифровых сервисов\n\nот 05 февраля 2026 года\n\nНастоящий документ является публичной офертой в соответствии со статьёй 435 и пунктом 2 статьи 437 Гражданского кодекса Российской Федерации.\n\nНастоящая оферта содержит предложение индивидуального предпринимателя (далее — «Исполнитель») заключить договор возмездного оказания услуг с любым дееспособным физическим лицом (далее — «Заказчик») на условиях, изложенных ниже.\n\n1. Общие положения\n1.1. Настоящая оферта регулирует отношения, связанные с оказанием Исполнителем платных услуг по технической настройке, поддержке и сопровождению доступа к цифровым сервисам, предоставляемым третьими лицами.\n1.2. Услуги включают, но не ограничиваются:\n— настройкой параметров защищённого соединения (VPN) для целей шифрования сетевого трафика;\n— технической помощью при подключении к цифровым платформам третьих лиц;\n— организацией приглашений и доступа в аккаунты и группы, поддерживаемые третьими лицами (в том числе сервисы Яндекс).\n1.3. Совершение Заказчиком любого действия в Telegram-боте Исполнителя, включая отправку команды, нажатие кнопки или ввод данных, означает:\n— ознакомление с условиями настоящей оферты;\n— полное и безоговорочное согласие с её условиями;\n— заключение договора возмездного оказания услуг.\n1.4. Договор считается заключённым с момента первого взаимодействия Заказчика с сервисом либо с момента оплаты услуг — в зависимости от выбранного типа доступа.\n\n2. Предмет договора\n2.1. Исполнитель оказывает Заказчику услуги технического характера, направленные на организацию и сопровождение доступа к цифровым сервисам.\n2.2. Исполнитель не является правообладателем контента, подписок или функционала сторонних сервисов, не осуществляет их продажу или перепродажу и не гарантирует их доступность.\n2.3. Все услуги оказываются дистанционно, без передачи материальных носителей.\n\n3. Права и обязанности сторон\n3.1. Исполнитель обязуется:\n— предоставить техническую возможность использования оказываемых услуг;\n— осуществлять обработку персональных данных в соответствии с Федеральным законом № 152-ФЗ;\n— предоставлять консультационную поддержку в рабочее время с 10:00 до 20:00 по московскому времени.\n3.2. Заказчик обязуется:\n— использовать услуги исключительно в личных, некоммерческих целях;\n— не передавать предоставленный доступ третьим лицам;\n— не использовать сервисы для противоправных целей, включая:\n  • доступ к ресурсам, запрещённым законодательством РФ;\n  • распространение запрещённого контента;\n  • осуществление сетевых атак, спама или мошенничества.\n3.3. Заказчик подтверждает, что самостоятельно ознакомился с правилами использования сторонних сервисов и несёт ответственность за их соблюдение.\n\n4. Стоимость и порядок оплаты\n4.1. Стоимость услуг указывается в интерфейсе Telegram-бота и выражается в рублях Российской Федерации.\n4.2. Оплата производится через платёжные системы, подключённые Исполнителем, с использованием безналичных способов оплаты.\n4.3. Оплата услуг означает подтверждение Заказчиком факта заказа и согласия с условиями настоящей оферты.\n\n5. Возврат денежных средств\n5.1. Возврат денежных средств возможен в случае:\n— если услуга не была оказана по вине Исполнителя;\n— если доступ не был предоставлен в течение 24 часов с момента оплаты.\n5.2. Возврат не производится, если:\n— услуга была оказана полностью или частично;\n— Заказчик нарушил условия настоящей оферты.\n5.3. Срок рассмотрения запроса на возврат — до 30 календарных дней.\n\n6. Ответственность и ограничения\n6.1. Исполнитель не несёт ответственности за:\n— изменение условий, ограничение или прекращение работы сторонних сервисов;\n— блокировку аккаунтов Заказчика третьими лицами;\n— перебои в работе сети Интернет у Заказчика.\n6.2. Услуги предоставляются «как есть». Исполнитель не гарантирует:\n— абсолютную анонимность;\n— конкретную скорость соединения;\n— доступ к определённым ресурсам.\n6.3. Использование технологий шифрования и VPN может быть ограничено или запрещено в отдельных юрисдикциях. Заказчик самостоятельно оценивает правовые риски использования таких технологий.\n\n7. Персональные данные\n7.1. Обрабатываются исключительно данные, необходимые для идентификации Заказчика в системе — Telegram ID.\n7.2. Персональные данные не передаются третьим лицам, за исключением случаев, предусмотренных законодательством РФ.\n7.3. Срок хранения данных — до 5 лет с момента последнего взаимодействия.\n\n8. Заключительные положения\n8.1. Все споры подлежат разрешению в судебном порядке по месту регистрации Исполнителя.\n8.2. Применимым правом является право Российской Федерации.\n8.3. Исполнитель вправе изменять условия настоящей оферты. Актуальная версия размещается в Telegram-боте.\n'

FAQ_PRIVACY_TEXT = """1. Общие положения

1.1. Настоящая Политика конфиденциальности (далее — «Политика») регулирует порядок обработки и защиты информации, которую Пользователь передаёт при использовании сервиса (далее — «Сервис»).

1.2. Используя Сервис, Пользователь подтверждает своё согласие с условиями Политики. Если Пользователь не согласен с условиями — он обязан прекратить использование Сервиса.

2. Сбор информации

2.1. Сервис может собирать следующие типы данных:
- идентификаторы аккаунта (логин, ID, никнейм и т.п.);
- техническую информацию (IP-адрес, данные о браузере, устройстве и операционной системе);
- историю взаимодействий с Сервисом.

2.2. Сервис не требует от Пользователя предоставления паспортных данных, документов, фотографий или другой личной информации, кроме минимально необходимой для работы.

3. Использование информации

3.1. Сервис может использовать полученную информацию исключительно для:
- обеспечения работы функционала;
- связи с Пользователем (в том числе для уведомлений и поддержки);
- анализа и улучшения работы Сервиса.

4. Передача информации третьим лицам

4.1. Администрация не передаёт полученные данные третьим лицам, за исключением случаев:
- если это требуется по закону;
- если это необходимо для исполнения обязательств перед Пользователем (например, при работе с платёжными системами);
- если Пользователь сам дал на это согласие.

5. Хранение и защита данных

5.1. Данные хранятся в течение срока, необходимого для достижения целей обработки.

5.2. Администрация принимает разумные меры для защиты данных, но не гарантирует абсолютную безопасность информации при передаче через интернет.

6. Отказ от ответственности

6.1. Пользователь понимает и соглашается, что передача информации через интернет всегда сопряжена с рисками.

6.2. Администрация не несёт ответственности за утрату, кражу или раскрытие данных, если это произошло по вине третьих лиц или самого Пользователя.

7. Изменения в Политике

7.1. Администрация вправе изменять условия Политики без предварительного уведомления.

7.2. Продолжение использования Сервиса после внесения изменений означает согласие Пользователя с новой редакцией Политики."""

FAQ_TERMS_TEXT = """1. Общие положения

1.1. Настоящее Пользовательское соглашение (далее — «Соглашение») регулирует порядок использования онлайн-сервиса (далее — «Сервис»), предоставляемого Администрацией.

1.2. Используя Сервис, включая запуск бота, регистрацию, оплату услуг или получение доступа к материалам, Пользователь подтверждает, что полностью ознакомился с условиями настоящего Соглашения и принимает их в полном объёме.

1.3. В случае несогласия с условиями Соглашения Пользователь обязан прекратить использование Сервиса.

2. Характер услуг и цифровых товаров

2.1. Сервис предоставляет цифровые товары и услуги нематериального характера, включая, но не ограничиваясь: информационные материалы, обучающие программы, консультации, цифровые продукты и сервисные услуги.

2.2. Материалы, предоставляемые через Сервис, могут включать:
- информацию из открытых источников;
- авторские материалы Администрации и/или третьих лиц;
- аналитические обзоры, подборки, рекомендации, структурированные данные.

2.3. Пользователь осознаёт и соглашается, что ценность цифровых товаров и услуг Сервиса заключается в систематизации, анализе, форме подачи, сопровождении, поддержке и обновлениях, а не в эксклюзивности отдельных фрагментов информации.

2.4. Сервис не заявляет и не гарантирует уникальность, исключительность или недоступность отдельных элементов материалов вне Сервиса.

3. Отказ от гарантий и ответственности

3.1. Сервис предоставляется на условиях «AS IS» («как есть»).

3.2. Администрация не гарантирует:
- соответствие Сервиса ожиданиям Пользователя;
- достижение каких-либо финансовых, коммерческих, профессиональных или иных результатов;
- бесперебойную и безошибочную работу Сервиса.

3.3. Администрация не несёт ответственности за:
- любые прямые или косвенные убытки, включая упущенную выгоду;
- последствия применения Пользователем полученных материалов;
- действия или бездействие третьих лиц;
- временные технические сбои и ограничения доступа.

3.4. Все решения о применении материалов, рекомендаций и услуг принимаются Пользователем самостоятельно и на его риск.

4. Законность использования

4.1. Сервис не предназначен для поощрения, организации или содействия противоправной деятельности.

4.2. Пользователь обязуется использовать Сервис исключительно в рамках применимого законодательства и правил третьих сторон.

4.3. Ответственность за законность использования материалов и услуг Сервиса полностью возлагается на Пользователя.

5. Интеллектуальная собственность

5.1. Все материалы, размещённые в Сервисе, охраняются законодательством об интеллектуальной собственности.

5.2. Пользователю запрещается копировать, распространять, перепродавать, передавать третьим лицам или иным образом использовать материалы Сервиса без разрешения правообладателя.

5.3. Нарушение прав интеллектуальной собственности может повлечь ограничение доступа к Сервису без компенсации.

6. Ограничение доступа

6.1. Администрация вправе приостановить или ограничить доступ Пользователя к Сервису в случае:
- нарушения условий настоящего Соглашения;
- выявления злоупотреблений;
- требований законодательства или платёжных провайдеров.

6.2. Ограничение доступа не освобождает Пользователя от обязательств, возникших ранее.

6.3. Администрация оставляет за собой право отказывать в обслуживании Пользователям, чьи действия могут создавать повышенные риски для Сервиса, платёжных провайдеров или третьих лиц.

7. Платежи и возвраты

7.1. Оплата услуг и цифровых товаров производится на условиях, указанных в Сервисе до момента оплаты.

7.2. В связи с нематериальным характером цифровых товаров и услуг, возврат денежных средств после предоставления доступа не осуществляется, за исключением случаев, указанных ниже.

7.3. Возврат средств возможен только если:
- услуга не была оказана по технической вине Сервиса;
- доступ к цифровому товару фактически не был предоставлен.

7.4. Для рассмотрения вопроса о возврате Пользователь обязан обратиться в службу поддержки в течение 24 часов с момента оплаты.

7.5. Решение о возврате принимается Администрацией индивидуально.

7.6. Пользователь подтверждает, что обязуется не инициировать возврат платежа (chargeback) через платёжные системы без предварительного обращения в службу поддержки Сервиса.

8. Конфиденциальность

8.1. Администрация может собирать минимально необходимые технические данные для обеспечения работы Сервиса.

8.2. Администрация принимает разумные меры для защиты данных, однако не гарантирует абсолютную безопасность передаваемой информации.

9. Изменение условий

9.1. Администрация вправе вносить изменения в настоящее Соглашение.

9.2. Актуальная версия Соглашения публикуется в Сервисе.

9.3. Продолжение использования Сервиса означает согласие Пользователя с обновлёнными условиями.

10. Контактная информация

10.1. По всем вопросам Пользователь может обратиться в службу поддержки через форму в самом боте.

Используя Сервис (в том числе запуская бота и/или вводя команду /start), Пользователь подтверждает, что ознакомлен с настоящим Соглашением и принимает его условия в полном объёме."""


@router.callback_query(lambda c: c.data == "faq:about")
async def faq_about(cb: CallbackQuery) -> None:
    try:
        await cb.message.edit_text(FAQ_ABOUT_TEXT, reply_markup=kb_back_faq())
    except Exception:
        await cb.message.answer(FAQ_ABOUT_TEXT, reply_markup=kb_back_faq())
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "faq:offer")
async def faq_offer(cb: CallbackQuery) -> None:
    data = FAQ_OFFER_TEXT.encode("utf-8")
    file = BufferedInputFile(data, filename="public_offer.txt")
    await cb.message.answer_document(file, caption="📄 Публичная оферта")
    await cb.message.answer("⬅️ Назад в FAQ", reply_markup=kb_back_faq())
    await _safe_cb_answer(cb)

@router.callback_query(lambda c: c.data == "faq:privacy")
async def faq_privacy(cb: CallbackQuery) -> None:
    data = FAQ_PRIVACY_TEXT.encode("utf-8")
    file = BufferedInputFile(data, filename="privacy_policy.txt")
    await cb.message.answer_document(file, caption="🔐 Политика конфиденциальности")
    await cb.message.answer("⬅️ Назад в FAQ", reply_markup=kb_back_faq())
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "faq:terms")
async def faq_terms(cb: CallbackQuery) -> None:
    data = FAQ_TERMS_TEXT.encode("utf-8")
    file = BufferedInputFile(data, filename="user_agreement.txt")
    await cb.message.answer_document(file, caption="📝 Пользовательское соглашение")
    await cb.message.answer("⬅️ Назад в FAQ", reply_markup=kb_back_faq())
    await _safe_cb_answer(cb)

