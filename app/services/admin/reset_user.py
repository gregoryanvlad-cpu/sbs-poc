from __future__ import annotations

import json
import logging
import os

from sqlalchemy import delete, select, update

from app.db.session import session_scope
from app.db.models.user import User
from app.db.models.subscription import Subscription
from app.db.models.payment import Payment
from app.db.models.vpn_peer import VpnPeer
from app.db.models.yandex_invite_slot import YandexInviteSlot
from app.db.models.yandex_membership import YandexMembership
from app.db.models.family_vpn_group import FamilyVpnGroup
from app.db.models.family_vpn_profile import FamilyVpnProfile
from app.db.models.lte_vpn_client import LteVpnClient
from app.db.models.region_vpn_session import RegionVpnSession
from app.db.models.content_request import ContentRequest
from app.db.models.referral import Referral
from app.db.models.referral_earning import ReferralEarning
from app.db.models.payout_request import PayoutRequest
from app.db.models.app_setting import AppSetting
from app.services.vpn.service import vpn_service
from app.services.vpn.ssh_provider import WireGuardSSHProvider
from app.services.lte_vpn.service import lte_vpn_service
from app.services.regionvpn.service import RegionVpnService
from app.core.config import settings

log = logging.getLogger(__name__)


def _load_vpn_servers_reset() -> list[dict]:
    raw = os.environ.get("VPN_SERVERS_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("servers"), list):
            data = data["servers"]
        if isinstance(data, list):
            return [dict(x) for x in data if isinstance(x, dict)]
    except Exception:
        log.exception("admin_reset_user_load_servers_failed")
    return []


def _server_aliases(code: str) -> set[str]:
    code_u = str(code or "").strip().upper()
    if not code_u:
        return set()
    aliases = {code_u, code_u.replace(" ", "")}
    if code_u in {"NL", "NL1", "SERVER1", "SERVER #1"}:
        aliases.update({"NL", "NL1", "SERVER1", "SERVER #1"})
    if code_u in {"NL2", "SERVER2", "SERVER #2"}:
        aliases.update({"NL2", "SERVER2", "SERVER #2"})
    return {a for a in aliases if a}


def _provider_candidates_for_peer(peer: VpnPeer) -> list[WireGuardSSHProvider]:
    providers: list[WireGuardSSHProvider] = []
    seen: set[tuple[str, int, str, str]] = set()

    def _add(host: str, port: int, user: str, password: str | None, interface: str) -> None:
        key = (host, int(port), user, interface)
        if key in seen or not host or not user:
            return
        seen.add(key)
        providers.append(WireGuardSSHProvider(host=host, port=int(port), user=user, password=password, interface=interface))

    # Current/default server provider.
    try:
        _add(
            host=os.environ.get("WG_SSH_HOST", ""),
            port=int(os.environ.get("WG_SSH_PORT", "22") or 22),
            user=os.environ.get("WG_SSH_USER", ""),
            password=os.environ.get("WG_SSH_PASSWORD") or None,
            interface=os.environ.get("VPN_INTERFACE", "wg0"),
        )
    except Exception:
        pass

    peer_code = str(getattr(peer, "server_code", "") or "").upper()
    aliases = _server_aliases(peer_code)
    for srv in _load_vpn_servers_reset():
        code = str(srv.get("code") or "").strip().upper()
        if aliases and code not in aliases:
            continue
        try:
            _add(
                host=str(srv.get("host") or ""),
                port=int(srv.get("port") or 22),
                user=str(srv.get("user") or ""),
                password=srv.get("password") or None,
                interface=str(srv.get("interface") or os.environ.get("VPN_INTERFACE", "wg0")),
            )
        except Exception:
            log.exception("admin_reset_user_bad_server_config peer_code=%s", peer_code)

    return providers


class AdminResetUserService:
    """
    Полный сброс пользователя для тестов:
    - удаляем Subscription
    - удаляем Payment
    - удаляем VpnPeer
    - удаляем YandexMembership
    - сбрасываем flow_state/flow_data у User (или удаляем User, если хочешь — но безопаснее сброс)
    """

    async def reset_user(self, *, tg_id: int) -> None:
        async with session_scope() as session:
            # 0) best-effort remote cleanup for LTE / RegionVPN before DB reset
            try:
                await lte_vpn_service.disable_remote_client(tg_id)
            except Exception:
                log.exception("admin_reset_user_disable_lte_failed tg_id=%s", tg_id)
            try:
                if getattr(settings, "region_enabled", False):
                    region_service = RegionVpnService(
                        ssh_host=settings.region_ssh_host,
                        ssh_port=settings.region_ssh_port,
                        ssh_user=settings.region_ssh_user,
                        ssh_password=settings.region_ssh_password,
                        xray_config_path=settings.region_xray_config_path,
                        xray_api_port=settings.region_xray_api_port,
                        max_clients=settings.region_max_clients,
                    )
                    await region_service.revoke_client(tg_id)
            except Exception:
                log.exception("admin_reset_user_revoke_region_failed tg_id=%s", tg_id)

            # 1) удаляем yandex_membership по tg_id (ВАЖНО: НЕ user_id)
            await session.execute(delete(YandexMembership).where(YandexMembership.tg_id == tg_id))

            # 1.1) В ручном режиме слоты не переиспользуются (S1), но после "reset" мы должны
            # убрать привязку слота к пользователю, чтобы в ЛК больше не отображались "семья/слот".
            # Сам слот остаётся issued/burned (мы его не возвращаем в free).
            await session.execute(
                update(YandexInviteSlot)
                .where(YandexInviteSlot.issued_to_tg_id == tg_id)
                .values(
                    issued_to_tg_id=None,
                    issued_at=None,
                )
            )
            # 1.2) чистим семейную VPN-группу и все её профили/peers
            family_peer_ids: list[int] = []
            try:
                fam_res = await session.execute(select(FamilyVpnProfile).where(FamilyVpnProfile.owner_tg_id == tg_id))
                fam_profiles = list(fam_res.scalars().all())
                family_peer_ids = [int(p.vpn_peer_id) for p in fam_profiles if getattr(p, "vpn_peer_id", None)]
            except Exception:
                log.exception("admin_reset_user_list_family_profiles_failed tg_id=%s", tg_id)

            # 2) блокируем VPN peers на сервере (best-effort), затем удаляем из БД
            try:
                peer_query = select(VpnPeer).where(VpnPeer.tg_id == tg_id)
                if family_peer_ids:
                    peer_query = select(VpnPeer).where((VpnPeer.tg_id == tg_id) | (VpnPeer.id.in_(family_peer_ids)))
                res = await session.execute(peer_query)
                peers = list(res.scalars().all())
                for p in peers:
                    removed = False
                    for provider in _provider_candidates_for_peer(p):
                        try:
                            await provider.remove_peer(p.client_public_key)
                            removed = True
                            break
                        except Exception:
                            log.exception(
                                "admin_reset_user_remove_peer_failed tg_id=%s peer_id=%s host=%s",
                                tg_id,
                                getattr(p, "id", None),
                                getattr(provider, "host", None),
                            )
                    if not removed:
                        log.warning(
                            "admin_reset_user_peer_not_removed_anywhere tg_id=%s peer_id=%s pub=%s server=%s",
                            tg_id,
                            getattr(p, "id", None),
                            getattr(p, "client_public_key", None),
                            getattr(p, "server_code", None),
                        )
            except Exception:
                log.exception("admin_reset_user_list_peers_failed tg_id=%s", tg_id)

            # 2) удаляем vpn peers пользователя + family peers
            await session.execute(delete(VpnPeer).where(VpnPeer.tg_id == tg_id))
            if family_peer_ids:
                await session.execute(delete(VpnPeer).where(VpnPeer.id.in_(family_peer_ids)))
            await session.execute(delete(FamilyVpnProfile).where(FamilyVpnProfile.owner_tg_id == tg_id))
            await session.execute(delete(FamilyVpnGroup).where(FamilyVpnGroup.owner_tg_id == tg_id))
            await session.execute(delete(LteVpnClient).where(LteVpnClient.tg_id == tg_id))
            await session.execute(delete(RegionVpnSession).where(RegionVpnSession.tg_id == tg_id))
            await session.execute(delete(ContentRequest).where(ContentRequest.user_id == tg_id))
            await session.execute(delete(PayoutRequest).where(PayoutRequest.tg_id == tg_id))
            await session.execute(delete(ReferralEarning).where((ReferralEarning.referrer_tg_id == tg_id) | (ReferralEarning.referred_tg_id == tg_id)))
            await session.execute(delete(Referral).where((Referral.referrer_tg_id == tg_id) | (Referral.referred_tg_id == tg_id)))
            for key in [
                f"family_seat_price_override:{tg_id}",
                f"trial_used:{tg_id}",
                f"ua:actions_total:{tg_id}",
                f"ua:messages_in:{tg_id}",
                f"ua:clicks_in:{tg_id}",
                f"ua:last_interaction_ts:{tg_id}",
            ]:
                await session.execute(delete(AppSetting).where(AppSetting.key == key))

            # 3) удаляем платежи
            await session.execute(
                delete(Payment).where(Payment.tg_id == tg_id)
            )

            # 4) сбрасываем подписку ЖЁСТКО:
            #    - удаляем все записи subscriptions по tg_id (на случай дублей из старых миграций/ручных вставок)
            #    - создаём "чистую" неактивную подписку
            await session.execute(delete(Subscription).where(Subscription.tg_id == tg_id))

            sub = Subscription(
                tg_id=tg_id,
                start_at=None,
                end_at=None,
                is_active=False,
                status="inactive",
            )
            session.add(sub)
            await session.flush()

            # 5) сбрасываем пользователя (не удаляем строку, чтобы не ломать связи/логику)
            user = await session.get(User, tg_id)
            if user:
                # reset referral click info (if present)
                if hasattr(user, "referred_by_tg_id"):
                    setattr(user, "referred_by_tg_id", None)
                if hasattr(user, "referred_at"):
                    setattr(user, "referred_at", None)

                user.flow_state = None
                user.flow_data = None

                # если у тебя есть поля, которые фиксируют яндекс-логин/статус в User — тоже сбрось:
                if hasattr(user, "yandex_login"):
                    setattr(user, "yandex_login", None)
                if hasattr(user, "yandex_status"):
                    setattr(user, "yandex_status", None)

            await session.commit()

        log.info("admin_reset_user_done tg_id=%s", tg_id)
