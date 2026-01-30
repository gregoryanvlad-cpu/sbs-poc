from __future__ import annotations

from sqlalchemy import delete, select

from app.db.session import session_scope
from app.db.models.user import User
from app.db.models.subscription import Subscription
from app.db.models.yandex_membership import YandexMembership
from app.db.models.vpn_peer import VpnPeer
from app.db.models.yandex_account import YandexAccount
from app.services.yandex.provider import build_provider
from app.core.config import settings


class AdminResetUserService:
    """
    Полный сброс пользователя для тестов.
    """

    def __init__(self) -> None:
        self.provider = build_provider()

    async def reset_user(self, *, tg_id: int) -> None:
        async with session_scope() as session:
            # ─── User ───────────────────────────────────────────
            user = await session.get(User, tg_id)
            if not user:
                return

            # ─── Yandex Membership ──────────────────────────────
            memberships = (
                await session.scalars(
                    select(YandexMembership)
                    .where(YandexMembership.user_id == tg_id)
                )
            ).all()

            for m in memberships:
                if m.status == "awaiting_join":
                    account = await session.get(YandexAccount, m.yandex_account_id)
                    if account:
                        try:
                            await self.provider.cancel_pending_invite(
                                storage_state_path=f"{settings.yandex_cookies_dir}/{account.credentials_ref}",
                                label=account.label,
                            )
                        except Exception:
                            pass
                await session.delete(m)

            # ─── VPN peers ──────────────────────────────────────
            await session.execute(
                delete(VpnPeer).where(VpnPeer.tg_id == tg_id)
            )

            # ─── Subscription ───────────────────────────────────
            await session.execute(
                delete(Subscription).where(Subscription.tg_id == tg_id)
            )

            # ─── User cleanup ───────────────────────────────────
            user.yandex_login = None
            user.flow_state = None
            user.flow_data = None

            await session.commit()
