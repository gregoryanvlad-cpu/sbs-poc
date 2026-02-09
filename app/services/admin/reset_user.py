from __future__ import annotations

import logging

from sqlalchemy import delete, select, update

from app.db.session import session_scope
from app.db.models.user import User
from app.db.models.subscription import Subscription
from app.db.models.payment import Payment
from app.db.models.vpn_peer import VpnPeer
from app.db.models.yandex_invite_slot import YandexInviteSlot
from app.db.models.yandex_membership import YandexMembership

log = logging.getLogger(__name__)


class AdminResetUserService:
    """
    Полный сброс пользователя для тестов:
    - удаляем Subscription
    - удаляем Payment
    - удаляем VpnPeer
    - удаляем YandexMembership
    - сбрасываем flow_state/flow_data у User (или удаляем User, если хочешь — но безопаснее сброс)
    """

    async def reset_user(self, *, tg_id: int) -> None:
        async with session_scope() as session:
            # 1) удаляем yandex_membership по tg_id (ВАЖНО: НЕ user_id)
            await session.execute(delete(YandexMembership).where(YandexMembership.tg_id == tg_id))

            # 1.1) В ручном режиме слоты не переиспользуются (S1), но после "reset" мы должны
            # убрать привязку слота к пользователю, чтобы в ЛК больше не отображались "семья/слот".
            # Сам слот остаётся issued/burned (мы его не возвращаем в free).
            await session.execute(
                update(YandexInviteSlot)
                .where(YandexInviteSlot.issued_to_tg_id == tg_id)
                .values(
                    issued_to_tg_id=None,
                    issued_at=None,
                    service_end_at=None,
                )
            )

            # 2) удаляем vpn peers
            await session.execute(
                delete(VpnPeer).where(VpnPeer.tg_id == tg_id)
            )

            # 3) удаляем платежи
            await session.execute(
                delete(Payment).where(Payment.tg_id == tg_id)
            )

            # 4) сбрасываем подписку ЖЁСТКО:
            #    - удаляем все записи subscriptions по tg_id (на случай дублей из старых миграций/ручных вставок)
            #    - создаём "чистую" неактивную подписку
            await session.execute(delete(Subscription).where(Subscription.tg_id == tg_id))

            sub = Subscription(
                tg_id=tg_id,
                start_at=None,
                end_at=None,
                is_active=False,
                status="inactive",
            )
            session.add(sub)
            await session.flush()

            # 5) сбрасываем пользователя (не удаляем строку, чтобы не ломать связи/логику)
            user = await session.get(User, tg_id)
            if user:
                user.flow_state = None
                user.flow_data = None

                # если у тебя есть поля, которые фиксируют яндекс-логин/статус в User — тоже сбрось:
                if hasattr(user, "yandex_login"):
                    setattr(user, "yandex_login", None)
                if hasattr(user, "yandex_status"):
                    setattr(user, "yandex_status", None)

            await session.commit()

        log.info("admin_reset_user_done tg_id=%s", tg_id)
