from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_membership import YandexMembership
from app.services.yandex.provider import MockYandexProvider
from app.services.yandex.repo import pick_account


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class YandexResult:
    invite_link: str | None = None
    message: str = "ok"


class YandexService:
    def __init__(self) -> None:
        # позже: provider = PlaywrightYandexProvider()
        self.provider = MockYandexProvider()

    async def ensure_membership_after_payment(self, session: AsyncSession, tg_id: int, yandex_login: str) -> YandexResult:
        """
        Создаёт (или находит) membership на пользователя.
        Если логин уже был зафиксирован ранее — менять нельзя.
        Выдаёт инвайт (pending) и выставляет TTL.
        """
        ym = await self._get_latest_membership(session, tg_id)

        # Если уже есть membership и логин отличный — запрещаем менять
        if ym and ym.yandex_login and ym.yandex_login != yandex_login:
            return YandexResult(
                invite_link=None,
                message="❌ Логин уже подтверждён и не может быть изменён. Обратитесь в поддержку.",
            )

        # Если membership существует и pending и инвайт ещё жив — просто возвращаем ссылку
        if ym and ym.status == "pending" and ym.invite_link and ym.invite_expires_at and ym.invite_expires_at > utcnow():
            return YandexResult(invite_link=ym.invite_link, message="invite_already_issued")

        # Если membership активен — не выдаём новые ссылки
        if ym and ym.status == "active":
            return YandexResult(invite_link=None, message="✅ Yandex Plus уже активен.")

        # Если membership не существует — создаём новый
        if not ym:
            ym = YandexMembership(
                tg_id=tg_id,
                yandex_login=yandex_login,
                status="pending",
            )
            session.add(ym)
            await session.flush()
        else:
            # фиксируем логин, если вдруг был пустой
            ym.yandex_login = yandex_login
            ym.status = "pending"

        # coverage_end_at пока можно не трогать (позже привяжем к subscriptions.end_at)
        need_cover_until = utcnow() + timedelta(days=1)  # минимальная страховка

        acc = await pick_account(session, need_cover_until)
        if not acc:
            ym.status = "need_support"
            await session.flush()
            return YandexResult(invite_link=None, message="⚠️ Нет доступных аккаунтов Yandex. Обратитесь в поддержку.")

        # если аккаунт меняем — учёт слотов
        if ym.yandex_account_id is None:
            ym.yandex_account_id = acc.id
            acc.used_slots += 1
        else:
            # если уже был аккаунт, подтянем объект
            if ym.yandex_account_id != acc.id:
                prev = await session.get(YandexAccount, ym.yandex_account_id)
                if prev:
                    prev.used_slots = max(0, prev.used_slots - 1)
                ym.yandex_account_id = acc.id
                acc.used_slots += 1

        # выдаём ссылку
        invite = await self.provider.create_invite_link(credentials_ref=acc.credentials_ref)

        ym.invite_link = invite
        ym.invite_issued_at = utcnow()
        ym.invite_expires_at = utcnow() + timedelta(seconds=settings.yandex_pending_ttl_seconds)
        # reinvite_used не трогаем тут, он для повторного
        await session.flush()

        return YandexResult(invite_link=invite, message="invite_issued")

    async def reinvite(self, session: AsyncSession, tg_id: int) -> YandexResult:
        """
        Выдаёт повторный инвайт максимум 1 раз, только если был invite_timeout.
        """
        ym = await self._get_latest_membership(session, tg_id)
        if not ym:
            return YandexResult(invite_link=None, message="❌ Сначала укажите логин в разделе Yandex Plus.")

        if ym.status != "invite_timeout":
            return YandexResult(invite_link=None, message="⚠️ Повторный инвайт доступен только после таймаута приглашения.")

        if int(ym.reinvite_used or 0) >= settings.yandex_reinvite_max:
            return YandexResult(invite_link=None, message="❌ Лимит повторных приглашений исчерпан. Обратитесь в поддержку.")

        acc = await session.get(YandexAccount, ym.yandex_account_id) if ym.yandex_account_id else None
        if not acc:
            # если аккаунта нет — подберём новый
            need_cover_until = utcnow() + timedelta(days=1)
            acc = await pick_account(session, need_cover_until)
            if not acc:
                ym.status = "need_support"
                await session.flush()
                return YandexResult(invite_link=None, message="⚠️ Нет доступных аккаунтов Yandex. Обратитесь в поддержку.")
            ym.yandex_account_id = acc.id
            acc.used_slots += 1

        invite = await self.provider.create_invite_link(credentials_ref=acc.credentials_ref)
        ym.invite_link = invite
        ym.invite_issued_at = utcnow()
        ym.invite_expires_at = utcnow() + timedelta(seconds=settings.yandex_pending_ttl_seconds)
        ym.status = "pending"
        ym.reinvite_used = int(ym.reinvite_used or 0) + 1
        await session.flush()

        return YandexResult(invite_link=invite, message="reinvite_issued")

    async def expire_pending_invites(self, session: AsyncSession) -> list[int]:
        """
        Возвращает список tg_id, у которых истёк pending invite.
        """
        now = utcnow()
        q = select(YandexMembership).where(
            YandexMembership.status == "pending",
            YandexMembership.invite_expires_at.is_not(None),
            YandexMembership.invite_expires_at <= now,
        )
        res = await session.execute(q)
        rows = list(res.scalars().all())
        if not rows:
            return []

        affected: list[int] = []
        for ym in rows:
            ym.status = "invite_timeout"
            ym.invite_link = None
            ym.invite_issued_at = None
            ym.invite_expires_at = None

            # освобождаем слот (pending занимал слот)
            if ym.yandex_account_id:
                acc = await session.get(YandexAccount, ym.yandex_account_id)
                if acc:
                    acc.used_slots = max(0, acc.used_slots - 1)

            affected.append(ym.tg_id)

        await session.flush()
        return affected

    async def _get_latest_membership(self, session: AsyncSession, tg_id: int) -> YandexMembership | None:
        q = select(YandexMembership).where(YandexMembership.tg_id == tg_id).order_by(YandexMembership.id.desc()).limit(1)
        res = await session.execute(q)
        return res.scalar_one_or_none()


yandex_service = YandexService()
