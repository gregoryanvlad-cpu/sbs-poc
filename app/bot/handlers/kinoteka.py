from __future__ import annotations
import json
import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.exceptions import SkipHandler
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.bot.keyboards import kb_kinoteka_back
from app.bot.ui import utcnow
from app.db.models.user import User
from app.db.session import session_scope
from app.repo import get_subscription, create_content_request
from app.core.config import settings
from app.services.rezka.client import rezka_client, RezkaError

router = Router()
log = logging.getLogger(__name__)

def _is_sub_active(end_at) -> bool:
    if not end_at:
        return False
    try:
        return end_at > utcnow()
    except Exception:
        return False


@router.callback_query(F.data == "kino:search")
async def on_kino_search(cb: CallbackQuery) -> None:
    await cb.answer()
    # Проверка подписки (как для VPN/Yandex)
    async with session_scope() as session:
        sub = await get_subscription(session, cb.from_user.id)
        if not _is_sub_active(sub.end_at):
            await cb.message.answer("⛔️ Подписка не активна. Сначала оплати доступ.")
            return

        user = await session.get(User, cb.from_user.id)
        if not user:
            # ensure_user вызывается внутри get_subscription
            user = await session.get(User, cb.from_user.id)

        if user:
            user.flow_state = "await_kino_query"
            user.flow_data = json.dumps({"started_at": utcnow().isoformat()})
            await session.commit()

    await cb.message.answer(
        "🔍 Напиши названием фильма/сериала одним сообщением.\n\n"
        "Пример: <code>Интерстеллар</code>",
        parse_mode="HTML",
        reply_markup=kb_kinoteka_back(),
    )


@router.message(F.text)
async def on_kino_query_input(msg: Message) -> None:
    tg_id = msg.from_user.id
    query = (msg.text or "").strip()
    if not query:
        # Не наш сценарий — даём шанс другим хендлерам.
        raise SkipHandler

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        # Этот хендлер ловит все текстовые сообщения. Если мы просто `return`,
        # апдейт считается обработанным и FSM/другие сценарии не сработают.
        if not user or user.flow_state != "await_kino_query":
            raise SkipHandler

        # Сбрасываем состояние в любом случае (чтобы не висело)
        user.flow_state = None
        await session.commit()

    try:
        results = await rezka_client.search(query, limit=6)
    except RezkaError:
        await msg.answer(
            "⚠️ Не удалось получить данные из Кинотеки.\nПопробуй ещё раз позже.",
            reply_markup=kb_kinoteka_back(),
        )
        return
    except Exception:
        log.exception("Kinoteka search failed", extra={"tg_id": tg_id, "query": query})
        await msg.answer(
            "⚠️ Временная ошибка Кинотеки. Попробуй ещё раз позже.",
            reply_markup=kb_kinoteka_back(),
        )
        return

    if not results:
        await msg.answer(
            "Ничего не нашёл 😕\n\nПопробуй другое название.",
            reply_markup=kb_kinoteka_back(),
        )
        return

    # Сохраняем результаты в flow_data (чтобы не превышать лимит callback_data)
    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if user:
            user.flow_data = json.dumps(
                {"rezka_results": results, "query": query, "saved_at": utcnow().isoformat()},
                ensure_ascii=False,
            )
            await session.commit()

    # Формируем компактный список с кнопками
    kb: list[list[InlineKeyboardButton]] = []
    lines = ["🎬 <b>Результаты</b>:"]

    for idx, m in enumerate(results[:6]):
        n = idx + 1
        name = m.get("title") or "Без названия"
        url = m.get("url")
        rating = m.get("rating")
        rating_str = f"{rating}" if rating else "—"

        lines.append(f"{n}) {name} — {rating_str}")

        if url:
            kb.append([InlineKeyboardButton(text=f"{n}️⃣ Открыть", callback_data=f"kino:item:{idx}")])

    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:kinoteka")])

    await msg.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )


@router.callback_query(F.data.startswith("kino:item:"))
async def on_kino_item(cb: CallbackQuery) -> None:
    await cb.answer()

    try:
        idx = int(cb.data.split(":", 2)[2])
    except Exception:
        return

    # Загружаем сохранённые результаты из flow_data
    async with session_scope() as session:
        user = await session.get(User, cb.from_user.id)
        data = {}
        if user and user.flow_data:
            try:
                data = json.loads(user.flow_data)
            except Exception:
                data = {}

    results = (data or {}).get("rezka_results") or []
    if not isinstance(results, list) or idx < 0 or idx >= len(results):
        await cb.message.answer("⚠️ Не удалось открыть карточку. Сделай поиск заново.", reply_markup=kb_kinoteka_back())
        return

    url = (results[idx] or {}).get("url")
    if not url:
        await cb.message.answer("⚠️ Не удалось открыть карточку. Сделай поиск заново.", reply_markup=kb_kinoteka_back())
        return

    try:
        info = await rezka_client.get_info(url)
    except RezkaError:
        await cb.message.answer(
            "⚠️ Не удалось открыть карточку. Попробуй ещё раз позже.",
            reply_markup=kb_kinoteka_back(),
        )
        return
    except Exception:
        log.exception("Kinoteka get_info failed", extra={"tg_id": cb.from_user.id, "url": url})
        await cb.message.answer("⚠️ Временная ошибка. Попробуй ещё раз позже.")
        return

    name = info.get("name") or "Без названия"
    orig = info.get("orig_name")
    desc = (info.get("description") or "").strip()
    if desc and len(desc) > 800:
        desc = desc[:800].rsplit(" ", 1)[0] + "…"

    rating = info.get("rating")
    year = info.get("year")
    category = info.get("category")

    title = f"🎬 <b>{name}</b>"
    meta = []
    if orig and orig != name:
        meta.append(str(orig))
    if year:
        meta.append(str(year))
    if category:
        meta.append(str(category))
    meta_line = " • ".join(meta) if meta else ""

    text = title
    if meta_line:
        text += f"\n{meta_line}"
    if rating:
        text += f"\nРейтинг: <b>{rating}</b>"
    if desc:
        text += f"\n\n{desc}"

    # Генерируем короткий токен для плеер-бота
    token = None
    try:
        async with session_scope() as session:
            sub = await get_subscription(session, cb.from_user.id)
            if not _is_sub_active(sub.end_at):
                await cb.message.answer("⛔️ Подписка не активна. Сначала оплати доступ.")
                return

            token = await create_content_request(
                session,
                cb.from_user.id,
                content_url=url,
                ttl_seconds=settings.content_request_ttl_seconds,
            )
            await session.commit()

    except Exception:
        log.exception("Failed to create content_request token", extra={"tg_id": cb.from_user.id, "url": url})

    player_link = None
    if token:
        player_link = f"https://t.me/{settings.player_bot_username}?start={token}"

    keyboard: list[list[InlineKeyboardButton]] = []
    if player_link:
        keyboard.append([InlineKeyboardButton(text="▶️ Смотреть онлайн", url=player_link)])

    keyboard.append([InlineKeyboardButton(text="🌐 Открыть на Rezka", url=url)])
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:kinoteka")])

    kb = InlineKeyboardMarkup(inline_keyboard=keyboard)

    poster_url = info.get("thumbnail_hq") or info.get("thumbnail")
    if poster_url:
        try:
            await cb.message.answer_photo(
                photo=poster_url,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb
            )
            return
        except Exception:
            pass  # если фото не загрузилось — просто текст

    await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
