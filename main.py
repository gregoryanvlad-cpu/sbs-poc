import asyncio
import base64
import os
import secrets
from datetime import datetime, timezone, timedelta

import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup,
    FSInputFile,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text
from dateutil.relativedelta import relativedelta


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

VPN_MODE = os.getenv("VPN_MODE", "mock").strip().lower()
VPN_ENDPOINT = os.getenv("VPN_ENDPOINT", "1.2.3.4:51820")
VPN_SERVER_PUBLIC_KEY = os.getenv("VPN_SERVER_PUBLIC_KEY", "REPLACE_ME")
VPN_ALLOWED_IPS = os.getenv("VPN_ALLOWED_IPS", "0.0.0.0/0, ::/0")
VPN_DNS = os.getenv("VPN_DNS", "1.1.1.1,8.8.8.8")


def make_async_db_url(url: str) -> str:
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
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS status VARCHAR(16)",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS start_at TIMESTAMPTZ",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS end_at TIMESTAMPTZ",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS is_active BOOLEAN",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS status VARCHAR(16)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS period_days INTEGER",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS period_months INTEGER",
]


async def run_migrations():
    async with SessionLocal() as session:
        for stmt in MIGRATION_SQL:
            try:
                await session.execute(text(stmt))
            except Exception as e:
                print("[MIGRATION WARN]", str(e)[:220], "||", stmt[:120])
        await session.commit()


# ================== INLINE UI ==================
def kb_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👤 Личный кабинет", callback_data="nav:cabinet")
    b.button(text="🌍 VPN", callback_data="nav:vpn")
    b.button(text="💳 Оплата", callback_data="nav:pay")
    b.button(text="❓ FAQ", callback_data="nav:faq")
    b.button(text="🛠 Поддержка", callback_data="nav:support")
    b.adjust(1)
    return b.as_markup()


def kb_back_home() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()


def kb_cabinet() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Продлить на 1 мес", callback_data="pay:mock:1m")
    b.button(text="⬅️ Назад", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()


def kb_pay() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Тест-оплата 299 ₽ (успех)", callback_data="pay:mock:1m")
    b.button(text="⬅️ Назад", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()


def kb_vpn() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📖 Инструкция", callback_data="vpn:guide")
    b.button(text="📦 Отправить конфиг + QR", callback_data="vpn:bundle")
    b.button(text="♻️ Сбросить VPN", callback_data="vpn:reset:confirm")
    b.button(text="⬅️ Назад", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()


def kb_vpn_reset_confirm() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, сбросить", callback_data="vpn:reset:do")
    b.button(text="❌ Отмена", callback_data="vpn:reset:cancel")
    b.adjust(2)
    return b.as_markup()


# ================== VPN MOCK HELPERS ==================
def fake_key_b64() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def alloc_ip(tg_id: int) -> str:
    # IP намеренно может оставаться одинаковым: в реальном WG обычно фиксируют IP за пользователем
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


async def send_conf_and_qr_linked(cb: CallbackQuery, peer_id: int, private_key: str, client_ip: str):
    """
    Telegram не позволяет смешать document+photo в одном media_group.
    Поэтому отправляем:
    1) QR фото
    2) .conf документ reply на QR (чтобы выглядело единым блоком)
    """
    tg_id = cb.from_user.id
    conf = build_wg_config(private_key, client_ip)

    conf_path = f"/tmp/sbs-{tg_id}.conf"
    with open(conf_path, "w", encoding="utf-8") as f:
        f.write(conf)

    img = qrcode.make(conf)
    qr_path = f"/tmp/sbs-{tg_id}-qr.png"
    img.save(qr_path)

    caption = (
        f"📦 VPN пакет\n"
        f"Peer #{peer_id}\n"
        f"IP: {client_ip}\n\n"
        f"Сканируй QR или импортируй .conf ниже."
    )

    qr_msg = await cb.message.answer_photo(FSInputFile(qr_path), caption=caption)
    await cb.message.answer_document(
        FSInputFile(conf_path),
        caption="📥 Конфиг WireGuard (.conf)",
        reply_to_message_id=qr_msg.message_id
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

    r = await session.execute(
        text("""
        INSERT INTO vpn_peers (tg_id, client_public_key, client_private_key_enc, client_ip, is_active, rotation_reason)
        VALUES (:id, :pub, :priv, :ip, TRUE, :reason)
        RETURNING id
        """),
        {"id": tg_id, "pub": public_key, "priv": private_key, "ip": client_ip, "reason": reason},
    )
    peer_id = r.scalar_one()
    await session.commit()
    return peer_id, private_key, client_ip


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

    end_at_utc = ensure_aware_utc(sub[1])
    status = sub[3]
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

    end_at = ensure_aware_utc(sub[1]) if sub else None
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

    await session.execute(
        text("""
        INSERT INTO payments (tg_id, amount, currency, provider, status, paid_at, period_days, period_months)
        VALUES (:id, :amount, 'RUB', 'mock', 'success', now(), :days, :months)
        """),
        {"id": tg_id, "amount": PRICE_RUB, "days": PERIOD_DAYS, "months": PERIOD_MONTHS},
    )

    await session.commit()
    return new_end


# ================== SCREENS (edit_text) ==================
HOME_TEXT = "✅ PoC запущен!\n\nВыберите раздел:"


async def render_home(cb: CallbackQuery):
    await cb.message.edit_text(HOME_TEXT, reply_markup=kb_main())
    await cb.answer()


async def render_cabinet(cb: CallbackQuery):
    tg_id = cb.from_user.id
    async with SessionLocal() as session:
        await ensure_user(session, tg_id)
        sub = await get_sub(session, tg_id)
        pay = await last_payment(session, tg_id)
        peer = await get_active_peer(session, tg_id)

    end_at_utc = ensure_aware_utc(sub[1]) if sub else None
    is_active = bool(sub and sub[2] and sub[3] == "active" and end_at_utc and end_at_utc > utcnow())

    vpn_status = "Активен ✅" if peer is not None else "Отключён ❌"

    pay_line = "—"
    if pay:
        pid, amount, currency, pstatus, paid_at = pay
        pay_line = f"{fmt_dt(ensure_aware_utc(paid_at))} / {amount} {currency} / {pstatus} (#{pid})"

    text_msg = (
        "👤 *Личный кабинет*\n\n"
        f"🧾 *СБС*: {'Активен ✅' if is_active else 'Истёк ❌'}\n"
        f"📅 Окончание: *{fmt_dt(end_at_utc)}*\n"
        f"⏳ Осталось дней: *{days_left(end_at_utc)}*\n\n"
        f"🌍 *VPN*: {vpn_status}\n\n"
        f"💳 *Последний платёж*: {pay_line}\n"
    )
    await cb.message.edit_text(text_msg, parse_mode="Markdown", reply_markup=kb_cabinet())
    await cb.answer()


async def render_vpn(cb: CallbackQuery):
    tg_id = cb.from_user.id
    async with SessionLocal() as session:
        await ensure_user(session, tg_id)
        peer = await ensure_peer_for_active_sub(session, tg_id)

    if not peer:
        text_msg = (
            "🌍 *VPN*\n\n"
            "Чтобы получить VPN — нужна активная подписка СБС.\n"
            "Открой *💳 Оплата* и нажми тест-оплату."
        )
    else:
        peer_id = peer[0]
        text_msg = (
            "🌍 *VPN*\n\n"
            f"Peer: *#{peer_id}*\n"
            "Кнопка ниже отправит QR + .conf связанными сообщениями.\n"
            "Сброс VPN создаст новый peer (номер изменится)."
        )

    await cb.message.edit_text(text_msg, parse_mode="Markdown", reply_markup=kb_vpn())
    await cb.answer()


async def render_pay(cb: CallbackQuery):
    text_msg = (
        "💳 *Оплата*\n\n"
        "PoC: кнопка ниже имитирует успешную оплату 299 ₽\n"
        "и продлевает подписку на **1 календарный месяц**."
    )
    await cb.message.edit_text(text_msg, parse_mode="Markdown", reply_markup=kb_pay())
    await cb.answer()


async def render_faq(cb: CallbackQuery):
    text_msg = (
        "❓ *FAQ*\n\n"
        "• СБС — единая подписка.\n"
        "• VPN-конфиг не меняется при продлении.\n"
        "• По окончании СБС доступ отключается автоматически.\n"
    )
    await cb.message.edit_text(text_msg, parse_mode="Markdown", reply_markup=kb_back_home())
    await cb.answer()


async def render_support(cb: CallbackQuery):
    await cb.message.edit_text(
        "🛠 Поддержка\n\nНапиши сюда и приложи скрин/описание проблемы.",
        reply_markup=kb_back_home(),
    )
    await cb.answer()


# ================== ACTIONS ==================
async def action_pay_success(cb: CallbackQuery):
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
        reply_markup=kb_main(),
    )
    await cb.answer()


async def action_vpn_bundle(cb: CallbackQuery):
    tg_id = cb.from_user.id
    async with SessionLocal() as session:
        peer = await ensure_peer_for_active_sub(session, tg_id)

    if not peer:
        await cb.answer("Нужна активная подписка.", show_alert=True)
        return

    peer_id, priv, ip = peer
    await send_conf_and_qr_linked(cb, peer_id, priv, ip)
    await cb.answer("Отправил")


async def action_vpn_guide(cb: CallbackQuery):
    await cb.message.edit_text(
        "📖 *Инструкция*\n\n"
        "1) Установи приложение WireGuard.\n"
        "2) Импортируй конфиг (.conf) или отсканируй QR.\n"
        "3) Включи туннель.\n\n"
        "Если проблемы — попробуй ♻️ Сбросить VPN.",
        parse_mode="Markdown",
        reply_markup=kb_vpn(),
    )
    await cb.answer()


async def action_vpn_reset_confirm(cb: CallbackQuery):
    await cb.message.edit_text(
        "♻️ *Сбросить VPN?*\n\n"
        "Старый доступ будет отключён, вы получите новый конфиг (новый peer).",
        parse_mode="Markdown",
        reply_markup=kb_vpn_reset_confirm(),
    )
    await cb.answer()


async def action_vpn_reset_do(cb: CallbackQuery):
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
            await revoke_peer(session, peer[0], "manual_reset")

        peer_id, priv, ip = await create_peer(session, tg_id, "manual_reset")

    await cb.message.edit_text(
        f"✅ *VPN сброшен.*\n\nСоздан новый peer: *#{peer_id}*\nСейчас отправлю QR и .conf связанными сообщениями.",
        parse_mode="Markdown",
        reply_markup=kb_vpn(),
    )

    await send_conf_and_qr_linked(cb, peer_id, priv, ip)
    await cb.answer()


async def action_vpn_reset_cancel(cb: CallbackQuery):
    await render_vpn(cb)


# ================== SCHEDULER 30s ==================
async def scheduler_loop(bot: Bot):
    if not SCHEDULER_ENABLED:
        return
    while True:
        try:
            async with SessionLocal() as session:
                now = utcnow()
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
                                "❌ СБС закончился. VPN отключён.\n\nНажмите «Оплата», чтобы продлить.",
                                reply_markup=kb_main(),
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
        # убрать старую нижнюю ReplyKeyboard, если она была ранее
        await msg.answer("⏳", reply_markup=ReplyKeyboardRemove())
        async with SessionLocal() as session:
            await ensure_user(session, msg.from_user.id)
        await msg.answer(HOME_TEXT, reply_markup=kb_main())

    @dp.message(Command("menu"))
    async def menu_cmd(msg: Message):
        await msg.answer("⏳", reply_markup=ReplyKeyboardRemove())
        async with SessionLocal() as session:
            await ensure_user(session, msg.from_user.id)
        await msg.answer(HOME_TEXT, reply_markup=kb_main())

    # NAV
    @dp.callback_query(F.data == "nav:home")
    async def _home(cb: CallbackQuery):
        await render_home(cb)

    @dp.callback_query(F.data == "nav:cabinet")
    async def _cab(cb: CallbackQuery):
        await render_cabinet(cb)

    @dp.callback_query(F.data == "nav:vpn")
    async def _vpn(cb: CallbackQuery):
        await render_vpn(cb)

    @dp.callback_query(F.data == "nav:pay")
    async def _pay(cb: CallbackQuery):
        await render_pay(cb)

    @dp.callback_query(F.data == "nav:faq")
    async def _faq(cb: CallbackQuery):
        await render_faq(cb)

    @dp.callback_query(F.data == "nav:support")
    async def _support(cb: CallbackQuery):
        await render_support(cb)

    # ACTIONS
    @dp.callback_query(F.data == "pay:mock:1m")
    async def _pay_success(cb: CallbackQuery):
        await action_pay_success(cb)

    @dp.callback_query(F.data == "vpn:bundle")
    async def _vpn_bundle(cb: CallbackQuery):
        await action_vpn_bundle(cb)

    @dp.callback_query(F.data == "vpn:guide")
    async def _vpn_guide(cb: CallbackQuery):
        await action_vpn_guide(cb)

    @dp.callback_query(F.data == "vpn:reset:confirm")
    async def _vpn_reset_confirm(cb: CallbackQuery):
        await action_vpn_reset_confirm(cb)

    @dp.callback_query(F.data == "vpn:reset:do")
    async def _vpn_reset_do(cb: CallbackQuery):
        await action_vpn_reset_do(cb)

    @dp.callback_query(F.data == "vpn:reset:cancel")
    async def _vpn_reset_cancel(cb: CallbackQuery):
        await action_vpn_reset_cancel(cb)

    asyncio.create_task(scheduler_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
