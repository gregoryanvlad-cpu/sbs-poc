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
    Жёсткая проверка вступления в семейную группу.
    Кикает левых, предупреждает, банит.
    """

    def __init__(self) -> None:
        self.provider = build_provider()

    async def verify_join(
        self,
        *,
        yandex_account_storage: str,
        expected_login: str,
        tg_id: int,
    ) -> None:
        """
        Вызывается ПОСЛЕ probe().
        """
        snapshot = await self.provider.probe(storage_state_path=yandex_account_storage)

        family = snapshot.family
        if not family:
            return

        # Берём всех гостей кроме админа
        joined_logins = set(family.guests or [])

        if not joined_logins:
            return

        # если ожидаемый логин есть — всё ок
        if expected_login in joined_logins:
            log.info("YandexGuard: correct login joined: %s", expected_login)
            await self._mark_joined(tg_id)
            return

        # ❌ ЗАШЁЛ ЛЕВЫЙ
        intruder = list(joined_logins)[0]
        log.warning("YandexGuard: intruder detected: %s", intruder)

        # 1. Кикаем
        await self.provider.kick_member(
            storage_state_path=yandex_account_storage,
            login=intruder,
        )

        # 2. Страйк
        async with session_scope() as session:
            user = await session.get(User, tg_id)
            ym = await self._get_membership(session, tg_id)

            if not user or not ym:
                return

            ym.strikes = (ym.strikes or 0) + 1

            # 3. Бан при повторе
            if ym.strikes >= MAX_STRIKES:
                ym.status = "banned"
                user.yandex_blocked = True

            await session.commit()

        # 4. Уведомление
        await self._notify_user(
            tg_id=tg_id,
            intruder=intruder,
            expected=expected_login,
            strikes=ym.strikes,
        )

    async def _get_membership(self, session, tg_id: int) -> YandexMembership | None:
        q = (
            select(YandexMembership)
            .where(YandexMembership.tg_id == tg_id)
            .order_by(YandexMembership.id.desc())
            .limit(1)
        )
        res = await session.execute(q)
        return res.scalar_one_or_none()

    async def _mark_joined(self, tg_id: int) -> None:
        async with session_scope() as session:
            ym = await self._get_membership(session, tg_id)
            if ym:
                ym.status = "joined"
                await session.commit()

    async def _notify_user(
        self,
        *,
        tg_id: int,
        intruder: str,
        expected: str,
        strikes: int,
    ) -> None:
        from aiogram import Bot
        from app.core.config import settings

        bot = Bot(settings.bot_token)

        if strikes >= MAX_STRIKES:
            text = (
                "⛔️ <b>Yandex Plus заблокирован</b>\n\n"
                "Вы повторно приняли приглашение под чужим логином.\n\n"
                f"Ожидался: <code>{expected}</code>\n"
                f"Вошёл: <code>{intruder}</code>\n\n"
                "Доступ к Yandex Plus отключён."
            )
        else:
            text = (
                "⚠️ <b>Предупреждение</b>\n\n"
                "Вы приняли приглашение под <b>неверным логином</b>.\n\n"
                f"Ожидался: <code>{expected}</code>\n"
                f"Вошёл: <code>{intruder}</code>\n\n"
                "Вы были удалены из семейной группы.\n"
                "Повторное нарушение приведёт к блокировке."
            )

        try:
            await bot.send_message(tg_id, text, parse_mode="HTML")
        except Exception:
            pass
