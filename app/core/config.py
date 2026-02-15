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
    # username of the secondary "player" bot (used by Bot1 to build deep links)
    player_bot_username: str
    database_url: str
    scheduler_enabled: bool
    auto_delete_seconds: int

    # owner (admin access)
    owner_tg_id: int

    # business defaults
    price_rub: int = 299
    period_months: int = 1
    period_days: int = 30  # legacy compatibility (payments.period_days is NOT NULL)

    # Payments
    # mock: instantly extends subscription (dev/test)
    # platega: uses Platega API to create a payment link and then checks payment status
    payment_provider: str = "mock"  # mock | platega
    platega_merchant_id: str | None = None
    platega_secret: str | None = None
    platega_payment_method: int = 2
    platega_return_url: str = "https://example.com/success"
    platega_failed_url: str = "https://example.com/fail"

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

    # PoiskKino (ex-openmoviedb)
    poiskkino_base_url: str = "https://api.poiskkino.dev"
    # IMPORTANT: keep secret out of repo; pass via env POISKKINO_API_KEY
    poiskkino_api_key: str | None = None

    # HdRezka (search & карточки)
    rezka_origin: str = "https://hdrezka.ag"

    # Deep-link token TTL (Bot1 -> Bot2)
    content_request_ttl_seconds: int = 900

    # Bot2 (player) settings (used only by main_player.py)
    main_bot_username: str = "sbsconnect_bot"
    player_whitelist_domains: tuple[str, ...] = ("youtube.com", "youtu.be")
    player_rate_limit_per_minute: int = 15

    # --- VPN-Region (VLESS+Reality via Xray) ---
    # NOTE: VLESS/Reality link parameters are read directly from env in regionvpn.service.
    # Here we keep only SSH + Xray control parameters used by the bot.
    region_ssh_host: str = ""
    region_ssh_port: int = 22
    region_ssh_user: str = "root"
    region_ssh_password: str | None = None
    region_xray_config_path: str = "/usr/local/etc/xray/config.json"
    region_xray_api_port: int = 10085
    region_max_clients: int = 40
    region_quota_gb: int = 0  # 0 = no quota enforcement


def _load_settings() -> Settings:
    # Bot1 uses BOT_TOKEN, Bot2 can use PLAYER_BOT_TOKEN.
    bot_token = (os.getenv("BOT_TOKEN") or os.getenv("PLAYER_BOT_TOKEN") or "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is missing (or set PLAYER_BOT_TOKEN for the player bot)")

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
        player_bot_username=(os.getenv("PLAYER_BOT_USERNAME") or "inoteka_secure_bot").strip(),
        database_url=make_async_db_url(database_url_raw),
        scheduler_enabled=_env_bool("SCHEDULER_ENABLED", True),
        auto_delete_seconds=int(os.getenv("AUTO_DELETE_SECONDS", "60")),
        owner_tg_id=owner_tg_id,

        # Payments
        payment_provider=os.getenv("PAYMENT_PROVIDER", "mock").strip().lower(),
        platega_merchant_id=(os.getenv("PLATEGA_MERCHANT_ID") or "").strip() or None,
        platega_secret=(os.getenv("PLATEGA_SECRET") or "").strip() or None,
        platega_payment_method=int(os.getenv("PLATEGA_PAYMENT_METHOD", "2")),
        platega_return_url=os.getenv("PLATEGA_RETURN_URL", "https://example.com/success").strip(),
        platega_failed_url=os.getenv("PLATEGA_FAILED_URL", "https://example.com/fail").strip(),
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

        # PoiskKino
        poiskkino_base_url=os.getenv("POISKKINO_BASE_URL", "https://api.poiskkino.dev").strip(),
        poiskkino_api_key=(os.getenv("POISKKINO_API_KEY") or "").strip() or None,
        # Rezka
        rezka_origin=os.getenv("REZKA_ORIGIN", "https://hdrezka.ag").strip(),

        # Player bot
        content_request_ttl_seconds=int(os.getenv("CONTENT_REQUEST_TTL_SECONDS", "900")),
        main_bot_username=(os.getenv("MAIN_BOT_USERNAME") or "sbsconnect_bot").strip(),
        player_whitelist_domains=tuple(
            d.strip().lower()
            for d in (os.getenv("PLAYER_WHITELIST_DOMAINS") or "youtube.com,youtu.be").split(",")
            if d.strip()
        ),
        player_rate_limit_per_minute=int(os.getenv("PLAYER_RATE_LIMIT_PER_MINUTE", "15")),

        # VPN-Region (VLESS+Reality)
        region_ssh_host=os.getenv("REGION_SSH_HOST", "").strip(),
        region_ssh_port=int(os.getenv("REGION_SSH_PORT", "22")),
        region_ssh_user=os.getenv("REGION_SSH_USER", "root").strip(),
        region_ssh_password=(os.getenv("REGION_SSH_PASSWORD") or "").strip() or None,
        region_xray_config_path=os.getenv("REGION_XRAY_CONFIG_PATH", "/usr/local/etc/xray/config.json").strip(),
        region_xray_api_port=int(os.getenv("REGION_XRAY_API_PORT", "10085")),
        region_max_clients=int(os.getenv("REGION_MAX_CLIENTS", "40")),
        region_quota_gb=int(os.getenv("REGION_QUOTA_GB", "0")),
    )


settings = _load_settings()
