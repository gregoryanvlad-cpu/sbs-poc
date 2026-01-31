from __future__ import annotations

import logging
from sqlalchemy import select

from app.core.config import settings
from app.db.models.yandex_membership import YandexMembership
from app.db.models.user import User
from app.db.session import session_scope
from app.services.yandex.provider import build_provider

log = logging.getLogger(__name__)

MAX_STRIKES = 2


class YandexGuardService:
    """
    Жёсткая проверка вступления в семейную группу.
    Кикает левых, предупреждает, банит (через yandex_membership.status = blocked).
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
        expected_login = (expected_login or "").lower().strip().lstrip("@")
        if not expected_login:
            return

        snapshot = await self.provider.probe(storage_state_path=yandex_account_storage)

        family = snapshot.family
        if not family:
            log.warning("YandexGuard: no family snapshot")
            return

        joined_logins = set(l.lower() for l in (family.guests or []))
        if not joined_logins:
            log.info("YandexGuard: nobody joined yet")
            return

        # ✅ правильный логин — всё ок
        if expected_login in joined_logins:
            log.info("YandexGuard: correct login joined: %s", expected_login)
            await self._mark_joined(tg_id)
            return

        # ❌ левые (все кто в guests, кроме ожидаемого)
        intruders = joined_logins - {expected_login}
        if not intruders:
            return

        log.warning("YandexGuard: intruders detected: %s", intruders)

        # 1) КИКАЕМ ВСЕХ ЛЕВЫХ
        for login in sorted(intruders):
            try:
                ok = await self.provider.remove_guest(
                    storage_state_path=yandex_account_storage,
                    guest_login=login,
                )
                log.info("YandexGuard: kicked %s -> %s", login, ok)
            except Exception:
                log.exception("YandexGuard: failed to kick %s", login)

        # 2) СТРАЙКИ/БАН (НО owner не трогаем)
        if tg_id == int(getattr(settings, "owner_tg_id", 0) or 0):
            log.info("YandexGuard: owner tg_id=%s -> no strikes", tg_id)
            return

        async with session_scope() as session:
            user = await session.get(User, tg_id)
            ym = await self._get_membership(session, tg_id)

            if not user or not ym:
                return

            ym.abuse_strikes = int(ym.abuse_strikes or 0) + 1

            # бан
            if ym.abuse_strikes >= MAX_STRIKES:
                ym.status = "blocked"

            await session.commit()

            strikes_now = int(ym.abuse_strikes or 0)

        # 3) уведомление
        await self._notify_user(
            tg_id=tg_id,
            intruder=", ".join(sorted(intruders)),
            expected=expected_login,
            strikes=strikes_now,
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
                "Лишний участник был удалён из семейной группы.\n"
                "Повторное нарушение приведёт к блокировке."
            )

        try:
            await bot.send_message(tg_id, text, parse_mode="HTML")
        except Exception:
            pass
