from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select, update

from app.db.session import session_scope
from app.db.models.user import User
from app.db.models.subscription import Subscription
from app.db.models.payment import Payment
from app.db.models.vpn_peer import VpnPeer
from app.db.models.yandex_invite_slot import YandexInviteSlot
from app.db.models.yandex_membership import YandexMembership

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AdminResetUserService:
    """
    Полный сброс пользователя для тестов:
    - удаляем YandexMembership
    - отвязываем YandexInviteSlot от пользователя (не возвращаем в free)
    - удаляем VpnPeer
    - удаляем Payment
    - жёстко сбрасываем Subscription (end_at=None + inactive)
    - сбрасываем flow_state/flow_data у User
    """

    async def reset_user(self, *, tg_id: int) -> None:
        async with session_scope() as session:
            # --- USER (may be missing) ---
            user = await session.get(User, tg_id)

            # 1) Yandex membership by tg_id
            await session.execute(delete(YandexMembership).where(YandexMembership.tg_id == tg_id))

            # 1.1) Detach slot (keep status issued/burned as-is)
            await session.execute(
                update(YandexInviteSlot)
                .where(YandexInviteSlot.issued_to_tg_id == tg_id)
                .values(
                    issued_to_tg_id=None,
                    issued_at=None,
                    service_end_at=None,
                )
            )

            # 2) VPN peers
            await session.execute(delete(VpnPeer).where(VpnPeer.tg_id == tg_id))

            # 3) Payments
            await session.execute(delete(Payment).where(Payment.tg_id == tg_id))

            # 4) Subscription: do NOT rely on delete+insert (can be fragile with FKs/constraints in old DBs).
            #    We either update existing row, or create a new inactive one.
            sub = await session.get(Subscription, tg_id)
            if sub is None:
                # If there is no user row, create it to satisfy FK, then create subscription.
                if user is None:
                    user = User(tg_id=tg_id)
                    session.add(user)
                    await session.flush()

                sub = Subscription(tg_id=tg_id)
                session.add(sub)
                await session.flush()

            sub.start_at = None
            sub.end_at = None
            sub.is_active = False
            sub.status = "inactive"

            # 5) Reset user flow + any cached yandex fields
            if user is not None:
                # reset referral click info (if present)
                if hasattr(user, "referred_by_tg_id"):
                    setattr(user, "referred_by_tg_id", None)
                if hasattr(user, "referred_at"):
                    setattr(user, "referred_at", None)

                user.flow_state = None
                user.flow_data = None

                if hasattr(user, "yandex_login"):
                    setattr(user, "yandex_login", None)
                if hasattr(user, "yandex_status"):
                    setattr(user, "yandex_status", None)

            await session.commit()

        log.info("admin_reset_user_done tg_id=%s", tg_id)
