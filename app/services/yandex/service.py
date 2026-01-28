from datetime import timedelta

from app.services.yandex.provider import MockYandexProvider
from app.services.yandex.repo import pick_account
from app.db.models.yandex_membership import YandexMembership
from app.core.config import settings
from app.utils.time import utcnow


class EnsureResult:
    def __init__(self, status: str, message: str, invite_link: str | None = None):
        self.status = status
        self.message = message
        self.invite_link = invite_link


class YandexService:
    def __init__(self):
        self.provider = MockYandexProvider()

    async def ensure_membership_after_payment(self, session, tg_id: int, yandex_login: str) -> EnsureResult:
        """
        Вызывается после успешной оплаты (или продления).
        НЕ выдаёт новую ссылку, если пользователь уже active/scheduled_switch.
        """
        # получаем подписку пользователя
        sub = await session.get_subscription(tg_id)
        if not sub or not sub.end_at:
            return EnsureResult("error", "Подписка не активна.")

        # ищем существующий membership
        q = await session.execute(
            YandexMembership.__table__
            .select()
            .where(YandexMembership.tg_id == tg_id)
            .order_by(YandexMembership.id.desc())
            .limit(1)
        )
        membership = q.fetchone()
        now = utcnow()

        # если уже активен — ничего не делаем
        if membership and membership.status in ("active", "scheduled_switch"):
            return EnsureResult(
                membership.status,
                "Продление учтено. Новая ссылка будет выдана при необходимости.",
            )

        # если уже есть pending
        if membership and membership.status == "pending":
            return EnsureResult(
                "pending",
                "У вас уже есть активное приглашение.",
                membership.invite_link,
            )

        # подбираем аккаунт Яндекса, который покрывает весь период
        account = await pick_account(session, need_cover_until=sub.end_at)
        if not account:
            return EnsureResult(
                "waiting",
                "Сейчас нет доступных аккаунтов Яндекс. Приглашение будет выдано автоматически.",
            )

        invite_link = await self.provider.create_invite_link(
            credentials_ref=account.credentials_ref
        )

        membership = YandexMembership(
            tg_id=tg_id,
            yandex_account_id=account.id,
            yandex_login=yandex_login,
            status="pending",
            invite_link=invite_link,
            invite_issued_at=now,
            invite_expires_at=now + timedelta(seconds=settings.yandex_pending_ttl_seconds),
            coverage_end_at=sub.end_at,
        )

        session.add(membership)
        account.used_slots += 1

        return EnsureResult(
            "pending",
            "Приглашение готово.",
            invite_link,
        )


yandex_service = YandexService()
