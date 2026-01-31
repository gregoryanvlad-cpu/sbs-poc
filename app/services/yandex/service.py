from __future__ import annotations

from datetime import timedelta
from typing import List, Tuple

from sqlalchemy import select

from app.core.config import settings
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_membership import YandexMembership
from app.repo import utcnow
from app.services.yandex.provider import build_provider

INVITE_TTL_MINUTES = 15


def _allowed_logins_from_env() -> set[str]:
    raw = getattr(settings, "yandex_allowed_logins", None)
    if not raw:
        return set()
    if isinstance(raw, str):
        return {x.strip().lstrip("@").lower() for x in raw.split(",") if x.strip()}
    return set()


class YandexService:
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
            abuse_strikes=0,
        )

        session.add(membership)
        await session.flush()
        return membership

    async def expire_pending_invites(self, session) -> List[int]:
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
                    pass

            m.status = "invite_timeout"
            m.invite_link = None
            m.invite_issued_at = None
            m.invite_expires_at = None
            m.updated_at = now

            affected.append(m.tg_id)

        return affected

    async def sync_family_and_activate(self, session) -> Tuple[List[int], List[str]]:
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
                continue

            fam = snap.family
            if not fam:
                continue

            try:
                acc.used_slots = int(fam.used_slots)
            except Exception:
                pass

            fam_admins = {x.lower() for x in (fam.admins or [])}
            fam_guests = {x.lower() for x in (fam.guests or [])}

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

                if login in fam_guests or login in fam_admins:
                    m.status = "active"
                    m.invite_link = None
                    m.invite_issued_at = None
                    m.invite_expires_at = None
                    m.updated_at = now
                    activated.append(m.tg_id)

        return activated, debug_dirs

    async def enforce_no_foreign_logins(self, session) -> Tuple[List[tuple[int, str]], List[str]]:
        """
        Автоматика "левые логины":
        - ищем гостей, которые НЕ в allowed
        - удаляем их
        - strikes выдаём только если есть ровно один awaiting_join
        - OWNER (settings.owner_tg_id) никогда не получает strikes/ban
        - allowlist логинов из ENV никогда не кикаем
        """
        warnings: List[tuple[int, str]] = []
        debug_dirs: List[str] = []

        owner_id = int(getattr(settings, "owner_tg_id", 0) or 0)
        allowlist = _allowed_logins_from_env()

        accounts = (
            await session.scalars(
                select(YandexAccount)
                .where(YandexAccount.status == "active")
                .order_by(YandexAccount.id.asc())
            )
        ).all()

        if not accounts:
            return warnings, debug_dirs

        now = utcnow()

        for acc in accounts:
            if not acc.credentials_ref:
                continue

            storage_path = self._account_state_path(acc)

            try:
                snap = await self.provider.probe(storage_state_path=storage_path)
                if snap.raw_debug and snap.raw_debug.get("debug_dir"):
                    debug_dirs.append(str(snap.raw_debug.get("debug_dir")))
            except Exception:
                continue

            fam = snap.family
            if not fam:
                continue

            fam_admins = {x.lower() for x in (fam.admins or [])}
            fam_guests = {x.lower() for x in (fam.guests or [])}

            # allowed = админы + active-members + allowlist
            active_members = (
                await session.scalars(
                    select(YandexMembership).where(
                        YandexMembership.yandex_account_id == acc.id,
                        YandexMembership.status == "active",
                    )
                )
            ).all()

            allowed = {m.yandex_login.strip().lstrip("@").lower() for m in active_members if m.yandex_login}
            allowed |= fam_admins
            allowed |= allowlist

            foreign = sorted([g for g in fam_guests if g not in allowed])
            if not foreign:
                continue

            # виновник только если есть ровно один awaiting_join
            awaiting = (
                await session.scalars(
                    select(YandexMembership).where(
                        YandexMembership.yandex_account_id == acc.id,
                        YandexMembership.status == "awaiting_join",
                    )
                )
            ).all()
            culprit = awaiting[0] if len(awaiting) == 1 else None

            # кикаем чужих (кроме allowlist)
            kicked = []
            for guest_login in foreign:
                if guest_login in allowlist:
                    continue
                try:
                    ok = await self.provider.remove_guest(storage_state_path=storage_path, guest_login=guest_login)
                    if ok:
                        kicked.append(guest_login)
                except Exception:
                    pass

            if not kicked:
                continue

            # OWNER не наказываем
            if culprit and int(culprit.tg_id) == owner_id:
                warnings.append(
                    (
                        owner_id,
                        "ℹ️ Обнаружены лишние логины в семье.\n\n"
                        f"Удалены: {', '.join(kicked)}\n\n"
                        "⚠️ Это только уведомление. Strikes владельцу НЕ выдаются.",
                    )
                )
                continue

            if culprit:
                culprit.abuse_strikes = int(culprit.abuse_strikes or 0) + 1
                culprit.updated_at = now

                if culprit.abuse_strikes >= 2:
                    culprit.reinvite_used = 1
                    culprit.status = "invite_timeout"
                    culprit.invite_link = None
                    culprit.invite_issued_at = None
                    culprit.invite_expires_at = None

                msg = (
                    "⚠️ Обнаружена попытка подключения по вашей ссылке другого аккаунта.\n\n"
                    f"Удалены лишние логины: {', '.join(kicked)}\n\n"
                    f"Strikes: {culprit.abuse_strikes}/2\n"
                )
                if culprit.abuse_strikes >= 2:
                    msg += "\n🚫 Повторное приглашение заблокировано. Напишите в поддержку."
                else:
                    msg += "\nИспользуйте приглашение только для вашего логина."

                warnings.append((culprit.tg_id, msg))

        return warnings, debug_dirs


yandex_service = YandexService()
