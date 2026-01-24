import os
import asyncio
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text

from dateutil.relativedelta import relativedelta


# ================== CONFIG ==================
PRICE_RUB = 299
PERIOD_MONTHS = 1
MSK = timezone(timedelta(hours=3))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware_utc(dt: datetime | None) -> datetime | None:
    """Bring datetime to tz-aware UTC (works for values returned by Postgres too)."""
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
    # округляем вверх до дней, если осталось хоть что-то
    return max(0, delta.days + (1 if delta.seconds > 0 else 0))


def make_async_db_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    raise RuntimeError("Unsupported DATABASE_URL")


# ================== DB: SAFE AUTO-MIGRATION ==================
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
        is_active BOOLEAN NOT NULL DEFAULT FALSE
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
        period_months INTEGER NOT NULL DEFAULT 1
    )
    """,
    # users
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS status VARCHAR(16)",
    "UPDATE users SET created_at = now() WHERE created_at IS NULL",
    "UPDATE users SET status = 'active' WHERE status IS NULL",
    "ALTER TABLE users ALTER COLUMN created_at SET DEFAULT now()",
    "ALTER TABLE users ALTER COLUMN status SET DEFAULT 'active'",
    "ALTER TABLE users ALTER COLUMN created_at SET NOT NULL",
    "ALTER TABLE users ALTER COLUMN status SET NOT NULL",
    # subscriptions
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS start_at TIMESTAMPTZ",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS end_at TIMESTAMPTZ",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS is_active BOOLEAN",
    "UPDATE subscriptions SET is_active = FALSE WHERE is_active IS NULL",
    "ALTER TABLE subscriptions ALTER COLUMN is_active SET DEFAULT FALSE",
    "ALTER TABLE subscriptions ALTER COLUMN is_active SET NOT NULL",
    # payments
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS currency VARCHAR(8)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider VARCHAR(32)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS status VARCHAR(16)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS period_months INTEGER",
    "UPDATE payments SET currency = 'RUB' WHERE currency IS NULL",
    "UPDATE payments SET provider = 'mock' WHERE provider IS NULL",
    "UPDATE payments SET status = 'success' WHERE status IS NULL",
    "UPDATE payments SET paid_at = now() WHERE paid_at IS NULL",
    "UPDATE payments SET period_months = 1 WHERE period_months IS NULL",
    "ALTER TABLE payments ALTER COLUMN currency SET DEFAULT 'RUB'",
    "ALTER TABLE payments ALTER COLUMN provider SET DEFAULT 'mock'",
    "ALTER TABLE payments ALTER COLUMN status SET DEFAULT 'success'",
    "ALTER TABLE payments ALTER COLUMN paid_at SET DEFAULT now()",
    "ALTER TABLE payments ALTER COLUMN period_months SET DEFAULT 1",
    "ALTER TABLE payments ALTER COLUMN currency SET NOT NULL",
    "ALTER TABLE payments ALTER COLUMN provider SET NOT NULL",
    "ALTER TABLE payments ALTER COLUMN status SET NOT NULL",
    "ALTER TABLE payments ALTER COLUMN paid_at SET NOT NULL",
    "ALTER TABLE payments ALTER COLUMN period_months SET NOT NULL",
]


async def run_migrations(session: AsyncSession) -> None:
    for stmt in MIGRATION_SQL:
        try:
            await session.execute(text(stmt))
        except Exception as e:
            print(f"[MIGRATION WARN] {e} :: {stmt[:140]}")
    await session.commit()


# ================== KEYBOARDS ==================
def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Личный кабинет", callback_data="cabinet")
    kb.button(text="🌍 VPN", callback_data="vpn")
    kb.button(text="💳 Оплата", callback_data="pay")
    kb.button(text="❓ FAQ", callback_data="faq")
    kb.button(text="🛠 Поддержка", callback_data="support")
    kb.adjust(1)
    return kb.as_markup()


def cabinet_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Продлить", callback_data="pay")
    kb.button(text="⬅️ Назад", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def pay_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Тест-оплата {PRICE_RUB} ₽ / 1 месяц", callback_data="pay_mock_success")
    kb.button(text="⬅️ Назад", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def vpn_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📖 Инструкция", callback_data="vpn_help")
    kb.button(text="📥 Скачать мой конфиг", callback_data="vpn_config")
    kb.button(text="🔁 Показать QR", callback_data="vpn_qr")
    kb.button(text="♻️ Сбросить VPN", callback_data="vpn_reset")
    kb.button(text="⬅️ Назад", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


# ================== DB HELPERS ==================
async def ensure_user(session: AsyncSession, tg_id: int) -> None:
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
        INSERT INTO subscriptions (tg_id, is_active)
        VALUES (:id, FALSE)
        ON CONFLICT (tg_id) DO NOTHING
        """),
        {"id": tg_id},
    )
    await session.commit()


async def get_subscription(session: AsyncSession, tg_id: int):
    res = await session.execute(
        text("SELECT start_at, end_at, is_active FROM subscriptions WHERE tg_id=:id"),
        {"id": tg_id},
    )
    return res.first()


async def get_last_payment(session: AsyncSession, tg_id: int):
    res = await session.execute(
        text("""
        SELECT id, amount, currency, status, paid_at
        FROM payments
        WHERE tg_id=:id
        ORDER BY id DESC
        LIMIT 1
        """),
        {"id": tg_id},
    )
    return res.first()


async def apply_success_payment(session: AsyncSession, tg_id: int):
    now = utcnow()

    row = await session.execute(
        text("SELECT end_at FROM subscriptions WHERE tg_id=:id"),
        {"id": tg_id},
    )
    r = row.first()
    current_end = ensure_aware_utc(r[0]) if r and r[0] else None

    base = current_end if (current_end and current_end > now) else now
    new_end = base + relativedelta(months=+PERIOD_MONTHS)

    await session.execute(
        text("""
        INSERT INTO subscriptions (tg_id, start_at, end_at, is_active)
        VALUES (:id, now(), :end_at, TRUE)
        ON CONFLICT (tg_id)
        DO UPDATE SET end_at = :end_at, is_active = TRUE
        """),
        {"id": tg_id, "end_at": new_end},
    )

    await session.execute(
        text("""
        INSERT INTO payments (tg_id, amount, currency, provider, status, paid_at, period_months)
        VALUES (:id, :amount, 'RUB', 'mock', 'success', now(), :months)
        """),
        {"id": tg_id, "amount": PRICE_RUB, "months": PERIOD_MONTHS},
    )

    await session.commit()

    p = await session.execute(
        text("SELECT id FROM payments WHERE tg_id=:id ORDER BY id DESC LIMIT 1"),
        {"id": tg_id},
    )
    payment_id = p.scalar_one()
    return payment_id, new_end


# ================== BOT ==================
async def main() -> None:
    bot_token = os.environ.get("BOT_TOKEN", "")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is missing")

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL is missing")

    engine = create_async_engine(make_async_db_url(database_url), pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    # migrations before polling
    async with Session() as session:
        await run_migrations(session)

    bot = Bot(token=bot_token)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message):
        async with Session() as session:
            await ensure_user(session, message.from_user.id)
        await message.answer("✅ PoC запущен!\n\nВыбирай раздел:", reply_markup=main_menu_kb())

    @dp.callback_query(F.data == "home")
    async def home(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text("Выбирай раздел:", reply_markup=main_menu_kb())

    @dp.callback_query(F.data == "cabinet")
    async def cabinet(cb: CallbackQuery):
        await cb.answer()
        async with Session() as session:
            await ensure_user(session, cb.from_user.id)
            sub = await get_subscription(session, cb.from_user.id)
            last_pay = await get_last_payment(session, cb.from_user.id)

        start_at, end_at, is_active = sub if sub else (None, None, False)
        end_at_utc = ensure_aware_utc(end_at)
        active = bool(end_at_utc and end_at_utc > utcnow() and is_active)

        last_pay_str = "—"
        if last_pay:
            pid, amount, currency, status, paid_at = last_pay
            paid_at_utc = ensure_aware_utc(paid_at)
            last_pay_str = f"{fmt_dt(paid_at_utc)} / {amount} {currency} / {status} (#{pid})"

        text_msg = (
            "👤 *Личный кабинет*\n\n"
            "7.1 *СБС*\n"
            f"• Статус: {'Активен ✅' if active else 'Истёк ❌'}\n"
            f"• Дата окончания: {fmt_dt(end_at_utc)}\n"
            f"• Осталось дней: *{days_left(end_at_utc)}*\n\n"
            "7.2 *VPN* (PoC)\n"
            "• Статус: —\n"
            "• 📥 Скачать конфиг / QR / Инструкция — подключим следующим шагом\n\n"
            "7.3 *Бонус (Яндекс)*\n"
            "• Статус: (подключим следующим шагом)\n\n"
            "7.4 *Платежи*\n"
            f"• Последний платёж: {last_pay_str}\n"
        )
        await cb.message.edit_text(text_msg, reply_markup=cabinet_kb(), parse_mode="Markdown")

    @dp.callback_query(F.data == "pay")
    async def pay(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text(
            "💳 *Оплата (PoC)*\n\n"
            "Сейчас вместо реального провайдера — тестовая кнопка.\n"
            "Нажатие продлевает СБС на **1 календарный месяц**.\n",
            reply_markup=pay_kb(),
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data == "pay_mock_success")
    async def pay_mock_success(cb: CallbackQuery):
        await cb.answer()
        async with Session() as session:
            await ensure_user(session, cb.from_user.id)
            payment_id, new_end = await apply_success_payment(session, cb.from_user.id)

        new_end_utc = ensure_aware_utc(new_end)
        await cb.message.edit_text(
            "✅ Оплата прошла успешно.\n\n"
            f"🧾 Платёж №{payment_id}\n"
            f"🟦 СБС активен до: {fmt_dt(new_end_utc)}\n"
            f"⏳ Осталось дней: {days_left(new_end_utc)}\n\n"
            "🌍 VPN: (PoC) подключим следующим шагом\n",
            reply_markup=main_menu_kb(),
        )

    @dp.callback_query(F.data == "vpn")
    async def vpn(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text(
            "🌍 *VPN-раздел (PoC)*\n\n"
            "Сейчас VPN в режиме заглушки. Реальный WireGuard подключим следующим шагом.",
            reply_markup=vpn_kb(),
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data.in_({"vpn_help", "vpn_config", "vpn_qr", "vpn_reset"}))
    async def vpn_stub(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text(
            "🌍 *VPN (PoC)*\n\n"
            "Пока заглушка. Дальше подключим WireGuard.",
            reply_markup=vpn_kb(),
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data == "faq")
    async def faq(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text(
            "❓ *FAQ*\n\n"
            "• СБС = VPN + бонус Яндекс Плюс.\n"
            "• Тариф: 299 ₽ / 1 календарный месяц.\n"
            "• Продление суммируется.\n",
            reply_markup=main_menu_kb(),
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data == "support")
    async def support(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text(
            "🛠 Поддержка\n\n"
            "Пиши сюда: @your_support_username\n",
            reply_markup=main_menu_kb(),
        )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
