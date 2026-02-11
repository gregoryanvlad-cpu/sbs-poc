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
from app.db.models import ContentRequest
from HdRezkaApi import HdRezkaApi, errors as rezka_errors
from urllib.parse import urlparse, urlunparse

# ----------------------------- Rezka helpers -----------------------------
_rezka_cookies_by_mirror: dict[str, dict] = {}
_rezka_login_attempted: set[str] = set()

def _parse_mirrors(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = []
    for chunk in raw.replace("\n", ",").replace(" ", ",").split(","):
        s = chunk.strip().strip('"').strip("'")
        if not s:
            continue
        if not s.startswith("http://") and not s.startswith("https://"):
            s = "https://" + s
        parts.append(s.rstrip("/"))
    seen = set()
    out = []
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out

def _build_proxy() -> dict:
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
    return _rezka_cookies_by_mirror.get(mirror_key, {})

def _maybe_login_and_store(url_for_login: str, mirror_key: str) -> None:
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
            cookies = HdRezkaApi.make_cookies(user_id=user_id, password_hash=pwd_hash)
            if isinstance(cookies, dict) and cookies:
                _rezka_cookies_by_mirror[mirror_key] = cookies
                log.info("✅ Rezka cookies built from REZKA_USER_ID/REZKA_PASSWORD_HASH")
            return
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
    try:
        src = urlparse(url)
        dst = urlparse(mirror)
        if not dst.scheme or not dst.netloc:
            return url
        return urlunparse((dst.scheme, dst.netloc, src.path, src.params, src.query, src.fragment))
    except Exception:
        return url

def _load_rezka(url: str) -> HdRezkaApi:
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
            last_exc = e
            continue
        except Exception as e:
            last_exc = e
            continue
    if last_exc:
        raise last_exc
    raise RuntimeError("Rezka mirrors exhausted")

def _normalize_stream_url(link) -> str | None:
    """Преобразует результат stream(quality) в одну строку-ссылку.
    Сильно предпочитает прямые .mp4 ссылки.
    """
    if not link:
        return None

    candidates = []
    if isinstance(link, str):
        candidates = [link]
    elif isinstance(link, (list, tuple, set)):
        candidates = list(link)
    elif isinstance(link, dict):
        candidates = list(link.values())

    cleaned = [c.strip() for c in candidates if isinstance(c, str) and c.strip() and len(c) > 20]

    if not cleaned:
        return None

    # 1. Самый приоритетный — прямой mp4
    for c in cleaned:
        lc = c.lower()
        if ".mp4" in lc or lc.endswith(".mp4") or "format=mp4" in lc:
            return c

    # 2. Любая ссылка без m3u8 (иногда бывают dash или другие)
    for c in cleaned:
        if "m3u8" not in c.lower() and "playlist" not in c.lower():
            return c

    # 3. Последний шанс — самая длинная / последняя ссылка (часто master.m3u8)
    cleaned.sort(key=len, reverse=True)
    return cleaned[0]

# Rate-limit cache
rate_cache = {}
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
    s = ":".join(str(p) for p in parts)
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

    try:
        rezka_item = _load_rezka(url)
        title = getattr(rezka_item, "name", "Без названия")
        year = getattr(rezka_item, "releaseYear", None) or getattr(rezka_item, "year", None) or "—"
        poster = getattr(rezka_item, "thumbnail", None) or getattr(rezka_item, "thumbnailHQ", None)
        description = (getattr(rezka_item, "description", "Описание отсутствует") or "Описание отсутствует")[:600]

        is_series = rezka_item.type == "TVSeries" or "/series/" in url or "/serials/" in url

        episodes_info = []
        if is_series:
            try:
                episodes_info = rezka_item.episodesInfo or []
            except Exception:
                episodes_info = []
                is_series = False

        if is_series:
            kb = InlineKeyboardMarkup(inline_keyboard=[])
            seasons = [s.get("season") for s in episodes_info if isinstance(s, dict) and s.get("season") is not None]
            for season_num in sorted(set(int(s) for s in seasons if s)):
                kb.inline_keyboard.append([
                    InlineKeyboardButton(text=f"Сезон {season_num}", callback_data=_cb("season", token, str(season_num)))
                ])
            text = f"<b>{title} ({year})</b>\n\n{description}\n\nВыберите сезон:"
            if poster:
                await message.answer_photo(photo=poster, caption=text, reply_markup=kb, parse_mode="HTML")
            else:
                await message.answer(text, reply_markup=kb, parse_mode="HTML")
            return

        # Фильм — сразу качества
        translators = getattr(rezka_item, "translators", None) or {}
        translation = next(iter(translators.keys())) if translators else None

        stream = rezka_item.getStream(translation=translation) if translation else rezka_item.getStream()
        videos = getattr(stream, "videos", {}) or {}

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for quality in sorted(videos.keys(), key=lambda q: int(''.join(filter(str.isdigit, str(q)))), reverse=True):
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
    parts = (callback.data or "").split(":", 2)
    if len(parts) < 3:
        await callback.answer("Ошибка данных.")
        return
    token = parts[1].strip()
    quality = parts[2].strip()
    await callback.answer("Получаю ссылку…", show_alert=False)

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
        translation = next(iter(translators.keys())) if translators else None

        stream = rezka_item.getStream(translation=translation) if translation else rezka_item.getStream()
        link = stream(quality)

        url1 = _normalize_stream_url(link)
        if not url1:
            await callback.message.answer("Не удалось получить рабочую ссылку на это качество. Попробуйте другое.")
            return

        title = getattr(rezka_item, "name", "Фильм")
        year = getattr(rezka_item, "releaseYear", None) or getattr(rezka_item, "year", None) or ""

        hint = ""
        if "m3u8" in url1.lower():
            hint = "\n\n(это HLS-плейлист — откройте в VLC, MX Player, Infuse или PotPlayer)"

        await callback.message.answer(
            f"<b>{title}{f' ({year})' if year else ''}</b>\n"
            f"Качество: {quality}\n\n"
            f"Ссылка для просмотра:\n{url1}\n\n"
            f"Рекомендуем открыть в:{hint}",
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    except Exception:
        log.exception("Ошибка при получении ссылки на фильм")
        await callback.message.answer("Не удалось получить ссылку. Попробуйте позже.")

# Остальные хендлеры (сериалы) остаются почти без изменений, только меняем answer_video → answer с текстом

@router.callback_query(F.data.startswith("season:"))
async def handle_season(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":", 2)
    if len(parts) < 3:
        await callback.answer("Ошибка данных.")
        return
    token = parts[1].strip()
    season_str = parts[2].strip()

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
        for quality in sorted(videos.keys(), key=lambda q: int(''.join(filter(str.isdigit, str(q)))), reverse=True):
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
    parts = (callback.data or "").split(":", 5)
    if len(parts) < 6:
        await callback.answer("Ошибка данных.")
        return
    token = parts[1].strip()
    season = int(parts[2])
    episode = int(parts[3])
    trans_id = parts[4].strip()
    quality = parts[5].strip()
    await callback.answer("Получаю ссылку…", show_alert=False)

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
        link = stream(quality)

        url1 = _normalize_stream_url(link)
        if not url1:
            await callback.message.answer("Не удалось получить рабочую ссылку на это качество. Попробуйте другое.")
            return

        title = getattr(rezka_item, "name", "Сериал")
        year = getattr(rezka_item, "releaseYear", None) or getattr(rezka_item, "year", None) or ""

        hint = ""
        if "m3u8" in url1.lower():
            hint = "\n\n(это HLS-плейлист — откройте в VLC, MX Player, Infuse или PotPlayer)"

        await callback.message.answer(
            f"<b>{title}{f' ({year})' if year else ''}</b>\n"
            f"Сезон {season}, серия {episode}\n"
            f"Качество: {quality}\n\n"
            f"Ссылка для просмотра:\n{url1}\n\n"
            f"Рекомендуем открыть в:{hint}",
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    except Exception:
        log.exception("Ошибка при получении ссылки на серию")
        await callback.message.answer("Не удалось получить ссылку. Попробуйте позже.")

def _run_alembic_upgrade_head_best_effort() -> None:
    try:
        subprocess.check_call([sys.executable, "-m", "alembic", "upgrade", "head"])
        log.info("✅ Alembic migrations applied: upgrade head")
    except Exception:
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
