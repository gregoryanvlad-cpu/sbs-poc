from __future__ import annotations

from sqlalchemy import select, delete

from app.core.config import settings
from app.db.session import session_scope
from app.db.models.user import User
from app.db.models.subscription import Subscription
from app.db.models.vpn_peer import VpnPeer
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_membership import YandexMembership
from app.services.yandex.provider import build_provider


class AdminResetUserService:
    """
    Полный сброс пользователя (ТОЛЬКО ДЛЯ ТЕСТОВ).

    Удаляет:
    - подписку
    - VPN peer'ы
    - Yandex membership (включая pending-инвайты)
    - сбрасывает User в "как новый"
    """

    def __init__(self) -> None:
        self.provider = build_provider()

    async def reset_user(self, *, tg_id: int) -> None:
        async with session_scope() as session:
            # ─────────────────────────────────────
            # USER
            # ─────────────────────────────────────
            user = await session.get(User, tg_id)
            if not user:
                return

            # ─────────────────────────────────────
            # YANDEX MEMBERSHIPS
            # ─────────────────────────────────────
            memberships = (
                await session.scalars(
                    select(YandexMembership).where(
                        YandexMembership.tg_id == tg_id
                    )
                )
            ).all()

            for m in memberships:
                # если было ожидающее приглашение — отменяем в Яндексе
                if m.status == "awaiting_join" and m.yandex_account_id:
                    account = await session.get(YandexAccount, m.yandex_account_id)
                    if account:
                        try:
                            await self.provider.cancel_pending_invite(
                                storage_state_path=f"{settings.yandex_cookies_dir}/{account.credentials_ref}",
                                login=m.login,
                            )
                        except Exception:
                            pass

                await session.delete(m)

            # ─────────────────────────────────────
            # VPN
            # ─────────────────────────────────────
            await session.execute(
                delete(VpnPeer).where(VpnPeer.tg_id == tg_id)
            )

            # ─────────────────────────────────────
            # SUBSCRIPTION
            # ─────────────────────────────────────
            await session.execute(
                delete(Subscription).where(Subscription.tg_id == tg_id)
            )

            # ─────────────────────────────────────
            # USER RESET (как новый)
            # ─────────────────────────────────────
            user.flow_state = None
            user.flow_data = None
            user.is_active = False

            await session.commit()
