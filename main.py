import os
import asyncio
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sqlalchemy import BigInteger, DateTime, String, Boolean, Integer, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from dateutil.relativedelta import relativedelta


PRICE_RUB = 299
PERIOD_MONTHS = 1

MSK = timezone(timedelta(hours=3))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_async_db_url(database_url: str) -> str:
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    if database_url.startswith("postgres://"):
        return "postgresql+asyncpg://" + database_url[len("postgres://"):]
    if database_url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + database_url[len("postgresql://"):]
    raise ValueError("Unsupported DATABASE_URL format")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="active")


class Subscription(Base):
    __tablename__ = "subscriptions"
    tg_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), primary_key=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger)
    amount: Mapped[int] = mapped_column(Integer)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


async def init_db(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def ensure_user(session: AsyncSession, tg_id: int):
    if await session.get(User, tg_id) is None:
        session.add(User(tg_id=tg_id))
        await session.commit()


async def get_or_create_sub(session: AsyncSession, tg_id: int) -> Subscription:
    sub = await session.get(Subscription, tg_id)
    if not sub:
        sub = Subscription(tg_id=tg_id)
        session.add(sub)
        await session.commit()
    return sub


def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Личный кабинет", callback_data="cabinet")
    kb.button(text="💳 Оплата", callback_data="pay")
    kb.adjust(1)
    return kb.as_markup()


def pay_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Оплатить 299 ₽ / 1 месяц", callback_data="pay_mock_success")
    kb.button(text="⬅️ Назад", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


async def apply_payment(session: AsyncSession, tg_id: int) -> datetime:
    await ensure_user(session, tg_id)
    sub = await get_or_create_sub(session, tg_id)

    now = utcnow()
    base = sub.end_at if sub.end_at and sub.end_at > now else now
    new_end = base + relativedelta(months=PERIOD_MONTHS)

    if not sub.start_at:
        sub.start_at = now

    sub.end_at = new_end
    sub.is_active = True

    session.add(Payment(tg_id=tg_id, amount=PRICE_RUB))
    await session.commit()

    return new_end


async def main():
    bot = Bot(token=os.environ["BOT_TOKEN"])
    engine = create_async_engine(make_async_db_url(os.environ["DATABASE_URL"]))
    Session = async_sessionmaker(engine, expire_on_commit=False)

    await init_db(engine)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(msg: Message):
        await msg.answer("Выбери действие:", reply_markup=main_menu_kb())

    @dp.callback_query(F.data == "home")
    async def home(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text("Выбери действие:", reply_markup=main_menu_kb())

    @dp.callback_query(F.data == "pay")
    async def pay(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text(
            "💳 Оплата (PoC)\n\n"
            "Нажми кнопку ниже — подписка продлится на 1 календарный месяц.",
            reply_markup=pay_kb()
        )

    @dp.callback_query(F.data == "pay_mock_success")
    async def pay_success(cb: CallbackQuery):
        await cb.answer("Оплата принята ✅")

        async with Session() as session:
            new_end = await apply_payment(session, cb.from_user.id)

        await cb.message.edit_text(
            f"✅ Подписка продлена!\n\n"
            f"🟦 СБС активен до: {fmt_dt(new_end)}",
            reply_markup=main_menu_kb()
        )

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

    import time
    while True:
        time.sleep(3600)
