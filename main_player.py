"""
Secondary bot entrypoint (player gateway).
Run this file in a separate Railway service (kinoteka-player).
Required env vars in that service:
 - DATABASE_URL (reference to the shared Postgres)
 - PLAYER_BOT_TOKEN (or BOT_TOKEN)
 - OWNER_TG_ID (any digits; used by shared config loader)
 - MAIN_BOT_USERNAME (e.g. sbsconnect_bot)
 - PLAYER_RATE_LIMIT_PER_MINUTE (comma-separated)
 - REZKA_MIRROR (optional, default https://rezka.ag)
"""

from __future__ import annotations
import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.fsm.storage.memory import MemoryStorage

from app.core.logging import setup_logging
from app.core.config import settings
from app.db.session import init_engine, session_scope
from app.repo import get_subscription, get_content_request_by_token
from app.bot.ui import utcnow
from app.db.models import ContentRequest  # модель content_requests
from HdRezkaApi import HdRezkaApi, errors as rezka_errors  # парсер Rezka

from urllib.parse import urlparse, urlunparse


# ----------------------------- Rezka helpers -----------------------------

# Cookie cache in-memory (per mirror). Railway service is long-lived, so this
# prevents re-login on every request.
_rezka_cookies_by_mirror: dict[str, dict] = {}
_rezka_login_attempted: set[str] = set()


def _parse_mirrors(raw: str | None) -> list[str]:
    if not raw:
        return []
    # allow comma/space/newline separated
    parts = []
    for chunk in raw.replace("\n", ",").replace(" ", ",").split(","):
        s = chunk.strip().strip('"').strip("'")
        if not s:
            continue
        if not s.startswith("http://") and not s.startswith("https://"):
            s = "https://" + s
        parts.append(s.rstrip("/"))
    # de-dup preserving order
    seen = set()
    out = []
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _build_proxy() -> dict:
    """Build proxy dict for HdRezkaApi from environment.

    Supports:
      - PROXY_URL (applies to http+https)
      - HTTPS_PROXY / HTTP_PROXY
    """
    proxy_url = (os.getenv("PROXY_URL") or "").strip()
    https_p = (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or "").strip()
    http_p = (os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or "").strip()

    if proxy_url:
        return {"http": proxy_url, "https": proxy_url}
    proxy = {}
    if http_p:
        proxy["http"] = http_p
    if https_p:
        proxy["https"] = https_p
    return proxy


def _get_auth_cookies(mirror_key: str) -> dict:
    """Return cookies (possibly empty) for this mirror."""
    return _rezka_cookies_by_mirror.get(mirror_key, {})


def _maybe_login_and_store(url_for_login: str, mirror_key: str) -> None:
    """Try to authenticate to Rezka if credentials are provided in env.

    Env options:
      - REZKA_USER_ID + REZKA_PASSWORD_HASH (best; no network login)
      - REZKA_EMAIL + REZKA_PASSWORD (network login)
    """
    if mirror_key in _rezka_login_attempted:
        return

    user_id = (os.getenv("REZKA_USER_ID") or "").strip()
    pwd_hash = (os.getenv("REZKA_PASSWORD_HASH") or "").strip()
    email = (os.getenv("REZKA_EMAIL") or "").strip()
    password = (os.getenv("REZKA_PASSWORD") or "").strip()

    if not ((user_id and pwd_hash) or (email and password)):
        return

    _rezka_login_attempted.add(mirror_key)

    try:
        if user_id and pwd_hash:
            # No network login required, just synth cookies.
            cookies = HdRezkaApi.make_cookies(user_id=user_id, password_hash=pwd_hash)
            if isinstance(cookies, dict) and cookies:
                _rezka_cookies_by_mirror[mirror_key] = cookies
                log.info("✅ Rezka cookies built from REZKA_USER_ID/REZKA_PASSWORD_HASH")
            return

        # Network login
        rezka_obj = HdRezkaApi(url_for_login, proxy=_build_proxy())
        rezka_obj.login(email=email, password=password, raise_exception=True)
        cookies = getattr(rezka_obj, "cookies", None)
        if isinstance(cookies, dict) and cookies:
            _rezka_cookies_by_mirror[mirror_key] = cookies
            log.info("✅ Rezka login succeeded; cookies stored")
    except Exception:
        log.exception("❌ Rezka login attempt failed")


log = logging.getLogger(__name__)


def _swap_domain(url: str, mirror: str) -> str:
    """Replace domain in `url` with `mirror` (scheme+netloc)."""
    try:
        src = urlparse(url)
        dst = urlparse(mirror)
        if not dst.scheme or not dst.netloc:
            return url
        return urlunparse((dst.scheme, dst.netloc, src.path, src.params, src.query, src.fragment))
    except Exception:
        return url


def _load_rezka(url: str) -> HdRezkaApi:
    """Create HdRezkaApi object for a specific title URL.

    Features:
      - mirror fallback (REZKA_MIRROR can be a list)
      - proxy support (PROXY_URL / HTTP(S)_PROXY)
      - optional auth cookies (via env login or prebuilt cookies)
    """

    mirrors = _parse_mirrors(os.getenv("REZKA_MIRROR"))
    if not mirrors:
        mirrors = ["https://rezka.ag"]

    proxy = _build_proxy()
    last_exc: Exception | None = None

    for mirror in mirrors:
        normalized = _swap_domain(url, mirror)
        mirror_key = urlparse(mirror).netloc
        cookies = _get_auth_cookies(mirror_key)

        try:
            rezka_obj = HdRezkaApi(normalized, proxy=proxy, cookies=cookies)

            if not getattr(rezka_obj, "ok", True):
                exc = getattr(rezka_obj, "exception", None)
                if exc:
                    raise exc
                raise RuntimeError("HdRezkaApi returned ok=False")

            return rezka_obj

        except rezka_errors.LoginRequiredError as e:
            # Try login (once per mirror) and retry.
            last_exc = e
            _maybe_login_and_store(normalized, mirror_key)
            cookies2 = _get_auth_cookies(mirror_key)
            if cookies2 and cookies2 != cookies:
                try:
                    rezka_obj = HdRezkaApi(normalized, proxy=proxy, cookies=cookies2)
                    if getattr(rezka_obj, "ok", True):
                        return rezka_obj
                    exc = getattr(rezka_obj, "exception", None)
                    if exc:
                        raise exc
                except Exception as e2:
                    last_exc = e2

        except rezka_errors.HTTP as e:
            # 403/503 etc. — try next mirror.
            last_exc = e
            continue
        except Exception as e:
            last_exc = e
            continue

    if last_exc:
        raise last_exc
    raise RuntimeError("Rezka mirrors exhausted")


def _normalize_stream_url(link) -> str | None:
    """Convert HdRezkaApi stream(quality) result to a single URL string.

    Telegram InlineKeyboardButton.url must be a single URL.
    HdRezkaApi may return:
      - str (URL)
      - list/tuple/set of URL strings
      - dict with URL values (rare)
    """
    if not link:
        return None

    if isinstance(link, str):
        s = link.strip()
        return s or None

    def _pick_best(candidates: list[str]) -> str | None:
        """Prefer direct MP4 links over HLS (m3u8).

        HdRezka often returns multiple URLs for the same quality.
        When Telegram tries to play HLS (m3u8) it may only show a short
        preview/bumper. Direct MP4 works much more reliably in Telegram.
        """
        cleaned = [c.strip() for c in candidates if isinstance(c, str) and c.strip()]
        if not cleaned:
            return None

        # Prefer obvious MP4 links
        for c in cleaned:
            lc = c.lower()
            if ".mp4" in lc or lc.endswith(".m4v") or "format=mp4" in lc:
                return c

        # Otherwise prefer non-m3u8
        for c in cleaned:
            if "m3u8" not in c.lower():
                return c

        # Fallback to the last (often the 'real' stream)
        return cleaned[-1]

    if isinstance(link, (list, tuple, set)):
        return _pick_best(list(link))

    if isinstance(link, dict):
        return _pick_best([v for v in link.values() if isinstance(v, str)])

    # Fallback: try stringify
    s = str(link).strip()
    if s.startswith("[") and s.endswith("]"):
        return None
    return s or None

# Rate-limit cache (простой, в памяти)
rate_cache = {}  # user_id → (count, last_time)

router = Router()


def rate_limit_exceeded(user_id: int) -> bool:
    limit = int(os.getenv("PLAYER_RATE_LIMIT_PER_MINUTE", "15"))
    now = datetime.utcnow().timestamp()
    if user_id in rate_cache:
        count, last_time = rate_cache[user_id]
        if now - last_time < 60:
            if count >= limit:
                return True
            rate_cache[user_id] = (count + 1, last_time)
            return False
    rate_cache[user_id] = (1, now)
    return False


def _cb(*parts: str) -> str:
    """Build safe callback_data (Telegram limit is 64 bytes).

    We NEVER put full URLs into callback_data – they are long and trigger
    BUTTON_DATA_INVALID. Instead we pass a short token and re-load URL from DB.
    """
    s = ":".join(str(p) for p in parts)
    # Hard truncate just in case; better to have a shorter callback than crash.
    return s[:64]


def _is_sub_active(end_at) -> bool:
    if not end_at:
        return False
    try:
        return end_at > utcnow()
    except Exception:
        return False


@router.message(CommandStart(deep_link=True))
async def handle_start_with_token(message: Message) -> None:
    """Обработка /start <token> из основного бота"""
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Недействительная ссылка. Откройте фильм из основного бота.")
        return

    token = args[1].strip()

    if rate_limit_exceeded(user_id):
        await message.answer("Слишком много запросов. Подождите минуту.")
        return

    async with session_scope() as session:
        req = await get_content_request_by_token(session, token)
        if not req:
            await message.answer("Ссылка устарела или недействительна.")
            return

        url = req.content_url

        # Повторная проверка подписки
        sub = await get_subscription(session, user_id)
        if not _is_sub_active(sub.end_at):
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Купить подписку", url=f"t.me/{settings.MAIN_BOT_USERNAME}")]
            ])
            await message.answer(
                "У вас нет активной подписки. Оформите в основном боте:",
                reply_markup=kb
            )
            return

    # Парсинг контента из Rezka
    try:
        rezka_item = _load_rezka(url)

        title = getattr(rezka_item, "name", "Без названия")
        year = getattr(rezka_item, "releaseYear", None) or getattr(rezka_item, "year", None) or "—"
        poster = getattr(rezka_item, "thumbnail", None) or getattr(rezka_item, "thumbnailHQ", None)
        description = (getattr(rezka_item, "description", "Описание отсутствует") or "Описание отсутствует")[:600]

        # `episodesInfo` is a property in HdRezkaApi and raises ValueError for non-TVSeries (e.g., films).
        # So we must only access it when we are confident the URL points to a series.
        is_series = ("/series/" in url) or ("/serials/" in url)
        episodes_info = []
        if is_series:
            try:
                episodes_info = rezka_item.episodesInfo or []
            except Exception:
                episodes_info = []
                is_series = False

        # Сериал — выбор сезона
        if is_series:
            kb = InlineKeyboardMarkup(inline_keyboard=[])
            seasons = [s.get("season") for s in episodes_info if isinstance(s, dict) and s.get("season") is not None]
            for season_num in sorted(set(int(s) for s in seasons)):
                kb.inline_keyboard.append([
                    InlineKeyboardButton(text=f"Сезон {season_num}", callback_data=_cb("season", token, str(season_num)))
                ])
            text = f"<b>{title} ({year})</b>\n\n{description}\n\nВыберите сезон:"
            if poster:
                await message.answer_photo(photo=poster, caption=text, reply_markup=kb, parse_mode="HTML")
            else:
                await message.answer(text, reply_markup=kb, parse_mode="HTML")
            return

        # Фильм — сразу качества.
        # Важно: НЕ используем InlineKeyboardButton(url=...) на прямые CDN-ссылки.
        # У Rezka/CDN часто включена защита от хотлинка/реферера.
        # В Telegram in-app browser ссылка открывается без нужных заголовков,
        # из-за чего пользователя может перекинуть на страницу Rezka,
        # а само видео не стартует. Поэтому делаем кнопки callback'ами
        # и отправляем видео через Telegram (answer_video), где Telegram
        # сам забирает файл по URL.
        translators = getattr(rezka_item, "translators", None) or {}
        translation = None
        try:
            if isinstance(translators, dict) and translators:
                translation = next(iter(translators.keys()))
        except Exception:
            translation = None

        stream = rezka_item.getStream(translation=translation) if translation else rezka_item.getStream()
        videos = getattr(stream, "videos", {}) or {}

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for quality in videos.keys():
            # callback вместо прямого URL, см. комментарий выше
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=str(quality), callback_data=_cb("playfilm", token, str(quality)))
            ])

        text = f"<b>{title} ({year})</b>\n\n{description}"
        if poster:
            await message.answer_photo(photo=poster, caption=text, reply_markup=kb, parse_mode="HTML")
        else:
            await message.answer(text, reply_markup=kb, parse_mode="HTML")

    except Exception:
        log.exception(f"Ошибка обработки контента {url}")
        await message.answer("Не удалось загрузить контент. Попробуйте позже.")


@router.callback_query(F.data.startswith("playfilm:"))
async def handle_play_film(callback: CallbackQuery) -> None:
    """Кнопка качества для фильма.

    Вместо открытия ссылки в браузере отдаём Telegram прямую ссылку
    как `video=URL`, чтобы Telegram сам скачал/кешировал и показал видео.
    Это обходится без реферера, который часто ломает воспроизведение в
    in-app браузере.
    """

    parts = (callback.data or "").split(":", 2)
    if len(parts) < 3:
        await callback.answer("Ошибка данных.")
        return

    token = parts[1].strip()
    quality = parts[2].strip()

    await callback.answer("Загружаю…", show_alert=False)

    # Load request + subscription
    async with session_scope() as session:
        req = await get_content_request_by_token(session, token)
        if not req:
            await callback.answer("Ссылка устарела.", show_alert=True)
            return

        sub = await get_subscription(session, callback.from_user.id)
        if not _is_sub_active(sub.end_at):
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Купить подписку", url=f"t.me/{settings.MAIN_BOT_USERNAME}")]
            ])
            await callback.message.answer("У вас нет активной подписки. Оформите в основном боте:", reply_markup=kb)
            return

        url = req.content_url

    try:
        rezka_item = _load_rezka(url)

        translators = getattr(rezka_item, "translators", None) or {}
        translation = None
        if isinstance(translators, dict) and translators:
            try:
                translation = next(iter(translators.keys()))
            except Exception:
                translation = None

        stream = rezka_item.getStream(translation=translation) if translation else rezka_item.getStream()

        link = None
        try:
            link = stream(quality)
        except Exception:
            # Иногда quality приходит как "1080p" и т.п.; пробуем ключи из videos
            videos = getattr(stream, "videos", {}) or {}
            for q in videos.keys():
                if str(q) == str(quality):
                    link = stream(q)
                    break

        url1 = _normalize_stream_url(link)
        if not url1:
            await callback.message.answer("Не удалось получить ссылку на видео. Попробуйте другое качество.")
            return

        title = getattr(rezka_item, "name", "Фильм")
        year = getattr(rezka_item, "releaseYear", None) or getattr(rezka_item, "year", None) or ""

        # Отправляем как видео, чтобы запускалось прямо в Telegram
        await callback.message.answer_video(
            video=url1,
            caption=f"<b>{title}{f' ({year})' if year else ''}</b>\nКачество: {quality}",
            parse_mode="HTML",
        )

    except Exception:
        log.exception("Ошибка при отправке видео")
        await callback.message.answer("Не удалось запустить видео. Попробуйте позже.")


@router.callback_query(F.data.startswith("season:"))
async def handle_season(callback: CallbackQuery) -> None:
    """Выбор сезона → список серий"""
    parts = (callback.data or "").split(":", 2)
    if len(parts) < 3:
        await callback.answer("Ошибка данных.")
        return

    token = parts[1].strip()
    season_str = parts[2].strip()

    # Resolve URL from DB
    async with session_scope() as session:
        req = await get_content_request_by_token(session, token)
        if not req:
            await callback.answer("Ссылка устарела.", show_alert=True)
            return
        url = req.content_url

    try:
        season = int(season_str)
        rezka_item = _load_rezka(url)
        episodes_info = getattr(rezka_item, "episodesInfo", None) or []

        episodes: list[int] = []
        for s in episodes_info:
            if not isinstance(s, dict):
                continue
            if int(s.get("season", -1)) != season:
                continue
            for ep in s.get("episodes", []) or []:
                if isinstance(ep, dict) and ep.get("episode") is not None:
                    episodes.append(int(ep["episode"]))

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for ep in sorted(set(episodes)):
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=f"Серия {ep}", callback_data=_cb("episode", token, str(season), str(ep)))
            ])

        await callback.message.edit_text(f"Сезон {season}: выберите серию", reply_markup=kb)
        await callback.answer()

    except Exception:
        log.exception("Ошибка обработки сезона")
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)


@router.callback_query(F.data.startswith("episode:"))
async def handle_episode(callback: CallbackQuery) -> None:
    """Выбор серии → выбор озвучки"""
    parts = (callback.data or "").split(":", 3)
    if len(parts) < 4:
        await callback.answer("Ошибка данных.")
        return

    token = parts[1].strip()
    season_str = parts[2].strip()
    episode_str = parts[3].strip()

    async with session_scope() as session:
        req = await get_content_request_by_token(session, token)
        if not req:
            await callback.answer("Ссылка устарела.", show_alert=True)
            return
        url = req.content_url

    try:
        season = int(season_str)
        episode = int(episode_str)
        rezka_item = _load_rezka(url)
        episodes_info = getattr(rezka_item, "episodesInfo", None) or []

        translations = []
        for s in episodes_info:
            if not isinstance(s, dict) or int(s.get("season", -1)) != season:
                continue
            for ep in s.get("episodes", []) or []:
                if not isinstance(ep, dict) or int(ep.get("episode", -1)) != episode:
                    continue
                translations = ep.get("translations", []) or []
                break

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for t in translations:
            if not isinstance(t, dict):
                continue
            trans_id = t.get("translator_id") or t.get("id")
            trans_name = t.get("translator_name") or t.get("name") or "Озвучка"
            if trans_id is None:
                continue
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=str(trans_name), callback_data=_cb("trans", token, str(season), str(episode), str(trans_id)))
            ])

        if not kb.inline_keyboard:
            kb.inline_keyboard.append([
                InlineKeyboardButton(text="По умолчанию", callback_data=_cb("trans", token, str(season), str(episode), "None"))
            ])

        await callback.message.edit_text(f"Серия {episode} (сезон {season}): выберите озвучку", reply_markup=kb)
        await callback.answer()

    except Exception:
        log.exception("Ошибка обработки серии")
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)


@router.callback_query(F.data.startswith("trans:"))
async def handle_translator(callback: CallbackQuery) -> None:
    """Выбор озвучки → показ качеств"""
    parts = (callback.data or "").split(":", 4)
    if len(parts) < 5:
        await callback.answer("Ошибка данных.")
        return

    token = parts[1].strip()
    season_str = parts[2].strip()
    episode_str = parts[3].strip()
    trans_id = parts[4].strip()

    async with session_scope() as session:
        req = await get_content_request_by_token(session, token)
        if not req:
            await callback.answer("Ссылка устарела.", show_alert=True)
            return
        url = req.content_url

    try:
        season = int(season_str)
        episode = int(episode_str)
        rezka_item = _load_rezka(url)

        translation = None if trans_id in {"None", "none", "null", ""} else trans_id
        stream = rezka_item.getStream(season, episode, translation=translation)
        videos = getattr(stream, "videos", {}) or {}

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for quality in videos.keys():
            # callback вместо прямого URL (см. комментарий в фильмах)
            kb.inline_keyboard.append([
                InlineKeyboardButton(
                    text=str(quality),
                    callback_data=_cb("playseries", token, str(season), str(episode), str(trans_id), str(quality)),
                )
            ])

        await callback.message.edit_text("Выберите качество:", reply_markup=kb)
        await callback.answer()

    except Exception:
        log.exception("Ошибка обработки озвучки")
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)


@router.callback_query(F.data.startswith("playseries:"))
async def handle_play_series(callback: CallbackQuery) -> None:
    """Кнопка качества для серии (HLS/Referer-safe, как у фильмов)."""

    parts = (callback.data or "").split(":", 5)
    if len(parts) < 6:
        await callback.answer("Ошибка данных.")
        return

    token = parts[1].strip()
    season = int(parts[2])
    episode = int(parts[3])
    trans_id = parts[4].strip()
    quality = parts[5].strip()

    await callback.answer("Загружаю…", show_alert=False)

    async with session_scope() as session:
        req = await get_content_request_by_token(session, token)
        if not req:
            await callback.answer("Ссылка устарела.", show_alert=True)
            return

        sub = await get_subscription(session, callback.from_user.id)
        if not _is_sub_active(sub.end_at):
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Купить подписку", url=f"t.me/{settings.MAIN_BOT_USERNAME}")]
            ])
            await callback.message.answer("У вас нет активной подписки. Оформите в основном боте:", reply_markup=kb)
            return

        url = req.content_url

    try:
        rezka_item = _load_rezka(url)
        translation = None if trans_id in {"None", "none", "null", ""} else trans_id
        stream = rezka_item.getStream(season, episode, translation=translation)

        link = None
        try:
            link = stream(quality)
        except Exception:
            videos = getattr(stream, "videos", {}) or {}
            for q in videos.keys():
                if str(q) == str(quality):
                    link = stream(q)
                    break

        url1 = _normalize_stream_url(link)
        if not url1:
            await callback.message.answer("Не удалось получить ссылку на видео. Попробуйте другое качество.")
            return

        title = getattr(rezka_item, "name", "Сериал")
        year = getattr(rezka_item, "releaseYear", None) or getattr(rezka_item, "year", None) or ""

        await callback.message.answer_video(
            video=url1,
            caption=(
                f"<b>{title}{f' ({year})' if year else ''}</b>\n"
                f"Сезон {season}, серия {episode}\nКачество: {quality}"
            ),
            parse_mode="HTML",
        )

    except Exception:
        log.exception("Ошибка при отправке серии")
        await callback.message.answer("Не удалось запустить серию. Попробуйте позже.")


def _run_alembic_upgrade_head_best_effort() -> None:
    """Apply migrations at boot (best-effort)."""
    try:
        subprocess.check_call([sys.executable, "-m", "alembic", "upgrade", "head"])
        log.info("✅ Alembic migrations applied: upgrade head")
    except Exception:
        # best-effort; do not crash player bot
        log.exception("❌ Alembic upgrade head failed. Continuing without migrations.")


async def main() -> None:
    setup_logging()
    init_engine(settings.database_url)
    _run_alembic_upgrade_head_best_effort()

    bot = Bot(token=settings.bot_token)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    log.info("🚀 Player bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
