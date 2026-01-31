from __future__ import annotations

import logging
from sqlalchemy import select

from app.db.models.yandex_membership import YandexMembership
from app.db.models.user import User
from app.db.session import session_scope
from app.services.yandex.provider import build_provider

log = logging.getLogger(__name__)

MAX_STRIKES = 2


class YandexGuardService:
    """
    Жёсткая проверка семей Yandex Plus.
    Кикает левых, предупреждает, банит.
    """

    def __init__(self) -> None:
        self.provider = build_provider()

    async def run_guard(self, session) -> list[int]:
        """
        Основной guard-цикл.
        Вызывается из worker.
        Возвращает tg_id пользователей с нарушениями.
        """
        affected: list[int] = []

        q = (
            select(YandexMembership)
            .where(
                YandexMembership.status.in_(("awaiting_join", "active")),
                YandexMembership.yandex_login.isnot(None),
            )
        )
        rows = (await session.execute(q)).scalars().all()

        for ym in rows:
            expected_login = ym.yandex_login.lower().strip()
            acc_storage = ym.yandex_account.credentials_ref
            tg_id = ym.tg_id

            snapshot = await self.provider.probe(
                storage_state_path=acc_storage
            )

            family = snapshot.family
            if not family:
                continue

            guests = {g.lower() for g in family.guests or []}

            # ✅ ожидаемый логин на месте — всё ок
            if expected_login in guests:
                if ym.status != "active":
                    ym.status = "active"
                continue

            # ❌ есть гость, но он НЕ тот
            if not guests:
                continue

            intruder = list(guests)[0]
            log.warning(
                "YandexGuard: intruder detected. expected=%s actual=%s",
                expected_login,
                intruder,
            )

            # 1. Кикаем ЛЕВОГО (ВАЖНО: remove_guest)
            await self.provider.remove_guest(
                storage_state_path=acc_storage,
                guest_login=intruder,
            )

            # 2. Страйк
            user = await session.get(User, tg_id)
            if not user:
                continue

            ym.strikes = (ym.strikes or 0) + 1

            # 3. Бан
            if ym.strikes >= MAX_STRIKES:
                ym.status = "banned"
                user.yandex_blocked = True
            else:
                ym.status = "awaiting_join"

            affected.append(tg_id)

        return affected
