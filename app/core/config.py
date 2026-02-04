import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def make_async_db_url(url: str) -> str:
    """Accepts Railway-style DATABASE_URL and returns sqlalchemy async url."""
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    raise RuntimeError("Unsupported DATABASE_URL format")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    bot_username: str | None
    database_url: str
    scheduler_enabled: bool
    auto_delete_seconds: int

    # owner (admin access)
    owner_tg_id: int

    # business defaults
    price_rub: int = 299
    period_months: int = 1
    period_days: int = 30  # legacy compatibility (payments.period_days is NOT NULL)

    # VPN (still mock by default)
    vpn_mode: str = "mock"
    vpn_endpoint: str = "1.2.3.4:51820"
    vpn_server_public_key: str = "REPLACE_ME"
    vpn_allowed_ips: str = "0.0.0.0/0, ::/0"
    vpn_dns: str = "1.1.1.1,8.8.8.8"

    # Yandex
    yandex_enabled: bool = True
    yandex_worker_period_seconds: int = 10
    yandex_pending_ttl_seconds: int = 600  # 10 минут
    # Only use accounts for inviting if their Plus remains active for at least this many days.
    yandex_invite_min_remaining_days: int = 30
    yandex_reinvite_max: int = 1
    yandex_max_strikes: int = 2
    yandex_provider: str = "mock"  # mock | playwright (позже)

    # where to store Playwright storage_state json files
    yandex_cookies_dir: str = "/data/yandex"

    # Referrals
    referral_hold_days: int = 7
    referral_min_payout_rub: int = 50


def _load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is missing")

    database_url_raw = os.getenv("DATABASE_URL", "").strip()
    if not database_url_raw:
        raise RuntimeError("DATABASE_URL is missing")

    owner_raw = os.getenv("OWNER_TG_ID", "").strip()
    if not owner_raw.isdigit():
        raise RuntimeError("OWNER_TG_ID is missing or invalid (must be digits)")
    owner_tg_id = int(owner_raw)

    return Settings(
        bot_token=bot_token,
        bot_username=(os.getenv("BOT_USERNAME") or "").strip() or None,
        database_url=make_async_db_url(database_url_raw),
        scheduler_enabled=_env_bool("SCHEDULER_ENABLED", True),
        auto_delete_seconds=int(os.getenv("AUTO_DELETE_SECONDS", "60")),
        owner_tg_id=owner_tg_id,
        vpn_mode=os.getenv("VPN_MODE", "mock").strip().lower(),
        vpn_endpoint=os.getenv("VPN_ENDPOINT", "1.2.3.4:51820").strip(),
        vpn_server_public_key=os.getenv("VPN_SERVER_PUBLIC_KEY", "REPLACE_ME").strip(),
        vpn_allowed_ips=os.getenv("VPN_ALLOWED_IPS", "0.0.0.0/0, ::/0").strip(),
        vpn_dns=os.getenv("VPN_DNS", "1.1.1.1,8.8.8.8").strip(),
        # Yandex
        yandex_enabled=_env_bool("YANDEX_ENABLED", True),
        yandex_worker_period_seconds=int(os.getenv("YANDEX_WORKER_PERIOD_SECONDS", "10")),
        yandex_pending_ttl_seconds=int(os.getenv("YANDEX_PENDING_TTL_SECONDS", "600")),
        yandex_invite_min_remaining_days=int(os.getenv("YANDEX_INVITE_MIN_REMAINING_DAYS", "30")),
        yandex_reinvite_max=int(os.getenv("YANDEX_REINVITE_MAX", "1")),
        yandex_max_strikes=int(os.getenv("YANDEX_MAX_STRIKES", "2")),
        yandex_provider=os.getenv("YANDEX_PROVIDER", "mock").strip().lower(),
        yandex_cookies_dir=os.getenv("YANDEX_COOKIES_DIR", "/data/yandex").strip(),

        # Referrals
        referral_hold_days=int(os.getenv("REFERRAL_HOLD_DAYS", "7")),
        referral_min_payout_rub=int(os.getenv("REFERRAL_MIN_PAYOUT_RUB", "50")),
    )


settings = _load_settings()
