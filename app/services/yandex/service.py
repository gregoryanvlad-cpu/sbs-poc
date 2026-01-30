from __future__ import annotations

from datetime import timedelta
from typing import List

from sqlalchemy import select

from app.core.config import settings
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_membership import YandexMembership
from app.repo import utcnow
from app.services.yandex.provider import build_provider

# ✅ TTL приглашения (ANTI-BLOCK): 15 минут
INVITE_TTL_MINUTES = 15


class YandexService:
    """
    Вся бизнес-логика Яндекса.
    Worker вызывает методы отсюда.
    """

    def __init__(self) -> None:
        self.provider = build_provider()

    async def expire_pending_invites(self, session) -> List[int]:
        """
        Отменяет просроченные инвайты (awaiting_join) и освобождает слот.
        Возвращает список tg_id, которым нужно отправить уведомление.
        """
        now = utcnow()
        affected: List[int] = []

        memberships = (
            await session.scalars(
                select(YandexMembership).where(
                    YandexMembership.status == "awaiting_join",
                    YandexMembership.invite_expires_at.is_not(None),
                    YandexMembership.invite_expires_at <= now,
                )
            )
        ).all()

        if not memberships:
            return affected

        for m in memberships:
            account = await session.get(YandexAccount, m.yandex_account_id) if m.yandex_account_id else None
            if account:
                try:
                    await self.provider.cancel_pending_invite(
                        storage_state_path=self._account_state_path(account),
                        label=account.label,
                    )
                except Exception:
                    # Даже если playwright не смог — всё равно помечаем как истёкшее
                    pass

            m.status = "invite_timeout"
            m.invite_link = None
            m.invite_issued_at = None
            m.invite_expires_at = None
            m.updated_at = now

            affected.append(m.tg_id)

        return affected

    async def ensure_membership_for_user(
        self,
        *,
        session,
        tg_id: int,
        yandex_login: str,
    ) -> YandexMembership:
        """
        Гарантирует, что у пользователя есть join-session.
        Если нет — создаёт invite и сохраняет membership.
        """
        existing = await session.scalar(
            select(YandexMembership).where(
                YandexMembership.tg_id == tg_id,
                YandexMembership.status.in_(["awaiting_join", "active"]),
            )
        )
        if existing:
            return existing

        account = await session.scalar(
            select(YandexAccount)
            .where(YandexAccount.status == "active")
            .order_by(YandexAccount.id.asc())
            .limit(1)
        )
        if not account:
            raise RuntimeError("No active YandexAccount")

        invite_link = await self.provider.create_invite_link(
            storage_state_path=self._account_state_path(account)
        )

        now = utcnow()

        membership = YandexMembership(
            tg_id=tg_id,
            yandex_account_id=account.id,
            yandex_login=yandex_login,
            invite_link=invite_link,
            invite_issued_at=now,
            invite_expires_at=now + timedelta(minutes=INVITE_TTL_MINUTES),
            status="awaiting_join",
            reinvite_used=0,
        )

        session.add(membership)
        await session.flush()
        return membership

    def _account_state_path(self, account: YandexAccount) -> str:
        return f"{settings.yandex_cookies_dir}/{account.credentials_ref}"


yandex_service = YandexService()
