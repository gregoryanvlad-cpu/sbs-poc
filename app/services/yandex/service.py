from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Tuple

from sqlalchemy import select

from app.core.config import settings
from app.db.models.subscription import Subscription
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_membership import YandexMembership
from app.repo import utcnow
from app.services.yandex.provider import build_provider

INVITE_TTL_MINUTES = 15


def _plus_ok_for_invite(acc: YandexAccount) -> bool:
    """Account can be used for inviting only if Plus remains active long enough."""
    if not acc.plus_end_at:
        return False

    min_days = int(getattr(settings, "yandex_invite_min_remaining_days", 30))

    # If min_days <= 0, treat as disabled check (allow any account with plus_end_at)
    if min_days <= 0:
        return True

    return acc.plus_end_at >= (datetime.now(timezone.utc) + timedelta(days=min_days))


async def _select_account_for_invite(session) -> YandexAccount:
    """
    Pick an account that:
    - active
    - has credentials
    - Plus end_at >= now + min_days (AFTER live probe refresh)
    - has free slots (based on live probe)

    We probe accounts ONLY at invite time (not continuously).
    """
    accounts = (
        await session.scalars(
            select(YandexAccount)
            .where(YandexAccount.status == "active")
            .order_by(YandexAccount.used_slots.asc(), YandexAccount.id.asc())
        )
    ).all()

    if not accounts:
        raise RuntimeError("No active YandexAccount")

    # only those with cookies
    accounts = [a for a in accounts if a.credentials_ref]
    if not accounts:
        raise RuntimeError("No active YandexAccount with cookies")

    provider = build_provider()

    # 1) First: probe every account once to refresh plus_end_at + slots
    probed: list[tuple[YandexAccount, int]] = []
    for acc in accounts:
        storage_path = f"{settings.yandex_cookies_dir}/{acc.credentials_ref}"

        try:
            snap = await provider.probe(storage_state_path=storage_path)
        except Exception:
            continue

        fam = getattr(snap, "family", None)
        if not fam:
            continue

        # refresh plus_end_at from probe
        dt = getattr(snap, "plus_end_at", None)
        if dt:
            acc.plus_end_at = dt

        # refresh used_slots from probe
        try:
            acc.used_slots = int(getattr(fam, "used_slots", acc.used_slots or 0) or 0)
        except Exception:
            pass

        free_slots = int(getattr(fam, "free_slots", 0) or 0)
        probed.append((acc, free_slots))

    if not probed:
        raise RuntimeError("No eligible Yandex accounts (probe failed or family not found)")

    # 2) Second: apply lifetime filter and pick first with free slot
    lifetime_ok_found = False
    for acc, free_slots in probed:
        if not _plus_ok_for_invite(acc):
            continue
        lifetime_ok_found = True
        if free_slots > 0:
            return acc

    if not lifetime_ok_found:
        raise RuntimeError("No YandexAccount with enough Plus lifetime")

    raise RuntimeError("No free slots on eligible Yandex accounts")


def _norm_login(x: str | None) -> str:
    return (x or "").strip().lstrip("@").lower()


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

        account = await _select_account_for_invite(session)

        invite_link: str | None = None
        try:
            invite_link = await self.provider.create_invite_link(
                storage_state_path=self._account_state_path(account)
            )
        except Exception:
            invite_link = None

        now = utcnow()
        membership = YandexMembership(
            tg_id=tg_id,
            yandex_account_id=account.id,
            yandex_login=_norm_login(yandex_login),
            invite_link=invite_link,
            invite_issued_at=now if invite_link else None,
            invite_expires_at=(now + timedelta(minutes=INVITE_TTL_MINUTES)) if invite_link else None,
            status="awaiting_join" if invite_link else "pending",
            reinvite_used=0,
            abuse_strikes=0,
        )

        session.add(membership)
        await session.flush()
        return membership

    async def issue_or_reissue_invite(
        self,
        *,
        session,
        membership: YandexMembership,
        count_as_reinvite: bool,
    ) -> YandexMembership:
        now = utcnow()

        async def _try_reuse_previous_account() -> YandexAccount | None:
            if not membership.yandex_account_id:
                return None
            prev = await session.get(YandexAccount, membership.yandex_account_id)
            if not prev or not prev.credentials_ref:
                return None

            if prev.status != "active":
                return None

            # IMPORTANT: refresh prev.plus_end_at from probe before checking
            storage_path = self._account_state_path(prev)
            try:
                snap = await self.provider.probe(storage_state_path=storage_path)
                dt = getattr(snap, "plus_end_at", None)
                if dt:
                    prev.plus_end_at = dt
            except Exception:
                return None

            if not _plus_ok_for_invite(prev):
                return None

            # Cancel pending invite first (best-effort) to free the waiting slot.
            if membership.status in ("awaiting_join", "pending"):
                try:
                    await self.provider.cancel_pending_invite(storage_state_path=storage_path)
                except Exception:
                    pass

            # Re-probe to confirm we still have a free slot (and update counters best-effort).
            try:
                snap2 = await self.provider.probe(storage_state_path=storage_path)
                fam2 = snap2.family
                if fam2:
                    try:
                        prev.used_slots = int(fam2.used_slots)
                    except Exception:
                        pass
                    if int(getattr(fam2, "free_slots", 0) or 0) > 0:
                        return prev
            except Exception:
                return None

            return None

        acc = await _try_reuse_previous_account()
        if not acc:
            acc = await _select_account_for_invite(session)

        invite_link = await self.provider.create_invite_link(
            storage_state_path=self._account_state_path(acc)
        )

        membership.yandex_account_id = acc.id
        membership.invite_link = invite_link
        membership.invite_issued_at = now
        membership.invite_expires_at = now + timedelta(minutes=INVITE_TTL_MINUTES)
        membership.status = "awaiting_join"

        if count_as_reinvite:
            membership.reinvite_used = int(membership.reinvite_used or 0) + 1

        membership.updated_at = now
        await session.flush()
        return membership

    async def remove_user_from_family_if_needed(self, *, session, tg_id: int) -> bool:
        m = await session.scalar(
            select(YandexMembership)
            .where(
                YandexMembership.tg_id == tg_id,
                YandexMembership.status == "active",
                YandexMembership.yandex_account_id.is_not(None),
            )
            .order_by(YandexMembership.id.desc())
            .limit(1)
        )
        if not m or not m.yandex_account_id:
            return False

        acc = await session.get(YandexAccount, m.yandex_account_id)
        if not acc or not acc.credentials_ref:
            m.status = "removed"
            m.updated_at = utcnow()
            return True

        try:
            await self.provider.remove_guest(
                storage_state_path=self._account_state_path(acc),
                guest_login=_norm_login(m.yandex_login),
            )
        except Exception:
            return True

        m.status = "removed"
        m.updated_at = utcnow()
        return True

    async def issue_missing_invites(self, session) -> List[YandexMembership]:
        now = utcnow()
        q = (
            select(YandexMembership)
            .join(Subscription, Subscription.tg_id == YandexMembership.tg_id)
            .where(
                YandexMembership.status == "pending",
                YandexMembership.invite_link.is_(None),
                YandexMembership.yandex_login.is_not(None),
                Subscription.end_at.is_not(None),
                Subscription.end_at > now,
            )
            .order_by(YandexMembership.id.asc())
            .limit(50)
        )
        items = (await session.scalars(q)).all()
        issued: List[YandexMembership] = []
        for m in items:
            try:
                await self.issue_or_reissue_invite(
                    session=session,
                    membership=m,
                    count_as_reinvite=False,
                )
                issued.append(m)
            except Exception:
                continue
        return issued

    async def issue_invites_for_reactivated_users(self, session) -> List[YandexMembership]:
        now = utcnow()
        q = (
            select(YandexMembership)
            .join(Subscription, Subscription.tg_id == YandexMembership.tg_id)
            .where(
                YandexMembership.status == "removed",
                Subscription.end_at.is_not(None),
                Subscription.end_at > now,
            )
            .order_by(YandexMembership.id.asc())
            .limit(50)
        )
        items = (await session.scalars(q)).all()
        issued: List[YandexMembership] = []
        for m in items:
            try:
                await self.issue_or_reissue_invite(
                    session=session,
                    membership=m,
                    count_as_reinvite=False,
                )
                issued.append(m)
            except Exception:
                continue
        return issued

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

            # refresh plus_end_at best-effort here too
            try:
                dt = getattr(snap, "plus_end_at", None)
                if dt:
                    acc.plus_end_at = dt
            except Exception:
                pass

            try:
                acc.used_slots = int(fam.used_slots)
            except Exception:
                pass

            fam_admins = {_norm_login(x) for x in (fam.admins or [])}
            fam_guests = {_norm_login(x) for x in (fam.guests or [])}

            pending_memberships = (
                await session.scalars(
                    select(YandexMembership).where(
                        YandexMembership.yandex_account_id == acc.id,
                        YandexMembership.status == "awaiting_join",
                    )
                )
            ).all()

            for m in pending_memberships:
                login = _norm_login(m.yandex_login)
                if not login:
                    continue

                if login in fam_guests or login in fam_admins:
                    m.status = "active"
                    m.invite_link = None
                    m.invite_issued_at = None
                    m.invite_expires_at = None
                    m.updated_at = now
                    activated.append(m.tg_id)

            active_memberships = (
                await session.scalars(
                    select(YandexMembership).where(
                        YandexMembership.yandex_account_id == acc.id,
                        YandexMembership.status == "active",
                    )
                )
            ).all()

            for m in active_memberships:
                login = _norm_login(m.yandex_login)
                if not login:
                    continue
                if login not in fam_guests and login not in fam_admins:
                    m.status = "removed"
                    m.updated_at = now

        return activated, debug_dirs

    async def enforce_no_foreign_logins(self, session) -> Tuple[List[tuple[int, str]], List[str]]:
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

            fam_admins = {_norm_login(x) for x in (fam.admins or [])}
            fam_guests = {_norm_login(x) for x in (fam.guests or [])}

            active_members = (
                await session.scalars(
                    select(YandexMembership).where(
                        YandexMembership.yandex_account_id == acc.id,
                        YandexMembership.status == "active",
                    )
                )
            ).all()

            active_present = set()
            for m in active_members:
                login = _norm_login(m.yandex_login)
                if login and (login in fam_guests or login in fam_admins):
                    active_present.add(login)

            allowed = set(fam_admins) | set(allowlist) | active_present

            foreign = sorted([g for g in fam_guests if g and g not in allowed])
            if not foreign:
                continue

            awaiting = (
                await session.scalars(
                    select(YandexMembership).where(
                        YandexMembership.yandex_account_id == acc.id,
                        YandexMembership.status == "awaiting_join",
                    )
                )
            ).all()
            culprit = awaiting[0] if len(awaiting) == 1 else None

            kicked: list[str] = []
            for guest_login in foreign:
                if guest_login in allowlist:
                    continue
                try:
                    ok = await self.provider.remove_guest(
                        storage_state_path=storage_path,
                        guest_login=guest_login,
                    )
                    if ok:
                        kicked.append(guest_login)
                except Exception:
                    pass

            if not kicked:
                continue

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
