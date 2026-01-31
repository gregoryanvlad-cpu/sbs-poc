from __future__ import annotations

from sqlalchemy import delete, select

from app.db.session import session_scope
from app.db.models.yandex_membership import YandexMembership
from app.db.models.subscription import Subscription
from app.db.models.payment import Payment
from app.db.models.vpn_peer import VpnPeer


class AdminForgiveUserService:
    async def forgive_yandex(self, tg_id: int) -> bool:
        """
        Снимает страйки/разблокирует reinvite/чистит статусные ограничения Яндекса,
        но НЕ удаляет пользователя целиком.
        """
        async with session_scope() as session:
            m = await session.scalar(
                select(YandexMembership)
                .where(YandexMembership.tg_id == tg_id)
                .order_by(YandexMembership.id.desc())
                .limit(1)
            )
            if not m:
                return False

            m.abuse_strikes = 0
            m.reinvite_used = 0

            # если он был заблокирован в invite_timeout из-за strikes — вернём в "нейтрал"
            if m.status == "invite_timeout":
                m.status = "awaiting_join" if m.invite_link else "invite_timeout"

            await session.commit()
            return True

    async def full_reset_user(self, tg_id: int) -> None:
        """
        Полный сброс (если у тебя уже есть — можно не использовать).
        Оставил тут на всякий: удаляем payments/sub/vpn/yandex.
        """
        async with session_scope() as session:
            await session.execute(delete(YandexMembership).where(YandexMembership.tg_id == tg_id))
            await session.execute(delete(VpnPeer).where(VpnPeer.tg_id == tg_id))
            await session.execute(delete(Payment).where(Payment.tg_id == tg_id))
            await session.execute(delete(Subscription).where(Subscription.tg_id == tg_id))
            await session.commit()
