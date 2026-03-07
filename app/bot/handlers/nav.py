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
from sqlalchemy import select, func, literal

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
    kb_kinoteka,
)
from app.bot.ui import days_left, fmt_dt, utcnow
from app.core.config import settings
from app.db.models import Payment, User
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.repo import extend_subscription, get_subscription, get_price_rub, is_trial_available, set_trial_used, has_used_trial, set_app_setting_int, get_app_setting_int

from app.services.vpn.service import vpn_service
from app.services.referrals.service import referral_service

router = Router()


async def _restore_wg_peers_after_payment(session, tg_id: int) -> None:
    """After a successful payment, re-enable WG peers disabled on expiration.

    Best-effort: never breaks payment flow.
    """
    try:
        await vpn_service.restore_expired_peers(session, tg_id, grace_hours=24)
    except Exception:
        # best-effort
        pass


async def _subscription_required_alert_text(session, tg_id: int) -> str:
    """Choose the correct access-denied text for users without an active subscription."""
    if await is_trial_available(session, tg_id):
        return "Сначала активируйте пробный период: кнопка «🎁 Пробный период 5 дней» в главном меню."
    if await has_used_trial(session, tg_id):
        return "Ваш пробный период закончился. Чтобы продолжить пользоваться сервисом, оплатите подписку."
    return "Для доступа необходимо оплатить подписку!"

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
    return f'sbsVPN{n}.conf'


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
    return f"sbsVPN{int(serial)}.conf"


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


async def _vpn_seats_by_server_nav() -> dict[str, int]:
    from app.db.models import VpnPeer, Subscription

    now = utcnow()
    default_code = (os.environ.get("VPN_CODE") or "NL").upper()
    default_code_lit = literal(default_code)
    async with session_scope() as session:
        q = (
            select(
                func.coalesce(VpnPeer.server_code, default_code_lit).label("code"),
                func.count(func.distinct(VpnPeer.tg_id)).label("cnt"),
            )
            .join(Subscription, Subscription.tg_id == VpnPeer.tg_id)
            .where(
                VpnPeer.is_active == True,  # noqa: E712
                Subscription.is_active == True,  # noqa: E712
                Subscription.end_at.is_not(None),
                Subscription.end_at > now,
            )
            .group_by(func.coalesce(VpnPeer.server_code, default_code_lit))
        )
        res = await session.execute(q)
        rows = res.all()
    return {str(code).upper(): int(cnt) for code, cnt in rows}


def _vpn_capacity_limit() -> int:
    try:
        return max(1, int(os.environ.get("VPN_MAX_ACTIVE", "40") or 40))
    except Exception:
        return 40


async def _pick_available_vpn_server(*, preferred_code: str | None = None, current_tg_id: int | None = None) -> dict | None:
    servers = [s for s in _load_vpn_servers() if _server_is_ready(s)]
    if not servers:
        return None

    used = await _vpn_seats_by_server_nav()
    cap = _vpn_capacity_limit()

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
        # If host/user are missing, treat as not connected yet.
        if not srv.get("host") or not srv.get("user"):
            return "Недоступно"

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
            if st.get("ok") and st.get("cpu_load_percent") is not None:
                cpu = float(st["cpu_load_percent"])
                cpu_str = f"{cpu:.1f}%" if cpu >= 0.1 else ("&lt;0.1%" if cpu > 0 else "0.0%")
                return cpu_str
        except Exception:
            pass
        return "Подключается..."

    for srv in servers:
        code = str(srv.get("code") or "").upper() or "??"
        name = str(srv.get("name") or f"VPN-{code}")
        load = await _fmt_status(srv)
        if load in ("Недоступно", "Подключается..."):
            lines.append(f'🌍{_flag(code)} "{name}", нагрузка: <b>{load}</b>')
        else:
            lines.append(f'🌍{_flag(code)} "{name}", нагрузка составляет: <b>{load}</b>')

    lines.append("")
    lines.append("🔐 Форма шифрования: <b>ChaCha20-Poly1305</b>")

    # Safe string building (prevents SyntaxError due to unterminated literals)
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
            await cb.message.edit_text(await _build_home_text(), reply_markup=kb_main(show_trial=show_trial), parse_mode="HTML")
        except Exception:
            pass
        return

    if where == "home":
        # Home text may wait on VPN status; callback already answered above.
        await _cleanup_flow_messages_for_user(cb.bot, cb.message.chat.id, cb.from_user.id)
        try:
            show_trial = await _trial_visible_for_user(cb.from_user.id)
            await cb.message.edit_text(await _build_home_text(), reply_markup=kb_main(show_trial=show_trial), parse_mode="HTML")
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
        async with session_scope() as session:
            price_rub = await get_price_rub(session)
        try:
            await cb.message.edit_text(
                f"💳 Оплата\n\nТариф: {price_rub} ₽ / {settings.period_months} мес.",
                reply_markup=kb_pay(price_rub=price_rub),
            )
        except Exception:
            pass
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
        if pay:
            await referral_service.on_successful_payment(session, pay)

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
                    # Reuse the same logic as manual check
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

                    # Mark one-time winback promo as consumed if this payment was promo.
                    try:
                        if (pay.provider or "").startswith("platega_winback_"):
                            await set_app_setting_int(session, f"winback_promo_consumed:{tg_id}", 1)
                    except Exception:
                        pass

                    await _restore_wg_peers_after_payment(session, tg_id)

                    pay.status = "success"
                    await referral_service.on_successful_payment(session, pay)

                    sub.end_at = new_end
                    sub.is_active = True
                    sub.status = "active"
                    await session.commit()

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
    description = f"Подписка SBS: {pay_months} мес (TG {tg_id})"

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
    from sqlalchemy import select, func, literal
    from app.db.models import Payment

    async with session_scope() as session:
        p = Payment(
            tg_id=tg_id,
            amount=price_rub,
            currency="RUB",
            provider=(f"platega_{promo_code}" if promo_code else "platega"),
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

            # Mark one-time winback promo as consumed if this payment was promo.
            try:
                if (pay.provider or "").startswith("platega_winback_"):
                    await set_app_setting_int(session, f"winback_promo_consumed:{cb.from_user.id}", 1)
            except Exception:
                pass

            await _restore_wg_peers_after_payment(session, cb.from_user.id)

            # referral earnings processing: use the newest successful payment row
            # (extend_subscription inserts a Payment row). We keep original pending row too.
            pay.status = "success"
            await referral_service.on_successful_payment(session, pay)

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

            await session.commit()

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
        "1) Нажмите «📦 Отправить конфиг + QR»\n"
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
            await cb.answer(await _subscription_required_alert_text(session, tg_id), show_alert=True)
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
                    "Чтобы получить новый конфиг, нажмите «📦 Отправить конфиг + QR» и выберите сервер."
                )
            else:
                text = (
                    "ℹ️ <b>У вас ещё нет конфига</b>\n\n"
                    "Чтобы посмотреть/установить свой конфиг, сначала получите его: "
                    "нажмите «📦 Отправить конфиг + QR» и выберите сервер."
                )

            try:
                await cb.message.answer(text, reply_markup=kb_vpn(show_my_config=False), parse_mode="HTML")
            except Exception:
                pass
            return

        # Determine user's current location for the active peer (best-effort).
        code = (active.server_code or os.environ.get("VPN_CODE", "NL")).upper()
        servers = _load_vpn_servers()
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
            conf_text = vpn_service.build_wg_conf(peer, user_label=str(tg_id))
            loc_title = "<b>ваша локация</b>"

        filename = await _get_or_assign_vpn_bundle_filename_for_peer(session, getattr(active, 'id', None))
        await session.commit()

    # Build QR + files.
    qr_img = qrcode.make(conf_text)
    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    buf.seek(0)

    conf_file = BufferedInputFile(conf_text.encode(), filename=filename)
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
    await _store_last_vpn_conf_messages(
        tg_id=tg_id,
        chat_id=chat_id,
        conf_msg_id=msg_conf.message_id,
        qr_msg_id=msg_qr.message_id,
    )

    asyncio.create_task(_schedule_vpn_cleanup_and_followup(cb.bot, chat_id=chat_id, messages=[msg_conf, msg_qr]))


# --- VPN location selection / migration ---


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
    servers = _load_vpn_servers()

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
                async with session_scope() as session:
                    for old_srv, old_pub in old_peers:
                        try:
                            await vpn_service.remove_peer_for_server(
                                public_key=old_pub,
                                host=str(old_srv["host"]),
                                port=int(old_srv.get("port") or 22),
                                user=str(old_srv["user"]),
                                password=old_srv.get("password"),
                                interface=str(old_srv.get("interface") or "wg0"),
                            )
                        except Exception:
                            pass

                    # Mark old peer rows inactive in DB (best-effort)
                    from app.db.models import VpnPeer
                    q = select(VpnPeer).where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True)  # noqa: E712
                    res = await session.execute(q)
                    rows = list(res.scalars().all())
                    for r in rows:
                        if r.client_public_key != new_public_key:
                            r.is_active = False
                            r.revoked_at = utcnow()
                            r.rotation_reason = "migrated"
                    await session.commit()

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
            await cb.answer(await _subscription_required_alert_text(session, tg_id), show_alert=True)
            return

    servers = _load_vpn_servers()
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
            await cb.answer(await _subscription_required_alert_text(session, tg_id), show_alert=True)
            return

    servers = _load_vpn_servers()
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
                )
            except Exception:
                pass
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

    conf_file = BufferedInputFile(conf_text.encode(), filename=filename)
    qr_file = BufferedInputFile(buf.getvalue(), filename="wg.png")

    msg_conf = await cb.message.answer_document(
        document=conf_file,
        caption=(
            (
                f"ℹ️ Выбранная локация была занята, поэтому выдан ближайший доступный сервер: {_vpn_flag(code)} <b>{srv['name']}</b>.\n\n"
                if auto_moved else ""
            )
            + f"WireGuard конфиг для локации {_vpn_flag(code)} <b>{srv['name']}</b>.\n"
            + (
                "⚠️ После подключения к новой локации <b>старый конфиг будет отключён</b>.\n"
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

    await cb.message.edit_text(text, reply_markup=_wg_download_kb(platform), parse_mode="HTML")
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "vpn:reset:confirm")
async def on_vpn_reset_confirm(cb: CallbackQuery) -> None:
    # ✅ FIX: запрет экрана reset_confirm без активной подписки
    tg_id = cb.from_user.id
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer(await _subscription_required_alert_text(session, tg_id), show_alert=True)
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
            await cb.answer(await _subscription_required_alert_text(session, tg_id), show_alert=True)
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

            conf_text = vpn_service.build_wg_conf(peer, user_label=str(tg_id))

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
                    text="⚠️ Не удалось сбросить VPN из-за временной ошибки. Попробуй ещё раз через минуту.",
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
        "Теперь откройте раздел VPN и нажмите «📦 Отправить конфиг + QR», чтобы получить конфиг и выбрать сервер."
    )
    try:
        await cb.message.edit_text(text, reply_markup=kb_main(show_trial=False), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb_main(show_trial=False), parse_mode="HTML")
    await _safe_cb_answer(cb)


@router.callback_query(lambda c: c.data == "vpn:bundle")
async def on_vpn_bundle(cb: CallbackQuery) -> None:
    # After pressing "Получить конфиг" we immediately ask for a location.
    # Выдача разрешена только с активной подпиской или активным пробным периодом.
    tg_id = cb.from_user.id
    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer(await _subscription_required_alert_text(session, tg_id), show_alert=True)
            return

    await on_vpn_location_menu(cb)


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

