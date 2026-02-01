from __future__ import annotations

from datetime import timedelta
from datetime import datetime, timezone
from typing import List, Tuple

from sqlalchemy import select

from app.core.config import settings
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_membership import YandexMembership
from app.db.models.subscription import Subscription
from app.repo import utcnow
from app.services.yandex.provider import build_provider

INVITE_TTL_MINUTES = 15


def _plus_ok_for_invite(acc: YandexAccount) -> bool:
    """Account can be used for inviting only if Plus remains active long enough."""
    if not acc.plus_end_at:
        return False
    min_days = int(getattr(settings, "yandex_invite_min_remaining_days", 0) or 30)
    return acc.plus_end_at >= (datetime.now(timezone.utc) + timedelta(days=min_days))


async def _select_account_for_invite(session) -> YandexAccount:
    """Pick an account that:
    - active
    - has credentials
    - Plus end_at >= now + min_days
    - has free slots (based on live probe)

    We probe accounts only at invite time (not continuously).
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

    # First, filter by Plus lifetime and cookies.
    candidates = [a for a in accounts if a.credentials_ref and _plus_ok_for_invite(a)]
    if not candidates:
        raise RuntimeError("No YandexAccount with enough Plus lifetime")

    # Probe candidates until we find a free slot.
    for acc in candidates:
        storage_path = f"{settings.yandex_cookies_dir}/{acc.credentials_ref}"
        snap = await build_provider().probe(storage_state_path=storage_path)
        fam = snap.family
        if not fam:
            continue
        # Keep DB counters best-effort.
        try:
            acc.used_slots = int(fam.used_slots)
        except Exception:
            pass
        if int(getattr(fam, "free_slots", 0) or 0) > 0:
            return acc

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

        # Pick account only when we really need to invite.
        account = await _select_account_for_invite(session)

        invite_link: str | None = None
        try:
            invite_link = await self.provider.create_invite_link(
                storage_state_path=self._account_state_path(account)
            )
        except Exception:
            # Don't fail user-flow hard: create membership without link.
            # Scheduler / user re-open can retry later.
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
        """(Re)issue invite for an existing membership.

        Used for:
        - reinvite button (count_as_reinvite=True)
        - auto reinvite after removal when subscription re-activated (count_as_reinvite=False)

        Always cancels any pending invite on previous account to free the slot.
        """
        now = utcnow()

        async def _try_reuse_previous_account() -> YandexAccount | None:
            """Try to re-issue invite on the same account (allowed).

            We can reuse the previous account if:
            - it exists and has cookies
            - Plus lifetime is sufficient (>= min days)
            - after cancelling pending invite, the account still has a free slot (live probe)
            """
            if not membership.yandex_account_id:
                return None
            prev = await session.get(YandexAccount, membership.yandex_account_id)
            if not prev or not prev.credentials_ref:
                return None
            if prev.status != "active" or not _plus_ok_for_invite(prev):
                return None

            storage_path = self._account_state_path(prev)

            # Cancel pending invite first (best-effort) to free the waiting slot.
            if membership.status in ("awaiting_join", "pending"):
                try:
                    await self.provider.cancel_pending_invite(storage_state_path=storage_path)
                except Exception:
                    pass

            # Re-probe to confirm we still have a free slot (and update counters best-effort).
            try:
                snap = await self.provider.probe(storage_state_path=storage_path)
                fam = snap.family
                if fam:
                    try:
                        prev.used_slots = int(fam.used_slots)
                    except Exception:
                        pass
                    if int(getattr(fam, "free_slots", 0) or 0) > 0:
                        return prev
            except Exception:
                # If probe fails, fall back to selecting a new account.
                return None
            return None

        # Prefer re-issuing on the same account if it still has a free slot.
        acc = await _try_reuse_previous_account()
        if not acc:
            # Pick a fresh eligible account with free slot.
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
        """Remove user from Yandex family when service subscription expires.

        Returns True if a membership was found and removal attempted.
        """
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
            # can't operate; still mark as removed logically
            m.status = "removed"
            m.updated_at = utcnow()
            return True

        try:
            await self.provider.remove_guest(
                storage_state_path=self._account_state_path(acc),
                guest_login=_norm_login(m.yandex_login),
            )
        except Exception:
            # best-effort: keep it active; scheduler can retry later if needed
            return True

        m.status = "removed"
        m.updated_at = utcnow()
        return True

    async def issue_missing_invites(self, session) -> List[YandexMembership]:
        """Issue invites for memberships that were created but have no invite_link yet.

        We only do this for users with an active subscription (end_at > now).
        This keeps user UX clean: they don't see internal errors, they just receive the invite when ready.
        """
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
                # keep it pending; we'll retry later
                continue
        return issued

    async def issue_invites_for_reactivated_users(self, session) -> List[YandexMembership]:
        """If user has active subscription but is not in family (removed), issue a new invite."""
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
        """
        1) awaiting_join -> active (если логин появился в семье)
        2) active -> removed (если активный логин ПРОПАЛ из семьи)
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

            try:
                acc.used_slots = int(fam.used_slots)
            except Exception:
                pass

            fam_admins = {_norm_login(x) for x in (fam.admins or [])}
            fam_guests = {_norm_login(x) for x in (fam.guests or [])}

            # awaiting_join -> active
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

            # active -> removed (если исчез из семьи)
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
                    # его реально нет в семье сейчас -> считаем удалённым
                    m.status = "removed"
                    m.updated_at = now

        return activated, debug_dirs

    async def enforce_no_foreign_logins(self, session) -> Tuple[List[tuple[int, str]], List[str]]:
        """
        Автоматика "левые логины":
        - берём состав семьи
        - строим allowed так, чтобы НЕ пропускать "устаревших active"
          (active считаем allowed только если он реально сейчас в семье)
        - кикаем всех гостей НЕ в allowed
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

            fam_admins = {_norm_login(x) for x in (fam.admins or [])}
            fam_guests = {_norm_login(x) for x in (fam.guests or [])}

            # Берём active-members из БД, но разрешаем ТОЛЬКО тех, кто реально в семье сейчас
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
