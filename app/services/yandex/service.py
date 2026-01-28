
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.subscription import Subscription
from app.db.models.yandex_membership import YandexMembership
from app.services.yandex.provider import MockYandexProvider
from app.services.yandex.repo import pick_account
from app.repo import utcnow


@dataclass
class EnsureResult:
    status: str
    message: str
    invite_link: str | None = None


class YandexService:
    def __init__(self) -> None:
        self.provider = MockYandexProvider()

    async def ensure_membership_after_payment(
        self,
        session: AsyncSession,
        tg_id: int,
        yandex_login: str,
    ) -> EnsureResult:
        # 1) subscription must exist
        sub = await session.get(Subscription, tg_id)
        if not sub or not sub.end_at:
            return EnsureResult(status="error", message="Подписка не активна.")

        # 2) last membership if exists
        q = (
            select(YandexMembership)
            .where(YandexMembership.tg_id == tg_id)
            .order_by(YandexMembership.id.desc())
            .limit(1)
        )
        res = await session.execute(q)
        m = res.scalar_one_or_none()

        # Do NOT issue new invite on renewal for already active/scheduled_switch
        if m and m.status in ("active", "scheduled_switch"):
            if not m.coverage_end_at or m.coverage_end_at < sub.end_at:
                m.coverage_end_at = sub.end_at
            await session.flush()
            return EnsureResult(
                status=m.status,
                message="Продление учтено. Новая ссылка будет выдана при необходимости.",
            )

        # Keep existing pending invite
        if m and m.status == "pending" and m.invite_link:
            return EnsureResult(status="pending", message="У вас уже есть активное приглашение.", invite_link=m.invite_link)

        # 3) allocate account that covers full period
        acc = await pick_account(session, need_cover_until=sub.end_at)
        if not acc:
            nm = YandexMembership(
                tg_id=tg_id,
                yandex_account_id=None,
                yandex_login=yandex_login,
                status="waiting_for_account",
                coverage_end_at=sub.end_at,
            )
            session.add(nm)
            await session.flush()
            return EnsureResult(
                status="waiting_for_account",
                message="Сейчас нет подходящих аккаунтов Яндекс. Приглашение будет выдано автоматически.",
            )

        # 4) create invite (mock for now)
        link = await self.provider.create_invite_link(credentials_ref=acc.credentials_ref)
        now = utcnow()

        nm = YandexMembership(
            tg_id=tg_id,
            yandex_account_id=acc.id,
            yandex_login=yandex_login,
            status="pending",
            invite_link=link,
            invite_issued_at=now,
            invite_expires_at=now + timedelta(seconds=settings.yandex_pending_ttl_seconds),
            reinvite_used=0,
            coverage_end_at=sub.end_at,
        )
        session.add(nm)

        # occupy slot
        acc.used_slots = (acc.used_slots or 0) + 1

        await session.flush()
        return EnsureResult(status="pending", message="Приглашение готово.", invite_link=link)


yandex_service = YandexService()
