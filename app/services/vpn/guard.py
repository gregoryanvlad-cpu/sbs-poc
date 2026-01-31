from __future__ import annotations

import logging
from typing import Iterable

from aiogram import Bot
from sqlalchemy import select

from app.core.config import settings
from app.db.models.user import User
from app.db.models.yandex_membership import YandexMembership
from app.db.session import session_scope
from app.services.yandex.provider import build_provider

log = logging.getLogger(__name__)

MAX_STRIKES = 2


class YandexGuardService:
    """
    Жёсткая проверка вступления в семейную группу.
    Если вошёл не тот логин — кикаем, выдаём страйк, при повторе баним.
    """

    def __init__(self) -> None:
        self.provider = build_provider()

    async def verify_join_for_user(
        self,
        *,
        storage_state_path: str,
        tg_id: int,
        expected_login: str,
        allowed_logins: Iterable[str] | None = None,
    ) -> None:
        """
        Проверяем семью по cookies админа и решаем:
        - если expected_login в гостях => OK, ставим joined
        - иначе кикаем "лишних" (в первую очередь тех, кто не в allowlist),
          выдаём страйк tg_id, при повторе бан.
        """
        expected = (expected_login or "").strip().lstrip("@").lower()
        if not expected:
            return

        allow = {expected}
        if allowed_logins:
            allow |= {(x or "").strip().lstrip("@").lower() for x in allowed_logins if x}

        snap = await self.provider.probe(storage_state_path=storage_state_path)
        family = snap.family
        if not family:
            return

        guests = {(g or "").strip().lower() for g in (family.guests or []) if g}
        if not guests:
            return

        # ✅ ожидаемый логин есть — всё отлично
        if expected in guests:
            log.info("YandexGuard: joined ok tg_id=%s login=%s", tg_id, expected)
            await self._mark_joined(tg_id)
            return

        # ❌ вошёл кто-то другой.
        # Логика "Вариант 2":
        # 1) кикаем всех гостей, которые НЕ в allowlist (чтобы убрать "левых")
        intruders = sorted([g for g in guests if g not in allow])
        if not intruders:
            # теоретически гости есть, но все в allow — тогда ничего не делаем
            return

        # кикаем каждого чужого (без падения всего джоба)
        kicked_any = False
        for login in intruders:
            try:
                ok = await self.provider.remove_guest(
                    storage_state_path=storage_state_path,
                    guest_login=login,
                )
                kicked_any = kicked_any or bool(ok)
            except Exception:
                log.exception("YandexGuard: failed to kick login=%s", login)

        # выдаём страйк именно текущему tg_id (того, кто должен был войти правильно)
        strikes = await self._add_strike_and_maybe_ban(tg_id)

        # уведомление пользователю
        if kicked_any:
            await self._notify_user(
                tg_id=tg_id,
                intruder=intruders[0],
                expected=expected,
                strikes=strikes,
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

    async def _add_strike_and_maybe_ban(self, tg_id: int) -> int:
        async with session_scope() as session:
            user = await session.get(User, tg_id)
            ym = await self._get_membership(session, tg_id)

            if not user or not ym:
                return 0

            ym.strikes = int(ym.strikes or 0) + 1

            if ym.strikes >= MAX_STRIKES:
                ym.status = "banned"
                user.yandex_blocked = True

            await session.commit()
            return int(ym.strikes or 0)

    async def _notify_user(self, *, tg_id: int, intruder: str, expected: str, strikes: int) -> None:
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
                "Лишний участник был удалён из семьи.\n"
                "Повторное нарушение приведёт к блокировке."
            )

        try:
            await bot.send_message(tg_id, text, parse_mode="HTML")
        except Exception:
            pass
