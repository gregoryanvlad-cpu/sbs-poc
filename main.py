\
import asyncio
import base64
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dateutil.relativedelta import relativedelta
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Integer, String, Text,
    ForeignKey, func, select, update, insert
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import insert as pg_insert


# -----------------------------
# Config
# -----------------------------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing (Railway should provide it after Postgres is attached).")

TZ = os.getenv("TZ", "UTC")

DEBUG = env_bool("DEBUG", False)

VPN_MODE = os.getenv("VPN_MODE", "mock").strip().lower()  # mock|real (real later)
SCHEDULER_ENABLED = env_bool("SCHEDULER_ENABLED", True)

VPN_ENDPOINT = os.getenv("VPN_ENDPOINT", "1.2.3.4:51820")
VPN_SERVER_PUBLIC_KEY = os.getenv("VPN_SERVER_PUBLIC_KEY", "REPLACE_ME")
VPN_ALLOWED_IPS = os.getenv("VPN_ALLOWED_IPS", "0.0.0.0/0, ::/0")
VPN_DNS = os.getenv("VPN_DNS", "1.1.1.1,8.8.8.8")


# -----------------------------
# DB models
# -----------------------------
class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), server_default="active", nullable=False)


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), server_default="active", nullable=False)  # active|expired
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, server_default="RUB")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, server_default="mock")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="success")
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    period_months: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # legacy compatibility if table existed before:
    period_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class VpnPeer(Base):
    __tablename__ = "vpn_peers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    client_public_key: Mapped[str] = mapped_column(String(128), nullable=False)
    client_private_key_enc: Mapped[str] = mapped_column(Text, nullable=False)  # base64 for PoC
    client_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rotation_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


# -----------------------------
# DB engine
# -----------------------------
engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fmt_dt(dt: datetime) -> str:
    # show UTC in PoC to avoid confusion; later we can render Moscow time.
    return dt.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def days_left(end_at: datetime) -> int:
    delta = end_at - utcnow()
    return max(0, (delta.days + (1 if delta.seconds > 0 else 0)))


# -----------------------------
# Keyboards
# -----------------------------
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
    b.adjust(1)
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
    b.adjust(1)
    return b.as_markup()


# -----------------------------
# Business logic
# -----------------------------
async def ensure_user(session: AsyncSession, tg_id: int) -> None:
    # Insert user if not exists (created_at is server_default)
    # (unused) sqlite variant removed
    return


async def ensure_user_pg(session: AsyncSession, tg_id: int) -> None:
    # PostgreSQL safe upsert
    await session.execute(
        pg_insert(User).values(tg_id=tg_id).on_conflict_do_nothing(index_elements=[User.tg_id])
    )


async def get_subscription(session: AsyncSession, tg_id: int) -> Optional[Subscription]:
    res = await session.execute(
        select(Subscription).where(Subscription.tg_id == tg_id).order_by(Subscription.id.desc()).limit(1)
    )
    return res.scalar_one_or_none()


async def upsert_subscription_add_month(session: AsyncSession, tg_id: int, months: int = 1) -> Tuple[Subscription, datetime]:
    sub = await get_subscription(session, tg_id)
    now = utcnow()
    if sub is None:
        new_end = now + relativedelta(months=+months)
        sub = Subscription(tg_id=tg_id, status="active", end_at=new_end)
        session.add(sub)
        return sub, new_end

    # Important: end_at is timezone-aware; compare with utcnow (aware)
    current_end = sub.end_at
    base = current_end if (current_end and current_end > now) else now
    new_end = base + relativedelta(months=+months)
    sub.status = "active"
    sub.end_at = new_end
    return sub, new_end


async def last_payment(session: AsyncSession, tg_id: int) -> Optional[Payment]:
    res = await session.execute(
        select(Payment).where(Payment.tg_id == tg_id).order_by(Payment.id.desc()).limit(1)
    )
    return res.scalar_one_or_none()


def _fake_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _derive_public_from_private(priv_b64: str) -> str:
    # PoC: not real WG math. We just generate another random value to look like a key.
    # Real implementation will use `wg genkey | wg pubkey` on server.
    return _fake_key()


async def get_active_peer(session: AsyncSession, tg_id: int) -> Optional[VpnPeer]:
    res = await session.execute(
        select(VpnPeer)
        .where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True)  # noqa
        .order_by(VpnPeer.id.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


def _allocate_client_ip(tg_id: int) -> str:
    # Simple deterministic pool: 10.66.0.0/16 -> 10.66.(tg_id % 250).(tg_id % 250 + 2)
    a = (tg_id % 250) + 2
    b = ((tg_id // 250) % 250) + 2
    return f"10.66.{b}.{a}/32"


def build_wg_config(peer: VpnPeer) -> str:
    priv = base64.b64decode(peer.client_private_key_enc.encode("ascii")).decode("ascii", errors="ignore")
    # We stored base64 of bytes, but for PoC we can just keep as string; ensure displayable:
    # if decode fails, fallback to stored b64.
    if not priv.strip():
        priv = peer.client_private_key_enc

    return (
        "[Interface]\n"
        f"PrivateKey = {priv}\n"
        f"Address = {peer.client_ip}\n"
        f"DNS = {VPN_DNS}\n\n"
        "[Peer]\n"
        f"PublicKey = {VPN_SERVER_PUBLIC_KEY}\n"
        f"AllowedIPs = {VPN_ALLOWED_IPS}\n"
        f"Endpoint = {VPN_ENDPOINT}\n"
        "PersistentKeepalive = 25\n"
    )


async def ensure_peer_for_active_sub(session: AsyncSession, tg_id: int) -> Optional[VpnPeer]:
    sub = await get_subscription(session, tg_id)
    if not sub or sub.status != "active" or sub.end_at <= utcnow():
        return None

    peer = await get_active_peer(session, tg_id)
    if peer:
        return peer

    # Create new peer (MOCK)
    priv = _fake_key()
    pub = _derive_public_from_private(priv)
    ip = _allocate_client_ip(tg_id)
    peer = VpnPeer(
        tg_id=tg_id,
        client_public_key=pub,
        client_private_key_enc=base64.b64encode(priv.encode("utf-8")).decode("ascii"),
        client_ip=ip,
        is_active=True,
        rotation_reason=None,
    )
    session.add(peer)
    return peer


async def revoke_peer(session: AsyncSession, peer: VpnPeer, reason: str) -> None:
    peer.is_active = False
    peer.revoked_at = utcnow()
    peer.rotation_reason = reason


# -----------------------------
# Handlers
# -----------------------------
async def show_cabinet(message: Message, session: AsyncSession) -> None:
    tg_id = message.from_user.id
    # ensure user
    await ensure_user_pg(session, tg_id)

    sub = await get_subscription(session, tg_id)
    if not sub:
        # create trial 1 month for PoC start
        sub, _ = await upsert_subscription_add_month(session, tg_id, months=1)
        session.add(Payment(tg_id=tg_id, amount=0, currency="RUB", provider="system", status="success", period_months=1))
        await session.commit()
    else:
        await session.commit()

    sub = await get_subscription(session, tg_id)
    peer = await get_active_peer(session, tg_id)
    pay = await last_payment(session, tg_id)

    s_status = "Активен ✅" if sub and sub.status == "active" and sub.end_at > utcnow() else "Истёк ❌"
    s_end = fmt_dt(sub.end_at) if sub else "—"
    s_left = f"{days_left(sub.end_at)}" if sub else "0"

    v_status = "Активен ✅" if peer and peer.is_active else "Отключён ❌"

    p_line = "—"
    if pay:
        p_line = f"{fmt_dt(pay.paid_at)} / {pay.amount} {pay.currency} / {pay.status}"

    text = (
        "👤 *Личный кабинет*\n\n"
        f"🧾 *СБС*: {s_status}\n"
        f"📅 Окончание: *{s_end}*\n"
        f"⏳ Осталось дней: *{s_left}*\n\n"
        f"🌍 *VPN*: {v_status}\n\n"
        f"💳 *Последний платёж*: {p_line}\n"
    )
    await message.answer(text, reply_markup=cabinet_inline_kb(), parse_mode="Markdown")


async def show_vpn(message: Message, session: AsyncSession) -> None:
    tg_id = message.from_user.id
    await ensure_user_pg(session, tg_id)
    peer = await ensure_peer_for_active_sub(session, tg_id)
    await session.commit()

    if not peer:
        await message.answer(
            "🌍 *VPN*\n\nЧтобы получить VPN — нужна активная подписка СБС.\nНажми *💳 Оплата* → тест-оплата.",
            parse_mode="Markdown",
            reply_markup=vpn_inline_kb(),
        )
        return

    await message.answer(
        "🌍 *VPN*\n\n"
        "Готово: у тебя есть конфиг (он не меняется при продлении).\n"
        "Можно скачать конфиг или показать QR.",
        parse_mode="Markdown",
        reply_markup=vpn_inline_kb(),
    )


async def show_payment(message: Message) -> None:
    await message.answer(
        "💳 *Оплата*\n\nЭто PoC: кнопка ниже имитирует успешную оплату 299 ₽ и продлевает СБС на 1 календарный месяц.",
        parse_mode="Markdown",
        reply_markup=payment_inline_kb(),
    )


async def show_faq(message: Message) -> None:
    await message.answer(
        "❓ *FAQ*\n\n"
        "• СБС — единая подписка.\n"
        "• VPN-конфиг не меняется при продлении.\n"
        "• По окончании СБС доступ отключается автоматически.\n\n"
        "Если что-то не работает — напиши в поддержку.",
        parse_mode="Markdown",
    )


async def show_support(message: Message) -> None:
    await message.answer("🛠 Поддержка: напиши сюда и приложи скрин/описание проблемы.")


# Callbacks
async def pay_mock_success(cb: CallbackQuery, session: AsyncSession) -> None:
    tg_id = cb.from_user.id
    await ensure_user_pg(session, tg_id)

    # record payment
    session.add(Payment(tg_id=tg_id, amount=299, currency="RUB", provider="mock", status="success", period_months=1))
    sub, new_end = await upsert_subscription_add_month(session, tg_id, months=1)

    # ensure vpn peer exists (do not rotate on extend)
    await ensure_peer_for_active_sub(session, tg_id)

    await session.commit()

    await cb.message.edit_text(
        "✅ *Оплата успешна!*\n\n"
        f"🧾 СБС активен до: *{fmt_dt(new_end)}*\n"
        "🌍 VPN работает — можете пользоваться.\n\n"
        "Открой *Личный кабинет* или *VPN* из меню.",
        parse_mode="Markdown",
        reply_markup=None,
    )
    await cb.answer()  # close loading


async def vpn_send_conf(cb: CallbackQuery, session: AsyncSession) -> None:
    tg_id = cb.from_user.id
    peer = await ensure_peer_for_active_sub(session, tg_id)
    await session.commit()
    if not peer:
        await cb.answer("Нужна активная подписка.", show_alert=True)
        return

    conf = build_wg_config(peer)
    # Save temp file
    path = f"/tmp/sbs-{tg_id}.conf"
    with open(path, "w", encoding="utf-8") as f:
        f.write(conf)

    await cb.message.answer_document(FSInputFile(path), caption="📥 Ваш WireGuard конфиг (.conf)")
    await cb.answer()


async def vpn_show_qr(cb: CallbackQuery, session: AsyncSession) -> None:
    tg_id = cb.from_user.id
    peer = await ensure_peer_for_active_sub(session, tg_id)
    await session.commit()
    if not peer:
        await cb.answer("Нужна активная подписка.", show_alert=True)
        return

    conf = build_wg_config(peer)

    import qrcode
    img = qrcode.make(conf)
    path = f"/tmp/sbs-{tg_id}-qr.png"
    img.save(path)

    await cb.message.answer_photo(FSInputFile(path), caption="🔁 QR для импорта в WireGuard")
    await cb.answer()


async def vpn_guide(cb: CallbackQuery) -> None:
    await cb.message.answer(
        "📖 *Инструкция*\n\n"
        "1) Установи приложение WireGuard.\n"
        "2) Импортируй конфиг (.conf) или отсканируй QR.\n"
        "3) Включи туннель.\n\n"
        "Если проблемы — попробуй ♻️ Сбросить VPN.",
        parse_mode="Markdown",
    )
    await cb.answer()


async def vpn_reset_confirm(cb: CallbackQuery) -> None:
    await cb.message.answer(
        "♻️ *Сбросить VPN?*\n\nСтарый доступ будет отключён, вы получите новый конфиг.",
        parse_mode="Markdown",
        reply_markup=vpn_reset_confirm_kb(),
    )
    await cb.answer()


async def vpn_reset_do(cb: CallbackQuery, session: AsyncSession) -> None:
    tg_id = cb.from_user.id
    sub = await get_subscription(session, tg_id)
    if not sub or sub.status != "active" or sub.end_at <= utcnow():
        await cb.answer("Нужна активная подписка.", show_alert=True)
        return

    peer = await get_active_peer(session, tg_id)
    if peer:
        await revoke_peer(session, peer, reason="manual_reset")

    # create new
    priv = _fake_key()
    pub = _derive_public_from_private(priv)
    ip = _allocate_client_ip(tg_id)
    new_peer = VpnPeer(
        tg_id=tg_id,
        client_public_key=pub,
        client_private_key_enc=base64.b64encode(priv.encode("utf-8")).decode("ascii"),
        client_ip=ip,
        is_active=True,
        rotation_reason="manual_reset",
    )
    session.add(new_peer)
    await session.commit()

    await cb.message.answer("✅ VPN сброшен. Отправляю новый конфиг и QR…")
    # send conf + qr
    conf = build_wg_config(new_peer)
    conf_path = f"/tmp/sbs-{tg_id}.conf"
    with open(conf_path, "w", encoding="utf-8") as f:
        f.write(conf)

    await cb.message.answer_document(FSInputFile(conf_path), caption="📥 Новый WireGuard конфиг (.conf)")

    import qrcode
    img = qrcode.make(conf)
    qr_path = f"/tmp/sbs-{tg_id}-qr.png"
    img.save(qr_path)
    await cb.message.answer_photo(FSInputFile(qr_path), caption="🔁 Новый QR для WireGuard")
    await cb.answer()


async def vpn_reset_cancel(cb: CallbackQuery) -> None:
    await cb.answer("Ок, отменено.")
    # no edit to keep history


# -----------------------------
# Scheduler
# -----------------------------
async def scheduler_loop(bot: Bot):
    if not SCHEDULER_ENABLED:
        return
    while True:
        try:
            async with SessionLocal() as session:
                now = utcnow()
                # Expire active subscriptions that ended
                res = await session.execute(
                    select(Subscription).where(Subscription.status == "active", Subscription.end_at <= now)
                )
                subs = res.scalars().all()

                for sub in subs:
                    sub.status = "expired"
                    # disable vpn peer(s)
                    res2 = await session.execute(
                        select(VpnPeer).where(VpnPeer.tg_id == sub.tg_id, VpnPeer.is_active == True)  # noqa
                    )
                    peers = res2.scalars().all()
                    for p in peers:
                        await revoke_peer(session, p, reason="expired")

                    # notify user (best-effort)
                    try:
                        await bot.send_message(
                            sub.tg_id,
                            "❌ СБС закончился. VPN отключён.\n\nНажмите «💳 Оплата», чтобы продлить.",
                            reply_markup=main_menu_kb(),
                        )
                    except Exception:
                        pass

                if subs:
                    await session.commit()
        except Exception:
            # never crash the bot because of scheduler
            pass

        await asyncio.sleep(30)


# -----------------------------
# App bootstrap
# -----------------------------
async def on_startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _is_text(message: Message, text: str) -> bool:
    return (message.text or "").strip() == text


async def main():
    await on_startup()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    # Messages
    @dp.message(CommandStart())
    async def cmd_start(message: Message):
        async with SessionLocal() as session:
            await ensure_user_pg(session, message.from_user.id)
            # For PoC: create subscription if missing (1 month)
            sub = await get_subscription(session, message.from_user.id)
            if not sub:
                await upsert_subscription_add_month(session, message.from_user.id, months=1)
                session.add(Payment(tg_id=message.from_user.id, amount=0, currency="RUB", provider="system", status="success", period_months=1))
                await ensure_peer_for_active_sub(session, message.from_user.id)
            await session.commit()

        await message.answer(
            "✅ PoC запущен!\n\n"
            "Это тестовая версия СБС.\n"
            "Дальше подключим: подписки / VPN / Yandex Monitor.",
            reply_markup=main_menu_kb(),
        )

    @dp.message(F.text)
    async def menu_router(message: Message):
        async with SessionLocal() as session:
            if _is_text(message, "👤 Личный кабинет"):
                await show_cabinet(message, session)
                return
            if _is_text(message, "🌍 VPN"):
                await show_vpn(message, session)
                return
            if _is_text(message, "💳 Оплата"):
                await show_payment(message)
                return
            if _is_text(message, "❓ FAQ"):
                await show_faq(message)
                return
            if _is_text(message, "🛠 Поддержка"):
                await show_support(message)
                return

        # fallback
        await message.answer("Выберите пункт меню 👇", reply_markup=main_menu_kb())

    # Callbacks
    @dp.callback_query(F.data == "pay:mock:1m")
    async def _pay(cb: CallbackQuery):
        async with SessionLocal() as session:
            await pay_mock_success(cb, session)

    @dp.callback_query(F.data == "vpn:conf")
    async def _vpn_conf(cb: CallbackQuery):
        async with SessionLocal() as session:
            await vpn_send_conf(cb, session)

    @dp.callback_query(F.data == "vpn:qr")
    async def _vpn_qr(cb: CallbackQuery):
        async with SessionLocal() as session:
            await vpn_show_qr(cb, session)

    @dp.callback_query(F.data == "vpn:guide")
    async def _vpn_guide(cb: CallbackQuery):
        await vpn_guide(cb)

    @dp.callback_query(F.data == "vpn:reset:confirm")
    async def _vpn_reset_confirm(cb: CallbackQuery):
        await vpn_reset_confirm(cb)

    @dp.callback_query(F.data == "vpn:reset:do")
    async def _vpn_reset_do(cb: CallbackQuery):
        async with SessionLocal() as session:
            await vpn_reset_do(cb, session)

    @dp.callback_query(F.data == "vpn:reset:cancel")
    async def _vpn_reset_cancel(cb: CallbackQuery):
        await vpn_reset_cancel(cb)

    # Scheduler task
    asyncio.create_task(scheduler_loop(bot))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
