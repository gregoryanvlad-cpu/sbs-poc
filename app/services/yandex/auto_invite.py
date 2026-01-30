from datetime import datetime, timedelta
from sqlalchemy import select

from app.db.session import session_scope
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_membership import YandexMembership
from app.services.yandex.provider import YandexProvider


INVITE_TTL_MINUTES = 10


class YandexAutoInviteService:
    def __init__(self):
        self.provider = YandexProvider()

    async def issue_invite_for_user(
        self,
        user_id: int,
        yandex_login: str,
    ) -> YandexMembership:
        async with session_scope() as session:
            # 1. Берём активный яндекс-аккаунт (пока 1)
            account = await session.scalar(
                select(YandexAccount)
                .where(YandexAccount.status == "active")
                .limit(1)
            )
            if not account:
                raise RuntimeError("No active YandexAccount available")

            # 2. Проверяем, что у пользователя нет активной сессии
            existing = await session.scalar(
                select(YandexMembership)
                .where(
                    YandexMembership.user_id == user_id,
                    YandexMembership.status.in_(["awaiting_join", "active"]),
                )
            )
            if existing:
                return existing

            # 3. Создаём invite через Playwright
            invite_link = await self.provider.create_invite_link(
                account=account
            )

            now = datetime.utcnow()
            expires_at = now + timedelta(minutes=INVITE_TTL_MINUTES)

            membership = YandexMembership(
                user_id=user_id,
                yandex_account_id=account.id,
                yandex_login=yandex_login,
                invite_link=invite_link,
                invite_issued_at=now,
                invite_expires_at=expires_at,
                reinvite_used=False,
                status="awaiting_join",
            )

            session.add(membership)
            await session.commit()

            return membership
