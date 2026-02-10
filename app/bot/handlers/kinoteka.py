from __future__ import annotations

import json

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.bot.keyboards import kb_kinoteka_back
from app.bot.ui import utcnow
from app.db.models.user import User
from app.db.session import session_scope
from app.repo import get_subscription
from app.services.poiskkino.client import poiskkino_client, PoiskKinoError

router = Router()


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

    # Check subscription (same as VPN/Yandex locks)
    async with session_scope() as session:
        sub = await get_subscription(session, cb.from_user.id)
        if not _is_sub_active(sub.end_at):
            await cb.message.answer("⛔️ Подписка не активна. Сначала оплати доступ.")
            return

        user = await session.get(User, cb.from_user.id)
        if not user:
            # ensure_user is called inside get_subscription
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
        return

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if not user or user.flow_state != "await_kino_query":
            return

        # stop flow no matter what (avoid stuck state)
        user.flow_state = None
        user.flow_data = None
        await session.commit()

    try:
        data = await poiskkino_client.search(query, limit=6, page=1)
    except PoiskKinoError:
        await msg.answer(
            "⚠️ Не удалось получить данные из Кинотеки.\n"
            "Проверь API-ключ/лимит и попробуй ещё раз.",
            reply_markup=kb_kinoteka_back(),
        )
        return
    except Exception:
        await msg.answer(
            "⚠️ Временная ошибка Кинотеки. Попробуй ещё раз позже.",
            reply_markup=kb_kinoteka_back(),
        )
        return

    docs = data.get("docs") if isinstance(data, dict) else None
    if not docs:
        await msg.answer(
            "Ничего не нашёл 😕\n\nПопробуй другое название.",
            reply_markup=kb_kinoteka_back(),
        )
        return

    # Render compact list with buttons (up to 6)
    kb = []
    lines = ["🎬 <b>Результаты</b>:"]
    for i, m in enumerate(docs[:6], start=1):
        mid = m.get("id")
        name = m.get("name") or m.get("alternativeName") or m.get("enName") or "Без названия"
        year = m.get("year")
        rating_kp = ((m.get("rating") or {}).get("kp"))
        rating_imdb = ((m.get("rating") or {}).get("imdb"))

        rating_bits = []
        if rating_kp:
            rating_bits.append(f"КП {rating_kp}")
        if rating_imdb:
            rating_bits.append(f"IMDb {rating_imdb}")
        rating_str = (" / ".join(rating_bits)) if rating_bits else "—"

        title_line = f"{i}) {name}"
        if year:
            title_line += f" ({year})"
        lines.append(f"{title_line} — {rating_str}")

        if isinstance(mid, int):
            kb.append([InlineKeyboardButton(text=f"{i}️⃣ Открыть", callback_data=f"kino:item:{mid}")])

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
        movie_id = int(cb.data.split(":", 2)[2])
    except Exception:
        return

    try:
        m = await poiskkino_client.get_movie(movie_id)
    except PoiskKinoError:
        await cb.message.answer(
            "⚠️ Не удалось открыть карточку. Возможно, закончился лимит или неверный API-ключ.",
            reply_markup=kb_kinoteka_back(),
        )
        return
    except Exception:
        await cb.message.answer("⚠️ Временная ошибка. Попробуй ещё раз позже.")
        return

    name = m.get("name") or m.get("alternativeName") or m.get("enName") or "Без названия"
    year = m.get("year")
    desc = m.get("description") or m.get("shortDescription") or ""
    if desc:
        desc = desc.strip()
        if len(desc) > 800:
            desc = desc[:800].rsplit(" ", 1)[0] + "…"

    rating = m.get("rating") or {}
    kp = rating.get("kp")
    imdb = rating.get("imdb")

    type_ = m.get("type")  # movie | tv-series etc
    title = f"🎬 <b>{name}</b>"
    meta = []
    if year:
        meta.append(str(year))
    if type_:
        meta.append(str(type_))
    meta_line = " • ".join(meta) if meta else ""

    r_line_bits = []
    if kp:
        r_line_bits.append(f"КП: <b>{kp}</b>")
    if imdb:
        r_line_bits.append(f"IMDb: <b>{imdb}</b>")
    r_line = " | ".join(r_line_bits) if r_line_bits else ""

    text = title
    if meta_line:
        text += f"\n{meta_line}"
    if r_line:
        text += f"\n{r_line}"
    if desc:
        text += f"\n\n{desc}"

    # Poster if available
    poster_url = ((m.get("poster") or {}).get("url"))
    if poster_url:
        try:
            await cb.message.answer_photo(poster_url, caption=text, parse_mode="HTML", reply_markup=kb_kinoteka_back())
            return
        except Exception:
            pass

    await cb.message.answer(text, parse_mode="HTML", reply_markup=kb_kinoteka_back())
