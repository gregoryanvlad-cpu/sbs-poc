from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from app.db.models.user import User
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.services.yandex.provider import build_provider

log = logging.getLogger(__name__)

MAX_STRIKES = 2


class YandexGuardService:
    """
    Проверка: если в семью зашёл не тот логин — кикаем, ставим страйк, при повторе баним.
    """

    def __init__(self) -> None:
        self.provider = build_provider()

    async def verify_join(
        self,
        *,
        storage_state_path: str,
        expected_login: str,
        tg_id: int,
    ) -> None:
        expected_login = (expected_login or "").strip().lstrip("@").lower()
        if not expected_login:
            return

        snap = await self.provider.probe(storage_state_path=storage_state_path)
        fam = snap.family
        if not fam:
            return

        joined_logins = set((fam.guests or []))
        if not joined_logins:
            return

        # ✅ Всё ок — ожидаемый логин действительно в гостях
        if expected_login in joined_logins:
            log.info("YandexGuard: correct login joined: %s", expected_login)
            await self._mark_joined(tg_id)
            return

        # ❌ Левый логин
        intruder = sorted(joined_logins)[0]
        log.warning("YandexGuard: intruder detected: %s (expected %s)", intruder, expected_login)

        # 1) Кикаем левого
        try:
            await self.provider.kick_member(storage_state_path=storage_state_path, login=intruder)
        except Exception:
            log.exception("YandexGuard: failed to kick intruder: %s", intruder)

        # 2) Страйки/бан
        strikes: int = 0
        async with session_scope() as session:
            user = await session.get(User, tg_id)
            ym = await self._get_membership(session, tg_id)
            if not user or not ym:
                return

            ym.strikes = (ym.strikes or 0) + 1
            strikes = int(ym.strikes or 0)

            # при повторе — бан по Yandex (как вы и обсуждали)
            if strikes >= MAX_STRIKES:
                ym.status = "banned"
                # если у тебя есть поле user.yandex_blocked — ставим
                if hasattr(user, "yandex_blocked"):
                    user.yandex_blocked = True

            await session.commit()

        # 3) Уведомление
        await self._notify_user(
            tg_id=tg_id,
            intruder=intruder,
            expected=expected_login,
            strikes=strikes,
        )

    async def _get_membership(self, session, tg_id: int) -> Optional[YandexMembership]:
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
                # у тебя где-то используется active/joined — оставляю joined как в твоём коде,
                # если нужно — поменяй на "active" (только здесь).
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

        bot = Bot(token=settings.bot_token)

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
                "Левый логин удалён из семейной группы.\n"
                "Повторное нарушение приведёт к блокировке."
            )

        try:
            await bot.send_message(tg_id, text, parse_mode="HTML")
        except Exception:
            pass
