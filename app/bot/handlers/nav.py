from __future__ import annotations

import asyncio
import io
import json
import os
from html import escape as html_escape
from pathlib import Path
from datetime import datetime, timezone, timedelta

import qrcode
from aiogram import Router, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

# Copy-to-clipboard button (Bot API 7.11+). aiogram exposes it as CopyTextButton.
try:
    from aiogram.types import CopyTextButton  # type: ignore
except Exception:  # pragma: no cover
    CopyTextButton = None  # type: ignore
from dateutil.relativedelta import relativedelta
from sqlalchemy import select, func, literal, text

from app.bot.auth import is_owner
from app.repo import utcnow
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
    kb_kinoteka,
    kb_lte_vpn,
    kb_lte_main_menu,
)
from app.bot.ui import days_left, fmt_dt, utcnow
from app.core.config import settings
from app.db.models import Payment, User, LteVpnClient, Referral, ReferralEarning
from app.db.models.app_setting import AppSetting
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.repo import extend_subscription, get_subscription, get_price_rub, is_trial_available, set_trial_used, has_used_trial, set_app_setting_int, get_app_setting_int, has_successful_payments

from app.services.vpn.service import vpn_service
from app.services.referrals.service import referral_service
from app.services.lte_vpn.service import lte_vpn_service
from app.services.message_audit import audit_send_message

router = Router()


class PromoCodeFSM(StatesGroup):
    waiting_code = State()


def _promo_norm(code: str) -> str:
    code = (code or "").strip().upper()
    code = "".join(ch for ch in code if ch.isalnum() or ch in "_-")
    return code[:32]


async def _promo_defs(session) -> dict[str, int]:
    rows = (await session.execute(select(AppSetting).where(AppSetting.key.like("promo:def:%")))).scalars().all()
    out: dict[str, int] = {}
    for row in rows:
        code = str(row.key or "").split(":", 2)[-1].strip().upper()
        try:
            price = int(row.int_value or 0)
        except Exception:
            price = 0
        if code and price > 0:
            out[code] = price
    return out


async def _get_user_active_promo(session, tg_id: int) -> tuple[str, int] | None:
    rows = (await session.execute(select(AppSetting).where(AppSetting.key.like(f"promo:applied:{int(tg_id)}:%")))).scalars().all()
    for row in rows:
        code = str(row.key or "").split(":")[-1].strip().upper()
        price = int(row.int_value or 0)
        if code and price > 0:
            return code, price
    return None


def _fmt_countdown_to(dt: datetime | None) -> str:
    if not dt:
        return "—"
    try:
        now = datetime.now(timezone.utc)
        target = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        total = int((target - now).total_seconds())
        if total <= 0:
            return "меньше минуты"
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days} дн.")
        if days or hours:
            parts.append(f"{hours} ч.")
        if days or hours or minutes:
            parts.append(f"{minutes} мин.")
        parts.append(f"{seconds} сек.")
        return " ".join(parts)
    except Exception:
        return "—"


async def _render_pay_screen(message, tg_id: int) -> None:
    async with session_scope() as session:
        base_price = int(await get_price_rub(session) or 0)
        active_promo = await _get_user_active_promo(session, tg_id)
    if active_promo:
        promo_code, promo_price = active_promo
        text = (
            "💳 <b>Оплата</b>\n\n"
            f"Тариф: <s>{base_price} ₽</s> <b>{promo_price} ₽</b> / {settings.period_months} мес.\n"
            f"🎟 Применён промокод: <code>{html_escape(promo_code)}</code>\n\n"
            "Промокод уже применён к вашему аккаунту и будет использован при ближайшей оплате."
        )
        kb = kb_pay(price_rub=promo_price, original_price_rub=base_price, promo_code=promo_code)
    else:
        text = f"💳 <b>Оплата</b>\n\nТариф: <b>{base_price} ₽</b> / {settings.period_months} мес."
        kb = kb_pay(price_rub=base_price)
    try:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


TG_PROXY_URL = "https://t.me/proxy?server=mt.masterpix.org&port=443&secret=7ix9qzx1rb59pdRm9E7ivEp4cC5hcHBsZS5jb20"


def _family_upsell_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить место", callback_data="family:buy")],
            [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="nav:cabinet")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


async def _send_first_payment_family_upsell(bot: Bot, tg_id: int) -> None:
    try:
        await audit_send_message(
            bot,
            int(tg_id),
            "➕ <b>Можно добавить ещё одно место</b>\n\n"
            "Если хотите подключить второе устройство или дать доступ мужу, жене или ребёнку — "
            "добавьте ещё одно место в семейной группе.",
            kind="upsell_after_first_payment",
            reply_markup=_family_upsell_kb(),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def _collect_referral_payment_notification(session, payment: Payment) -> dict | None:
    earning = await session.scalar(
        select(ReferralEarning)
        .where(ReferralEarning.payment_id == int(payment.id))
        .order_by(ReferralEarning.id.desc())
        .limit(1)
    )
    if not earning:
        return None
    referral = await session.scalar(
        select(Referral)
        .where(Referral.referred_tg_id == int(payment.tg_id))
        .order_by(Referral.id.desc())
        .limit(1)
    )
    return {
        "referrer_tg_id": int(earning.referrer_tg_id),
        "earned_rub": int(getattr(earning, "earned_rub", 0) or 0),
        "is_activation": bool(referral and int(getattr(referral, "first_payment_id", 0) or 0) == int(payment.id)),
        "available_at": getattr(earning, "available_at", None),
    }


async def _send_referral_payment_notifications(bot: Bot, payload: dict | None) -> None:
    if not payload:
        return
    referrer_tg_id = int(payload.get("referrer_tg_id") or 0)
    if referrer_tg_id <= 0:
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="👥 Открыть рефералку", callback_data="nav:referrals")]]
    )
    if payload.get("is_activation"):
        try:
            await audit_send_message(
                bot,
                referrer_tg_id,
                "🎉 Ваш друг оплатил подписку по реферальной ссылке. Реферал активирован.",
                kind="referral_paid",
                reply_markup=kb,
            )
        except Exception:
            pass
    hold_note = ""
    available_at = payload.get("available_at")
    if isinstance(available_at, datetime):
        hold_note = f"\n\nДоступно к выводу после: <b>{available_at.astimezone(timezone.utc).astimezone().strftime('%d.%m.%Y %H:%M')}</b>."
    try:
        await audit_send_message(
            bot,
            referrer_tg_id,
            f"💸 Начислено реферальное вознаграждение: <b>{int(payload.get('earned_rub') or 0)} ₽</b>.{hold_note}",
            kind="referral_earning_created",
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception:
        pass


async def _mark_purchase_notified_once(session, *, provider_payment_id: str | None, payment_id: int | None) -> bool:
    """Return True only for the first successful admin notification for this payment."""
    marker = (provider_payment_id or "").strip() or (f"db:{int(payment_id)}" if payment_id is not None else "")
    if not marker:
        return True
    key = f"purchase_admin_notified:{marker}"
    try:
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
    except Exception:
        pass
    if await get_app_setting_int(session, key, default=0):
        return False
    await set_app_setting_int(session, key, 1)
    return True


def kb_tgproxy() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Подключить Антилаг-Telegram", url=TG_PROXY_URL)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


async def _notify_admins_new_purchase(
    bot: Bot,
    *,
    buyer_tg_id: int,
    amount_rub: int,
    months: int,
    provider: str,
    new_end_at: datetime | None,
    item_label: str = "Подписка VPN",
) -> None:
    """Best-effort: notify owner/admins about a successful purchase."""

    admin_ids: set[int] = set()
    try:
        admin_ids.add(int(settings.owner_tg_id))
    except Exception:
        pass
    try:
        admin_ids.update({int(x) for x in (settings.admin_tg_ids or [])})
    except Exception:
        pass
    # Don't spam buyer if they are admin
    admin_ids.discard(int(buyer_tg_id))
    if not admin_ids:
        return

    username = "—"
    full_name = "—"
    try:
        async with session_scope() as s:
            u = (await s.execute(select(User).where(User.tg_id == buyer_tg_id).limit(1))).scalar_one_or_none()
            if u:
                username = u.username or "—"
                full_name = ((u.first_name or "") + (" " + u.last_name if u.last_name else "")).strip() or "—"
    except Exception:
        pass

    end_str = fmt_dt(new_end_at) if new_end_at else "—"

    period_str = f"<b>{months} мес.</b>" if months > 0 else "<b>текущий цикл</b>"

    text = (
        "🧾 <b>Новая покупка</b>\n\n"
        f"Услуга: <b>{html_escape(item_label)}</b>\n"
        f"ID: <code>{buyer_tg_id}</code>\n"
        f"Профиль: @{username} | {full_name}\n"
        f"Сумма: <b>{amount_rub} ₽</b>\n"
        f"Период: {period_str}\n"
        f"Провайдер: <code>{html_escape(provider)}</code>\n"
        f"Активно до: <b>{end_str}</b>"
    )

    for aid in admin_ids:
        try:
            await bot.send_message(int(aid), text, parse_mode="HTML")
        except Exception:
            pass


async def _restore_wg_peers_after_payment(session, tg_id: int) -> None:
    """After a successful payment, re-enable WG peers disabled on expiration.

    Best-effort: never breaks payment flow.
    """
    try:
        await vpn_service.restore_expired_peers(session, tg_id, grace_hours=24)
    except Exception:
        # best-effort
        pass


def _kb_subscription_required(*, show_trial: bool) -> InlineKeyboardMarkup:
    rows = []
    if show_trial:
        rows.append([InlineKeyboardButton(text="🎁 Пробный период 5 дней", callback_data="trial:start")])
    rows.append([InlineKeyboardButton(text="💳 Купить подписку", callback_data="nav:pay")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _subscription_required_prompt(session, tg_id: int) -> tuple[str, InlineKeyboardMarkup, str]:
    """Return a friendly subscription prompt with concrete next-step buttons."""
    trial_available = await is_trial_available(session, tg_id)
    if trial_available:
        text = (
            "🔒 <b>Нужна активная подписка</b>\n\n"
            "Чтобы получить доступ, выберите удобный вариант:\n"
            "• активируйте <b>пробный период 5 дней</b>\n"
            "• или сразу <b>оформите платную подписку</b>."
        )
        alert_text = "Нужна подписка: можно взять пробный период или сразу оплатить."
    elif await has_used_trial(session, tg_id):
        text = (
            "🔒 <b>Нужна активная подписка</b>\n\n"
            "Ваш пробный период уже был использован.\n"
            "Чтобы продолжить пользоваться сервисом, оформите платную подписку."
        )
        alert_text = "Пробный период уже использован. Оформите платную подписку."
    else:
        text = (
            "🔒 <b>Нужна активная подписка</b>\n\n"
            "Чтобы получить доступ, оформите платную подписку."
        )
        alert_text = "Для доступа нужна платная подписка."
    return text, _kb_subscription_required(show_trial=trial_available), alert_text


async def _show_subscription_required_prompt(cb: CallbackQuery, session, tg_id: int) -> None:
    text, kb, alert_text = await _subscription_required_prompt(session, tg_id)
    try:
        await cb.answer(alert_text)
    except Exception:
        pass
    try:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass

# --- VPN-Region (VLESS + Reality) ---

# --- VPN-Region (VLESS + Reality) ---

from app.services.regionvpn import RegionVpnService
from app.db.models.region_vpn_session import RegionVpnSession


def _region_service() -> RegionVpnService:
    return RegionVpnService(
        ssh_host=settings.region_ssh_host,
        ssh_port=settings.region_ssh_port,
        ssh_user=settings.region_ssh_user,
        ssh_password=settings.region_ssh_password,
        xray_config_path=settings.region_xray_config_path,
        xray_api_port=settings.region_xray_api_port,
        max_clients=settings.region_max_clients,
    )


def _kb_region_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Получить конфиг", callback_data="region:get")],
            [InlineKeyboardButton(text="🔄 Сбросить VPN-Region", callback_data="region:reset")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:home")],
        ]
    )


def _kb_region_after_get() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌐 VPN-Region", callback_data="nav:region")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


@router.callback_query(lambda c: c.data == "nav:region")
async def on_nav_region(cb: CallbackQuery) -> None:
    text = f"""🌐 <b>VPN-Region</b>

Здесь выдается конфигурация для <b>VLESS + Reality</b> (обход блокировок).

📌 После выдачи вы получите:
• QR-код (можно сохранить в галерею и импортировать в Happ)
• ссылку <b>vless://</b> (можно скопировать и импортировать «Из буфера»)

⏳ Ссылка и QR удаляются автоматически через <b>{settings.auto_delete_seconds} сек.</b>
"""
    await cb.message.edit_text(text, reply_markup=_kb_region_menu(), parse_mode="HTML", disable_web_page_preview=True)
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "region:get")
async def on_region_get(cb: CallbackQuery) -> None:
    tg_id = int(cb.from_user.id)

    # Answer callback early so Telegram doesn't show an endless spinner
    # if something takes time or fails later.
    try:
        await cb.answer("Генерирую конфиг…")
    except Exception:
        pass

    # Subscription required (same gating as VPN)
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)

    if not sub or not sub.is_active:
        await cb.message.answer("❌ Для доступа нужен активный тариф. Оформите подписку в разделе «💳 Оплата».")
        await _safe_cb_answer(cb)
        return

    # Optional quota gating (best-effort)
    if settings.region_quota_gb and settings.region_quota_gb > 0:
        try:
            traffic = await _region_service().get_user_traffic_bytes(tg_id)
            if traffic:
                up, down = traffic
                used_gb = (up + down) / (1024 ** 3)
                if used_gb >= settings.region_quota_gb:
                    await cb.message.answer(
                        "⚠️ Достигнут лимит трафика для VPN-Region.\n"
                        "Если это ошибка — обратитесь в поддержку."
                    )
                    await _safe_cb_answer(cb)
                    return
        except Exception:
            # If stats are unavailable, don't block issuance.
            pass

    try:
        vless_url = await _region_service().ensure_client(tg_id)
    except RuntimeError as e:
        if str(e) == "server_overloaded":
            await cb.message.answer(
                "⚠️ Сервер VPN-Region сейчас перегружен.\n"
                "Попробуйте позже или используйте обычный VPN."
            )
            await _safe_cb_answer(cb)
            return
        raise

    # QR (may fail if the link is too long: QR versions are limited)
    qr_file: BufferedInputFile | None = None
    try:
        qr = qrcode.QRCode(
            error_correction=getattr(qrcode.constants, "ERROR_CORRECT_L", 1),
            box_size=10,
            border=3,
        )
        qr.add_data(vless_url)
        qr.make(fit=True)
        img = qr.make_image()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        qr_file = BufferedInputFile(buf.getvalue(), filename="vpn-region.png")
    except Exception:
        # Link may be too long for QR; we'll still send it as text/file.
        qr_file = None

    # Button that copies link to clipboard in 1 tap (supported in newer Telegram clients).
    copy_btn: InlineKeyboardButton | None = None
    if CopyTextButton is not None and 1 <= len(vless_url) <= 256:
        try:
            copy_btn = InlineKeyboardButton(
                text="📋 Скопировать ссылку",
                copy_text=CopyTextButton(text=vless_url),  # type: ignore[arg-type]
            )
        except Exception:
            copy_btn = None

    # We can't use vless:// in InlineKeyboardButton.url (Telegram blocks non-http(s) schemes).
    # Provide a copy button + App Store link.
    kb_rows: list[list[InlineKeyboardButton]] = []
    if copy_btn:
        kb_rows.append([copy_btn])

    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🍏 Happ Plus (App Store)",
                url="https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973",
            )
        ]
    )
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    kb_link = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    # Make the message clean and user-friendly.
    if copy_btn:
        howto = (
            "1) Нажмите кнопку <b>«📋 Скопировать ссылку»</b> — она копируется сразу.\n"
            "2) Откройте <b>Happ Plus</b> → «<b>+</b>» → <b>Из буфера</b>."
        )
    else:
        # Fallback if CopyTextButton isn't supported in current runtime.
        howto = (
            "1) Скопируйте ссылку ниже (долгий тап → «Копировать»).\n"
            "2) Откройте <b>Happ Plus</b> → «<b>+</b>» → <b>Из буфера</b>."
        )

    # Telegram message limit is 4096 chars. With mldsa65Verify the link can be very long,
    # so we fall back to sending it as a file.
    url_as_file: BufferedInputFile | None = None
    show_inline_link = len(vless_url) <= 3500
    if not show_inline_link:
        url_as_file = BufferedInputFile(vless_url.encode("utf-8"), filename="vpn-region-vless.txt")

    # Show full link inline only when it fits.
    link_block = (
        f"<code>{html_escape(vless_url)}</code>"
        if show_inline_link
        else "<i>Ссылка слишком длинная для сообщения — отправил её файлом ниже.</i>"
    )

    qr_hint = (
        "📷 <b>Через QR</b>: сохраните QR (долгий тап → «Сохранить») и импортируйте в Happ из галереи."
        if qr_file is not None
        else "📷 <b>QR</b>: ссылка слишком длинная для QR-кода, поэтому QR не отправлен. Используйте импорт из буфера."
    )

    link_text = f"""✅ <b>VPN-Region конфиг готов</b>

📌 <b>Как добавить в Happ Plus</b>
{howto}

{qr_hint}

🔗 <b>Ссылка для импорта</b>:
{link_block}

⏳ Сообщения удалятся через <b>{settings.auto_delete_seconds} сек.</b>
"""

    msg_link = await cb.message.answer(
        link_text,
        reply_markup=kb_link,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    msg_qr_id: int | None = None
    if qr_file is not None:
        msg_qr = await cb.message.answer_photo(photo=qr_file, caption="📷 QR для импорта (VPN-Region).")
        msg_qr_id = msg_qr.message_id

    msg_file_id: int | None = None
    if url_as_file is not None:
        msg_file = await cb.message.answer_document(
            document=url_as_file,
            caption="📎 VLESS-ссылка для импорта (VPN-Region).",
        )
        msg_file_id = msg_file.message_id

    # If we couldn't add a 1-tap copy button and the URL fits a Telegram message,
    # also send the raw URL as plain text (some clients treat it as selectable/clickable).
    msg_plain: int | None = None
    if copy_btn is None and show_inline_link:
        msg_plain_obj = await cb.message.answer(vless_url, disable_web_page_preview=True)
        msg_plain = msg_plain_obj.message_id

    async def _del_later(mid: int) -> None:
        await asyncio.sleep(settings.auto_delete_seconds)
        try:
            await cb.bot.delete_message(chat_id=cb.message.chat.id, message_id=mid)
        except Exception:
            pass

    asyncio.create_task(_del_later(msg_link.message_id))
    if msg_qr_id is not None:
        asyncio.create_task(_del_later(msg_qr_id))
    if msg_file_id is not None:
        asyncio.create_task(_del_later(msg_file_id))
    if msg_plain is not None:
        asyncio.create_task(_del_later(msg_plain))

    # callback already answered at the beginning (best-effort)


@router.callback_query(lambda c: c.data == "region:reset")
async def on_region_reset(cb: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, сбросить", callback_data="region:reset:do")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:region")],
        ]
    )
    await cb.message.answer(
        "⚠️ <b>Сброс VPN-Region</b>\n\n"
        "Это отключит текущий конфиг на сервере.\n"
        "После сброса нужно будет заново нажать «📦 Получить конфиг».",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "region:reset:do")
async def on_region_reset_do(cb: CallbackQuery) -> None:
    tg_id = int(cb.from_user.id)

    # Сначала отвечаем на callback, чтобы не ловить "query is too old"
    await _safe_cb_answer(cb)

    try:
        removed = await _region_service().revoke_client(tg_id)
    except Exception:
        removed = False
    # Clean up session tracking (single-device enforcement)
    try:
        async with session_scope() as s:
            row = await s.get(RegionVpnSession, tg_id)
            if row:
                await s.delete(row)
            await s.commit()
    except Exception:
        pass

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Получить конфиг", callback_data="region:get")],
            [InlineKeyboardButton(text="🌐 VPN-Region", callback_data="nav:region")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )

    if removed:
        text = "✅ <b>VPN-Region сброшен</b>\n\nТекущий конфиг отключён на сервере. Теперь можно получить новый."
    else:
        text = "ℹ️ <b>Активный VPN-Region конфиг не найден</b>\n\nМожно сразу нажать «📦 Получить конфиг»."

    await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "noop")
async def _noop(cb: CallbackQuery) -> None:
    # UI-only buttons
    await _safe_cb_answer(cb)

# Store message ids of iOS guide screenshots to delete on Back
VPN_BUNDLE_COUNTER: dict[int, tuple[str, int]] = {}

IOS_GUIDE_MEDIA: dict[int, list[int]] = {}

# Auto payment status watchers (in-process). Keyed by Payment.id.
_PAY_WATCH_TASKS: dict[int, asyncio.Task] = {}

# VPN location migration watchers (in-process). Keyed by tg_id.
_VPN_MIGRATE_TASKS: dict[int, asyncio.Task] = {}


def _vpn_flag(code: str) -> str:
    code = (code or "").upper()
    return {
        "NL": "🇳🇱",
        "DE": "🇩🇪",
        "TR": "🇹🇷",
        "US": "🇺🇸",
    }.get(code, "🌍")


def _load_vpn_servers() -> list[dict]:
    """Load VPN servers from VPN_SERVERS_JSON or build a safe default list.

    Each server dict may contain:
      code, name, host, port, user, password, interface,
      server_public_key, endpoint, dns

    Servers without host/user/endpoint/public_key are shown as "Подключается...".
    """

    servers_json = (os.environ.get("VPN_SERVERS_JSON") or "").strip()
    servers: list[dict] = []
    if servers_json:
        try:
            v = json.loads(servers_json)
            if isinstance(v, list):
                servers = [x for x in v if isinstance(x, dict)]
        except Exception:
            servers = []

    if not servers:
        # Default showcase list.
        # Populate NL from current WG_* env vars so at least one server works.
        pwd = os.environ.get("WG_SSH_PASSWORD")
        if pwd is not None and pwd.strip() == "":
            pwd = None
        servers = [
            {
                "code": os.environ.get("VPN_CODE", "NL"),
                "name": os.environ.get("VPN_NAME", "VPN-Нидерланды"),
                "host": os.environ.get("WG_SSH_HOST"),
                "port": int(os.environ.get("WG_SSH_PORT", "22")),
                "user": os.environ.get("WG_SSH_USER"),
                "password": pwd,
                "interface": os.environ.get("VPN_INTERFACE", "wg0"),
                "server_public_key": os.environ.get("VPN_SERVER_PUBLIC_KEY"),
                "endpoint": os.environ.get("VPN_ENDPOINT"),
                "dns": os.environ.get("VPN_DNS", "1.1.1.1"),
                "tc_dev": os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV"),
            },
            {"code": "DE", "name": "VPN-Германия"},
            {"code": "TR", "name": "VPN-Турция"},
            {"code": "US", "name": "VPN-США"},
        ]

    out: list[dict] = []
    for s in servers:
        code = str(s.get("code") or "").upper() or "XX"
        out.append(
            {
                "code": code,
                "name": str(s.get("name") or f"VPN-{code}"),
                "host": s.get("host"),
                "port": int(s.get("port") or 22),
                "user": s.get("user"),
                "password": s.get("password"),
                "interface": str(s.get("interface") or os.environ.get("VPN_INTERFACE", "wg0")),
                "server_public_key": s.get("server_public_key") or s.get("server_public") or s.get("public_key"),
                "endpoint": s.get("endpoint"),
                "dns": s.get("dns") or os.environ.get("VPN_DNS", "1.1.1.1"),
                "tc_dev": s.get("tc_dev") or s.get("wg_tc_dev") or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV"),
            }
        )
    return out



async def _next_vpn_bundle_filename_global(session) -> str:
    """Generate a global sequential filename for every issued WG config.

    Required format: sbsVPN<N>.conf where N is the total number of configs
    ever issued by the bot (across all users).
    """
    from app.repo import get_app_setting_int, set_app_setting_int

    n = await get_app_setting_int(session, 'vpn_conf_serial', default=0)
    n = int(n) + 1
    await set_app_setting_int(session, 'vpn_conf_serial', n)
    # Keep exactly the requested casing
    return f'wg{n}.conf'


async def _get_or_assign_vpn_bundle_filename_for_peer(session, peer_id: int | None) -> str:
    """Return a stable filename for the same peer, assigning a global serial once."""
    if not peer_id:
        return await _next_vpn_bundle_filename_global(session)
    key = f"vpn_conf_peer_serial:{int(peer_id)}"
    serial = await get_app_setting_int(session, key, default=0)
    if int(serial or 0) <= 0:
        serial = await get_app_setting_int(session, 'vpn_conf_serial', default=0)
        serial = int(serial) + 1
        await set_app_setting_int(session, 'vpn_conf_serial', serial)
        await set_app_setting_int(session, key, int(serial))
    return f"wg{int(serial)}.conf"


def _reset_vpn_bundle_counter(tg_id: int) -> None:
    """Legacy no-op (we use a global counter for filenames now).

    Keeping the function to avoid breaking call sites.
    """
    return



async def _safe_cb_answer(cb: CallbackQuery) -> None:
    """Best-effort callback answer (avoid 'query is too old' noise)."""
    try:
        await cb.answer()
    except Exception:
        pass


def _vpn_config_ttl_seconds() -> int:
    """VPN config messages should live for 3 minutes regardless of global auto-delete."""
    return 180


def _kb_vpn_missed_config() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏱ Я не успел получить конфиг", callback_data="vpn:my")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
            [InlineKeyboardButton(text="🛠 Поддержка", callback_data="nav:support")],
        ]
    )


async def _schedule_vpn_cleanup_and_followup(bot, *, chat_id: int, messages: list) -> None:
    """Delete VPN config messages after 3 minutes and then show a quick recovery keyboard."""
    await asyncio.sleep(_vpn_config_ttl_seconds())
    deleted_any = False
    for m in messages:
        if not m:
            continue
        try:
            await bot.delete_message(chat_id=chat_id, message_id=m.message_id)
            deleted_any = True
        except Exception:
            pass


def _vpn_lastmsg_key(tg_id: int, kind: str) -> str:
    return f"vpn_lastmsg:{int(tg_id)}:{kind}"  # kind in {chat,conf,qr}


async def _store_last_vpn_conf_messages(tg_id: int, *, chat_id: int, conf_msg_id: int, qr_msg_id: int) -> None:
    """Persist last config/QR message ids to allow immediate cleanup on user action."""
    async with session_scope() as session:
        await set_app_setting_int(session, _vpn_lastmsg_key(tg_id, "chat"), int(chat_id))
        await set_app_setting_int(session, _vpn_lastmsg_key(tg_id, "conf"), int(conf_msg_id))
        await set_app_setting_int(session, _vpn_lastmsg_key(tg_id, "qr"), int(qr_msg_id))
        await session.commit()


async def _delete_last_vpn_conf_messages(bot: Bot, *, tg_id: int) -> None:
    """Delete last stored VPN config/QR messages (best-effort) and clear pointers."""
    async with session_scope() as session:
        chat_id = await get_app_setting_int(session, _vpn_lastmsg_key(tg_id, "chat"), default=0)
        conf_id = await get_app_setting_int(session, _vpn_lastmsg_key(tg_id, "conf"), default=0)
        qr_id = await get_app_setting_int(session, _vpn_lastmsg_key(tg_id, "qr"), default=0)

        # Clear first to avoid repeated deletes on races.
        await set_app_setting_int(session, _vpn_lastmsg_key(tg_id, "chat"), 0)
        await set_app_setting_int(session, _vpn_lastmsg_key(tg_id, "conf"), 0)
        await set_app_setting_int(session, _vpn_lastmsg_key(tg_id, "qr"), 0)
        await session.commit()

    if chat_id and conf_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=conf_id)
        except Exception:
            pass
    if chat_id and qr_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=qr_id)
        except Exception:
            pass

    if deleted_any:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "⏱ Сообщения с конфигом были автоматически удалены.\n\n"
                    "Если не успели сохранить конфиг, нажмите кнопку ниже — бот отправит его повторно."
                ),
                reply_markup=_kb_vpn_missed_config(),
            )
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


def _wg_download_kb(platform: str) -> InlineKeyboardMarkup:
    platform = (platform or "").lower()
    links = {
        "android": [("⬇️ Скачать WireGuard (Google Play)", "https://play.google.com/store/apps/details?id=com.wireguard.android")],
        "windows": [("⬇️ Скачать WireGuard для Windows", "https://www.wireguard.com/install/")],
        "macos": [("⬇️ Скачать WireGuard для macOS", "https://apps.apple.com/app/wireguard/id1451685025")],
        "linux": [("⬇️ Официальная инструкция WireGuard", "https://www.wireguard.com/install/")],
    }
    rows: list[list[InlineKeyboardButton]] = []
    for title, url in links.get(platform, []):
        rows.append([InlineKeyboardButton(text=title, url=url)])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="vpn:guide")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _trial_visible_for_user(tg_id: int) -> bool:
    async with session_scope() as session:
        return await is_trial_available(session, tg_id)


def _server_code_aliases_nav(servers: list[dict], code: str) -> set[str]:
    code_u = str(code or '').strip().upper()
    aliases = {code_u}
    ordinal = None

    for idx, s in enumerate(servers, start=1):
        sc = str(s.get("code") or os.environ.get("VPN_CODE", "NL")).strip().upper()
        if sc == code_u:
            ordinal = idx
            break
        if code_u.startswith("SERVER #"):
            try:
                n = int(code_u.replace("SERVER #", "").strip())
                if idx == n:
                    ordinal = idx
                    break
            except Exception:
                pass
        if code_u.startswith("SERVER") and code_u != "SERVER":
            try:
                n = int(code_u.replace("SERVER", "").strip())
                if idx == n:
                    ordinal = idx
                    break
            except Exception:
                pass

    if ordinal is not None:
        aliases.update({f"SERVER{ordinal}", f"SERVER #{ordinal}", f"NL{ordinal}"})
        if ordinal == 1:
            aliases.add("NL")

    if code_u in {"NL", "NL1"}:
        aliases.update({"NL", "NL1", "SERVER1", "SERVER #1"})
    if code_u == "NL2":
        aliases.update({"SERVER2", "SERVER #2"})

    return {a for a in aliases if a}


async def _vpn_seats_by_server_nav() -> dict[str, int]:
    """Return occupied WG slots per configured server code.

    Legacy rows can store aliases for the same server (NL/NL1/SERVER1). We
    normalize them into the configured code first, then reconcile with the real
    WireGuard peer total from SSH.
    """
    from app.db.models import VpnPeer

    servers = await _enabled_vpn_servers_nav(include_not_ready=False)
    default_code = (os.environ.get("VPN_CODE") or "NL").upper()
    default_code_lit = literal(default_code)

    canonical_for_alias: dict[str, str] = {}
    for s in servers:
        code = str(s.get("code") or default_code).upper()
        for alias in _server_code_aliases_nav(servers, code):
            canonical_for_alias[str(alias).upper()] = code
        canonical_for_alias.setdefault(code, code)

    result: dict[str, int] = {str(s.get("code") or default_code).upper(): 0 for s in servers}

    async with session_scope() as session:
        q = (
            select(
                func.coalesce(func.upper(VpnPeer.server_code), default_code_lit).label("code"),
                func.count(VpnPeer.id).label("cnt"),
            )
            .where(VpnPeer.is_active == True)  # noqa: E712
            .group_by(func.coalesce(func.upper(VpnPeer.server_code), default_code_lit))
        )
        res = await session.execute(q)
        for raw_code, cnt in res.all():
            raw = str(raw_code or default_code).upper()
            canonical = canonical_for_alias.get(raw, raw)
            result[canonical] = int(result.get(canonical, 0)) + int(cnt or 0)

    for s in servers:
        code = str(s.get("code") or default_code).upper()
        try:
            st = await vpn_service.get_server_status_for(
                host=str(s.get("host") or ""),
                port=int(s.get("port") or 22),
                user=str(s.get("user") or ""),
                password=s.get("password"),
                interface=str(s.get("interface") or os.environ.get("VPN_INTERFACE", "wg0")),
            )
            if st.get("ok") and st.get("total_peers") is not None:
                result[code] = max(int(result.get(code, 0)), int(st.get("total_peers") or 0))
        except Exception:
            pass

    if not servers:
        result.setdefault(default_code, 0)
    return result

def _vpn_capacity_limit(server: dict | None = None) -> int:
    try:
        if server and server.get("max_active") is not None:
            return max(1, int(server.get("max_active")))
        return max(1, int(os.environ.get("VPN_MAX_ACTIVE", "40") or 40))
    except Exception:
        return 40


async def _pick_available_vpn_server(*, preferred_code: str | None = None, current_tg_id: int | None = None) -> dict | None:
    servers = await _enabled_vpn_servers_nav(include_not_ready=False)
    if not servers:
        return None

    used = await _vpn_seats_by_server_nav()

    current_code: str | None = None
    if current_tg_id is not None:
        from app.repo import get_active_peer
        async with session_scope() as session:
            active = await get_active_peer(session, int(current_tg_id))
            if active:
                current_code = (active.server_code or os.environ.get("VPN_CODE", "NL")).upper()

    def can_use(server: dict) -> bool:
        code = str(server.get("code") or "").upper()
        seats = int(used.get(code, 0))
        cap = _vpn_capacity_limit(server)
        if current_code == code:
            return True
        return seats < cap

    if preferred_code:
        preferred_code = preferred_code.upper()
        for s in servers:
            if str(s.get("code") or "").upper() == preferred_code and can_use(s):
                return s

    for s in servers:
        if can_use(s):
            return s
    return None


async def _build_home_text() -> str:
    """Main menu text with best-effort VPN status (supports optional multi-server display).

    If VPN_SERVERS_JSON is provided (list of servers), we will try to query each server status.
    Otherwise we show a single status line for the current WG_SSH_* server.
    """
    import os
    lines: list[str] = []

    servers_json = (os.environ.get("VPN_SERVERS_JSON") or "").strip()
    if servers_json:
        try:
            servers = json.loads(servers_json)
        except Exception:
            servers = []
    else:
        servers = []

    # Default showcase list (can be overridden by VPN_SERVERS_JSON).
    if not servers:
        # IMPORTANT: The first (current) server must include SSH connection params,
        # otherwise it will always show as "Недоступно" even though WG_SSH_* is configured.
        # Other servers may be left without host/user until they are actually connected.
        pwd = os.environ.get("WG_SSH_PASSWORD")
        if pwd is not None and pwd.strip() == "":
            pwd = None

        servers = [
            {
                "code": os.environ.get("VPN_COUNTRY_CODE", "NL"),
                "name": os.environ.get("VPN_NAME", "VPN-Нидерланды"),
                "host": os.environ.get("WG_SSH_HOST"),
                "port": int(os.environ.get("WG_SSH_PORT", "22")),
                "user": os.environ.get("WG_SSH_USER"),
                "password": pwd,
                "interface": os.environ.get("VPN_INTERFACE", "wg0"),
            },
            {"code": "DE", "name": "VPN-Германия"},
            {"code": "TR", "name": "VPN-Турция"},
            {"code": "US", "name": "VPN-США"},
        ]

    def _flag(code: str) -> str:
        code = (code or "").upper()
        flags = {"NL": "🇳🇱", "DE": "🇩🇪", "TR": "🇹🇷", "US": "🇺🇸"}
        return flags.get(code, "🌍")

    async def _fmt_status(srv: dict) -> str:
        # If critical fields are missing, the server is not fully configured yet.
        if not srv.get("host") or not srv.get("user") or not srv.get("endpoint") or not srv.get("server_public_key"):
            return "Подключается..."

        try:
            st = await asyncio.wait_for(
                vpn_service.get_server_status_for(
                    host=str(srv["host"]),
                    port=int(srv.get("port", 22)),
                    user=str(srv["user"]),
                    password=(srv.get("password") or None),
                    interface=str(srv.get("interface") or os.environ.get("VPN_INTERFACE", "wg0")),
                ),
                timeout=4,
            )
            if st.get("ok"):
                return "Active ✅"
        except Exception:
            # Home menu should not hang forever on “Подключается...” for a fully configured server.
            return "Active ✅"
        return "Active ✅"

    primary_srv = None
    for srv in servers:
        code = str(srv.get("code") or "").upper()
        if code in ("NL", "NL1"):
            primary_srv = srv
            break
    if primary_srv is None and servers:
        primary_srv = servers[0]
    if primary_srv:
        status = await _fmt_status(primary_srv)
        lines.append('🇳🇱 "VPN-Cервер": <b>%s</b>' % status)
    else:
        lines.append('🇳🇱 "VPN-Cервер": <b>Подключается...</b>')

    lte_status = "Active ✅" if settings.lte_enabled else "Отключён ⛔️"
    lines.append('📶 "LTE-Обход-Сервер": <b>%s</b>' % lte_status)
    lines.append("")
    lines.append('🔐 Шифрование: <a href="https://en.wikipedia.org/wiki/ChaCha20-Poly1305?ysclid=mmxjfy37uz259328312">ChaCha20-Poly1305</a>')

    return "\n".join([
        "🏠 <b>Главное меню</b>",
        "",
        *lines,
    ])



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

    if where == "home_vpn":
        # "Главное меню" under VPN config/QR: delete config messages immediately.
        try:
            await _delete_last_vpn_conf_messages(cb.bot, tg_id=cb.from_user.id)
        except Exception:
            pass
        await _cleanup_flow_messages_for_user(cb.bot, cb.message.chat.id, cb.from_user.id)
        try:
            show_trial = await _trial_visible_for_user(cb.from_user.id)
            home_text = await _build_home_text()
            home_kb = kb_main(show_trial=show_trial)
            try:
                await cb.message.edit_text(home_text, reply_markup=home_kb, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                try:
                    await cb.message.delete()
                except Exception:
                    pass
                await cb.message.answer(home_text, reply_markup=home_kb, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            pass
        return

    if where == "home":
        # Home text may wait on VPN status; callback already answered above.
        await _cleanup_flow_messages_for_user(cb.bot, cb.message.chat.id, cb.from_user.id)
        try:
            show_trial = await _trial_visible_for_user(cb.from_user.id)
            home_text = await _build_home_text()
            home_kb = kb_main(show_trial=show_trial)
            try:
                await cb.message.edit_text(home_text, reply_markup=home_kb, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                try:
                    await cb.message.delete()
                except Exception:
                    pass
                await cb.message.answer(home_text, reply_markup=home_kb, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            pass
        return

    if where == "tgproxy":
        txt = (
            "⚡ <b>Антилаг-Telegram</b>\n\n"
            "Это бесплатный Telegram-прокси для ускоренной работы мессендежера.\n\n"
            "<b>Как использовать</b>\n"
            "1) Нажмите кнопку <b>«⚡ Подключить Антилаг-Telegram»</b>.\n"
            "2) Telegram предложит добавить и включить прокси — подтвердите.\n"
            "3) Если прокси работает нестабильно, просто подключите его заново этой же кнопкой.\n\n"
            "<b>Важно</b>\n"
            "— не используйте этот прокси одновременно с любым включённым VPN;\n"
            "— если Telegram работает некорректно, сначала отключите VPN, затем переустановите прокси;\n"
            "— работа прокси может быть нестабильной, это нормально для такого типа подключения.\n\n"
            "Нажмите кнопку ниже, чтобы протестировать. Не забудьте выключить VPN."
        )
        try:
            await cb.message.edit_text(txt, reply_markup=kb_tgproxy(), parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            await cb.message.answer(txt, reply_markup=kb_tgproxy(), parse_mode="HTML", disable_web_page_preview=True)
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

    if where == "kinoteka":
        # Temporarily closed: feature is in development.
        try:
            await cb.answer()
        except Exception:
            pass
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
        )
        try:
            await cb.message.edit_text(
                "🚧 <b>Кинотека</b>\n\nРаздел находится в разработке. Скоро будет доступен ✨",
                reply_markup=kb,
                parse_mode="HTML",
            )
        except Exception:
            try:
                await cb.message.answer(
                    "🚧 <b>Кинотека</b>\n\nРаздел находится в разработке. Скоро будет доступен ✨",
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    if where == "referrals":
        async with session_scope() as session:
            user = await session.get(User, cb.from_user.id)
            if not user:
                user = await ensure_user(session, cb.from_user.id)
                await session.commit()
            code = await referral_service.ensure_ref_code(session, user)

            progress = await referral_service.percent_progress(session, cb.from_user.id)
            active_cnt = int(progress.get("active_referrals", 0) or 0)
            pending_sum, avail_sum = await referral_service.get_balance(session, cb.from_user.id)
            pct = int(progress.get("current_percent", 0) or 0)
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
        await _render_pay_screen(cb.message, cb.from_user.id)
        await _safe_cb_answer(cb)
        return

    if where == "vpn":
        # Show "Мой конфиг" only for users who have ever received a WG config
        # and only when subscription is active.
        show_my = False
        try:
            async with session_scope() as session:
                sub = await get_subscription(session, cb.from_user.id)
                if _is_sub_active(sub.end_at):
                    from app.db.models.vpn_peer import VpnPeer

                    q = select(VpnPeer.id).where(VpnPeer.tg_id == cb.from_user.id).limit(1)
                    res = await session.execute(q)
                    show_my = res.first() is not None
        except Exception:
            show_my = False

        try:
            await cb.message.edit_text("🌍 VPN", reply_markup=kb_vpn(show_my_config=show_my))
        except Exception:
            pass
        await _safe_cb_answer(cb)
        return

    if where == "yandex":
        async with session_scope() as session:
            sub = await get_subscription(session, cb.from_user.id)
            ym = await _get_yandex_membership(session, cb.from_user.id)
            has_paid_purchase = bool(await session.scalar(
                select(Payment.id)
                .where(
                    Payment.tg_id == int(cb.from_user.id),
                    Payment.status == "success",
                    Payment.amount.is_not(None),
                    Payment.amount > 0,
                )
                .order_by(Payment.id.desc())
                .limit(1)
            ))
            invites_blocked = bool(await get_app_setting_int(session, "yandex_invites_blocked", default=0) or 0)

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

        cov = getattr(ym, "coverage_end_at", None) if ym else None
        target_end = cov or (sub.end_at if sub and sub.end_at else None)
        rotate_hint = ""
        try:
            if not has_paid_purchase:
                if target_end:
                    rotate_hint = (
                        f"⏳ <b>До окончания текущего приглашения:</b> <b>{_fmt_countdown_to(target_end)}</b>\n"
                        f"🕒 <b>Текущее приглашение действует до:</b> <b>{fmt_dt(target_end)}</b>\n"
                        "ℹ️ <b>Во время пробного периода автоперевыдача ссылки недоступна.</b>\n\n"
                    )
                else:
                    rotate_hint = "ℹ️ <b>Во время пробного периода автоперевыдача ссылки недоступна.</b>\n\n"
            elif target_end:
                sub_end = sub.end_at if sub else None
                if sub_end and _ensure_tz(sub_end) > _ensure_tz(target_end):
                    rotate_hint = (
                        f"⏳ <b>До автоматической перевыдачи:</b> <b>{_fmt_countdown_to(target_end)}</b>\n"
                        f"🕒 <b>Плановая перевыдача:</b> <b>{fmt_dt(target_end)}</b>\n\n"
                    )
                    if ym and ym.invite_link:
                        buttons.append([InlineKeyboardButton(text="♻️ Получить новое приглашение уже сейчас", callback_data="yandex:issue_now")])
                else:
                    rotate_hint = (
                        f"⏳ <b>До окончания текущего приглашения:</b> <b>{_fmt_countdown_to(target_end)}</b>\n"
                        f"🕒 <b>Текущее приглашение действует до:</b> <b>{fmt_dt(target_end)}</b>\n"
                        "ℹ️ Новая ссылка появится после следующего продления подписки.\n\n"
                    )
            elif sub and _is_sub_active(sub.end_at):
                rotate_hint = (
                    f"🕒 <b>Подписка активна до:</b> <b>{fmt_dt(sub.end_at)}</b>\n"
                    "ℹ️ Таймер перевыдачи появится после выдачи приглашения.\n\n"
                )
        except Exception:
            pass

        # Если ссылка уже есть — показываем кнопку открыть.
        if ym and ym.invite_link:
            buttons.insert(0, [InlineKeyboardButton(text="🔗 Открыть приглашение", url=ym.invite_link)])
            info = (
                "🟡 <b>Yandex Plus</b>\n\n"
                "✅ Приглашение уже выдано и доступно по кнопке ниже.\n\n"
                f"Семья: <code>{getattr(ym, 'account_label', '—') or '—'}</code>\n"
                f"Слот: <b>{getattr(ym, 'slot_index', '—') or '—'}</b>\n\n"
                + rotate_hint
                + "⚠️ <b>Важно: откройте приглашение сразу ⚠️</b>\n"
                + "Ссылка-приглашение действует ограниченное время и позже может устареть.\n\n"
                + "Если ссылка уже не открывается — запроси новую у поддержки: @sbsmanager_bot."
            )
        else:
            if invites_blocked:
                info = (
                    "🟡 <b>Yandex Plus</b>\n\n"
                    "⚠️ <b>Сейчас места в семейной подписке временно заняты.</b>\n\n"
                    "Наша команда уже знает об этом и скоро загрузит новые аккаунты. "
                    "Как только появятся новые места, выдача приглашений возобновится.\n\n"
                    + rotate_hint
                )
            else:
                buttons.append([InlineKeyboardButton(text="Получить приглашение", callback_data="yandex:issue")])
                info = (
                    "🟡 <b>Yandex Plus</b>\n\n"
                    + rotate_hint
                    + "Нажмите кнопку ниже — вам будет выслано приглашение в семейную подписку.\n\n"
                    "⚠️ <b>Важно:</b> после получения ссылки перейдите по ней <b>сразу сейчас</b>.\n"
                    "Ссылка-приглашение действует ограниченное время и позже может устареть."
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
            "❓ <b>FAQ</b>\n\n"
            "Выберите раздел ниже."
        )
        try:
            await cb.message.edit_text(text, reply_markup=kb_faq(), parse_mode="HTML")
        except Exception:
            try:
                await cb.message.answer(text, reply_markup=kb_faq(), parse_mode="HTML")
            except Exception:
                pass
        await _safe_cb_answer(cb)
        return

    if where == "support":
        text = (
            "🛠 <b>Поддержка</b>\n\n"
            "По всем вопросам пиши сюда: @sbsmanager_bot\n\n"
            "Контакты для связи:\n"
            "sbs@sertera.group"
        )
        try:
            await cb.message.edit_text(text, reply_markup=kb_back_home(), parse_mode="HTML")
        except Exception:
            try:
                await cb.message.answer(text, reply_markup=kb_back_home(), parse_mode="HTML")
            except Exception:
                pass
        await _safe_cb_answer(cb)
        return

    await cb.answer("Неизвестный раздел")


@router.callback_query(lambda c: c.data == "yandex:issue_now")
async def on_yandex_issue_now(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer('Подписка не активна', show_alert=True)
            return
        try:
            from app.services.yandex.service import yandex_service
            link = await yandex_service.force_issue_new_invite(session, tg_id=tg_id)
            await session.commit()
        except Exception:
            await cb.answer('Не удалось выпустить новое приглашение. Попробуйте позже.', show_alert=True)
            return
    await cb.message.answer(
        '🟡 <b>Yandex Plus</b>\n\nМы выпустили для вас новое приглашение уже сейчас. Откройте его сразу, чтобы старая ссылка не успела устареть.',
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='🔗 Открыть новое приглашение', url=link)],
                [InlineKeyboardButton(text='🟡 Открыть раздел Yandex Plus', callback_data='nav:yandex')],
            ]
        ),
    )
    await cb.answer('Новое приглашение выдано')






@router.callback_query(lambda c: c.data == "pay:promo:enter")
async def pay_promo_enter(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoCodeFSM.waiting_code)
    await cb.answer()
    text = (
        "🎟 <b>Промокод</b>\n\n"
        "Отправьте промокод одним сообщением.\n\n"
        "Промокод применяется к вашему аккаунту и будет использован при ближайшей оплате. Отменить применение нельзя."
    )
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:pay")]]), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:pay")]]), parse_mode="HTML")


@router.message(PromoCodeFSM.waiting_code)
async def pay_promo_apply(message, state: FSMContext) -> None:
    code = _promo_norm(message.text or "")
    if not code:
        await message.answer("❌ Введите промокод буквами и цифрами.", reply_markup=kb_back_home())
        return
    tg_id = int(message.from_user.id)
    async with session_scope() as session:
        used = await session.get(AppSetting, f"promo:used:{tg_id}:{code}")
        if used and int(used.int_value or 0) == 1:
            await state.clear()
            await message.answer("❌ Этот промокод уже был применён ранее и не может быть использован повторно.", reply_markup=kb_back_home())
            return
        active = await _get_user_active_promo(session, tg_id)
        if active:
            active_code, _ = active
            await state.clear()
            if active_code == code:
                await message.answer(f"ℹ️ Промокод <code>{html_escape(code)}</code> уже применён к вашему аккаунту и ждёт оплаты.", parse_mode="HTML", reply_markup=kb_back_home())
            else:
                await message.answer(f"❌ У вас уже применён другой промокод: <code>{html_escape(active_code)}</code>. Отменить его нельзя.", parse_mode="HTML", reply_markup=kb_back_home())
            return
        defs = await _promo_defs(session)
        promo_price = int(defs.get(code) or 0)
        if promo_price <= 0:
            await state.clear()
            await message.answer("❌ Промокод не найден или больше не действует.", reply_markup=kb_back_home())
            return
        base_price = int(await get_price_rub(session) or 0)
        if promo_price >= base_price:
            await state.clear()
            await message.answer("❌ Этот промокод сейчас недействителен.", reply_markup=kb_back_home())
            return
        await set_app_setting_int(session, f"promo:applied:{tg_id}:{code}", promo_price)
        await session.commit()
    await state.clear()
    await _render_pay_screen(message, tg_id)


@router.callback_query(lambda c: c.data and (c.data.startswith("pay:buy") or c.data.startswith("pay:mock") or c.data.startswith("pay:promo:")))
async def on_buy(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id

    # Promo flow: discounted first month (winback)
    promo_amount: int | None = None
    promo_code: str | None = None
    promo_months: int | None = None
    if cb.data and cb.data.startswith("pay:promo:"):
        parts = (cb.data or "").split(":")
        # pay:promo:<amount>
        try:
            promo_amount = int(parts[2])
        except Exception:
            promo_amount = None
        if promo_amount in (69, 29):
            promo_code = f"winback_{promo_amount}"
            promo_months = 1
        else:
            promo_amount = None

    if promo_amount is None and promo_code is None and (cb.data or "").startswith("pay:buy"):
        async with session_scope() as session:
            active_promo = await _get_user_active_promo(session, tg_id)
        if active_promo:
            promo_code, promo_amount = active_promo
            promo_months = 1

    # legacy support: old buttons used pay:mock
    provider = settings.payment_provider
    if cb.data and cb.data.startswith("pay:mock"):
        provider = "mock"

    if provider == "platega":
        await _start_platega_payment(
            cb,
            tg_id=tg_id,
            amount_override=promo_amount,
            months_override=promo_months,
            promo_code=promo_code,
        )
        return

    async with session_scope() as session:
        price_rub = promo_amount if promo_amount is not None else await get_price_rub(session)
        sub = await get_subscription(session, tg_id)
        now = utcnow()
        base = sub.end_at if sub.end_at and sub.end_at > now else now
        add_months = promo_months if promo_months is not None else settings.period_months
        new_end = base + relativedelta(months=add_months)

        await extend_subscription(
            session,
            tg_id,
            months=add_months,
            days_legacy=30 if promo_months is not None else settings.period_days,
            amount_rub=price_rub,
            provider=("mock_promo" if promo_months is not None else "mock"),
            status="success",
        )

        # Consume winback promo once per user lifetime.
        if promo_months is not None:
            await set_app_setting_int(session, f"winback_promo_consumed:{tg_id}", 1)

        # Restore WG access if peers were disabled due to expiration (within 24h).
        await _restore_wg_peers_after_payment(session, tg_id)

        # process referral earnings (first payment activates referral)
        pay = await session.scalar(
            select(Payment)
            .where(Payment.tg_id == tg_id)
            .order_by(Payment.id.desc())
            .limit(1)
        )
        referral_payload = None
        first_paid_before = int(
            await session.scalar(
                select(func.count(Payment.id)).where(
                    Payment.tg_id == tg_id,
                    Payment.status == "success",
                    Payment.amount.is_not(None),
                    Payment.amount > 0,
                )
            )
            or 0
        )
        if pay:
            await referral_service.on_successful_payment(session, pay)
            referral_payload = await _collect_referral_payment_notification(session, pay)

        sub.end_at = new_end
        sub.is_active = True
        sub.status = "active"
        await session.commit()

        # RegionVPN: if the user already had a config earlier, re-enable it
        # (do NOT rotate UUID) so the same link starts working again.
        if settings.regionvpn_enabled:
            try:
                rsvc = _region_service()
                await rsvc.set_client_enabled(tg_id, True)

                row = await session.scalar(
                    select(RegionVpnSession).where(RegionVpnSession.tg_id == tg_id).limit(1)
                )
                if row and row.active_ip:
                    await rsvc.apply_active_ip_map({tg_id: row.active_ip})
            except Exception:
                # Payment should succeed even if RegionVPN server is temporarily unavailable.
                pass

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



async def _auto_watch_platega_payment(bot, *, payment_db_id: int, tg_id: int) -> None:
    """Poll Platega status every 5 seconds and auto-confirm payment when it becomes successful.

    This is best-effort: if the process restarts, the watcher stops (manual check still works).
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from app.services.payments.platega import PlategaClient, PlategaError
    from app.db.models import Payment

    if not settings.platega_merchant_id or not settings.platega_secret:
        return

    client = PlategaClient(merchant_id=settings.platega_merchant_id, secret=settings.platega_secret)

    # up to 10 minutes
    for _ in range(int(600 / 5)):
        await asyncio.sleep(5)

        try:
            async with session_scope() as session:
                pay = await session.get(Payment, payment_db_id)
                if not pay or pay.tg_id != tg_id:
                    return
                if pay.status in ("success", "failed"):
                    return
                provider_tid = pay.provider_payment_id
                if not provider_tid:
                    return

                try:
                    st = await client.get_transaction_status(transaction_id=provider_tid)
                except PlategaError:
                    continue

                status = (st.status or "").upper()
                if status in ("CONFIRMED", "SUCCESS", "PAID", "COMPLETED"):
                    # Family group payment (seats) vs normal subscription
                    if (pay.provider or "").startswith("platega_family_"):
                        mode, count, slot_no = await _get_family_payment_context(session, tg_id)
                        try:
                            seats = int((pay.provider or "").split("_")[-1])
                        except Exception:
                            seats = int(count or 0)
                        seats = max(0, min(FAMILY_MAX_SEATS, seats or count or 0))
                        grp, touched_slots = await _apply_family_payment(
                            session,
                            owner_tg_id=tg_id,
                            seats=seats,
                            mode=mode,
                            slot_no=slot_no,
                        )
                        await set_app_setting_int(session, f"family_grace_started_ts:{tg_id}", None)
                        await set_app_setting_int(session, f"family_grace_seats:{tg_id}", None)
                        pay.status = "success"
                        await session.commit()
                        try:
                            await _notify_admins_new_purchase(
                                bot,
                                buyer_tg_id=tg_id,
                                amount_rub=int(pay.amount),
                                months=1,
                                provider=str(pay.provider or "platega_family"),
                                new_end_at=grp.active_until,
                                item_label="Семейная группа VPN",
                            )
                        except Exception:
                            pass
                        try:
                            await bot.send_message(
                                tg_id,
                                "✅ <b>Оплата семейной группы подтверждена!</b>\n\n"
                                "Запомнить и присылать счёт ежемесячно для оплаты семейной группы?",
                                parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(
                                    inline_keyboard=[
                                        [InlineKeyboardButton(text="✅ Да", callback_data="family:bill:yes")],
                                        [InlineKeyboardButton(text="❌ Нет", callback_data="family:bill:no")],
                                    ]
                                ),
                            )
                        except Exception:
                            pass
                        return

                    if (pay.provider or "") == "platega_lte":
                        sub = await get_subscription(session, tg_id)
                        await lte_vpn_service.activate_paid_month(tg_id)
                        pay.status = "success"
                        await session.commit()
                        try:
                            await _notify_admins_new_purchase(
                                bot,
                                buyer_tg_id=tg_id,
                                amount_rub=int(pay.amount),
                                months=1,
                                provider=str(pay.provider or "platega_lte"),
                                new_end_at=sub.end_at,
                                item_label="VPN LTE",
                            )
                        except Exception:
                            pass
                        try:
                            await bot.send_message(
                                tg_id,
                                "✅ <b>VPN LTE активирован!</b>\n\nТеперь можно открыть раздел «📶 VPN LTE» и установить конфиг.",
                                parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(
                                    inline_keyboard=[[InlineKeyboardButton(text="📶 Открыть VPN LTE", callback_data="vpn:lte")]]
                                ),
                            )
                        except Exception:
                            pass
                        return

                    # Reuse the same logic as manual check for normal subscription
                    sub = await get_subscription(session, tg_id)
                    now = utcnow()
                    base = sub.end_at if sub.end_at and sub.end_at > now else now
                    add_months = int(getattr(pay, "period_months", 1) or 1)
                    new_end = base + relativedelta(months=add_months)

                    await extend_subscription(
                        session,
                        tg_id,
                        months=add_months,
                        days_legacy=int(getattr(pay, "period_days", 30) or 30),
                        amount_rub=int(pay.amount),
                        provider="platega",
                        status="success",
                        provider_payment_id=provider_tid,
                    )

                    # Mark promo as consumed for successful discounted payment.
                    try:
                        provider_name = str(pay.provider or "")
                        if provider_name.startswith("platega_winback_"):
                            await set_app_setting_int(session, f"winback_promo_consumed:{tg_id}", 1)
                        elif provider_name.startswith("platega_promo_"):
                            used_code = provider_name.split("platega_promo_", 1)[1].strip().upper()
                            if used_code:
                                await set_app_setting_int(session, f"promo:used:{tg_id}:{used_code}", 1)
                                await set_app_setting_int(session, f"promo:applied:{tg_id}:{used_code}", None)
                    except Exception:
                        pass

                    await _restore_wg_peers_after_payment(session, tg_id)

                    pay.status = "success"
                    first_paid_before = int(
                        await session.scalar(
                            select(func.count(Payment.id)).where(
                                Payment.tg_id == tg_id,
                                Payment.status == "success",
                                Payment.amount.is_not(None),
                                Payment.amount > 0,
                                Payment.id != int(pay.id),
                            )
                        )
                        or 0
                    )
                    await referral_service.on_successful_payment(session, pay)
                    referral_payload = await _collect_referral_payment_notification(session, pay)

                    sub.end_at = new_end
                    sub.is_active = True
                    sub.status = "active"
                    should_notify_admins = await _mark_purchase_notified_once(
                        session,
                        provider_payment_id=provider_tid,
                        payment_id=getattr(pay, "id", None),
                    )
                    await session.commit()

                    # Admin notification about new purchase (best-effort)
                    if should_notify_admins:
                        try:
                            await _notify_admins_new_purchase(
                                bot,
                                buyer_tg_id=tg_id,
                                amount_rub=int(pay.amount),
                                months=add_months,
                                provider=str(pay.provider or "platega"),
                                new_end_at=new_end,
                            )
                        except Exception:
                            pass

                    try:
                        await bot.send_message(
                            tg_id,
                            "✅ <b>Оплата подтверждена!</b>\n\nПодписка активирована.",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(
                                inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
                            ),
                        )
                    except Exception:
                        pass
                    if first_paid_before == 0:
                        await _send_first_payment_family_upsell(bot, tg_id)
                    await _send_referral_payment_notifications(bot, referral_payload)
                    return

                if status in ("FAILED", "CANCELLED", "EXPIRED", "REJECTED"):
                    pay.status = "failed"
                    await session.commit()
                    return
        except Exception:
            # keep polling on transient errors
            continue


def _ensure_pay_watch_task(bot, *, payment_db_id: int, tg_id: int) -> None:
    """Start a single watcher task per payment id (idempotent)."""
    t = _PAY_WATCH_TASKS.get(payment_db_id)
    if t and not t.done():
        return

    async def _runner():
        try:
            await _auto_watch_platega_payment(bot, payment_db_id=payment_db_id, tg_id=tg_id)
        finally:
            _PAY_WATCH_TASKS.pop(payment_db_id, None)

    _PAY_WATCH_TASKS[payment_db_id] = asyncio.create_task(_runner())

async def _start_platega_payment(
    cb: CallbackQuery,
    *,
    tg_id: int,
    amount_override: int | None = None,
    months_override: int | None = None,
    promo_code: str | None = None,
) -> None:
    """Creates a Platega transaction and sends user the payment link + check button."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.services.payments.platega import PlategaClient, PlategaError

    if not settings.platega_merchant_id or not settings.platega_secret:
        await cb.answer("Платежи временно недоступны")
        try:
            await cb.message.edit_text(
                "💳 <b>Оплата</b>\n\n"
                "Платежи временно отключены (не настроены переменные окружения).\n"
                "Админу: добавь PLATEGA_MERCHANT_ID и PLATEGA_SECRET в Variables.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    client = PlategaClient(merchant_id=settings.platega_merchant_id, secret=settings.platega_secret)

    async with session_scope() as session:
        price_rub = amount_override if amount_override is not None else await get_price_rub(session)

    pay_months = months_override if months_override is not None else settings.period_months
    pay_days = 30 if months_override is not None else settings.period_days

    # We pack some useful info into payload for easier troubleshooting.
    payload = f"tg_id={tg_id};period={pay_months}m"
    if promo_code:
        payload += f";promo={promo_code}"
    if promo_code == "lte":
        payload += ";purpose=lte"
    description = (f"VPN LTE activation (TG {tg_id})" if promo_code == "lte" else f"Подписка SBS: {pay_months} мес (TG {tg_id})")

    try:
        res = await client.create_transaction(
            payment_method=settings.platega_payment_method,
            amount=price_rub,
            currency="RUB",
            description=description,
            return_url=settings.platega_return_url,
            failed_url=settings.platega_failed_url,
            payload=payload,
        )
    except PlategaError:
        await cb.answer("Ошибка платежного провайдера")
        try:
            await cb.message.edit_text(
                "💳 <b>Оплата</b>\n\n"
                "Не удалось создать платеж. Попробуйте позже или обратитесь в поддержку.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # Store pending payment
    from sqlalchemy import select, func, literal, text
    from app.db.models import Payment

    async with session_scope() as session:
        p = Payment(
            tg_id=tg_id,
            amount=price_rub,
            currency="RUB",
            provider=(f"platega_promo_{promo_code}" if (promo_code and promo_code not in {"lte", "winback_69", "winback_29"}) else (f"platega_{promo_code}" if promo_code else "platega")),
            status="pending",
            period_days=pay_days,
            period_months=pay_months,
            provider_payment_id=res.transaction_id,
        )
        session.add(p)
        await session.commit()
        payment_db_id = p.id

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Перейти к оплате", url=res.redirect_url)],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"pay:check:{payment_db_id}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )

    await cb.answer("Ссылка на оплату создана")
    await cb.message.edit_text(
        "💳 <b>Оплата подписки</b>\n\n"
        f"Сумма: <b>{price_rub} ₽</b>\n"
        "1) Нажмите «✅ Перейти к оплате»\n"
        "2) После оплаты вернитесь и нажмите «🔄 Проверить оплату»\n\n"
        "Если статус не обновился сразу — подождите 10–20 секунд и попробуйте ещё раз.",
        reply_markup=kb,
        parse_mode="HTML",
    )

    # Start auto-checking payment status (best-effort, every 5 seconds)
    _ensure_pay_watch_task(cb.bot, payment_db_id=payment_db_id, tg_id=tg_id)


@router.callback_query(lambda c: c.data and c.data.startswith("pay:check:"))
async def on_pay_check(cb: CallbackQuery) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.services.payments.platega import PlategaClient, PlategaError
    from app.db.models import Payment

    parts = (cb.data or "").split(":")
    if len(parts) != 3:
        await cb.answer()
        return
    try:
        payment_id = int(parts[2])
    except Exception:
        await cb.answer()
        return

    if not settings.platega_merchant_id or not settings.platega_secret:
        await cb.answer("Платежи не настроены")
        return

    async with session_scope() as session:
        pay = await session.get(Payment, payment_id)
        if not pay or pay.tg_id != cb.from_user.id:
            await cb.answer("Платеж не найден")
            return
        if not pay.provider_payment_id:
            await cb.answer("Платеж без ID")
            return
        if pay.status == "success":
            await cb.answer("Уже оплачено")
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
            )
            await cb.message.edit_text(
                "✅ <b>Оплата уже подтверждена</b>",
                reply_markup=kb,
                parse_mode="HTML",
            )
            return

        client = PlategaClient(merchant_id=settings.platega_merchant_id, secret=settings.platega_secret)
        try:
            st = await client.get_transaction_status(transaction_id=pay.provider_payment_id)
        except PlategaError:
            await cb.answer("Не удалось проверить статус")
            return

        status = (st.status or "").upper()

        # Platega callback docs use status="CONFIRMED".
        # The status API page doesn't enumerate all terminal statuses,
        # so we treat CONFIRMED as success too.
        if status in ("CONFIRMED", "SUCCESS", "PAID", "COMPLETED"):
            # Family group payment (seats)
            if (pay.provider or "").startswith("platega_family_"):
                mode, count, slot_no = await _get_family_payment_context(session, cb.from_user.id)
                try:
                    seats = int((pay.provider or "").split("_")[-1])
                except Exception:
                    seats = int(count or 0)
                seats = max(0, min(FAMILY_MAX_SEATS, seats or count or 0))

                grp, touched_slots = await _apply_family_payment(
                    session,
                    owner_tg_id=cb.from_user.id,
                    seats=seats,
                    mode=mode,
                    slot_no=slot_no,
                )
                pay.status = "success"
                await session.commit()

                try:
                    await _notify_admins_new_purchase(
                        cb.bot,
                        buyer_tg_id=cb.from_user.id,
                        amount_rub=int(pay.amount),
                        months=1,
                        provider=str(pay.provider or "platega_family"),
                        new_end_at=grp.active_until,
                        item_label="Семейная группа VPN",
                    )
                except Exception:
                    pass

                await cb.answer("Оплата подтверждена")
                await cb.message.edit_text(
                    "✅ <b>Оплата семейной группы подтверждена!</b>\n\n"
                    "Запомнить и присылать счёт ежемесячно для оплаты семейной группы?",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="✅ Да", callback_data="family:bill:yes")],
                            [InlineKeyboardButton(text="❌ Нет", callback_data="family:bill:no")],
                            [InlineKeyboardButton(text="👨‍👩‍👧‍👦 Открыть семейную группу", callback_data="vpn:family")],
                        ]
                    ),
                    parse_mode="HTML",
                )
                return

            if (pay.provider or "") == "platega_lte":
                sub = await get_subscription(session, cb.from_user.id)
                await lte_vpn_service.activate_paid_month(cb.from_user.id)
                pay.status = "success"
                await session.commit()

                try:
                    await _notify_admins_new_purchase(
                        cb.bot,
                        buyer_tg_id=cb.from_user.id,
                        amount_rub=int(pay.amount),
                        months=1,
                        provider=str(pay.provider or "platega_lte"),
                        new_end_at=sub.end_at,
                        item_label="VPN LTE",
                    )
                except Exception:
                    pass

                await cb.answer("Оплата подтверждена")
                await cb.message.edit_text(
                    "✅ <b>VPN LTE активирован!</b>\n\nТеперь можно открыть раздел «📶 VPN LTE» и установить конфиг.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="📶 Открыть VPN LTE", callback_data="vpn:lte")]]
                    ),
                    parse_mode="HTML",
                )
                return

            # extend subscription and mark payment
            sub = await get_subscription(session, cb.from_user.id)
            now = utcnow()
            base = sub.end_at if sub.end_at and sub.end_at > now else now
            add_months = int(getattr(pay, "period_months", 1) or 1)
            new_end = base + relativedelta(months=add_months)

            await extend_subscription(
                session,
                cb.from_user.id,
                months=add_months,
                days_legacy=int(getattr(pay, "period_days", 30) or 30),
                amount_rub=int(pay.amount),
                provider="platega",
                status="success",
                provider_payment_id=pay.provider_payment_id,
            )

            # Mark promo as consumed for successful discounted payment.
            try:
                provider_name = str(pay.provider or "")
                if provider_name.startswith("platega_winback_"):
                    await set_app_setting_int(session, f"winback_promo_consumed:{cb.from_user.id}", 1)
                elif provider_name.startswith("platega_promo_"):
                    used_code = provider_name.split("platega_promo_", 1)[1].strip().upper()
                    if used_code:
                        await set_app_setting_int(session, f"promo:used:{cb.from_user.id}:{used_code}", 1)
                        await set_app_setting_int(session, f"promo:applied:{cb.from_user.id}:{used_code}", None)
            except Exception:
                pass

            await _restore_wg_peers_after_payment(session, cb.from_user.id)

            # referral earnings processing: use the newest successful payment row
            # (extend_subscription inserts a Payment row). We keep original pending row too.
            pay.status = "success"
            first_paid_before = int(
                await session.scalar(
                    select(func.count(Payment.id)).where(
                        Payment.tg_id == cb.from_user.id,
                        Payment.status == "success",
                        Payment.amount.is_not(None),
                        Payment.amount > 0,
                        Payment.id != int(pay.id),
                    )
                )
                or 0
            )
            await referral_service.on_successful_payment(session, pay)
            referral_payload = await _collect_referral_payment_notification(session, pay)

            sub.end_at = new_end
            sub.is_active = True
            sub.status = "active"

            # If user has (or had) Yandex Plus membership, refresh invite link immediately
            # after renewal so they don't keep an outdated link/family in the cabinet.
            new_invite_link: str | None = None
            try:
                if settings.yandex_enabled:
                    from app.services.yandex.service import yandex_service

                    new_invite_link = await yandex_service.rotate_membership_for_user_if_needed(
                        session, tg_id=cb.from_user.id
                    )
            except Exception:
                # do not fail payment flow
                new_invite_link = None

            should_notify_admins = await _mark_purchase_notified_once(
                session,
                provider_payment_id=pay.provider_payment_id,
                payment_id=getattr(pay, "id", None),
            )
            await session.commit()

            # Admin notification about new purchase (best-effort)
            if should_notify_admins:
                try:
                    await _notify_admins_new_purchase(
                        cb.bot,
                        buyer_tg_id=cb.from_user.id,
                        amount_rub=int(pay.amount),
                        months=add_months,
                        provider=str(pay.provider or "platega"),
                        new_end_at=new_end,
                    )
                except Exception:
                    pass

            await cb.answer("Оплата подтверждена")
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
            )
            await cb.message.edit_text(
                "✅ <b>Оплата подтверждена!</b>\n\n"
                "Подписка активирована.",
                reply_markup=kb,
                parse_mode="HTML",
            )

            # Notify about a refreshed Yandex Plus invite if it was re-issued.
            if new_invite_link:
                try:
                    await cb.bot.send_message(
                        cb.from_user.id,
                        "🟡 <b>Yandex Plus</b>\n\n"
                        "Мы обновили ваше приглашение в семейную подписку."
                        "\nНажмите кнопку ниже, чтобы открыть приглашение:",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="🔗 Открыть приглашение", url=new_invite_link)],
                                [InlineKeyboardButton(text="🟡 Yandex Plus", callback_data="nav:yandex")],
                                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                            ]
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return

        if status in ("FAILED", "CANCELLED", "EXPIRED", "REJECTED"):
            pay.status = "failed"
            await session.commit()
            await cb.answer("Платеж не завершён")
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Попробовать снова", callback_data="pay:buy:1m")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                ]
            )
            await cb.message.edit_text(
                "❌ <b>Платеж не завершён</b>\n\n"
                "Если вы оплатили, подождите минуту и попробуйте проверить ещё раз.\n"
                "Если оплата не прошла — создайте новый платеж.",
                reply_markup=kb,
                parse_mode="HTML",
            )
            return

        await cb.answer("Пока не оплачено")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Проверить ещё раз", callback_data=f"pay:check:{payment_id}")],
                [InlineKeyboardButton(text="💳 Создать новый платеж", callback_data="pay:buy:1m")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
            ]
        )
        try:
            await cb.message.edit_text(
                f"💳 <b>Статус платежа:</b> <code>{status}</code>\n\n"
                "Если вы оплатили — подождите 10–20 секунд и нажмите «Проверить ещё раз».",
                reply_markup=kb,
                parse_mode="HTML",
            )
        except Exception:
            pass


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
        "1) Нажмите «Подключиться к серверу»\n"
        "2) Импортируйте конфигурацию (.conf) в приложение WireGuard\n"
        f"3) Конфиг будет удалён автоматически через <b>{_vpn_config_ttl_seconds()} сек.</b>\n\n"
        "Выберите устройство, чтобы открыть инструкцию:"
    )
    await cb.message.edit_text(text, reply_markup=kb_vpn_guide_platforms(), parse_mode="HTML")
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "vpn:my")
async def on_vpn_my_config(cb: CallbackQuery) -> None:
    """Re-send user's WireGuard config if they have received it before.

    The message (config + QR) is auto-deleted after settings.auto_delete_seconds.
    """

    tg_id = cb.from_user.id
    chat_id = cb.message.chat.id

    # Answer early to avoid Telegram timeout spinner.
    try:
        await cb.answer("Отправляю…")
    except Exception:
        pass

    # Require active subscription.
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await _show_subscription_required_prompt(cb, session, tg_id)
            return

        from app.db.models.vpn_peer import VpnPeer

        q = (
            select(VpnPeer)
            .where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True)  # noqa: E712
            .order_by(VpnPeer.id.desc())
            .limit(1)
        )
        res = await session.execute(q)
        active = res.scalar_one_or_none()

        if not active:
            # User paid but hasn't got a config (or all peers are inactive).
            q2 = select(VpnPeer.id).where(VpnPeer.tg_id == tg_id).limit(1)
            res2 = await session.execute(q2)
            has_any = res2.first() is not None

            if has_any:
                text = (
                    "ℹ️ <b>Активный VPN-конфиг не найден</b>\n\n"
                    "Чтобы получить новый конфиг, нажмите «Подключиться к серверу»."
                )
            else:
                text = (
                    "ℹ️ <b>У вас ещё нет конфига</b>\n\n"
                    "Чтобы посмотреть/установить свой конфиг, сначала получите его: "
                    "нажмите «Подключиться к серверу»."
                )

            try:
                await cb.message.answer(text, reply_markup=kb_vpn(show_my_config=False), parse_mode="HTML")
            except Exception:
                pass
            return

        # Determine user's current location for the active peer (best-effort).
        code = (active.server_code or os.environ.get("VPN_CODE", "NL")).upper()
        servers = await _enabled_vpn_servers_nav()
        srv = next((s for s in servers if str(s.get("code")).upper() == code), None)

        # Rebuild the exact same config from the stored peer row (no re-issue, no rotation).
        peer = vpn_service._row_to_peer_dict(active)
        if srv and _server_is_ready(srv):
            try:
                await vpn_service.ensure_rate_limit_for_server(
                    tg_id=tg_id,
                    ip=str(active.client_ip),
                    host=str(srv["host"]),
                    port=int(srv.get("port") or 22),
                    user=str(srv["user"]),
                    password=srv.get("password"),
                    interface=str(srv.get("interface") or "wg0"),
                    tc_dev=str(srv.get("tc_dev") or srv.get("wg_tc_dev") or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or ""),
                )
            except Exception:
                pass
            conf_text = vpn_service.build_wg_conf(
                peer,
                user_label=str(tg_id),
                server_public_key=str(srv.get("server_public_key")),
                endpoint=str(srv.get("endpoint")),
                dns=str(srv.get("dns") or os.environ.get("VPN_DNS", "1.1.1.1")),
            )
            loc_title = f"{_vpn_flag(code)} <b>{srv.get('name') or code}</b>"
        else:
            # Legacy single-server mode.
            try:
                await vpn_service.ensure_rate_limit(tg_id=tg_id, ip=str(active.client_ip))
            except Exception:
                pass
            conf_text = vpn_service.build_wg_conf(
                peer,
                user_label=str(tg_id),
                server_public_key=str(peer.get("server_public_key") or ""),
                endpoint=str(peer.get("endpoint") or ""),
                dns=str(peer.get("dns") or os.environ.get("VPN_DNS", "1.1.1.1")),
            )
            loc_title = "<b>ваша локация</b>"

        filename = await _get_or_assign_vpn_bundle_filename_for_peer(session, getattr(active, 'id', None))

        family_note = None
        try:
            from app.db.models import FamilyVpnGroup
            grp = await session.scalar(select(FamilyVpnGroup).where(FamilyVpnGroup.owner_tg_id == tg_id).limit(1))
            if grp and grp.seats_total and grp.active_until and grp.active_until > utcnow():
                family_note = (
                    "ℹ️ <b>Важно:</b> это <b>ваш личный конфиг</b>, а не конфиг из семейной группы.\n\n"
                    "Если включить этот личный конфиг сразу на другом устройстве, соединение может работать нестабильно и с просадками по скорости.\n\n"
                    "Чтобы выдать отдельный профиль другому человеку или устройству, откройте: <b>VPN → Семейная группа</b> → <b>📤 Поделиться VPN</b> и отправьте нужный профиль из группы."
                )
        except Exception:
            family_note = None
        await session.commit()

    # Build QR + files.
    qr_img = qrcode.make(conf_text)
    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    buf.seek(0)

    conf_file = BufferedInputFile(conf_text.encode(), filename="wg.conf")
    qr_file = BufferedInputFile(buf.getvalue(), filename="wg.png")

    msg_conf = await cb.bot.send_document(
        chat_id=chat_id,
        document=conf_file,
        caption=(
            f"📌 <b>Ваш VPN-конфиг</b> ({loc_title})\n\n"
            f"Конфиг будет удалён через <b>{_vpn_config_ttl_seconds()} сек.</b>"
        ),
        parse_mode="HTML",
    )
    msg_qr = await cb.bot.send_photo(
        chat_id=chat_id,
        photo=qr_file,
        caption=f"QR для WireGuard (удалится через {_vpn_config_ttl_seconds()} сек.)",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home_vpn")]]
        ),
    )

    # Allow immediate cleanup when user navigates back to main menu.
    if family_note:
        try:
            await cb.bot.send_message(
                chat_id=chat_id,
                text=family_note,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home_vpn")]]
                ),
            )
        except Exception:
            pass

    await _store_last_vpn_conf_messages(
        tg_id=tg_id,
        chat_id=chat_id,
        conf_msg_id=msg_conf.message_id,
        qr_msg_id=msg_qr.message_id,
    )

    asyncio.create_task(_schedule_vpn_cleanup_and_followup(cb.bot, chat_id=chat_id, messages=[msg_conf, msg_qr]))


# --- VPN location selection / migration ---


async def _vpn_server_enabled_map_nav() -> dict[str, bool]:
    servers = _load_vpn_servers()
    codes = [str((s or {}).get("code") or "").strip().upper() for s in servers if str((s or {}).get("code") or "").strip()]
    if not codes:
        return {}
    async with session_scope() as session:
        rows = (await session.execute(
            select(AppSetting.key, AppSetting.int_value).where(AppSetting.key.in_([f"vpn_server_enabled:{c}" for c in codes]))
        )).all()
    raw = {str(k).split(":", 1)[1].upper(): (None if v is None else int(v)) for k, v in rows}
    return {c: (raw.get(c, 1) != 0) for c in codes}


async def _enabled_vpn_servers_nav(*, include_not_ready: bool = True) -> list[dict]:
    servers = _load_vpn_servers()
    enabled = await _vpn_server_enabled_map_nav()
    filtered = [s for s in servers if enabled.get(str((s or {}).get("code") or "").strip().upper(), True)]
    if not include_not_ready:
        filtered = [s for s in filtered if _server_is_ready(s)]
    return filtered


def _server_is_ready(srv: dict) -> bool:
    return bool(srv.get("host") and srv.get("user") and srv.get("server_public_key") and srv.get("endpoint"))


async def _vpn_server_label(srv: dict) -> str:
    """Return UI label: recommend/overloaded/connecting.

    We intentionally DO NOT show occupied/total places.
    """

    if not _server_is_ready(srv):
        return "Подключается…"

    # Best-effort CPU-based indicator
    st = await vpn_service.get_server_status_for(
        host=str(srv["host"]),
        port=int(srv.get("port") or 22),
        user=str(srv["user"]),
        password=srv.get("password"),
        interface=str(srv.get("interface") or "wg0"),
    )
    cpu = st.get("cpu_load_percent")
    if st.get("ok") and isinstance(cpu, (int, float)):
        if float(cpu) >= 85.0:
            return "Перегружен"
        if float(cpu) <= 70.0:
            return "<i>(Рекомендуем)</i>"
    return "Доступен"


@router.callback_query(lambda c: c.data == "vpn:loc")
async def on_vpn_location_menu(cb: CallbackQuery) -> None:
    servers = await _enabled_vpn_servers_nav()

    lines = ["🌍 <b>Выбор локации VPN</b>", "", "Выберите сервер."]

    kb_rows: list[list[InlineKeyboardButton]] = []
    for srv in servers:
        code = srv.get("code")
        name = srv.get("name")
        flag = _vpn_flag(str(code))
        label = await _vpn_server_label(srv)
        lines.append(f"{flag} <b>{name}</b> — {label}")

        # Make all locations clickable: for not-ready locations we show an alert
        # and suggest choosing Netherlands.
        btn_text = f"{flag} {name}" if _server_is_ready(srv) else f"{flag} {name} (недоступно)"
        kb_rows.append([InlineKeyboardButton(text=btn_text, callback_data=f"vpn:loc:sel:{code}")])

    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:vpn")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    try:
        await cb.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await _safe_cb_answer(cb)


async def _start_vpn_migration_watch(
    *,
    bot,
    tg_id: int,
    new_srv: dict,
    new_public_key: str,
    old_peers: list[tuple[dict, str]],
) -> None:
    """Poll new server handshakes; once user connects, disable old peers immediately."""

    # Cancel existing watcher for this user
    t = _VPN_MIGRATE_TASKS.pop(tg_id, None)
    if t and not t.done():
        t.cancel()

    async def _run() -> None:
        # IMPORTANT:
        # A peer on the target server can already have a previous handshake
        # (e.g., user re-selects the same location or reuses an existing peer).
        # If we only check `hs >= start_ts`, clock skew or a recent handshake
        # may immediately trigger migration and disable the old peer *too early*.
        #
        # So we snapshot the current handshake first, and only treat as "migrated"
        # when the handshake value *changes*.
        start_ts = int(utcnow().timestamp())
        deadline = start_ts + 15 * 60  # 15 minutes
        try:
            hs0 = await vpn_service.get_peer_handshake_for_server(
                public_key=new_public_key,
                host=str(new_srv["host"]),
                port=int(new_srv.get("port") or 22),
                user=str(new_srv["user"]),
                password=new_srv.get("password"),
                interface=str(new_srv.get("interface") or "wg0"),
            )
            hs0 = int(hs0 or 0)
        except Exception:
            hs0 = 0

        while int(utcnow().timestamp()) < deadline:
            try:
                hs = await vpn_service.get_peer_handshake_for_server(
                    public_key=new_public_key,
                    host=str(new_srv["host"]),
                    port=int(new_srv.get("port") or 22),
                    user=str(new_srv["user"]),
                    password=new_srv.get("password"),
                    interface=str(new_srv.get("interface") or "wg0"),
                )
                hs = int(hs or 0)
            except Exception:
                hs = 0

            # Trigger only when we observe a *new* handshake after watcher started.
            if hs and hs > hs0 and hs >= start_ts - 5:
                # User has connected to the new location — disable old peers right away.
                await _finalize_pending_vpn_migration(tg_id=tg_id, new_peer_public_key=new_public_key)

                try:
                    await bot.send_message(
                        tg_id,
                        "✅ <b>Локация переключена</b>\n\nСтарый конфиг отключён, вы подключены к новому серверу.",
                        reply_markup=kb_back_home(),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                return

            await asyncio.sleep(5)

    _VPN_MIGRATE_TASKS[tg_id] = asyncio.create_task(_run())


async def _finalize_pending_vpn_migration(*, tg_id: int, new_peer_public_key: str) -> bool:
    """Disable old peers marked as pending migration for this user.

    Returns True when at least one old peer was disabled. Safe to call repeatedly.
    """

    if not new_peer_public_key:
        return False

    async with session_scope() as session:
        q = select(VpnPeer).where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True)  # noqa: E712
        res = await session.execute(q)
        rows = list(res.scalars().all())
        pending_old = [r for r in rows if r.client_public_key != new_peer_public_key and (r.rotation_reason or "") == "pending_migration"]
        if not pending_old:
            try:
                await set_app_setting_int(session, f"vpn_migration_pending:{tg_id}", 0)
                await set_app_setting_int(session, f"vpn_migration_target_peer:{tg_id}", None)
                await set_app_setting_int(session, f"vpn_migration_started_ts:{tg_id}", None)
                await session.commit()
            except Exception:
                pass
            return False

        servers = _load_vpn_servers()
        servers_by_code = {str(s.get("code") or "").upper(): s for s in servers}

        for r in pending_old:
            code = (r.server_code or os.environ.get("VPN_CODE", "NL")).upper()
            old_srv = servers_by_code.get(code)
            if old_srv and _server_is_ready(old_srv):
                try:
                    await vpn_service.remove_peer_for_server(
                        public_key=r.client_public_key,
                        host=str(old_srv["host"]),
                        port=int(old_srv.get("port") or 22),
                        user=str(old_srv["user"]),
                        password=old_srv.get("password"),
                        interface=str(old_srv.get("interface") or "wg0"),
                        tc_dev=str(old_srv.get("tc_dev") or old_srv.get("wg_tc_dev") or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or ""),
                    )
                except Exception:
                    pass

            r.is_active = False
            r.revoked_at = utcnow()
            r.rotation_reason = "migrated"

        await set_app_setting_int(session, f"vpn_migration_pending:{tg_id}", 0)
        await set_app_setting_int(session, f"vpn_migration_target_peer:{tg_id}", None)
        await set_app_setting_int(session, f"vpn_migration_started_ts:{tg_id}", None)
        await session.commit()
        return True


@router.callback_query(lambda c: c.data and c.data.startswith("vpn:loc:sel:"))
async def on_vpn_location_select(cb: CallbackQuery) -> None:
    # Step 1: show a warning + confirmation.
    # We do NOT generate or revoke anything here.
    tg_id = cb.from_user.id
    parts = (cb.data or "").split(":")
    if len(parts) != 4:
        await _safe_cb_answer(cb)
        return
    code = parts[3].upper()

    # Require active subscription
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await _show_subscription_required_prompt(cb, session, tg_id)
            return

    servers = await _enabled_vpn_servers_nav()
    srv = next((s for s in servers if str(s.get("code")).upper() == code), None)
    if not srv or not _server_is_ready(srv):
        await cb.answer(
            "Эта локация пока недоступна. Сейчас доступна только 🇳🇱 Нидерланды.",
            show_alert=True,
        )

        # Offer Netherlands directly.
        nl = next((s for s in servers if str(s.get("code")).upper() == "NL"), None)
        text = (
            "❌ <b>Локация пока недоступна</b>\n\n"
            "Сейчас доступна: 🇳🇱 <b>Нидерланды</b>\n\n"
            "Нажмите кнопку ниже, чтобы получить конфиг."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🇳🇱 Нидерланды", callback_data="vpn:loc:sel:NL")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:vpn")],
            ]
        )
        try:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
        return
    # If the selected location is full, silently redirect to another available server.
    resolved_srv = await _pick_available_vpn_server(preferred_code=code, current_tg_id=tg_id)
    if not resolved_srv:
        await cb.answer("Сейчас все VPN-серверы заняты. Попробуйте чуть позже.", show_alert=True)
        return
    if str(resolved_srv.get("code") or "").upper() != code:
        fallback_code = str(resolved_srv.get("code") or "").upper()
        fallback_name = str(resolved_srv.get("name") or fallback_code)
        await cb.answer(
            f"На выбранной локации сейчас нет мест. Подготовим конфиг для {fallback_name}.",
            show_alert=True,
        )
        code = fallback_code
        srv = resolved_srv

    # Show the migration warning only if user already has an active peer
    from app.repo import get_active_peer
    async with session_scope() as session:
        active_peer = await get_active_peer(session, tg_id)

    if active_peer and (active_peer.server_code or os.environ.get('VPN_CODE', 'NL')).upper() != code:
        warn = (
            "⚠️ <b>Внимание</b>\n\n"
            "После того как вы подключитесь к новой локации, <b>старый VPN-конфиг будет отключён</b>.\n"
            "Чтобы не потерять интернет в момент переключения, рекомендуется <b>выключить VPN</b> перед сменой и включить уже с новым конфигом.\n\n"
            f"Переключить на {_vpn_flag(code)} <b>{srv['name']}</b>?"
        )
    else:
        warn = f"Получить VPN-конфиг для {_vpn_flag(code)} <b>{srv['name']}</b>?"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Продолжить", callback_data=f"vpn:loc:go:{code}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="vpn:loc")],
        ]
    )

    try:
        await cb.message.edit_text(warn, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    return


@router.callback_query(lambda c: c.data and c.data.startswith("vpn:loc:go:"))
async def on_vpn_location_go(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    parts = (cb.data or "").split(":")
    if len(parts) != 4:
        await _safe_cb_answer(cb)
        return
    code = parts[3].upper()

    # Require active subscription
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await _show_subscription_required_prompt(cb, session, tg_id)
            return

    servers = await _enabled_vpn_servers_nav()
    requested_srv = next((s for s in servers if str(s.get("code")).upper() == code), None)
    if not requested_srv or not _server_is_ready(requested_srv):
        await cb.answer("Сервер пока недоступен", show_alert=True)
        return

    srv = await _pick_available_vpn_server(preferred_code=code, current_tg_id=tg_id)
    if not srv:
        await cb.answer("Сейчас все VPN-серверы заняты. Попробуйте чуть позже.", show_alert=True)
        return
    actual_code = str(srv.get("code") or code).upper()
    auto_moved = actual_code != code
    code = actual_code

    # Find currently active peers (could be more than one if previous migration was interrupted)
    from app.db.models import VpnPeer
    async with session_scope() as session:
        q = select(VpnPeer).where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True).order_by(VpnPeer.id.desc())  # noqa: E712
        res = await session.execute(q)
        active_rows = list(res.scalars().all())

        # Build list of old peers (server def + public key) excluding target server.
        old: list[tuple[dict, str]] = []
        for r in active_rows:
            r_code = (r.server_code or os.environ.get("VPN_CODE", "NL")).upper()
            if r_code == code:
                continue
            old_srv = next((s for s in servers if str(s.get("code")).upper() == r_code), None)
            if old_srv and _server_is_ready(old_srv):
                old.append((old_srv, r.client_public_key))

        try:
            peer = await vpn_service.ensure_peer_for_server(
                session,
                tg_id,
                server_code=code,
                host=str(srv["host"]),
                port=int(srv.get("port") or 22),
                user=str(srv["user"]),
                password=srv.get("password"),
                interface=str(srv.get("interface") or "wg0"),
                tc_dev=str(srv.get("tc_dev") or srv.get("wg_tc_dev") or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or ""),
            )
            try:
                await vpn_service.ensure_rate_limit_for_server(
                    tg_id=tg_id,
                    ip=str(peer.get("client_ip") or ""),
                    host=str(srv["host"]),
                    port=int(srv.get("port") or 22),
                    user=str(srv["user"]),
                    password=srv.get("password"),
                    interface=str(srv.get("interface") or "wg0"),
                    tc_dev=str(srv.get("tc_dev") or srv.get("wg_tc_dev") or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or ""),
                )
            except Exception:
                pass
            if old and peer.get("peer_id"):
                q_pending = select(VpnPeer).where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True)  # noqa: E712
                pending_rows = list((await session.execute(q_pending)).scalars().all())
                for r in pending_rows:
                    if r.id != int(peer.get("peer_id")) and (r.server_code or os.environ.get("VPN_CODE", "NL")).upper() != code:
                        r.rotation_reason = "pending_migration"
                await set_app_setting_int(session, f"vpn_migration_pending:{tg_id}", 1)
                await set_app_setting_int(session, f"vpn_migration_target_peer:{tg_id}", int(peer.get("peer_id")))
                await set_app_setting_int(session, f"vpn_migration_started_ts:{tg_id}", int(utcnow().timestamp()))

            await session.commit()

            filename = await _get_or_assign_vpn_bundle_filename_for_peer(session, peer.get("peer_id"))
            await session.commit()
        except Exception:
            await cb.answer("⚠️ Сервер временно недоступен", show_alert=True)
            return

    conf_text = vpn_service.build_wg_conf(
        peer,
        user_label=str(tg_id),
        server_public_key=str(srv.get("server_public_key")),
        endpoint=str(srv.get("endpoint")),
        dns=str(srv.get("dns") or os.environ.get("VPN_DNS", "1.1.1.1")),
    )

    qr_img = qrcode.make(conf_text)
    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    buf.seek(0)

    conf_file = BufferedInputFile(conf_text.encode(), filename="wg.conf")
    qr_file = BufferedInputFile(buf.getvalue(), filename="wg.png")

    msg_conf = await cb.message.answer_document(
        document=conf_file,
        caption=(
            f"WireGuard конфиг для сервера {_vpn_flag(code)} <b>{srv['name']}</b>.\n"
            + (
                "⚠️ После подключения к новому серверу <b>старый конфиг будет отключён</b>.\n"
                "Рекомендуем на время переключения выключить VPN, затем включить с новым конфигом.\n\n"
                if old
                else "\n"
            )
            + f"Конфиг будет удалён через <b>{_vpn_config_ttl_seconds()} сек.</b>"
        ),
        parse_mode="HTML",
    )
    msg_qr = await cb.message.answer_photo(
        photo=qr_file,
        caption=f"QR для WireGuard (удалится через {_vpn_config_ttl_seconds()} сек.)",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home_vpn")]]
        ),
    )

    await _store_last_vpn_conf_messages(
        tg_id=tg_id,
        chat_id=cb.message.chat.id,
        conf_msg_id=msg_conf.message_id,
        qr_msg_id=msg_qr.message_id,
    )

    asyncio.create_task(_schedule_vpn_cleanup_and_followup(cb.bot, chat_id=cb.message.chat.id, messages=[msg_conf, msg_qr]))

    await cb.answer("Конфиг отправлен")

    # Start watcher to disable old peers immediately once user connects to new location.
    await _start_vpn_migration_watch(
        bot=cb.bot,
        tg_id=tg_id,
        new_srv=srv,
        new_public_key=str(peer.get("public_key") or ""),
        old_peers=old,
    )



@router.callback_query(lambda c: c.data == "trialhelp:devices")
async def on_trialhelp_devices(cb: CallbackQuery) -> None:
    await cb.message.edit_text(
        "📲 <b>Выберите ваше устройство</b>\n\nМы отправим подробную инструкцию и подскажем, что делать дальше.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📱 Android", callback_data="trialhelp:howto:android")],
                [InlineKeyboardButton(text="🍎 iPhone / iPad", callback_data="trialhelp:howto:ios")],
                [InlineKeyboardButton(text="💻 Windows", callback_data="trialhelp:howto:windows")],
                [InlineKeyboardButton(text="🍏 macOS", callback_data="trialhelp:howto:macos")],
                [InlineKeyboardButton(text="🐧 Linux", callback_data="trialhelp:howto:linux")],
                [InlineKeyboardButton(text="🌍 Открыть раздел VPN", callback_data="nav:vpn")],
            ]
        ),
        parse_mode="HTML",
    )
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data and c.data.startswith("trialhelp:howto:"))
async def on_trialhelp_howto(cb: CallbackQuery) -> None:
    platform = cb.data.split(":", 2)[2]
    instructions = _load_wg_instructions()
    lines = instructions.get(platform, [])
    if platform == "linux" and not lines:
        lines = [
            "1) Установите WireGuard: <code>sudo apt update && sudo apt install wireguard</code>",
            "2) Скопируйте конфиг в <code>/etc/wireguard/wg0.conf</code>",
            "3) Запустите: <code>sudo wg-quick up wg0</code>",
        ]
    if platform != "ios" and not lines:
        lines = [
            "1) Установите приложение WireGuard на устройство.",
            "2) В боте откройте раздел VPN и получите конфиг.",
            "3) Импортируйте .conf в приложение WireGuard.",
            "4) Включите VPN.",
        ]
    title_map = {
        "android": "📱 Android",
        "ios": "🍎 iPhone / iPad",
        "windows": "💻 Windows",
        "macos": "🍏 macOS",
        "linux": "🐧 Linux",
    }
    title = title_map.get(platform, platform)
    if platform == "ios":
        text = (
            "🍎 <b>iPhone / iPad — подключение WireGuard</b>\n\n"
            "1) Установите WireGuard из App Store\n"
            "2) В боте откройте раздел VPN\n"
            "3) Нажмите «Подключиться к серверу»\n"
            "4) Откройте .conf и импортируйте его в WireGuard\n\n"
            "Ниже после этого сможете сразу перейти к подключению."
        )
    else:
        text = f"{title} — <b>подключение WireGuard</b>\n\n{_fmt_instruction_block(lines)}"
    await cb.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🌍 Перейти к подключению", callback_data="nav:vpn")],
                [InlineKeyboardButton(text="📲 Другое устройство", callback_data="trialhelp:devices")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home_vpn")],
            ]
        ),
        parse_mode="HTML",
    )
    await cb.message.answer(
        "🌍 Теперь откройте раздел VPN и нажмите «Подключиться к серверу», чтобы получить конфиг и подключиться.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🌍 Открыть VPN", callback_data="nav:vpn")]]
        ),
    )
    await _safe_cb_answer(cb)

@router.callback_query(lambda c: c.data and c.data.startswith("vpn:howto:"))
async def on_vpn_howto(cb: CallbackQuery) -> None:
    platform = cb.data.split(":", 2)[2]

    if platform == "ios":
        text = (
            "🍎 <b>iPhone / iPad — подключение WireGuard</b>\n\n"
            "1) Установите WireGuard из App Store\n"
            "2) В боте нажмите «Подключиться к серверу»\n"
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

    await cb.message.edit_text(text, reply_markup=_wg_download_kb(platform), parse_mode="HTML")
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "vpn:reset:confirm")
async def on_vpn_reset_confirm(cb: CallbackQuery) -> None:
    # ✅ FIX: запрет экрана reset_confirm без активной подписки
    tg_id = cb.from_user.id
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await _show_subscription_required_prompt(cb, session, tg_id)
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
            await _show_subscription_required_prompt(cb, session, tg_id)
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
                try:
                    await vpn_service.ensure_rate_limit(tg_id=tg_id, ip=str(peer.get("client_ip") or ""))
                except Exception:
                    pass
                filename = await _get_or_assign_vpn_bundle_filename_for_peer(session, peer.get("peer_id"))
                await session.commit()

            conf_text = vpn_service.build_wg_conf(
                peer,
                user_label=str(tg_id),
                server_public_key=str(peer.get("server_public_key") or ""),
                endpoint=str(peer.get("endpoint") or ""),
                dns=str(peer.get("dns") or os.environ.get("VPN_DNS", "1.1.1.1")),
            )

            qr_img = qrcode.make(conf_text)
            buf = io.BytesIO()
            qr_img.save(buf, format="PNG")
            buf.seek(0)

            conf_file = BufferedInputFile(
                conf_text.encode(),
                filename=filename,
            )
            qr_file = BufferedInputFile(buf.getvalue(), filename="wg.png")

            msg_conf = await cb.bot.send_document(
                chat_id=chat_id,
                document=conf_file,
                caption=f"WireGuard конфиг (после сброса). Будет удалён через {_vpn_config_ttl_seconds()} сек.",
            )
            msg_qr = await cb.bot.send_photo(
                chat_id=chat_id,
                photo=qr_file,
                caption=f"QR для WireGuard (после сброса, удалится через {_vpn_config_ttl_seconds()} сек.)",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home_vpn")]]
                ),
            )

            await _store_last_vpn_conf_messages(
                tg_id=tg_id,
                chat_id=chat_id,
                conf_msg_id=msg_conf.message_id,
                qr_msg_id=msg_qr.message_id,
            )

            asyncio.create_task(_schedule_vpn_cleanup_and_followup(cb.bot, chat_id=chat_id, messages=[msg_conf, msg_qr]))

        except Exception:
            try:
                await cb.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ Не удалось сразу сбросить VPN на текущем сервере. Мы попробовали выдать новый конфиг на доступном сервере, но сейчас это тоже не удалось. Попробуй ещё раз через минуту.",
                )
            except Exception:
                pass

    asyncio.create_task(_do_reset_and_send())


@router.callback_query(lambda c: c.data == "trial:start")
async def on_trial_start(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    async with session_scope() as session:
        if not await is_trial_available(session, tg_id):
            sub = await get_subscription(session, tg_id)
            if _is_sub_active(sub.end_at):
                await cb.answer("У вас уже есть активная подписка — пробный период не нужен.", show_alert=True)
            else:
                await cb.answer("Пробный период уже был использован.", show_alert=True)
            return

        now = utcnow()
        sub = await get_subscription(session, tg_id)
        new_end = now + timedelta(days=5)
        await extend_subscription(
            session,
            tg_id,
            months=0,
            days_legacy=5,
            amount_rub=0,
            provider="trial",
            status="success",
            provider_payment_id=f"trial:{tg_id}",
        )
        sub.start_at = sub.start_at or now
        sub.end_at = new_end
        sub.is_active = True
        sub.status = "active"
        await set_trial_used(session, tg_id)
        await set_app_setting_int(session, f"trial_end_ts:{tg_id}", int(new_end.timestamp()))
        await set_app_setting_int(session, f"trial_reengagement_stage:{tg_id}", 0)
        await session.commit()

    text = (
        "🎁 <b>Пробный период активирован</b>\n\n"
        "На 5 дней вам доступны все возможности сервиса.\n"
        "Теперь откройте раздел VPN и нажмите «Подключиться к серверу», чтобы получить конфиг."
    )
    try:
        await cb.message.edit_text(text, reply_markup=kb_main(show_trial=False), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb_main(show_trial=False), parse_mode="HTML")
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "vpn:bundle")
async def on_vpn_bundle(cb: CallbackQuery) -> None:
    # Автоматически выдаём конфиг на доступный сервер без выбора локации.
    tg_id = cb.from_user.id

    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await _show_subscription_required_prompt(cb, session, tg_id)
            return

    srv = await _pick_available_vpn_server(current_tg_id=tg_id)
    if not srv:
        await cb.answer("Сейчас все VPN-серверы заняты. Попробуйте чуть позже.", show_alert=True)
        return

    servers = _load_vpn_servers()
    code = str(srv.get("code") or "").upper()

    from app.db.models import VpnPeer
    async with session_scope() as session:
        q = select(VpnPeer).where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True).order_by(VpnPeer.id.desc())  # noqa: E712
        res = await session.execute(q)
        active_rows = list(res.scalars().all())

        old: list[tuple[dict, str]] = []
        for r in active_rows:
            r_code = (r.server_code or os.environ.get("VPN_CODE", "NL")).upper()
            if r_code == code:
                continue
            old_srv = next((s for s in servers if str(s.get("code")).upper() == r_code), None)
            if old_srv and _server_is_ready(old_srv):
                old.append((old_srv, r.client_public_key))

        try:
            peer = await vpn_service.ensure_peer_for_server(
                session,
                tg_id,
                server_code=code,
                host=str(srv["host"]),
                port=int(srv.get("port") or 22),
                user=str(srv["user"]),
                password=srv.get("password"),
                interface=str(srv.get("interface") or "wg0"),
                tc_dev=str(srv.get("tc_dev") or srv.get("wg_tc_dev") or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or ""),
            )
            try:
                await vpn_service.ensure_rate_limit_for_server(
                    tg_id=tg_id,
                    ip=str(peer.get("client_ip") or ""),
                    host=str(srv["host"]),
                    port=int(srv.get("port") or 22),
                    user=str(srv["user"]),
                    password=srv.get("password"),
                    interface=str(srv.get("interface") or "wg0"),
                    tc_dev=str(srv.get("tc_dev") or srv.get("wg_tc_dev") or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or ""),
                )
            except Exception:
                pass
            if old and peer.get("peer_id"):
                q_pending = select(VpnPeer).where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True)  # noqa: E712
                pending_rows = list((await session.execute(q_pending)).scalars().all())
                for r in pending_rows:
                    if r.id != int(peer.get("peer_id")) and (r.server_code or os.environ.get("VPN_CODE", "NL")).upper() != code:
                        r.rotation_reason = "pending_migration"
                await set_app_setting_int(session, f"vpn_migration_pending:{tg_id}", 1)
                await set_app_setting_int(session, f"vpn_migration_target_peer:{tg_id}", int(peer.get("peer_id")))
                await set_app_setting_int(session, f"vpn_migration_started_ts:{tg_id}", int(utcnow().timestamp()))

            await session.commit()

            filename = await _get_or_assign_vpn_bundle_filename_for_peer(session, peer.get("peer_id"))
            await session.commit()
        except Exception:
            await cb.answer("⚠️ Сервер временно недоступен", show_alert=True)
            return

    conf_text = vpn_service.build_wg_conf(
        peer,
        user_label=str(tg_id),
        server_public_key=str(srv.get("server_public_key")),
        endpoint=str(srv.get("endpoint")),
        dns=str(srv.get("dns") or os.environ.get("VPN_DNS", "1.1.1.1")),
    )

    qr_img = qrcode.make(conf_text)
    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    buf.seek(0)

    conf_file = BufferedInputFile(conf_text.encode(), filename=filename or "wg.conf")
    qr_file = BufferedInputFile(buf.getvalue(), filename="wg.png")

    msg_conf = await cb.message.answer_document(
        document=conf_file,
        caption=(
            f"WireGuard конфиг для сервера {_vpn_flag(code)} <b>{srv['name']}</b>.\n"
            + (
                "⚠️ После подключения к новому серверу <b>старый конфиг будет отключён</b>.\n"
                "Рекомендуем на время переключения выключить VPN, затем включить с новым конфигом.\n\n"
                if old
                else "\n"
            )
            + f"Конфиг будет удалён через <b>{_vpn_config_ttl_seconds()} сек.</b>"
        ),
        parse_mode="HTML",
    )
    msg_qr = await cb.message.answer_photo(
        photo=qr_file,
        caption=f"QR для WireGuard (удалится через {_vpn_config_ttl_seconds()} сек.)",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home_vpn")]]
        ),
    )

    await _store_last_vpn_conf_messages(
        tg_id=tg_id,
        chat_id=cb.message.chat.id,
        conf_msg_id=msg_conf.message_id,
        qr_msg_id=msg_qr.message_id,
    )

    asyncio.create_task(_schedule_vpn_cleanup_and_followup(cb.bot, chat_id=cb.message.chat.id, messages=[msg_conf, msg_qr]))

    await cb.answer("Конфиг отправлен")

    await _start_vpn_migration_watch(
        bot=cb.bot,
        tg_id=tg_id,
        new_srv=srv,
        new_public_key=str(peer.get("public_key") or ""),
        old_peers=old,
    )


# -------------------- VPN Family Group --------------------

FAMILY_MAX_SEATS = 10
FAMILY_SEAT_PRICE_DEFAULT = 100




async def _reconcile_pending_family_payment(session, *, tg_id: int) -> bool:
    """Best-effort activation for a paid family-group order that was not manually confirmed.

    Returns True if any pending family payment was confirmed and applied.
    """
    from app.db.models import Payment
    from app.services.payments.platega import PlategaClient, PlategaError

    if settings.payment_provider != "platega":
        return False
    if not settings.platega_merchant_id or not settings.platega_secret:
        return False

    pay = await session.scalar(
        select(Payment)
        .where(
            Payment.tg_id == tg_id,
            Payment.status == "pending",
            Payment.provider.like("platega_family_%"),
        )
        .order_by(Payment.id.desc())
        .limit(1)
    )
    if not pay or not pay.provider_payment_id:
        return False

    client = PlategaClient(merchant_id=settings.platega_merchant_id, secret=settings.platega_secret)
    try:
        st = await client.get_transaction_status(transaction_id=pay.provider_payment_id)
    except PlategaError:
        return False

    status = (st.status or "").upper()
    if status not in ("CONFIRMED", "SUCCESS", "PAID", "COMPLETED"):
        return False

    mode, count, slot_no = await _get_family_payment_context(session, tg_id)
    try:
        seats = int((pay.provider or "").split("_")[-1])
    except Exception:
        seats = int(count or 0)
    seats = max(0, min(FAMILY_MAX_SEATS, seats or count or 0))
    if seats <= 0:
        return False

    await _apply_family_payment(session, owner_tg_id=tg_id, seats=seats, mode=mode, slot_no=slot_no)
    pay.status = "success"
    await session.flush()
    return True

async def _get_family_seat_price(session, owner_tg_id: int) -> int:
    # per-user override first, then global, then default
    v = await get_app_setting_int(session, f"family_seat_price_override:{owner_tg_id}", default=0)
    if v and v > 0:
        return int(v)
    v = await get_app_setting_int(session, "family_seat_price_default", default=FAMILY_SEAT_PRICE_DEFAULT)
    return int(v) if v and v > 0 else FAMILY_SEAT_PRICE_DEFAULT


def _family_kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:vpn")]])


async def _get_or_create_family_group(session, owner_tg_id: int):
    from app.db.models import FamilyVpnGroup

    grp = await session.scalar(select(FamilyVpnGroup).where(FamilyVpnGroup.owner_tg_id == owner_tg_id).limit(1))
    if grp:
        return grp
    grp = FamilyVpnGroup(owner_tg_id=owner_tg_id, seats_total=0, active_until=None, billing_opt_in=False)
    session.add(grp)
    await session.flush()
    return grp


async def _ensure_family_profiles(session, owner_tg_id: int, seats_total: int) -> None:
    from app.db.models import FamilyVpnProfile

    if seats_total <= 0:
        return
    rows = list(
        (await session.execute(
            select(FamilyVpnProfile).where(FamilyVpnProfile.owner_tg_id == owner_tg_id)
        )).scalars().all()
    )
    existing = {int(r.slot_no) for r in rows}
    for i in range(1, seats_total + 1):
        if i in existing:
            continue
        session.add(
            FamilyVpnProfile(
                owner_tg_id=owner_tg_id,
                slot_no=i,
                label=None,
                vpn_peer_id=None,
                expires_at=None,
                is_paused=False,
            )
        )
    await session.flush()


async def _get_family_profiles(session, owner_tg_id: int):
    from app.db.models import FamilyVpnProfile

    return list(
        (
            await session.execute(
                select(FamilyVpnProfile)
                .where(FamilyVpnProfile.owner_tg_id == owner_tg_id)
                .order_by(FamilyVpnProfile.slot_no.asc())
            )
        ).scalars().all()
    )


def _profile_expiry(profile) -> datetime | None:
    dt = getattr(profile, "expires_at", None)
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_family_slot_paid(profile, now: datetime | None = None) -> bool:
    now = now or utcnow()
    exp = _profile_expiry(profile)
    return bool(exp and exp > now)


def _is_family_slot_shareable(profile, now: datetime | None = None) -> bool:
    return _is_family_slot_paid(profile, now=now) and not bool(getattr(profile, "is_paused", False))


async def _sync_family_group_summary(session, owner_tg_id: int):
    grp = await _get_or_create_family_group(session, owner_tg_id)
    rows = await _get_family_profiles(session, owner_tg_id)
    now = utcnow()
    active_expires = [_profile_expiry(r) for r in rows[: int(grp.seats_total or 0)] if _is_family_slot_paid(r, now=now)]
    grp.active_until = max(active_expires) if active_expires else None
    await session.flush()
    return grp


async def _extend_family_profile(profile, *, months: int = 1) -> None:
    now = utcnow()
    exp = _profile_expiry(profile)
    base = exp if exp and exp > now else now
    profile.expires_at = base + relativedelta(months=months)
    profile.is_paused = False


async def _apply_family_payment(session, *, owner_tg_id: int, seats: int, mode: int, slot_no: int | None = None) -> tuple[object, list[int]]:
    grp = await _get_or_create_family_group(session, owner_tg_id)
    touched: list[int] = []
    seats = max(0, min(FAMILY_MAX_SEATS, int(seats or 0)))
    current_total = int(grp.seats_total or 0)

    if mode == 1:
        if seats <= 0:
            return grp, touched
        new_total = min(FAMILY_MAX_SEATS, current_total + seats)
        await _ensure_family_profiles(session, owner_tg_id, new_total)
        profiles = {int(r.slot_no): r for r in await _get_family_profiles(session, owner_tg_id)}
        for sn in range(current_total + 1, new_total + 1):
            prof = profiles.get(sn)
            if not prof:
                continue
            prof.expires_at = utcnow() + relativedelta(months=1)
            prof.is_paused = False
            touched.append(sn)
        grp.seats_total = new_total
    else:
        await _ensure_family_profiles(session, owner_tg_id, current_total)
        rows = [r for r in await _get_family_profiles(session, owner_tg_id) if int(r.slot_no or 0) <= current_total]
        if not rows:
            return grp, touched
        if mode == 4 and slot_no:
            targets = [r for r in rows if int(r.slot_no or 0) == int(slot_no)]
        elif mode == 3:
            targets = rows
        else:
            # nearest/oldest place first: expired first, then the earliest active one
            def key_fn(r):
                exp = _profile_expiry(r)
                return exp or datetime(1970, 1, 1, tzinfo=timezone.utc)
            targets = [sorted(rows, key=key_fn)[0]]
        for prof in targets:
            await _extend_family_profile(prof, months=1)
            touched.append(int(prof.slot_no or 0))

    await _sync_family_group_summary(session, owner_tg_id)
    await _restore_family_peers_within_grace(session, owner_tg_id)
    await session.flush()
    return grp, touched


async def _get_family_payment_context(session, owner_tg_id: int) -> tuple[int, int, int | None]:
    mode = int(await get_app_setting_int(session, f"family_pay_mode:{owner_tg_id}", default=1) or 1)
    count = int(await get_app_setting_int(session, f"family_pay_count:{owner_tg_id}", default=0) or 0)
    slot_raw = await get_app_setting_int(session, f"family_pay_slot:{owner_tg_id}", default=None)
    slot_no = int(slot_raw) if slot_raw is not None else None
    return mode, count, slot_no


async def _set_family_payment_context(session, owner_tg_id: int, *, mode: int, count: int, slot_no: int | None = None) -> None:
    await set_app_setting_int(session, f"family_pay_mode:{owner_tg_id}", int(mode))
    await set_app_setting_int(session, f"family_pay_count:{owner_tg_id}", int(count))
    await set_app_setting_int(session, f"family_pay_slot:{owner_tg_id}", int(slot_no) if slot_no else None)


def _family_slot_state_text(profile, now: datetime | None = None) -> str:
    now = now or utcnow()
    exp = _profile_expiry(profile)
    if exp and exp > now:
        return f"до {fmt_dt(exp)}"
    if exp:
        return f"истекло {fmt_dt(exp)}"
    return "не оплачено"


async def _restore_family_peers_within_grace(session, owner_tg_id: int) -> int:
    from app.db.models import FamilyVpnProfile, VpnPeer
    restored = 0
    try:
        from app.services.vpn.service import vpn_service
    except Exception:
        return 0
    rows = list((await session.execute(select(FamilyVpnProfile).where(FamilyVpnProfile.owner_tg_id == owner_tg_id, FamilyVpnProfile.vpn_peer_id.is_not(None)))).scalars().all())
    for prof in rows:
        peer = await session.get(VpnPeer, int(prof.vpn_peer_id or 0))
        if not peer or peer.is_active:
            continue
        try:
            rv = getattr(peer, 'revoked_at', None)
            if rv is None or (utcnow() - rv) > timedelta(hours=24):
                continue
            old_code = str(getattr(peer, 'server_code', None) or os.environ.get('VPN_CODE') or 'NL1').upper()
            preferred = await vpn_service._pick_server_for_extra_peer(session, inherited_code=old_code)
            preferred_code = str(preferred.get('code') or '').upper()
            if preferred_code != old_code:
                continue
            old_server = None
            for srv in (vpn_service._load_vpn_servers() or []):
                if str(srv.get('code') or '').upper() == old_code:
                    old_server = srv
                    break
            if not old_server:
                continue
            provider = vpn_service._provider_for(
                host=str(old_server.get('host') or os.environ.get('WG_SSH_HOST') or ''),
                port=int(old_server.get('port') or 22),
                user=str(old_server.get('user') or os.environ.get('WG_SSH_USER') or ''),
                password=old_server.get('password'),
                interface=str(old_server.get('interface') or os.environ.get('VPN_INTERFACE', 'wg0')),
                tc_dev=str(old_server.get('tc_dev') or old_server.get('wg_tc_dev') or os.environ.get('WG_TC_DEV') or os.environ.get('VPN_TC_DEV') or ''),
                tc_parent_rate_mbit=int(old_server.get('tc_parent_rate_mbit') or old_server.get('wg_tc_parent_rate_mbit') or os.environ.get('WG_TC_PARENT_RATE_MBIT') or os.environ.get('VPN_TC_PARENT_RATE_MBIT') or 1000),
            )
            await provider.add_peer(peer.client_public_key, peer.client_ip, tg_id=owner_tg_id)
            peer.is_active = True
            peer.revoked_at = None
            peer.rotation_reason = None
            restored += 1
        except Exception:
            continue
    if restored:
        await set_app_setting_int(session, f'family_grace_started_ts:{owner_tg_id}', None)
        await set_app_setting_int(session, f'family_grace_seats:{owner_tg_id}', None)
    return restored


@router.callback_query(lambda c: c.data == 'family:renew')
async def on_family_renew(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    async with session_scope() as session:
        grp = await _get_or_create_family_group(session, tg_id)
        target = int(await get_app_setting_int(session, f'family_grace_seats:{tg_id}', default=int(grp.seats_total or 0)) or int(grp.seats_total or 0))
        target = max(0, min(FAMILY_MAX_SEATS, target))
        await set_app_setting_int(session, f'family_seats_target:{tg_id}', target)
        price = await _get_family_seat_price(session, tg_id)
        await session.commit()
    if target <= 0:
        await cb.answer('Не удалось определить количество мест для продления', show_alert=True)
        return
    text = (
        '👨‍👩‍👧‍👦 <b>Продление семейной группы</b>\n\n'
        f'Будет продлено мест: <b>{target}</b>\n'
        f'Цена: <b>{price} ₽</b> за место в месяц.\n\n'
        'Если оплатить в течение 24 часов после окончания семейной группы, старые конфиги будут восстановлены без замены.'
    )
    await cb.message.edit_text(text, reply_markup=_family_seats_kb(current=target, target=target), parse_mode='HTML')
    await _safe_cb_answer(cb)


def _family_manage_kb(*, can_manage: bool, has_seats: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if can_manage:
        rows.append([
            InlineKeyboardButton(text="➕ Купить место", callback_data="family:buy"),
            InlineKeyboardButton(text="🔄 Продлить места", callback_data="family:renew_menu"),
        ])
        if has_seats:
            rows.append([InlineKeyboardButton(text="📋 Мои места", callback_data="family:slots")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:vpn")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(lambda c: c.data == "vpn:family")
async def on_vpn_family(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    now = utcnow()
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        owner_active = _is_sub_active(sub.end_at)

        grp = await _get_or_create_family_group(session, tg_id)
        try:
            changed = await _reconcile_pending_family_payment(session, tg_id=tg_id)
            if changed:
                await session.commit()
                grp = await _get_or_create_family_group(session, tg_id)
        except Exception:
            pass

        price = await _get_family_seat_price(session, tg_id)
        await _ensure_family_profiles(session, tg_id, int(grp.seats_total or 0))
        grp = await _sync_family_group_summary(session, tg_id)
        await session.commit()

        profiles = await _get_family_profiles(session, tg_id)
        active_profiles = [p for p in profiles[: int(grp.seats_total or 0)] if _is_family_slot_paid(p, now=now)]
        free_profiles = [p for p in profiles[: int(grp.seats_total or 0)] if _is_family_slot_paid(p, now=now) and not p.vpn_peer_id]
        nearest = None
        nearest_exp = None
        for p in profiles[: int(grp.seats_total or 0)]:
            exp = _profile_expiry(p)
            if not exp:
                continue
            if nearest_exp is None or exp < nearest_exp:
                nearest_exp = exp
                nearest = p

        header = "👨‍👩‍👧‍👦 <b>Семейная группа VPN</b>\n\n"
        desc = (
            "Добавляйте отдельные семейные места для родственников, друзей и устройств.\n"
            f"Цена: <b>{price} ₽</b> за 1 место в месяц.\n"
            f"Максимум мест: <b>{FAMILY_MAX_SEATS}</b>.\n\n"
        )

        if int(grp.seats_total or 0) <= 0:
            body = "У вас пока нет купленных семейных мест.\n\n"
        else:
            body_lines = [
                f"Мест всего: <b>{int(grp.seats_total or 0)}</b>",
                f"Активно: <b>{len(active_profiles)}</b>",
                f"Свободно: <b>{len(free_profiles)}</b>",
            ]
            if nearest and nearest_exp:
                body_lines.append(f"Ближайшее истекает: <b>место #{int(nearest.slot_no)}</b> — {fmt_dt(nearest_exp)}")
            if sub.end_at:
                body_lines.append(f"Основная подписка: <b>до {fmt_dt(sub.end_at)}</b>")
            body_lines.append(f"Автосчёт семьи: <b>{'включён' if bool(getattr(grp, 'billing_opt_in', False)) else 'выключен'}</b>")
            body = "\n".join(body_lines) + "\n\n"

        note = ""
        if not owner_active:
            note = (
                "⚠️ <b>Ваша основная подписка не активна.</b>\n"
                "Пока она не продлена, семейные места нельзя покупать, продлевать и перевыпускать.\n\n"
            )

        kb = _family_manage_kb(can_manage=owner_active, has_seats=bool(grp.seats_total))
        await cb.message.edit_text(header + desc + note + body, reply_markup=kb, parse_mode="HTML")
    await _safe_cb_answer(cb)


def _family_buy_counter_kb(current: int) -> InlineKeyboardMarkup:
    current = max(1, min(FAMILY_MAX_SEATS, int(current or 1)))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➖", callback_data="family:buy:delta:-1"),
                InlineKeyboardButton(text=f"{current} мест", callback_data="family:buy:noop"),
                InlineKeyboardButton(text="➕", callback_data="family:buy:delta:1"),
            ],
            [InlineKeyboardButton(text="💳 Купить", callback_data="family:buy:pay")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="vpn:family")],
        ]
    )


def _family_buy_kb(current: int = 1) -> InlineKeyboardMarkup:
    return _family_buy_counter_kb(current)


def _family_renew_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Продлить 1 ближайшее", callback_data="family:renew_one")],
            [InlineKeyboardButton(text="Продлить все", callback_data="family:renew_all")],
            [InlineKeyboardButton(text="Выбрать место вручную", callback_data="family:renew_pick")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="vpn:family")],
        ]
    )


def _family_slots_list_kb(seats_total: int, *, mode: str = "view") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    prefix = "family:slot:" if mode == "view" else "family:renewslot:"
    for i in range(1, seats_total + 1):
        rows.append([InlineKeyboardButton(text=f"Место {i}", callback_data=f"{prefix}{i}")])
    back = "family:renew_menu" if mode == "renew" else "vpn:family"
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _family_slot_actions_kb(slot_no: int, *, paid: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if paid:
        rows.append([
            InlineKeyboardButton(text="📤 Получить конфиг", callback_data=f"family:share:{slot_no}"),
            InlineKeyboardButton(text="♻️ Сбросить", callback_data=f"family:reset:{slot_no}"),
        ])
    rows.append([InlineKeyboardButton(text="🔄 Продлить", callback_data=f"family:renewslot:{slot_no}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="family:slots")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(lambda c: c.data in ("family:seats", "family:buy"))
async def on_family_buy(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Сначала продлите свою подписку.", show_alert=True)
            return
        price = await _get_family_seat_price(session, tg_id)
    async with session_scope() as session:
        grp = await _get_or_create_family_group(session, tg_id)
        current_total = int(grp.seats_total or 0)
        max_add = max(1, FAMILY_MAX_SEATS - current_total)
        target = int(await get_app_setting_int(session, f"family_buy_target:{tg_id}", default=1) or 1)
        target = max(1, min(max_add, target))
        await set_app_setting_int(session, f"family_buy_target:{tg_id}", target)
        await session.commit()
    await cb.message.edit_text(
        "👨‍👩‍👧‍👦 <b>Купить семейные места</b>\n\n"
        f"Цена: <b>{price} ₽</b> за 1 место в месяц.\n"
        "Выберите, сколько новых мест хотите добавить.",
        reply_markup=_family_buy_kb(target),
        parse_mode="HTML",
    )
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "family:renew")
async def on_family_renew_compat(cb: CallbackQuery) -> None:
    await on_family_renew_menu(cb)


@router.callback_query(lambda c: c.data == "family:renew_menu")
async def on_family_renew_menu(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Сначала продлите свою подписку.", show_alert=True)
            return
        grp = await _get_or_create_family_group(session, tg_id)
        if int(grp.seats_total or 0) <= 0:
            await cb.answer("Сначала купите семейные места.", show_alert=True)
            return
    await cb.message.edit_text(
        "🔄 <b>Продление семейных мест</b>\n\n"
        "Выберите удобный вариант продления.",
        reply_markup=_family_renew_menu_kb(),
        parse_mode="HTML",
    )
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "family:slots")
async def on_family_slots(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    now = utcnow()
    async with session_scope() as session:
        grp = await _get_or_create_family_group(session, tg_id)
        seats_total = int(grp.seats_total or 0)
        if seats_total <= 0:
            await cb.answer("Семейных мест пока нет.", show_alert=True)
            return
        await _ensure_family_profiles(session, tg_id, seats_total)
        rows = await _get_family_profiles(session, tg_id)
        lines = ["📋 <b>Мои места</b>", ""]
        for p in rows[:seats_total]:
            state = _family_slot_state_text(p, now=now)
            cfg = "создан" if p.vpn_peer_id else "не создан"
            lines.append(f"• Место <b>#{int(p.slot_no)}</b> — {state} | {cfg}")
    await cb.message.edit_text(
        "\n".join(lines),
        reply_markup=_family_slots_list_kb(seats_total, mode="view"),
        parse_mode="HTML",
    )
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "family:renew_pick")
async def on_family_renew_pick(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    async with session_scope() as session:
        grp = await _get_or_create_family_group(session, tg_id)
        seats_total = int(grp.seats_total or 0)
        if seats_total <= 0:
            await cb.answer("Семейных мест пока нет.", show_alert=True)
            return
    await cb.message.edit_text(
        "🔄 <b>Выберите место для продления</b>",
        reply_markup=_family_slots_list_kb(seats_total, mode="renew"),
        parse_mode="HTML",
    )
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "family:buy:noop")
async def on_family_buy_noop(cb: CallbackQuery) -> None:
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data and c.data.startswith("family:buy:delta:"))
async def on_family_buy_delta(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    delta = -1 if (cb.data or "").endswith(":-1") else 1
    async with session_scope() as session:
        grp = await _get_or_create_family_group(session, tg_id)
        current_total = int(grp.seats_total or 0)
        max_add = max(1, FAMILY_MAX_SEATS - current_total)
        target = int(await get_app_setting_int(session, f"family_buy_target:{tg_id}", default=1) or 1)
        target = max(1, min(max_add, target + delta))
        await set_app_setting_int(session, f"family_buy_target:{tg_id}", target)
        price = await _get_family_seat_price(session, tg_id)
        await session.commit()
    await cb.message.edit_text(
        "👨‍👩‍👧‍👦 <b>Покупка семейных мест</b>\n\n"
        f"Цена: <b>{price} ₽</b> за 1 место в месяц.\n"
        f"Вы добавляете: <b>{target}</b> мест.\n",
        reply_markup=_family_buy_counter_kb(target),
        parse_mode="HTML",
    )
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "family:buy:pay")
async def on_family_buy_pay(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Сначала продлите свою подписку.", show_alert=True)
            return
        grp = await _get_or_create_family_group(session, tg_id)
        current_total = int(grp.seats_total or 0)
        count = int(await get_app_setting_int(session, f"family_buy_target:{tg_id}", default=1) or 1)
        count = max(1, min(FAMILY_MAX_SEATS - current_total, count))
        price = await _get_family_seat_price(session, tg_id)
        await _set_family_payment_context(session, tg_id, mode=1, count=count, slot_no=None)
        await session.commit()
    if settings.payment_provider == "platega":
        await _start_platega_family_payment(cb, tg_id=tg_id, seats=count, amount_rub=price * count)
    else:
        async with session_scope() as session:
            await _apply_family_payment(session, owner_tg_id=tg_id, seats=count, mode=1)
            await session.commit()
        await cb.message.edit_text("✅ Новые семейные места добавлены.", reply_markup=_family_manage_kb(can_manage=True, has_seats=True))
        await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data and c.data.startswith("family:buy_count:"))
async def on_family_buy_count(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    parts = (cb.data or "").split(":")
    count = int(parts[-1]) if parts and parts[-1].isdigit() else 0
    if count <= 0:
        await _safe_cb_answer(cb)
        return
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Сначала продлите свою подписку.", show_alert=True)
            return
        grp = await _get_or_create_family_group(session, tg_id)
        current_total = int(grp.seats_total or 0)
        if current_total + count > FAMILY_MAX_SEATS:
            await cb.answer(f"Максимум мест: {FAMILY_MAX_SEATS}", show_alert=True)
            return
        price = await _get_family_seat_price(session, tg_id)
        await _set_family_payment_context(session, tg_id, mode=1, count=count, slot_no=None)
        await session.commit()
    if settings.payment_provider == "platega":
        await _start_platega_family_payment(cb, tg_id=tg_id, seats=count, amount_rub=price * count)
    else:
        async with session_scope() as session:
            await _apply_family_payment(session, owner_tg_id=tg_id, seats=count, mode=1)
            await session.commit()
        await cb.message.edit_text("✅ Новые семейные места добавлены.", reply_markup=_family_manage_kb(can_manage=True, has_seats=True))
        await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data and c.data.startswith("family:renewslot:"))
async def on_family_renew_slot(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    parts = (cb.data or "").split(":")
    slot_no = int(parts[-1]) if parts and parts[-1].isdigit() else 0
    if slot_no <= 0:
        await _safe_cb_answer(cb)
        return
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Сначала продлите свою подписку.", show_alert=True)
            return
        grp = await _get_or_create_family_group(session, tg_id)
        if slot_no > int(grp.seats_total or 0):
            await cb.answer("Такого места нет.", show_alert=True)
            return
        price = await _get_family_seat_price(session, tg_id)
        await _set_family_payment_context(session, tg_id, mode=4, count=1, slot_no=slot_no)
        await session.commit()
    if settings.payment_provider == "platega":
        await _start_platega_family_payment(cb, tg_id=tg_id, seats=1, amount_rub=price)
    else:
        async with session_scope() as session:
            await _apply_family_payment(session, owner_tg_id=tg_id, seats=1, mode=4, slot_no=slot_no)
            await session.commit()
        await cb.message.edit_text(f"✅ Место #{slot_no} продлено на месяц.", reply_markup=_family_renew_menu_kb())
        await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data and c.data.startswith("family:slot:"))
async def on_family_slot_view(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    parts = (cb.data or "").split(":")
    slot_no = int(parts[-1]) if parts and parts[-1].isdigit() else 0
    if slot_no <= 0:
        await _safe_cb_answer(cb)
        return
    now = utcnow()
    async with session_scope() as session:
        grp = await _get_or_create_family_group(session, tg_id)
        if slot_no > int(grp.seats_total or 0):
            await cb.answer("Такого места нет.", show_alert=True)
            return
        rows = await _get_family_profiles(session, tg_id)
        prof = next((r for r in rows if int(r.slot_no or 0) == slot_no), None)
        if not prof:
            await cb.answer("Место не найдено.", show_alert=True)
            return
        paid = _is_family_slot_paid(prof, now=now)
        state = _family_slot_state_text(prof, now=now)
        cfg = "создан" if prof.vpn_peer_id else "не создан"
        text = (
            f"📋 <b>Место #{slot_no}</b>\n\n"
            f"Статус: <b>{state}</b>\n"
            f"Профиль: <b>{cfg}</b>\n"
            f"Пауза: <b>{'да' if prof.is_paused else 'нет'}</b>"
        )
    await cb.message.edit_text(text, reply_markup=_family_slot_actions_kb(slot_no, paid=paid), parse_mode="HTML")
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "family:renew_one")
async def on_family_renew_one(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Сначала продлите свою подписку.", show_alert=True)
            return
        price = await _get_family_seat_price(session, tg_id)
        await _set_family_payment_context(session, tg_id, mode=2, count=1, slot_no=None)
        await session.commit()
    if settings.payment_provider == "platega":
        await _start_platega_family_payment(cb, tg_id=tg_id, seats=1, amount_rub=price)
    else:
        async with session_scope() as session:
            await _apply_family_payment(session, owner_tg_id=tg_id, seats=1, mode=2)
            await session.commit()
        await cb.message.edit_text("✅ Ближайшее семейное место продлено на месяц.", reply_markup=_family_renew_menu_kb())
        await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "family:renew_all")
async def on_family_renew_all(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Сначала продлите свою подписку.", show_alert=True)
            return
        grp = await _get_or_create_family_group(session, tg_id)
        seats_total = int(grp.seats_total or 0)
        if seats_total <= 0:
            await cb.answer("Семейных мест пока нет.", show_alert=True)
            return
        price = await _get_family_seat_price(session, tg_id)
        await _set_family_payment_context(session, tg_id, mode=3, count=seats_total, slot_no=None)
        await session.commit()
    if settings.payment_provider == "platega":
        await _start_platega_family_payment(cb, tg_id=tg_id, seats=seats_total, amount_rub=price * seats_total)
    else:
        async with session_scope() as session:
            await _apply_family_payment(session, owner_tg_id=tg_id, seats=seats_total, mode=3)
            await session.commit()
        await cb.message.edit_text("✅ Все семейные места продлены на месяц.", reply_markup=_family_renew_menu_kb())
        await _safe_cb_answer(cb)


async def _start_platega_family_payment(cb: CallbackQuery, *, tg_id: int, seats: int, amount_rub: int) -> None:
    """Create a Platega transaction for family group seats."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from app.services.payments.platega import PlategaClient, PlategaError
    from app.db.models import Payment

    if not settings.platega_merchant_id or not settings.platega_secret:
        await cb.answer("Платежи временно недоступны")
        return

    client = PlategaClient(merchant_id=settings.platega_merchant_id, secret=settings.platega_secret)
    payload = f"tg_id={tg_id};family_seats={seats};period=1m"
    description = f"Семейная группа VPN: {seats} мест (TG {tg_id})"
    try:
        res = await client.create_transaction(
            payment_method=settings.platega_payment_method,
            amount=int(amount_rub),
            currency="RUB",
            description=description,
            return_url=settings.platega_return_url,
            failed_url=settings.platega_failed_url,
            payload=payload,
        )
    except PlategaError:
        await cb.answer("Ошибка платежного провайдера")
        return

    async with session_scope() as session:
        p = Payment(
            tg_id=tg_id,
            amount=int(amount_rub),
            currency="RUB",
            provider=f"platega_family_{int(seats)}",
            status="pending",
            period_days=30,
            period_months=1,
            provider_payment_id=res.transaction_id,
        )
        session.add(p)
        await session.commit()
        payment_db_id = p.id

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Перейти к оплате", url=res.redirect_url)],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"pay:check:{payment_db_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="vpn:family")],
        ]
    )

    await cb.message.edit_text(
        "💳 <b>Оплата семейной группы VPN</b>\n\n"
        f"Мест: <b>{seats}</b>\n"
        f"Сумма за 1 месяц: <b>{int(amount_rub)} ₽</b>\n\n"
        "1) Нажмите «✅ Перейти к оплате»\n"
        "2) После оплаты нажмите «🔄 Проверить оплату»",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("family:pay:"))
async def on_family_pay(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    parts = (cb.data or "").split(":")
    if len(parts) != 3:
        await _safe_cb_answer(cb)
        return
    try:
        seats = int(parts[2])
    except Exception:
        seats = 0
    seats = max(0, min(FAMILY_MAX_SEATS, seats))
    if seats <= 0:
        await cb.answer("Выберите количество мест", show_alert=True)
        return

    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Сначала продлите свою подписку.", show_alert=True)
            return
        price = await _get_family_seat_price(session, tg_id)
        amount = seats * price
        await _set_family_payment_context(session, tg_id, mode=1, count=seats, slot_no=None)
        await session.commit()

    if settings.payment_provider == "platega":
        await _start_platega_family_payment(cb, tg_id=tg_id, seats=seats, amount_rub=amount)
        return

    # mock provider
    async with session_scope() as session:
        grp, touched_slots = await _apply_family_payment(session, owner_tg_id=tg_id, seats=seats, mode=1)
        await set_app_setting_int(session, f"family_grace_started_ts:{tg_id}", None)
        await set_app_setting_int(session, f"family_grace_seats:{tg_id}", None)
        await session.commit()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да", callback_data="family:bill:yes")],
            [InlineKeyboardButton(text="❌ Нет", callback_data="family:bill:no")],
        ]
    )
    try:
        async with session_scope() as session:
            grp = await _get_or_create_family_group(session, tg_id)
            await _notify_admins_new_purchase(
                cb.bot,
                buyer_tg_id=tg_id,
                amount_rub=int(amount),
                months=1,
                provider="mock_family",
                new_end_at=grp.active_until,
                item_label="Семейная группа VPN",
            )
    except Exception:
        pass

    await cb.message.edit_text(
        "✅ <b>Оплата семейной группы прошла успешно!</b>\n\n"
        "Запомнить и присылать счёт ежемесячно для оплаты семейной группы?",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data in ("family:bill:yes", "family:bill:no"))
async def on_family_billing_opt(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    opt = cb.data.endswith(":yes")
    async with session_scope() as session:
        grp = await _get_or_create_family_group(session, tg_id)
        grp.billing_opt_in = bool(opt)
        await session.commit()
    await cb.answer("Сохранено")
    await on_vpn_family(cb)


def _family_share_kb(seats_total: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(1, seats_total + 1):
        rows.append([
            InlineKeyboardButton(text=f"📤 Профиль {i}", callback_data=f"family:share:{i}"),
            InlineKeyboardButton(text=f"♻️ Сброс {i}", callback_data=f"family:reset:{i}"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="vpn:family")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(lambda c: c.data == "family:share")
async def on_family_share_menu(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    now = utcnow()
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        grp = await _get_or_create_family_group(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Сначала продлите свою подписку.", show_alert=True)
            return
        if not (grp.active_until and grp.active_until > now):
            await cb.answer("Семейная группа не активна. Оплатите продление.", show_alert=True)
            return
        seats_total = int(grp.seats_total or 0)
        if seats_total <= 0:
            await cb.answer("Сначала купите места", show_alert=True)
            return
    await cb.message.edit_text(
        "📤 <b>Поделиться VPN</b>\n\nВыберите профиль (конфиг), который хотите отправить.\n"
        "Бот выдаст файл — вы просто перешлёте его человеку.",
        reply_markup=_family_share_kb(seats_total),
        parse_mode="HTML",
    )
    await _safe_cb_answer(cb)


def _family_forward_instructions_text() -> str:
    return """📌 <b>Как подключить профиль семейной группы</b>

<b>Это отдельный профиль семейной группы.</b> Его можно передать другому человеку или установить на отдельное устройство.

<b>iPhone / iPad</b>
1) Установите приложение <b>WireGuard</b> из App Store.
2) Откройте присланный файл <code>.conf</code>.
3) Нажмите <b>Поделиться</b> → <b>Открыть в WireGuard</b>.
4) Подтвердите импорт и включите туннель.

<b>Android</b>
1) Установите приложение <b>WireGuard</b> из Google Play.
2) Откройте файл <code>.conf</code> или сохраните его в память устройства.
3) В WireGuard нажмите <b>+</b> → <b>Импорт из файла</b>.
4) Выберите присланный файл и включите профиль.
5) Если WireGuard пишет <b>«Неправильное имя»</b>, переименуйте файл в короткое имя латиницей, например <code>wg.conf</code>, и импортируйте снова.

<b>Windows</b>
1) Установите <b>WireGuard</b> для Windows.
2) Откройте программу и нажмите <b>Import tunnel(s) from file</b>.
3) Выберите присланный файл <code>.conf</code>.
4) Нажмите <b>Activate</b>.

<b>macOS</b>
1) Установите <b>WireGuard</b> из App Store.
2) Откройте приложение и нажмите <b>Import tunnel(s) from file</b>.
3) Выберите присланный файл <code>.conf</code>.
4) Активируйте профиль.

<b>Linux</b>
1) Установите WireGuard.
2) Сохраните файл как, например, <code>/etc/wireguard/wg0.conf</code>.
3) Запустите: <code>sudo wg-quick up wg0</code>.
4) Для отключения: <code>sudo wg-quick down wg0</code>.

Подробные инструкции для каждого устройства также есть в боте: <b>VPN → Инструкция</b>."""


@router.callback_query(lambda c: c.data and c.data.startswith("family:reset:"))
async def on_family_reset_slot(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    parts = (cb.data or "").split(":")
    if len(parts) != 3:
        await _safe_cb_answer(cb)
        return
    try:
        slot_no = int(parts[2])
    except Exception:
        slot_no = 0
    if slot_no <= 0:
        await _safe_cb_answer(cb)
        return

    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Сначала продлите свою подписку.", show_alert=True)
            return

        grp = await _get_or_create_family_group(session, tg_id)
        now = utcnow()
        if not (grp.active_until and grp.active_until > now):
            await cb.answer("Семейная группа не активна. Оплатите продление.", show_alert=True)
            return

        from app.db.models import FamilyVpnProfile, VpnPeer
        prof = await session.scalar(
            select(FamilyVpnProfile).where(FamilyVpnProfile.owner_tg_id == tg_id, FamilyVpnProfile.slot_no == slot_no).limit(1)
        )
        if not prof:
            await cb.answer("Профиль не найден", show_alert=True)
            return
        if not _is_family_slot_paid(prof, now=now):
            await cb.answer("Срок этого места истёк. Сначала продлите его.", show_alert=True)
            return
        if prof.is_paused:
            await cb.answer("Профиль на паузе. Сначала включите его.", show_alert=True)
            return

        old_peer = await session.get(VpnPeer, int(prof.vpn_peer_id or 0)) if getattr(prof, "vpn_peer_id", None) else None
        if old_peer:
            old_code = str(getattr(old_peer, "server_code", None) or os.environ.get("VPN_CODE") or "NL1").upper()
            try:
                await vpn_service.remove_peer_for_server(public_key=old_peer.client_public_key, server_code=old_code)
            except Exception:
                pass
            old_peer.is_active = False
            old_peer.revoked_at = utcnow()
            old_peer.rotation_reason = f"family_slot_{slot_no}_manual_reset"

        peer_dict = await vpn_service.create_extra_peer(session, tg_id=tg_id, prefer_current_server=False)
        row = await session.get(VpnPeer, int(peer_dict.get("peer_id")))
        if row:
            row.rotation_reason = f"family_slot_{slot_no}"
        prof.vpn_peer_id = int(peer_dict.get("peer_id"))
        await session.commit()

    conf_text = vpn_service.build_wg_conf(
        peer_dict,
        user_label=f"family:{tg_id}:{slot_no}",
        server_public_key=str(peer_dict.get("server_public_key") or ""),
        endpoint=str(peer_dict.get("endpoint") or ""),
        dns=str(peer_dict.get("dns") or os.environ.get("VPN_DNS", "1.1.1.1")),
    )
    conf_file = BufferedInputFile(conf_text.encode(), filename=f"wgf{slot_no}.conf")
    await cb.message.answer_document(
        conf_file,
        caption=(
            f"♻️ Новый конфиг для семейного профиля <b>#{slot_no}</b> готов.\n"
            "Старый конфиг больше не работает — отправьте человеку новый файл."
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
        ),
    )
    await cb.answer("Профиль сброшен")


@router.callback_query(lambda c: c.data and c.data.startswith("family:share:"))
async def on_family_share_slot(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id
    parts = (cb.data or "").split(":")
    if len(parts) != 3:
        await _safe_cb_answer(cb)
        return
    try:
        slot_no = int(parts[2])
    except Exception:
        slot_no = 0
    if slot_no <= 0:
        await _safe_cb_answer(cb)
        return

    now = utcnow()
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Сначала продлите свою подписку.", show_alert=True)
            return
        grp = await _get_or_create_family_group(session, tg_id)
        if not (grp.active_until and grp.active_until > now):
            await cb.answer("Семейная группа не активна. Оплатите продление.", show_alert=True)
            return
        if slot_no > int(grp.seats_total or 0):
            await cb.answer("Такого профиля нет", show_alert=True)
            return

        from app.db.models import FamilyVpnProfile, VpnPeer
        prof = await session.scalar(
            select(FamilyVpnProfile).where(FamilyVpnProfile.owner_tg_id == tg_id, FamilyVpnProfile.slot_no == slot_no).limit(1)
        )
        if not prof:
            await cb.answer("Профиль не найден", show_alert=True)
            return
        if not _is_family_slot_paid(prof, now=now):
            await cb.answer("Срок этого места истёк. Сначала продлите его.", show_alert=True)
            return

        peer_dict = None
        if prof.vpn_peer_id:
            # load existing peer (can be inactive if paused/expired)
            row = await session.scalar(select(VpnPeer).where(VpnPeer.id == prof.vpn_peer_id).limit(1))
            if row and row.is_active and not prof.is_paused:
                peer_dict = vpn_service._row_to_peer_dict(row)  # type: ignore
            elif row and not prof.is_paused:
                # Family-slot restore policy:
                # - within 24h from revoked_at: the SAME peer may be restored,
                #   but only on its original server and only if that server still has free capacity;
                # - after 24h (or if restore within grace is impossible due to capacity):
                #   the old peer is fully removed from its original server and a NEW peer is issued
                #   on any server with free capacity.
                old_code = str(getattr(row, "server_code", None) or os.environ.get("VPN_CODE") or "NL1").upper()
                within_grace = False
                try:
                    rv = getattr(row, "revoked_at", None)
                    if rv is not None:
                        within_grace = (utcnow() - rv) <= timedelta(hours=24)
                except Exception:
                    within_grace = False

                if within_grace:
                    try:
                        preferred = await vpn_service._pick_server_for_extra_peer(session, inherited_code=old_code)
                        preferred_code = str(preferred.get("code") or "").upper()
                    except Exception:
                        preferred_code = ""
                    if preferred_code == old_code:
                        try:
                            old_server = None
                            for s in (vpn_service._load_vpn_servers() or []):
                                if str(s.get("code") or "").upper() == old_code:
                                    old_server = s
                                    break
                            if old_server:
                                provider = vpn_service._provider_for(
                                    host=str(old_server.get("host") or os.environ.get("WG_SSH_HOST") or ""),
                                    port=int(old_server.get("port") or 22),
                                    user=str(old_server.get("user") or os.environ.get("WG_SSH_USER") or ""),
                                    password=old_server.get("password"),
                                    interface=str(old_server.get("interface") or os.environ.get("VPN_INTERFACE", "wg0")),
                                    tc_dev=str(old_server.get("tc_dev") or old_server.get("wg_tc_dev") or os.environ.get("WG_TC_DEV") or os.environ.get("VPN_TC_DEV") or ""),
                                    tc_parent_rate_mbit=int(old_server.get("tc_parent_rate_mbit") or old_server.get("wg_tc_parent_rate_mbit") or os.environ.get("WG_TC_PARENT_RATE_MBIT") or os.environ.get("VPN_TC_PARENT_RATE_MBIT") or 1000),
                                )
                                await provider.add_peer(row.client_public_key, row.client_ip, tg_id=tg_id)
                                row.is_active = True
                                row.revoked_at = None
                                row.rotation_reason = None
                                peer_dict = vpn_service._row_to_peer_dict(row)  # type: ignore
                        except Exception:
                            peer_dict = None

                if not peer_dict:
                    try:
                        await vpn_service.remove_peer_for_server(public_key=row.client_public_key, server_code=old_code)
                    except Exception:
                        pass
                    row.is_active = False
                    row.revoked_at = utcnow()
                    row.rotation_reason = row.rotation_reason or f"family_slot_{slot_no}_expired_replace"

        if not peer_dict and not prof.is_paused:
            # Create a new extra peer (same tg_id, unique IP)
            peer_dict = await vpn_service.create_extra_peer(session, tg_id=tg_id, prefer_current_server=False)
            # mark as family slot
            try:
                row = await session.get(VpnPeer, int(peer_dict.get("peer_id")))
                if row:
                    row.rotation_reason = f"family_slot_{slot_no}"
            except Exception:
                pass
            prof.vpn_peer_id = int(peer_dict.get("peer_id"))

        if prof.is_paused:
            await cb.answer("Профиль на паузе. Сначала включите его.", show_alert=True)
            return

        await session.commit()

    # Build and send config (no QR)
    conf_text = vpn_service.build_wg_conf(
        peer_dict,
        user_label=f"family:{tg_id}:{slot_no}",
        server_public_key=str(peer_dict.get("server_public_key") or ""),
        endpoint=str(peer_dict.get("endpoint") or ""),
        dns=str(peer_dict.get("dns") or os.environ.get("VPN_DNS", "1.1.1.1")),
    )
    conf_file = BufferedInputFile(conf_text.encode(), filename=f"wgf{slot_no}.conf")
    await cb.message.answer_document(
        conf_file,
        caption=(
            f"WireGuard конфиг для семейной группы (профиль {slot_no}).\n"
            "Перешлите этот файл человеку, которому нужен VPN." 
        ),
    )
    await cb.message.answer(_family_forward_instructions_text(), parse_mode="HTML")
    await cb.message.answer("⬅️ Назад", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад к семейной группе", callback_data="vpn:family")]]))
    await _safe_cb_answer(cb)




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





def _lte_summary_text(*, has_access: bool, paid: bool, sub_end: datetime | None, lte_price: int) -> str:
    lines = [
        "📶 <b>VPN LTE</b>",
        "",
        "Это отдельный профиль для мобильного интернета, который помогает, когда обычный мобильный доступ работает нестабильно.",
        "",
    ]
    if has_access and sub_end:
        lines.append(f"✅ LTE активирован.")
        lines.append(f"Активно до: <b>{fmt_dt(sub_end)}</b>")
        lines.append("")
        lines.append("Ниже вы можете скопировать конфиг для Happ+ или получить новый конфиг.")
    else:
        lines.append("ℹ️ Подробное описание, когда использовать LTE-профиль и какие есть ограничения, откроется по кнопке <b>«Что это?»</b>.")
        lines.append("")
        lines.append(f"Активация на 1 месяц: <b>{lte_price} ₽</b>.")
        if not paid:
            lines.append("Для пользователей на пробном периоде доплата не требуется.")
    return "\n".join(lines)




def _lte_menu_text(*, has_access: bool, sub_end: datetime | None, lte_price: int, paid: bool) -> str:
    lines = [
        "📶 <b>VPN LTE</b>",
        "",
        "Это отдельный профиль для случаев, когда обычный мобильный интернет работает нестабильно или с ограничениями.",
        "",
    ]
    if has_access and sub_end:
        lines.append(f"Статус подписки: <b>активна до {fmt_dt(sub_end)}</b>")
        lines.append("")
        lines.append("Ниже вы можете скопировать конфиг для Happ+ или получить новый конфиг.")
    else:
        lines.append("Статус подписки: <b>неактивна</b>")
        lines.append("")
        lines.append("Нажмите <b>«Что это?»</b>, чтобы прочитать подробную информацию о разделе.")
        lines.append("")
        lines.append(f"Активация на 1 месяц: <b>{lte_price} ₽</b>.")
        if not paid:
            lines.append("Для пользователей на пробном периоде доплата не требуется.")
    return "\n".join(lines)
def _lte_about_text(*, has_access: bool, sub_end: datetime | None) -> str:
    active_until_text = f"\n\n✅ LTE уже активирован.\nАктивно до: <b>{fmt_dt(sub_end)}</b>." if has_access and sub_end else ""
    return LTE_INFO_TEXT + active_until_text

LTE_INFO_TEXT = """📶 <b>VPN LTE</b>

<b>Что это такое</b>
VPN LTE — это отдельный профиль для ситуаций, когда в регионе временно ограничен или нестабильно работает обычный мобильный интернет. Он помогает открыть нужные приложения и сайты через защищённое соединение, когда стандартное подключение по LTE/4G/5G/3G работает с перебоями.

<b>Как это работает</b>
Сервис шифрует трафик и отправляет его через отдельный сервер. Для пользователя это обычное защищённое подключение: вы просто включаете профиль в приложении и пользуетесь интернетом как обычно. Это технический инструмент для стабильного доступа и защиты соединения, а не замена мобильной связи и не обещание доступа ко всем ресурсам в любой момент.

<b>Когда включать</b>
— когда мобильный интернет в вашем регионе работает нестабильно;
— когда приложения или сайты не открываются через обычный LTE;
— когда нужно быстро восстановить доступ через мобильную сеть.

<b>Важно</b>
— профиль рассчитан именно на мобильный интернет (LTE/5G/4G/3G);
— при переходе на Wi‑Fi его лучше отключать;
— некоторые сервисы из так называемых белых списков могут через этот профиль не открываться; если нужный вам сервис не загружается, просто отключите VPN LTE и попробуйте снова без него.

<b>Где такие ограничения уже встречались</b>
По открытым сообщениям СМИ, ограничения мобильного интернета уже отмечались, в том числе, в Крыму, Ростовской области, Краснодарском крае, Дагестане, Самарской, Саратовской, Орловской, Ивановской, Ярославской, Воронежской и Ульяновской областях, а также в Санкт‑Петербурге и Ленинградской области. Перечень может меняться в зависимости от региона и текущей обстановки.

<b>Законность использования</b>
Профиль предназначен для личного использования как средство защищённого соединения и стабильного доступа к обычным интернет‑сервисам. Пользователь обязан соблюдать применимое законодательство и правила используемых сервисов.

<i>⚠️ Не работает в городе Санкт-Петербург.</i>"""


async def _lte_is_main_sub_active(tg_id: int) -> tuple[bool, datetime | None]:
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        return _is_sub_active(sub.end_at), sub.end_at


async def _lte_has_access(tg_id: int) -> tuple[bool, datetime | None, bool]:
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            return False, None, False
        paid = await has_successful_payments(session, tg_id)
    allowed = await lte_vpn_service.has_lte_access(tg_id, subscription_end_at=sub.end_at, has_success_payment=paid)
    lte_row = await lte_vpn_service.get_client(tg_id)
    lte_end = lte_row.cycle_anchor_end_at if lte_row else (sub.end_at if not paid else None)
    return allowed, lte_end, paid


async def _lte_price_rub() -> int:
    async with session_scope() as session:
        return await get_app_setting_int(session, "lte_activation_rub", default=settings.lte_activation_rub)


@router.callback_query(lambda c: c.data == "vpn:lte")
async def on_vpn_lte_menu(cb: CallbackQuery) -> None:
    if not settings.lte_enabled:
        await cb.answer("Раздел временно отключён", show_alert=True)
        return
    has_sub, sub_end = await _lte_is_main_sub_active(cb.from_user.id)
    if not has_sub:
        await cb.message.edit_text(
            "📶 <b>VPN LTE</b>\n\n🚫 Доступен только при активной основной подписке.\nОформи подписку в разделе «💳 Оплата».",
            reply_markup=kb_back_home(),
            parse_mode="HTML",
        )
        await _safe_cb_answer(cb)
        return
    has_access, sub_end, paid = await _lte_has_access(cb.from_user.id)
    lte_price = await _lte_price_rub()
    txt = _lte_menu_text(has_access=has_access, sub_end=sub_end, lte_price=lte_price, paid=paid)
    await cb.message.edit_text(txt, reply_markup=kb_lte_vpn(has_access=has_access, activation_rub=lte_price), parse_mode="HTML")
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "vpn:lte:about")
async def on_vpn_lte_about(cb: CallbackQuery) -> None:
    has_access, sub_end, _ = await _lte_has_access(cb.from_user.id)
    lte_price = await _lte_price_rub()
    await cb.message.edit_text(_lte_about_text(has_access=has_access, sub_end=sub_end), reply_markup=kb_lte_vpn(has_access=has_access, activation_rub=lte_price), parse_mode="HTML")
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "vpn:lte:pay")
async def on_vpn_lte_pay(cb: CallbackQuery) -> None:
    has_sub, _ = await _lte_is_main_sub_active(cb.from_user.id)
    if not has_sub:
        await cb.answer("Сначала нужна основная подписка", show_alert=True)
        return
    async with session_scope() as session:
        paid = await has_successful_payments(session, cb.from_user.id)
        if not paid:
            await cb.answer("Для пробного периода доплата не нужна", show_alert=True)
            return
    provider = settings.payment_provider
    if provider == "platega":
        lte_price = await _lte_price_rub()
        await _start_platega_payment(cb, tg_id=cb.from_user.id, amount_override=lte_price, months_override=0, promo_code="lte")
        return
    async with session_scope() as session:
        sub = await get_subscription(session, cb.from_user.id)
        await lte_vpn_service.activate_paid_month(cb.from_user.id)
        lte_price = await _lte_price_rub()
        pay = Payment(tg_id=cb.from_user.id, amount=lte_price, currency="RUB", provider="mock_lte", status="success", period_days=0, period_months=0)
        session.add(pay)
        await session.commit()
    try:
        async with session_scope() as session:
            sub = await get_subscription(session, cb.from_user.id)
            await _notify_admins_new_purchase(
                cb.bot,
                buyer_tg_id=cb.from_user.id,
                amount_rub=int(lte_price),
                months=1,
                provider="mock_lte",
                new_end_at=sub.end_at,
                item_label="VPN LTE",
            )
    except Exception:
        pass
    await cb.answer("LTE активирован")
    await on_vpn_lte_menu(cb)


@router.callback_query(lambda c: c.data == "vpn:lte:install")
async def on_vpn_lte_install(cb: CallbackQuery) -> None:
    has_access, sub_end, _ = await _lte_has_access(cb.from_user.id)
    if not has_access:
        await cb.answer("Сначала активируйте VPN LTE", show_alert=True)
        return
    used = await lte_vpn_service.active_clients_count()
    if used >= settings.lte_max_clients:
        await cb.message.edit_text(
            f"📶 <b>VPN LTE</b>\n\nСейчас все места заняты: <b>{used}/{settings.lte_max_clients}</b>. Попробуйте позже.",
            reply_markup=kb_lte_vpn(has_access=True, activation_rub=await _lte_price_rub()),
            parse_mode="HTML",
        )
        await _safe_cb_answer(cb)
        return
    row = await lte_vpn_service.sync_client(cb.from_user.id, subscription_end_at=sub_end, force_rotate=False)
    url = lte_vpn_service.build_vless_url(row.uuid, tg_id=cb.from_user.id)

    copy_btn: InlineKeyboardButton | None = None
    if CopyTextButton is not None and 1 <= len(url) <= 256:
        try:
            copy_btn = InlineKeyboardButton(
                text="📋 Скопировать в Happ+",
                copy_text=CopyTextButton(text=url),  # type: ignore[arg-type]
            )
        except Exception:
            copy_btn = None

    kb_rows: list[list[InlineKeyboardButton]] = []
    if copy_btn:
        kb_rows.append([copy_btn])
    kb_rows.append([
        InlineKeyboardButton(
            text="🍏 Happ Plus (App Store)",
            url="https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973",
        )
    ])
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    if copy_btn:
        howto = (
            "1) Нажмите кнопку <b>«📋 Скопировать в Happ+»</b> — ссылка скопируется автоматически.\n"
            "2) Откройте <b>Happ Plus</b>.\n"
            "3) Нажмите <b>+</b>.\n"
            "4) Выберите <b>Из буфера</b> / <b>Import from Clipboard</b>.\n"
            "5) Подтвердите импорт конфига.\n\n"
            "Если кнопка копирования не сработала, ниже есть сама ссылка — её можно скопировать вручную долгим нажатием."
        )
    else:
        howto = (
            "1) Скопируйте ссылку ниже долгим нажатием → <b>Копировать</b>.\n"
            "2) Откройте <b>Happ Plus</b>.\n"
            "3) Нажмите <b>+</b>.\n"
            "4) Выберите <b>Из буфера</b> / <b>Import from Clipboard</b>.\n"
            "5) Подтвердите импорт конфига."
        )

    show_inline_link = len(url) <= 3500
    link_block = f"<code>{html_escape(url)}</code>" if show_inline_link else "<i>Ссылка слишком длинная для сообщения. Получите новый конфиг или обратитесь в поддержку.</i>"

    await cb.message.edit_text(
        "📶 <b>VPN LTE</b>\n\n"
        "📌 <b>Как добавить конфиг в Happ Plus</b>\n"
        f"{howto}\n\n"
        "🔗 <b>Ссылка для Happ Plus</b>\n"
        f"{link_block}\n\n"
        "После подключения отключайтесь при переходе на Wi‑Fi.",
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    # LTE connection reminder disabled to avoid noisy unsolicited notifications.
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "vpn:lte:reset")
async def on_vpn_lte_reset(cb: CallbackQuery) -> None:
    has_access, sub_end, _ = await _lte_has_access(cb.from_user.id)
    if not has_access:
        await cb.answer("Сначала активируйте VPN LTE", show_alert=True)
        return
    row = await lte_vpn_service.sync_client(cb.from_user.id, subscription_end_at=sub_end, force_rotate=True)
    url = lte_vpn_service.build_vless_url(row.uuid, tg_id=cb.from_user.id)

    copy_btn: InlineKeyboardButton | None = None
    if CopyTextButton is not None and 1 <= len(url) <= 256:
        try:
            copy_btn = InlineKeyboardButton(
                text="📋 Скопировать новый конфиг в Happ+",
                copy_text=CopyTextButton(text=url),  # type: ignore[arg-type]
            )
        except Exception:
            copy_btn = None

    kb_rows: list[list[InlineKeyboardButton]] = []
    if copy_btn:
        kb_rows.append([copy_btn])
    kb_rows.append([
        InlineKeyboardButton(
            text="🍏 Happ Plus (App Store)",
            url="https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973",
        )
    ])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="vpn:lte")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    if copy_btn:
        howto = (
            "1) Нажмите кнопку <b>«📋 Скопировать новый конфиг в Happ+»</b>.\n"
            "2) Откройте <b>Happ Plus</b> → <b>+</b> → <b>Из буфера</b>.\n"
            "3) Подтвердите импорт нового конфига."
        )
    else:
        howto = (
            "1) Скопируйте ссылку ниже вручную.\n"
            "2) Откройте <b>Happ Plus</b> → <b>+</b> → <b>Из буфера</b>.\n"
            "3) Подтвердите импорт нового конфига."
        )

    link_block = f"<code>{html_escape(url)}</code>" if len(url) <= 3500 else "<i>Ссылка слишком длинная для сообщения. Попробуйте ещё раз позже.</i>"

    await cb.message.edit_text(
        "♻️ <b>Конфиг VPN LTE обновлён.</b>\n\n"
        "Старый UUID отключён, новый конфиг готов.\n\n"
        "📌 <b>Как добавить новый конфиг в Happ Plus</b>\n"
        f"{howto}\n\n"
        "🔗 <b>Новая ссылка</b>\n"
        f"{link_block}",
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await _safe_cb_answer(cb)

