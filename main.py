import os
import asyncio
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|expired|blocked


async def init_db(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def upsert_user(session: AsyncSession, tg_id: int) -> None:
    user = await session.get(User, tg_id)
    if user is None:
        session.add(User(tg_id=tg_id))
        await session.commit()


def make_async_db_url(database_url: str) -> str:
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    if database_url.startswith("postgres://"):
        return "postgresql+asyncpg://" + database_url[len("postgres://"):]
    if database_url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + database_url[len("postgresql://"):]
    raise ValueError("Unsupported DATABASE_URL format")


async def main() -> None:
    bot_token = os.environ["BOT_TOKEN"]
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL is missing (Railway should provide it after Postgres is attached).")

    engine = create_async_engine(make_async_db_url(database_url), pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    await init_db(engine)

    bot = Bot(token=bot_token)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message):
        async with Session() as session:
            await upsert_user(session, message.from_user.id)

        await message.answer(
            "✅ PoC запущен!\n\n"
            "Это тестовая версия СБС.\n"
            "Дальше подключим: подписки / VPN / Yandex Monitor.\n"
        )

    @dp.message(F.text == "ping")
    async def ping(message: Message):
        await message.answer("pong")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
