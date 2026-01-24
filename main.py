import os
import asyncio
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sqlalchemy import BigInteger, DateTime, String, Boolean, Integer, ForeignKey, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from dateutil.relativedelta import relativedelta


# ================== CONFIG ==================
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


def fmt_dt(dt):
    if not dt:
        return "—"
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def days_left(end_at):
    if not end_at:
        return 0
    delta = end_at - utcnow()
    return max(0, delta.days + (1 if delta.seconds > 0 else 0))


# ================== DB ==================
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


# ================== CORE LOGIC ==================
async def get_subscription(session: AsyncSession, tg_id: int) -> Subscription:
    result = await session.execute(
        select(Subscription).where(Subscription.tg_id == tg_id)
    )
    sub = result.scalar_one_or_none()

    if not sub:
        sub = Subscription(tg_id=tg_id)
        session.add(sub)
        await session.commit()
        await session.refresh(sub)

    return sub


async def apply_payment(session: AsyncSession, tg_id: int) -> Subscription:
    sub = await get_subscription(session, tg_id)

    now = utcnow()
    base = sub.end_at if sub.end_at and sub.end_at > now else now
    new_end = base + relativedelta(months=+PERIOD_MONTHS)

    if not sub.start_at:
        sub.start_at = now

    sub.end_at = new_end
    sub.is_active = True

    session.add(Payment(tg_id=tg_id, amount=PRICE_RUB))
    await session.flush()
    await session.commit()
    await session.refresh(sub)  # 🔥 КЛЮЧЕВОЙ МОМЕНТ

    return sub


# ================== KEYBOARDS ==================
def main_menu():
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
    kb.button(text=f"✅ Оплатить {PRICE_RUB} ₽ / 1 месяц", callback_data="pay_success")
    kb.button(text="⬅️ Назад", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


# ================== BOT ==================
async def main():
    bot = Bot(token=os.environ["BOT_TOKEN"])
    engine = create_async_engine(make_async_db_url(os.environ["DATABASE_URL"]))
    Session = async_sessionmaker(engine, expire_on_commit=False)

    await init_db(engine)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(msg: Message):
        await msg.answer("Выбирай раздел:", reply_markup=main_menu())

    @dp.callback_query(F.data == "home")
    async def home(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text("Выбирай раздел:", reply_markup=main_menu())

    @dp.callback_query(F.data == "cabinet")
    async def cabinet(cb: CallbackQuery):
        await cb.answer()
        async with Session() as session:
            sub = await get_subscription(session, cb.from_user.id)

        await cb.message.edit_text(
            "👤 *Личный кабинет*\n\n"
            f"📅 Окончание: {fmt_dt(sub.end_at)}\n"
            f"⏳ Осталось дней: *{days_left(sub.end_at)}*",
            reply_markup=cabinet_kb(),
            parse_mode="Markdown",
        )

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
            sub = await apply_payment(session, cb.from_user.id)

        await cb.message.edit_text(
            "✅ Оплата прошла успешно!\n\n"
            f"📅 Новый срок: {fmt_dt(sub.end_at)}\n"
            f"⏳ Осталось дней: {days_left(sub.end_at)}",
            reply_markup=main_menu(),
        )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
