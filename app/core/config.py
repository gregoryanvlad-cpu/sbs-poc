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
    database_url: str
    scheduler_enabled: bool
    auto_delete_seconds: int

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


def _load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is missing")

    database_url_raw = os.getenv("DATABASE_URL", "").strip()
    if not database_url_raw:
        raise RuntimeError("DATABASE_URL is missing")

    return Settings(
        bot_token=bot_token,
        database_url=make_async_db_url(database_url_raw),
        scheduler_enabled=_env_bool("SCHEDULER_ENABLED", True),
        auto_delete_seconds=int(os.getenv("AUTO_DELETE_SECONDS", "60")),
        vpn_mode=os.getenv("VPN_MODE", "mock").strip().lower(),
        vpn_endpoint=os.getenv("VPN_ENDPOINT", "1.2.3.4:51820").strip(),
        vpn_server_public_key=os.getenv("VPN_SERVER_PUBLIC_KEY", "REPLACE_ME").strip(),
        vpn_allowed_ips=os.getenv("VPN_ALLOWED_IPS", "0.0.0.0/0, ::/0").strip(),
        vpn_dns=os.getenv("VPN_DNS", "1.1.1.1,8.8.8.8").strip(),
    )


settings = _load_settings()
