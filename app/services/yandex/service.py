from __future__ import annotations

from datetime import timedelta
from typing import List, Tuple

from sqlalchemy import select

from app.core.config import settings
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_membership import YandexMembership
from app.repo import utcnow
from app.services.yandex.provider import build_provider

# ✅ TTL приглашения: 15 минут
INVITE_TTL_MINUTES = 15


class YandexService:
    """
    Вся бизнес-логика Яндекса.
    Worker вызывает методы отсюда.
    """

    def __init__(self) -> None:
        self.provider = build_provider()

    def _account_state_path(self, account: YandexAccount) -> str:
        return f"{settings.yandex_cookies_dir}/{account.credentials_ref}"

    async def ensure_membership_for_user(
        self,
        *,
        session,
        tg_id: int,
        yandex_login: str,
    ) -> YandexMembership:
        """
        Гарантирует, что у пользователя есть join-session.
        Если нет — создаёт invite и сохраняет membership.
        """
        existing = await session.scalar(
            select(YandexMembership).where(
                YandexMembership.tg_id == tg_id,
                YandexMembership.status.in_(["awaiting_join", "active"]),
            )
        )
        if existing:
            return existing

        account = await session.scalar(
            select(YandexAccount)
            .where(YandexAccount.status == "active")
            .order_by(YandexAccount.id.asc())
            .limit(1)
        )
        if not account or not account.credentials_ref:
            raise RuntimeError("No active YandexAccount")

        invite_link = await self.provider.create_invite_link(
            storage_state_path=self._account_state_path(account)
        )

        now = utcnow()
        membership = YandexMembership(
            tg_id=tg_id,
            yandex_account_id=account.id,
            yandex_login=yandex_login,
            invite_link=invite_link,
            invite_issued_at=now,
            invite_expires_at=now + timedelta(minutes=INVITE_TTL_MINUTES),
            status="awaiting_join",
            reinvite_used=0,
        )

        session.add(membership)
        await session.flush()
        return membership

    async def expire_pending_invites(self, session) -> List[int]:
        """
        Отменяет просроченные инвайты (awaiting_join) и освобождает слот.
        Возвращает список tg_id, которым нужно отправить уведомление.
        """
        now = utcnow()
        affected: List[int] = []

        memberships = (
            await session.scalars(
                select(YandexMembership).where(
                    YandexMembership.status == "awaiting_join",
                    YandexMembership.invite_expires_at.is_not(None),
                    YandexMembership.invite_expires_at <= now,
                )
            )
        ).all()

        if not memberships:
            return affected

        for m in memberships:
            account = await session.get(YandexAccount, m.yandex_account_id) if m.yandex_account_id else None
            if account and account.credentials_ref:
                try:
                    await self.provider.cancel_pending_invite(
                        storage_state_path=self._account_state_path(account)
                    )
                except Exception:
                    # Даже если playwright не смог — всё равно помечаем как истёкшее
                    pass

            m.status = "invite_timeout"
            m.invite_link = None
            m.invite_issued_at = None
            m.invite_expires_at = None
            m.updated_at = now

            affected.append(m.tg_id)

        return affected

    async def sync_family_and_activate(self, session) -> Tuple[List[int], List[str]]:
        """
        Автоматика:
        - открываем id.yandex.ru/family
        - получаем текущих guests/admins
        - если awaiting_join и логин появился в guests -> status=active
        - обновляем used_slots у аккаунта (best-effort)

        Возвращает:
        - activated_tg_ids: кому можно отправить "✅ подключено"
        - debug_dirs: для логов/наблюдения (опционально)
        """
        activated: List[int] = []
        debug_dirs: List[str] = []

        accounts = (
            await session.scalars(
                select(YandexAccount)
                .where(YandexAccount.status == "active")
                .order_by(YandexAccount.id.asc())
            )
        ).all()

        if not accounts:
            return activated, debug_dirs

        now = utcnow()

        for acc in accounts:
            if not acc.credentials_ref:
                continue

            try:
                snap = await self.provider.probe(storage_state_path=self._account_state_path(acc))
                if snap.raw_debug and snap.raw_debug.get("debug_dir"):
                    debug_dirs.append(str(snap.raw_debug.get("debug_dir")))
            except Exception:
                # если Яндекс временно не ответил — не ломаем воркер
                continue

            fam = snap.family
            if not fam:
                continue

            # best-effort: обновим used_slots в аккаунте
            try:
                acc.used_slots = int(fam.used_slots)
            except Exception:
                pass

            fam_admins = {x.lower() for x in (fam.admins or [])}
            fam_guests = {x.lower() for x in (fam.guests or [])}

            # memberships на этом аккаунте, которые ждут вступления
            pending_memberships = (
                await session.scalars(
                    select(YandexMembership).where(
                        YandexMembership.yandex_account_id == acc.id,
                        YandexMembership.status == "awaiting_join",
                    )
                )
            ).all()

            for m in pending_memberships:
                login = (m.yandex_login or "").strip().lstrip("@").lower()
                if not login:
                    continue

                # ✅ если логин появился в гостях — значит вступил
                if login in fam_guests:
                    m.status = "active"
                    m.invite_link = None
                    m.invite_issued_at = None
                    m.invite_expires_at = None
                    m.updated_at = now
                    activated.append(m.tg_id)
                    continue

                # (опционально) если логин почему-то стал админом — тоже считаем активным
                if login in fam_admins:
                    m.status = "active"
                    m.invite_link = None
                    m.invite_issued_at = None
                    m.invite_expires_at = None
                    m.updated_at = now
                    activated.append(m.tg_id)
                    continue

        return activated, debug_dirs


yandex_service = YandexService()
