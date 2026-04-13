from __future__ import annotations

import asyncio
import logging
import re
from typing import Final

import aiohttp

from app.core.config import settings

log = logging.getLogger(__name__)

_LOGIN_PREFIXES: Final[tuple[str, ...]] = ("web_login_", "web_register_")
_SELECTOR_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{6,128}$")


def is_web_login_payload(payload: str | None) -> bool:
    if not payload:
        return False
    return any(payload.startswith(prefix) for prefix in _LOGIN_PREFIXES)


def extract_web_login_selector(payload: str | None) -> str | None:
    if not payload:
        return None
    for prefix in _LOGIN_PREFIXES:
        if payload.startswith(prefix):
            selector = payload[len(prefix):].strip()
            if _SELECTOR_RE.fullmatch(selector):
                return selector
            return None
    return None


async def approve_site_telegram_login(*, selector: str, tg_id: int) -> tuple[bool, str]:
    """Approve pending web login token on the website service.

    Returns (ok, message). The message is safe to show to the user.
    """
    base_url = (settings.web_app_base_url or "").rstrip("/")
    api_key = (settings.web_internal_api_key or "").strip()

    if not base_url:
        return False, "На стороне бота не настроен WEB_APP_BASE_URL."
    if not api_key:
        return False, "На стороне бота не настроен WEB_INTERNAL_API_KEY."

    url = f"{base_url}/internal/telegram/approve"
    timeout = aiohttp.ClientTimeout(total=12)
    payload = {"selector": selector, "tg_id": int(tg_id)}
    headers = {"x-internal-api-key": api_key}

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    text = await resp.text()
                    data = {"ok": False, "error": text[:300]}

                if 200 <= resp.status < 300 and bool(data.get("ok")):
                    return True, "Вход на сайте подтверждён."

                error_code = str(data.get("error") or f"HTTP_{resp.status}")
                if error_code == "TOKEN_NOT_FOUND_OR_EXPIRED":
                    return False, "Ссылка для входа истекла или уже была использована. Открой сайт и запроси новую ссылку."
                if error_code == "UNAUTHORIZED":
                    log.error("web_auth_approve_unauthorized selector=%s", selector)
                    return False, "Сайт отклонил внутренний запрос бота. Проверь WEB_INTERNAL_API_KEY."
                if error_code == "INVALID_INPUT":
                    return False, "Сайт получил некорректные данные для входа. Попробуй ещё раз с новой ссылкой."

                log.warning("web_auth_approve_failed selector=%s status=%s error=%s", selector, resp.status, error_code)
                return False, "Не удалось подтвердить вход на сайте. Попробуй ещё раз чуть позже."
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("web_auth_approve_network_error selector=%s err=%r", selector, exc)
        return False, "Сайт сейчас недоступен. Попробуй ещё раз через минуту."
