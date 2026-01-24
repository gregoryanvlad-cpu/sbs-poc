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


def utcnow():
    return datetime.now(timezone.utc)


def make_async_db_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    raise ValueError("Unsupported DATABASE_URL format")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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


def fmt_dt(dt):
    if not dt:
        return "—"
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def days_left(end_at):
    if not end_at:
        return 0
    delta = end_at - utcnow()
    return max(0, delta.days + (1 if delta.seconds > 0 else 0))


def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Личный кабинет", callback_data="cabinet")
    kb.button(text="💳 Оплата", callback_data="pay")
    kb.button(text="🌍 VPN", callback_data="vpn")
    kb.adjust(1)
    return kb.as_markup()


def pay_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Оплатить 299 ₽ / 1 месяц", callback_data="pay_success")
    kb.button(text="⬅️ Назад", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


async def apply_payment(session: AsyncSession, tg_id: int):
    user = await session.get(User, tg_id)
    if not user:
        session.add(User(tg_id=tg_id))
        await session.commit()

    sub = await session.get(Subscription, tg_id)
    if not sub:
        sub = Subscription(tg_id=tg_id)
        session.add(sub)
        await session.commit()

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
        await msg.answer("Выбери раздел:", reply_markup=main_menu())

    @dp.callback_query(F.data == "home")
    async def home(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text("Выбери раздел:", reply_markup=main_menu())

    @dp.callback_query(F.data == "pay")
    async def pay(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text(
            "💳 Оплата\n\nПродление на **1 календарный месяц**.",
            reply_markup=pay_kb(),
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data == "pay_success")
    async def pay_success(cb: CallbackQuery):
        await cb.answer()

        async with Session() as session:
            new_end = await apply_payment(session, cb.from_user.id)

        # ❗ ВАЖНО: НОВОЕ сообщение, а не edit_text
        await cb.message.answer(
            "✅ Оплата прошла успешно!\n\n"
            f"🟦 СБС активен до: {fmt_dt(new_end)}\n"
            f"⏳ Осталось дней: {days_left(new_end)}",
            reply_markup=main_menu(),
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
