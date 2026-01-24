import os
import asyncio
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sqlalchemy import (
    BigInteger,
    DateTime,
    String,
    Boolean,
    Integer,
    ForeignKey,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from dateutil.relativedelta import relativedelta


# ================== CONFIG ==================
PRICE_RUB = 299
PERIOD_MONTHS = 1
MSK = timezone(timedelta(hours=3))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def days_left(end_at: datetime | None) -> int:
    if not end_at:
        return 0
    delta = end_at - utcnow()
    # округляем вверх, чтобы "сегодня" считался как день
    return max(0, delta.days + (1 if delta.seconds > 0 else 0))


def make_async_db_url(url: str) -> str:
    """
    Railway часто даёт DATABASE_URL как postgres://...
    asyncpg хочет postgresql+asyncpg://...
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    raise ValueError("Unsupported DATABASE_URL format")


# ================== DB MODELS ==================
class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)  # active|expired|blocked


class Subscription(Base):
    __tablename__ = "subscriptions"

    tg_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), primary_key=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"))
    amount: Mapped[int] = mapped_column(Integer, nullable=False)

    # ВАЖНО: эти поля должны быть NOT NULL (у тебя в БД так и есть)
    currency: Mapped[str] = mapped_column(String(8), default="RUB", nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="mock", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="success", nullable=False)

    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    period_months: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


async def init_db(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ================== CORE (DB HELPERS) ==================
async def ensure_user_and_sub(session: AsyncSession, tg_id: int) -> Subscription:
    user = await session.get(User, tg_id)
    if user is None:
        session.add(User(tg_id=tg_id))
        await session.flush()

    sub = await session.get(Subscription, tg_id)
    if sub is None:
        sub = Subscription(tg_id=tg_id)
        session.add(sub)
        await session.flush()

    await session.commit()
    await session.refresh(sub)
    return sub


async def apply_success_payment(session: AsyncSession, tg_id: int) -> tuple[int, datetime, int]:
    """
    Возвращает: (payment_id, new_end, left_days)
    Продление на 1 календарный месяц:
      - если подписка активна -> от end_at
      - если истекла/пустая -> от now
    """
    sub = await ensure_user_and_sub(session, tg_id)

    now = utcnow()
    base = sub.end_at if sub.end_at and sub.end_at > now else now
    new_end = base + relativedelta(months=+PERIOD_MONTHS)

    if sub.start_at is None:
        sub.start_at = now

    sub.end_at = new_end
    sub.is_active = True

    payment = Payment(
        tg_id=tg_id,
        amount=PRICE_RUB,
        currency="RUB",
        provider="mock",
        status="success",
        period_months=PERIOD_MONTHS,
    )
    session.add(payment)

    await session.flush()
    await session.commit()
    await session.refresh(payment)
    await session.refresh(sub)

    return payment.id, new_end, days_left(new_end)


async def get_last_payment(session: AsyncSession, tg_id: int) -> Payment | None:
    res = await session.execute(
        select(Payment).where(Payment.tg_id == tg_id).order_by(Payment.id.desc()).limit(1)
    )
    return res.scalar_one_or_none()


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


# ================== RENDERERS ==================
async def render_cabinet(session: AsyncSession, tg_id: int) -> str:
    sub = await ensure_user_and_sub(session, tg_id)
    active = bool(sub.end_at and sub.end_at > utcnow() and sub.is_active)

    last_pay = await get_last_payment(session, tg_id)
    last_pay_str = "—"
    if last_pay:
        last_pay_str = f"{fmt_dt(last_pay.paid_at)} / {last_pay.amount} {last_pay.currency} / {last_pay.status}"

    return (
        "👤 *Личный кабинет*\n\n"
        "7.1 *СБС*\n"
        f"• Статус: {'Активен ✅' if active else 'Истёк ❌'}\n"
        f"• Дата окончания: {fmt_dt(sub.end_at)}\n"
        f"• Осталось дней: *{days_left(sub.end_at)}*\n\n"
        "7.2 *VPN* (PoC / mock)\n"
        "• Статус: —\n\n"
        "7.3 *Бонус (Яндекс)*\n"
        "• Статус: (подключим следующим шагом)\n\n"
        "7.4 *Платежи*\n"
        f"• Последний платёж: {last_pay_str}\n"
    )


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

    await init_db(engine)

    bot = Bot(token=bot_token)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message):
        async with Session() as session:
            await ensure_user_and_sub(session, message.from_user.id)

        await message.answer("✅ PoC запущен!\n\nВыбирай раздел:", reply_markup=main_menu_kb())

    @dp.callback_query(F.data == "home")
    async def home(cb: CallbackQuery):
        await cb.answer()  # без всплывашки
        await cb.message.edit_text("Выбирай раздел:", reply_markup=main_menu_kb())

    @dp.callback_query(F.data == "cabinet")
    async def cabinet(cb: CallbackQuery):
        await cb.answer()
        async with Session() as session:
            text = await render_cabinet(session, cb.from_user.id)
        await cb.message.edit_text(text, reply_markup=cabinet_kb(), parse_mode="Markdown")

    @dp.callback_query(F.data == "pay")
    async def pay(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text(
            "💳 *Оплата (PoC)*\n\n"
            "Сейчас вместо реального провайдера — тестовая кнопка.\n"
            "Нажатие продлевает СБС на **1 календарный месяц**.\n\n"
            "После нажатия ничего дополнительно нажимать не нужно.",
            reply_markup=pay_kb(),
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data == "pay_mock_success")
    async def pay_mock_success(cb: CallbackQuery):
        await cb.answer()

        async with Session() as session:
            payment_id, new_end, left = await apply_success_payment(session, cb.from_user.id)

        # Текст гарантированно меняется (payment_id и дата), Telegram не проигнорирует edit_text
        await cb.message.edit_text(
            "✅ Оплата прошла успешно.\n\n"
            f"🧾 Платёж №{payment_id}\n"
            f"🟦 СБС активен до: {fmt_dt(new_end)}\n"
            f"⏳ Осталось дней: {left}\n\n"
            "🌍 VPN: (PoC / mock)\n"
            "🎁 Яндекс: подключим следующим шагом.",
            reply_markup=main_menu_kb(),
        )

    @dp.callback_query(F.data == "vpn")
    async def vpn(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text(
            "🌍 *VPN-раздел (PoC / mock)*\n\n"
            "Сейчас VPN в режиме заглушки. Реальный WireGuard подключим после завершения PoC логики.",
            reply_markup=vpn_kb(),
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data.in_({"vpn_help", "vpn_config", "vpn_qr", "vpn_reset"}))
    async def vpn_stub(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text(
            "🌍 *VPN (PoC / mock)*\n\n"
            "Функции VPN будут подключены позже.\n"
            "Сейчас проверяем подписку/продления и базовую архитектуру.",
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
            "• Продление суммируется и не режет дни.\n",
            reply_markup=main_menu_kb(),
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data == "support")
    async def support(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text(
            "🛠 Поддержка\n\n"
            "Пиши сюда: @your_support_username\n"
            "(позже заменим на реальный контакт/чат)",
            reply_markup=main_menu_kb(),
        )

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

    # Railway иногда гасит процесс при idle — держим контейнер живым
    import time
    while True:
        time.sleep(3600)
