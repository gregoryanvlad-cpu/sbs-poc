from datetime import datetime
from sqlalchemy import select

from app.db.session import session_scope
from app.db.models.yandex_membership import YandexMembership
from app.db.models.yandex_account import YandexAccount
from app.services.yandex.provider import YandexProvider


async def yandex_invite_timeout_task():
    provider = YandexProvider()
    now = datetime.utcnow()

    async with session_scope() as session:
        memberships = (
            await session.scalars(
                select(YandexMembership)
                .where(
                    YandexMembership.status == "awaiting_join",
                    YandexMembership.invite_expires_at < now,
                )
            )
        ).all()

        for membership in memberships:
            account = await session.get(
                YandexAccount, membership.yandex_account_id
            )
            if not account:
                continue

            # Отменяем приглашение
            await provider.cancel_pending_invite(account=account)

            membership.status = "invite_timeout"
            membership.invite_link = None
            membership.invite_expires_at = None

        await session.commit()

