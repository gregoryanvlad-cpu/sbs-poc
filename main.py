import os
import asyncio
from datetime import datetime
from dateutil.relativedelta import relativedelta

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

def make_async_db_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    raise RuntimeError("Unsupported DATABASE_URL")

engine = create_async_engine(make_async_db_url(DATABASE_URL))
Session = async_sessionmaker(engine, expire_on_commit=False)

# ---------- AUTO MIGRATION ----------
MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS users (
    tg_id BIGINT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status VARCHAR(16) NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS subscriptions (
    tg_id BIGINT PRIMARY KEY,
    start_at TIMESTAMPTZ,
    end_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    tg_id BIGINT NOT NULL,
    amount INTEGER NOT NULL,
    currency VARCHAR(8) NOT NULL DEFAULT 'RUB',
    provider VARCHAR(32) NOT NULL DEFAULT 'mock',
    status VARCHAR(16) NOT NULL DEFAULT 'success',
    paid_at TIMESTAMPTZ NOT NULL,
    period_months INTEGER NOT NULL DEFAULT 1
);
"""

async def run_migration():
    async with engine.begin() as conn:
        for stmt in MIGRATION_SQL.split(";"):
            s = stmt.strip()
            if s:
                await conn.execute(text(s))
    print("✅ DB migration done")

# ---------- BOT ----------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="cabinet")],
        [InlineKeyboardButton(text="💳 Оплатить 1 месяц", callback_data="pay")]
    ])

@dp.message(CommandStart())
async def start(m: Message):
    async with Session() as s:
        await s.execute(
            text("INSERT INTO users (tg_id) VALUES (:id) ON CONFLICT DO NOTHING"),
            {"id": m.from_user.id}
        )
        await s.commit()
    await m.answer("Добро пожаловать в СБС", reply_markup=main_kb())

@dp.callback_query(F.data == "cabinet")
async def cabinet(cb):
    async with Session() as s:
        r = await s.execute(
            text("SELECT end_at FROM subscriptions WHERE tg_id=:id"),
            {"id": cb.from_user.id}
        )
        row = r.first()
    if row and row[0]:
        await cb.message.answer(f"✅ СБС активен до: {row[0].strftime('%d.%m.%Y %H:%M UTC')}")
    else:
        await cb.message.answer("❌ Подписка не активна")

@dp.callback_query(F.data == "pay")
async def pay(cb):
    now = datetime.utcnow()
    async with Session() as s:
        r = await s.execute(
            text("SELECT end_at FROM subscriptions WHERE tg_id=:id"),
            {"id": cb.from_user.id}
        )
        row = r.first()
        base = row[0] if row and row[0] and row[0] > now else now
        new_end = base + relativedelta(months=1)

        await s.execute(text("""
            INSERT INTO subscriptions (tg_id, start_at, end_at, is_active)
            VALUES (:id, :start, :end, TRUE)
            ON CONFLICT (tg_id)
            DO UPDATE SET end_at=:end, is_active=TRUE
        """), {"id": cb.from_user.id, "start": now, "end": new_end})

        await s.execute(text("""
            INSERT INTO payments (tg_id, amount, paid_at, period_months)
            VALUES (:id, 299, :paid_at, 1)
        """), {"id": cb.from_user.id, "paid_at": now})

        await s.commit()

    await cb.message.answer(
        f"✅ Оплата успешна\nПодписка до {new_end.strftime('%d.%m.%Y %H:%M UTC')}",
        reply_markup=main_kb()
    )

async def main():
    await run_migration()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
