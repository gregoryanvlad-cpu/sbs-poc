from __future__ import annotations

from datetime import timedelta
from typing import List

from sqlalchemy import select

from app.core.config import settings
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.repo import utcnow
from app.services.yandex.provider import build_provider


INVITE_TTL_MINUTES = 10


class YandexService:
    """
    Вся бизнес-логика Яндекса.
    Worker вызывает только методы отсюда.
    UI/handlers сюда НЕ лезут.
    """

    def __init__(self) -> None:
        self.provider = build_provider()

    # ============================================================
    # PUBLIC API (используется worker'ом)
    # ============================================================

    async def expire_pending_invites(self, session) -> List[int]:
        """
        Автоматически отменяет просроченные инвайты.
        Возвращает список tg_id, которым нужно отправить уведомление.
        """
        now = utcnow()
        affected_users: List[int] = []

        memberships = (
            await session.scalars(
                select(YandexMembership)
                .where(
                    YandexMembership.status == "awaiting_join",
                    YandexMembership.invite_expires_at <= now,
                )
            )
        ).all()

        if not memberships:
            return affected_users

        for m in memberships:
            account = await session.get(YandexAccount, m.yandex_account_id)
            if not account:
                continue

            try:
                await self.provider.cancel_pending_invite(
                    storage_state_path=self._account_state_path(account),
                    label=account.label,
                )
            except Exception:
                # Даже если playwright не смог — всё равно освобождаем слот
                pass

            m.status = "invite_timeout"
            m.invite_link = None
            m.invite_expires_at = None
            m.updated_at = now

            affected_users.append(m.user_id)

        return affected_users

    # ============================================================
    # PUBLIC API (используется UI при входе в Yandex Plus)
    # ============================================================

    async def ensure_membership_for_user(
        self,
        *,
        session,
        user_id: int,
        yandex_login: str,
    ) -> YandexMembership:
        """
        Гарантирует, что у пользователя есть join-session.
        Если нет — автоматически создаёт invite.
        """
        existing = await session.scalar(
            select(YandexMembership)
            .where(
                YandexMembership.user_id == user_id,
                YandexMembership.status.in_(["awaiting_join", "active"]),
            )
        )
        if existing:
            return existing

        account = await session.scalar(
            select(YandexAccount)
            .where(YandexAccount.status == "active")
            .limit(1)
        )
        if not account:
            raise RuntimeError("No active YandexAccount")

        invite_link = await self.provider.create_invite_link(
            storage_state_path=self._account_state_path(account),
            label=account.label,
        )

        now = utcnow()

        membership = YandexMembership(
            user_id=user_id,
            yandex_account_id=account.id,
            yandex_login=yandex_login,
            invite_link=invite_link,
            invite_issued_at=now,
            invite_expires_at=now + timedelta(minutes=INVITE_TTL_MINUTES),
            status="awaiting_join",
            reinvite_used=False,
        )

        session.add(membership)
        await session.flush()

        return membership

    # ============================================================
    # INTERNAL
    # ============================================================

    def _account_state_path(self, account: YandexAccount) -> str:
        return f"{settings.yandex_cookies_dir}/{account.credentials_ref}"


# Singleton, как у тебя принято
yandex_service = YandexService()
