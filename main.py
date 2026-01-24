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
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|expired|blocked


class Subscription(Base):
    __tablename__ = "subscriptions"

    tg_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), primary_key=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"))
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    provider: Mapped[str] = mapped_column(String(32), default="mock")
    status: Mapped[str] = mapped_column(String(16), default="success")
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


async def init_db(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def ensure_user(session: AsyncSession, tg_id: int) -> None:
    if await session.get(User, tg_id) is None:
        session.add(User(tg_id=tg_id))
        await session.commit()


async def get_or_create_sub(session: AsyncSession, tg_id: int) -> Subscription:
    sub = await session.get(Subscription, tg_id)
    if sub is None:
        sub = Subscription(tg_id=tg_id)
        session.add(sub)
        await session.commit()
    return sub


def days_left(end_at: datetime | None) -> int:
    if not end_at:
        return 0
    delta = end_at - utcnow()
    return max(0, delta.days + (1 if delta.seconds > 0 else 0))


def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Личный кабинет", callback_data="cabinet")
    kb.button(text="💳 Оплата", callback_data="pay")
    kb.button(text="🌍 VPN", callback_data="vpn")
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
    kb.button(text=f"✅ Оплатить {PRICE_RUB} ₽ / 1 месяц", callback_data="pay_mock_success")
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


async def render_cabinet(session: AsyncSession, tg_id: int) -> str:
    sub = await get_or_create_sub(session, tg_id)

    active = bool(sub.end_at and sub.end_at > utcnow() and sub.is_active)

    return (
        "👤 *Личный кабинет*\n\n"
        f"🟦 *СБС*: {'Активен ✅' if active else 'Истёк ❌'}\n"
        f"📅 Окончание: {fmt_dt(sub.end_at)}\n"
        f"⏳ Осталось дней: *{days_left(sub.end_at)}*\n\n"
        "🌍 *VPN*: PoC / mock\n"
        "🎁 *Яндекс*: подключим следующим шагом\n"
    )


async def apply_success_payment(session: AsyncSession, tg_id: int) -> datetime:
    await ensure_user(session, tg_id)
    sub = await get_or_create_sub(session, tg_id)

    now = utcnow()
    base = sub.end_at if sub.end_at and sub.end_at > now else now
    new_end = base + relativedelta(months=PERIOD_MONTHS)

    if sub.start_at is None:
        sub.start_at = now

    sub.end_at = new_end
    sub.is_active = True

    session.add(Payment(tg_id=tg_id, amount=PRICE_RUB))
    await session.commit()

    return new_end


async def main() -> None:
    bot = Bot(token=os.environ["BOT_TOKEN"])
    engine = create_async_engine(make_async_db_url(os.environ["DATABASE_URL"]), pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    await init_db(engine)

    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message):
        async with Session() as session:
            await ensure_user(session, message.from_user.id)
            await get_or_create_sub(session, message.from_user.id)

        await message.answer("Выбери раздел:", reply_markup=main_menu_kb())

    @dp.callback_query(F.data == "home")
    async def home(cb: CallbackQuery):
        await cb.message.edit_text("Выбери раздел:", reply_markup=main_menu_kb())
        await cb.answer()

    @dp.callback_query(F.data == "cabinet")
    async def cabinet(cb: CallbackQuery):
        async with Session() as session:
            text = await render_cabinet(session, cb.from_user.id)
        await cb.message.edit_text(text, reply_markup=cabinet_kb(), parse_mode="Markdown")
        await cb.answer()

    @dp.callback_query(F.data == "pay")
    async def pay(cb: CallbackQuery):
        await cb.message.edit_text(
            "💳 *Оплата (PoC)*\n\n"
            "Тестовая кнопка.\n"
            "Продлевает СБС на **1 календарный месяц**.",
            reply_markup=pay_kb(),
            parse_mode="Markdown",
        )
        await cb.answer()

    @dp.callback_query(F.data == "pay_mock_success")
    async def pay_success(cb: CallbackQuery):
        async with Session() as session:
            new_end = await apply_success_payment(session, cb.from_user.id)

        await cb.message.edit_text(
            "✅ Оплата прошла успешно.\n\n"
            f"🟦 СБС активен до: {fmt_dt(new_end)}\n"
            f"⏳ Осталось дней: {days_left(new_end)}",
            reply_markup=main_menu_kb(),
        )
        await cb.answer()

    @dp.callback_query(F.data == "vpn")
    async def vpn(cb: CallbackQuery):
        await cb.message.edit_text(
            "🌍 *VPN (PoC / mock)*\n\n"
            "Реальный WireGuard подключим позже.",
            reply_markup=vpn_kb(),
            parse_mode="Markdown",
        )
        await cb.answer()

    @dp.callback_query(F.data.in_({"vpn_help", "vpn_config", "vpn_qr", "vpn_reset"}))
    async def vpn_stub(cb: CallbackQuery):
        await cb.answer("PoC: VPN будет позже.", show_alert=True)

    @dp.callback_query(F.data == "faq")
    async def faq(cb: CallbackQuery):
        await cb.message.edit_text(
            "❓ *FAQ*\n\n"
            "СБС = VPN + бонус Яндекс Плюс.\n"
            "Подписка — **1 календарный месяц**.",
            reply_markup=main_menu_kb(),
            parse_mode="Markdown",
        )
        await cb.answer()

    @dp.callback_query(F.data == "support")
    async def support(cb: CallbackQuery):
        await cb.message.edit_text(
            "🛠 Поддержка\n\n"
            "Напиши: @your_support_username",
            reply_markup=main_menu_kb(),
        )
        await cb.answer()

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

    import time
    while True:
        time.sleep(3600)
