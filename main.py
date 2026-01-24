import asyncio
import base64
import os
import secrets
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text
from dateutil.relativedelta import relativedelta

import qrcode
from aiogram.types import FSInputFile


# ================== CONFIG ==================
def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

SCHEDULER_ENABLED = env_bool("SCHEDULER_ENABLED", True)

PRICE_RUB = 299
PERIOD_MONTHS = 1
PERIOD_DAYS = 30  # legacy compatibility (your DB has NOT NULL period_days)

MSK = timezone(timedelta(hours=3))

VPN_MODE = os.getenv("VPN_MODE", "mock").strip().lower()  # mock now
VPN_ENDPOINT = os.getenv("VPN_ENDPOINT", "1.2.3.4:51820")
VPN_SERVER_PUBLIC_KEY = os.getenv("VPN_SERVER_PUBLIC_KEY", "REPLACE_ME")
VPN_ALLOWED_IPS = os.getenv("VPN_ALLOWED_IPS", "0.0.0.0/0, ::/0")
VPN_DNS = os.getenv("VPN_DNS", "1.1.1.1,8.8.8.8")


def make_async_db_url(url: str) -> str:
    # Railway: postgres://... -> asyncpg needs postgresql+asyncpg://...
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    raise RuntimeError("Unsupported DATABASE_URL format")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def days_left(end_at: datetime | None) -> int:
    if not end_at:
        return 0
    delta = end_at - utcnow()
    return max(0, delta.days + (1 if delta.seconds > 0 else 0))


# ================== DB ==================
engine = create_async_engine(make_async_db_url(DATABASE_URL), pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


MIGRATION_SQL = [
    # base tables (create if empty)
    """
    CREATE TABLE IF NOT EXISTS users (
        tg_id BIGINT PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        status VARCHAR(16) NOT NULL DEFAULT 'active'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subscriptions (
        tg_id BIGINT PRIMARY KEY,
        start_at TIMESTAMPTZ,
        end_at TIMESTAMPTZ,
        is_active BOOLEAN NOT NULL DEFAULT FALSE,
        status VARCHAR(16) NOT NULL DEFAULT 'active'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS payments (
        id SERIAL PRIMARY KEY,
        tg_id BIGINT NOT NULL,
        amount INTEGER NOT NULL,
        currency VARCHAR(8) NOT NULL DEFAULT 'RUB',
        provider VARCHAR(32) NOT NULL DEFAULT 'mock',
        status VARCHAR(16) NOT NULL DEFAULT 'success',
        paid_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        period_days INTEGER NOT NULL DEFAULT 30,
        period_months INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vpn_peers (
        id SERIAL PRIMARY KEY,
        tg_id BIGINT NOT NULL,
        client_public_key VARCHAR(128) NOT NULL,
        client_private_key_enc TEXT NOT NULL,
        client_ip VARCHAR(64) NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        revoked_at TIMESTAMPTZ NULL,
        rotation_reason VARCHAR(32) NULL
    )
    """,

    # patch existing schema (safe)
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS status VARCHAR(16)",
    "UPDATE users SET created_at = now() WHERE created_at IS NULL",
    "UPDATE users SET status = 'active' WHERE status IS NULL",
    "ALTER TABLE users ALTER COLUMN created_at SET DEFAULT now()",
    "ALTER TABLE users ALTER COLUMN status SET DEFAULT 'active'",

    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS start_at TIMESTAMPTZ",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS end_at TIMESTAMPTZ",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS is_active BOOLEAN",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS status VARCHAR(16)",
    "UPDATE subscriptions SET is_active = FALSE WHERE is_active IS NULL",
    "UPDATE subscriptions SET status = 'active' WHERE status IS NULL",
    "ALTER TABLE subscriptions ALTER COLUMN is_active SET DEFAULT FALSE",
    "ALTER TABLE subscriptions ALTER COLUMN status SET DEFAULT 'active'",

    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS currency VARCHAR(8)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider VARCHAR(32)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS status VARCHAR(16)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS period_days INTEGER",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS period_months INTEGER",
    "UPDATE payments SET currency = 'RUB' WHERE currency IS NULL",
    "UPDATE payments SET provider = 'mock' WHERE provider IS NULL",
    "UPDATE payments SET status = 'success' WHERE status IS NULL",
    "UPDATE payments SET paid_at = now() WHERE paid_at IS NULL",
    "UPDATE payments SET period_days = 30 WHERE period_days IS NULL",
    "UPDATE payments SET period_months = 1 WHERE period_months IS NULL",
    "ALTER TABLE payments ALTER COLUMN currency SET DEFAULT 'RUB'",
    "ALTER TABLE payments ALTER COLUMN provider SET DEFAULT 'mock'",
    "ALTER TABLE payments ALTER COLUMN status SET DEFAULT 'success'",
    "ALTER TABLE payments ALTER COLUMN paid_at SET DEFAULT now()",
    "ALTER TABLE payments ALTER COLUMN period_days SET DEFAULT 30",
    "ALTER TABLE payments ALTER COLUMN period_months SET DEFAULT 1",
]


async def run_migrations():
    async with SessionLocal() as session:
        for stmt in MIGRATION_SQL:
            try:
                await session.execute(text(stmt))
            except Exception as e:
                # не валим бот миграциями — в логах будет видно, но бот будет жить
                print("[MIGRATION WARN]", str(e)[:250], "||", stmt[:120])
        await session.commit()


# ================== UI ==================
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Личный кабинет"), KeyboardButton(text="🌍 VPN")],
            [KeyboardButton(text="💳 Оплата"), KeyboardButton(text="❓ FAQ")],
            [KeyboardButton(text="🛠 Поддержка")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def cabinet_inline_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Продлить на 1 мес", callback_data="pay:mock:1m")
    return b.as_markup()


def vpn_inline_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📖 Инструкция", callback_data="vpn:guide")
    b.button(text="📥 Скачать мой конфиг", callback_data="vpn:conf")
    b.button(text="🔁 Показать QR", callback_data="vpn:qr")
    b.button(text="♻️ Сбросить VPN", callback_data="vpn:reset:confirm")
    b.adjust(1)
    return b.as_markup()


def vpn_reset_confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, сбросить", callback_data="vpn:reset:do")
    b.button(text="❌ Отмена", callback_data="vpn:reset:cancel")
    b.adjust(2)
    return b.as_markup()


def payment_inline_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Тест-оплата 299 ₽ (успех)", callback_data="pay:mock:1m")
    return b.as_markup()


# ================== VPN MOCK ==================
def fake_key_b64() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def alloc_ip(tg_id: int) -> str:
    # 10.66.0.0/16 deterministic
    a = (tg_id % 250) + 2
    b = ((tg_id // 250) % 250) + 2
    return f"10.66.{b}.{a}/32"


def build_wg_config(private_key: str, client_ip: str) -> str:
    return (
        "[Interface]\n"
        f"PrivateKey = {private_key}\n"
        f"Address = {client_ip}\n"
        f"DNS = {VPN_DNS}\n\n"
        "[Peer]\n"
        f"PublicKey = {VPN_SERVER_PUBLIC_KEY}\n"
        f"AllowedIPs = {VPN_ALLOWED_IPS}\n"
        f"Endpoint = {VPN_ENDPOINT}\n"
        "PersistentKeepalive = 25\n"
    )


# ================== DB LOGIC ==================
async def ensure_user(session: AsyncSession, tg_id: int):
    await session.execute(
        text("""
        INSERT INTO users (tg_id, created_at, status)
        VALUES (:id, now(), 'active')
        ON CONFLICT (tg_id) DO NOTHING
        """),
        {"id": tg_id},
    )
    await session.execute(
        text("""
        INSERT INTO subscriptions (tg_id, is_active, status)
        VALUES (:id, FALSE, 'active')
        ON CONFLICT (tg_id) DO NOTHING
        """),
        {"id": tg_id},
    )
    await session.commit()


async def get_sub(session: AsyncSession, tg_id: int):
    r = await session.execute(
        text("SELECT start_at, end_at, is_active, status FROM subscriptions WHERE tg_id=:id"),
        {"id": tg_id},
    )
    return r.first()


async def last_payment(session: AsyncSession, tg_id: int):
    r = await session.execute(
        text("""
        SELECT id, amount, currency, status, paid_at
        FROM payments
        WHERE tg_id=:id
        ORDER BY id DESC
        LIMIT 1
        """),
        {"id": tg_id},
    )
    return r.first()


async def get_active_peer(session: AsyncSession, tg_id: int):
    r = await session.execute(
        text("""
        SELECT id, client_private_key_enc, client_ip
        FROM vpn_peers
        WHERE tg_id=:id AND is_active=TRUE
        ORDER BY id DESC
        LIMIT 1
        """),
        {"id": tg_id},
    )
    return r.first()


async def create_peer(session: AsyncSession, tg_id: int, reason: str | None):
    private_key = fake_key_b64()
    client_ip = alloc_ip(tg_id)
    public_key = fake_key_b64()  # mock

    await session.execute(
        text("""
        INSERT INTO vpn_peers (tg_id, client_public_key, client_private_key_enc, client_ip, is_active, rotation_reason)
        VALUES (:id, :pub, :priv, :ip, TRUE, :reason)
        """),
        {"id": tg_id, "pub": public_key, "priv": private_key, "ip": client_ip, "reason": reason},
    )
    await session.commit()
    return private_key, client_ip


async def revoke_peer(session: AsyncSession, peer_id: int, reason: str):
    await session.execute(
        text("""
        UPDATE vpn_peers
        SET is_active=FALSE, revoked_at=now(), rotation_reason=:reason
        WHERE id=:pid
        """),
        {"pid": peer_id, "reason": reason},
    )
    await session.commit()


async def ensure_peer_for_active_sub(session: AsyncSession, tg_id: int):
    sub = await get_sub(session, tg_id)
    if not sub:
        return None
    _, end_at, _, status = sub
    end_at_utc = ensure_aware_utc(end_at)
    if not end_at_utc or status != "active" or end_at_utc <= utcnow():
        return None

    peer = await get_active_peer(session, tg_id)
    if peer:
        return peer
    await create_peer(session, tg_id, reason=None)
    return await get_active_peer(session, tg_id)


async def apply_payment_add_month(session: AsyncSession, tg_id: int):
    sub = await get_sub(session, tg_id)
    now = utcnow()

    end_at = None
    if sub:
        end_at = ensure_aware_utc(sub[1])

    base = end_at if (end_at and end_at > now) else now
    new_end = base + relativedelta(months=+PERIOD_MONTHS)

    await session.execute(
        text("""
        INSERT INTO subscriptions (tg_id, start_at, end_at, is_active, status)
        VALUES (:id, now(), :end_at, TRUE, 'active')
        ON CONFLICT (tg_id)
        DO UPDATE SET end_at=:end_at, is_active=TRUE, status='active'
        """),
        {"id": tg_id, "end_at": new_end},
    )

    # IMPORTANT: fill BOTH period_days and period_months (your DB requires period_days NOT NULL)
    await session.execute(
        text("""
        INSERT INTO payments (tg_id, amount, currency, provider, status, paid_at, period_days, period_months)
        VALUES (:id, :amount, 'RUB', 'mock', 'success', now(), :days, :months)
        """),
        {"id": tg_id, "amount": PRICE_RUB, "days": PERIOD_DAYS, "months": PERIOD_MONTHS},
    )

    await session.commit()
    return new_end


# ================== HANDLERS ==================
def is_menu(message: Message, text_: str) -> bool:
    return (message.text or "").strip() == text_


async def show_cabinet(msg: Message):
    async with SessionLocal() as session:
        tg_id = msg.from_user.id
        await ensure_user(session, tg_id)

        sub = await get_sub(session, tg_id)
        pay = await last_payment(session, tg_id)
        peer = await get_active_peer(session, tg_id)

    start_at, end_at, is_active, status = sub if sub else (None, None, False, "expired")
    end_at_utc = ensure_aware_utc(end_at)
    active = bool(end_at_utc and end_at_utc > utcnow() and status == "active" and is_active)

    vpn_status = "Активен ✅" if (peer is not None) else "Отключён ❌"
    pay_line = "—"
    if pay:
        pid, amount, currency, pstatus, paid_at = pay
        pay_line = f"{fmt_dt(ensure_aware_utc(paid_at))} / {amount} {currency} / {pstatus} (#{pid})"

    text_msg = (
        "👤 *Личный кабинет*\n\n"
        f"🧾 *СБС*: {'Активен ✅' if active else 'Истёк ❌'}\n"
        f"📅 Окончание: *{fmt_dt(end_at_utc)}*\n"
        f"⏳ Осталось дней: *{days_left(end_at_utc)}*\n\n"
        f"🌍 *VPN*: {vpn_status}\n\n"
        f"💳 *Последний платёж*: {pay_line}\n"
    )
    await msg.answer(text_msg, parse_mode="Markdown", reply_markup=cabinet_inline_kb())


async def show_vpn(msg: Message):
    async with SessionLocal() as session:
        tg_id = msg.from_user.id
        await ensure_user(session, tg_id)

        peer = await ensure_peer_for_active_sub(session, tg_id)

    if not peer:
        await msg.answer(
            "🌍 *VPN*\n\nНужна активная подписка.\nОткрой *💳 Оплата* и нажми тест-оплату.",
            parse_mode="Markdown",
            reply_markup=vpn_inline_kb(),
        )
        return

    await msg.answer(
        "🌍 *VPN*\n\nКонфиг готов. Он **не меняется при продлении**.\nМожно скачать конфиг или показать QR.",
        parse_mode="Markdown",
        reply_markup=vpn_inline_kb(),
    )


async def show_pay(msg: Message):
    await msg.answer(
        "💳 *Оплата*\n\nPoC: кнопка ниже имитирует успешную оплату 299 ₽ и продлевает на 1 календарный месяц.",
        parse_mode="Markdown",
        reply_markup=payment_inline_kb(),
    )


async def show_faq(msg: Message):
    await msg.answer(
        "❓ *FAQ*\n\n"
        "• СБС — единая подписка.\n"
        "• VPN-конфиг не меняется при продлении.\n"
        "• По окончании СБС доступ отключается автоматически.\n",
        parse_mode="Markdown",
    )


async def show_support(msg: Message):
    await msg.answer("🛠 Поддержка: напиши сюда и приложи скрин/описание проблемы.")


# ================== CALLBACKS ==================
async def cb_pay_success(cb: CallbackQuery):
    tg_id = cb.from_user.id
    async with SessionLocal() as session:
        await ensure_user(session, tg_id)
        new_end = await apply_payment_add_month(session, tg_id)
        await ensure_peer_for_active_sub(session, tg_id)

    await cb.message.edit_text(
        "✅ *Оплата успешна!*\n\n"
        f"🟦 СБС активен до: *{fmt_dt(new_end)}*\n"
        "🌍 VPN работает — можете пользоваться.",
        parse_mode="Markdown",
    )
    await cb.answer()


async def cb_vpn_conf(cb: CallbackQuery):
    tg_id = cb.from_user.id
    async with SessionLocal() as session:
        peer = await ensure_peer_for_active_sub(session, tg_id)

    if not peer:
        await cb.answer("Нужна активная подписка.", show_alert=True)
        return

    _, priv, ip = peer
    conf = build_wg_config(priv, ip)

    path = f"/tmp/sbs-{tg_id}.conf"
    with open(path, "w", encoding="utf-8") as f:
        f.write(conf)

    await cb.message.answer_document(FSInputFile(path), caption="📥 Ваш WireGuard конфиг (.conf)")
    await cb.answer()


async def cb_vpn_qr(cb: CallbackQuery):
    tg_id = cb.from_user.id
    async with SessionLocal() as session:
        peer = await ensure_peer_for_active_sub(session, tg_id)

    if not peer:
        await cb.answer("Нужна активная подписка.", show_alert=True)
        return

    _, priv, ip = peer
    conf = build_wg_config(priv, ip)

    img = qrcode.make(conf)
    path = f"/tmp/sbs-{tg_id}-qr.png"
    img.save(path)

    await cb.message.answer_photo(FSInputFile(path), caption="🔁 QR для импорта в WireGuard")
    await cb.answer()


async def cb_vpn_guide(cb: CallbackQuery):
    await cb.message.answer(
        "📖 *Инструкция*\n\n"
        "1) Установи приложение WireGuard.\n"
        "2) Импортируй конфиг (.conf) или отсканируй QR.\n"
        "3) Включи туннель.\n\n"
        "Если проблемы — попробуй ♻️ Сбросить VPN.",
        parse_mode="Markdown",
    )
    await cb.answer()


async def cb_vpn_reset_confirm(cb: CallbackQuery):
    await cb.message.answer(
        "♻️ *Сбросить VPN?*\n\nСтарый доступ будет отключён, вы получите новый конфиг.",
        parse_mode="Markdown",
        reply_markup=vpn_reset_confirm_kb(),
    )
    await cb.answer()


async def cb_vpn_reset_do(cb: CallbackQuery):
    tg_id = cb.from_user.id
    async with SessionLocal() as session:
        sub = await get_sub(session, tg_id)
        end_at_utc = ensure_aware_utc(sub[1]) if sub else None
        status = sub[3] if sub else "expired"
        if not end_at_utc or status != "active" or end_at_utc <= utcnow():
            await cb.answer("Нужна активная подписка.", show_alert=True)
            return

        peer = await get_active_peer(session, tg_id)
        if peer:
            peer_id = peer[0]
            await revoke_peer(session, peer_id, "manual_reset")

        priv, ip = await create_peer(session, tg_id, "manual_reset")

    conf = build_wg_config(priv, ip)
    conf_path = f"/tmp/sbs-{tg_id}.conf"
    with open(conf_path, "w", encoding="utf-8") as f:
        f.write(conf)

    img = qrcode.make(conf)
    qr_path = f"/tmp/sbs-{tg_id}-qr.png"
    img.save(qr_path)

    await cb.message.answer("✅ VPN сброшен. Отправляю новый конфиг и QR…")
    await cb.message.answer_document(FSInputFile(conf_path), caption="📥 Новый конфиг (.conf)")
    await cb.message.answer_photo(FSInputFile(qr_path), caption="🔁 Новый QR")
    await cb.answer()


async def cb_vpn_reset_cancel(cb: CallbackQuery):
    await cb.answer("Ок, отменено.")


# ================== SCHEDULER 30s ==================
async def scheduler_loop(bot: Bot):
    if not SCHEDULER_ENABLED:
        return
    while True:
        try:
            async with SessionLocal() as session:
                now = utcnow()
                # expire subs
                r = await session.execute(
                    text("""
                    SELECT tg_id FROM subscriptions
                    WHERE status='active' AND end_at IS NOT NULL AND end_at <= :now
                    """),
                    {"now": now},
                )
                tg_ids = [row[0] for row in r.fetchall()]
                if tg_ids:
                    await session.execute(
                        text("""
                        UPDATE subscriptions
                        SET status='expired', is_active=FALSE
                        WHERE tg_id = ANY(:ids)
                        """),
                        {"ids": tg_ids},
                    )
                    await session.execute(
                        text("""
                        UPDATE vpn_peers
                        SET is_active=FALSE, revoked_at=now(), rotation_reason='expired'
                        WHERE tg_id = ANY(:ids) AND is_active=TRUE
                        """),
                        {"ids": tg_ids},
                    )
                    await session.commit()

                    for tg_id in tg_ids:
                        try:
                            await bot.send_message(
                                tg_id,
                                "❌ СБС закончился. VPN отключён.\n\nНажмите «💳 Оплата», чтобы продлить.",
                                reply_markup=main_menu_kb(),
                            )
                        except Exception:
                            pass
        except Exception as e:
            print("[SCHEDULER WARN]", str(e)[:200])

        await asyncio.sleep(30)


# ================== BOOT ==================
async def main():
    await run_migrations()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(msg: Message):
        async with SessionLocal() as session:
            await ensure_user(session, msg.from_user.id)
        await msg.answer("✅ PoC запущен!\n\nВыбирай раздел:", reply_markup=main_menu_kb())

    @dp.message(F.text)
    async def router(msg: Message):
        if is_menu(msg, "👤 Личный кабинет"):
            await show_cabinet(msg)
            return
        if is_menu(msg, "🌍 VPN"):
            await show_vpn(msg)
            return
        if is_menu(msg, "💳 Оплата"):
            await show_pay(msg)
            return
        if is_menu(msg, "❓ FAQ"):
            await show_faq(msg)
            return
        if is_menu(msg, "🛠 Поддержка"):
            await show_support(msg)
            return
        await msg.answer("Выберите пункт меню 👇", reply_markup=main_menu_kb())

    @dp.callback_query(F.data == "pay:mock:1m")
    async def _pay(cb: CallbackQuery):
        await cb_pay_success(cb)

    @dp.callback_query(F.data == "vpn:conf")
    async def _conf(cb: CallbackQuery):
        await cb_vpn_conf(cb)

    @dp.callback_query(F.data == "vpn:qr")
    async def _qr(cb: CallbackQuery):
        await cb_vpn_qr(cb)

    @dp.callback_query(F.data == "vpn:guide")
    async def _guide(cb: CallbackQuery):
        await cb_vpn_guide(cb)

    @dp.callback_query(F.data == "vpn:reset:confirm")
    async def _reset_confirm(cb: CallbackQuery):
        await cb_vpn_reset_confirm(cb)

    @dp.callback_query(F.data == "vpn:reset:do")
    async def _reset_do(cb: CallbackQuery):
        await cb_vpn_reset_do(cb)

    @dp.callback_query(F.data == "vpn:reset:cancel")
    async def _reset_cancel(cb: CallbackQuery):
        await cb_vpn_reset_cancel(cb)

    asyncio.create_task(scheduler_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
