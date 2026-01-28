
from sqlalchemy import select
from app.db.models.yandex_account import YandexAccount

async def pick_account(session, need_cover_until):
    q = (
        select(YandexAccount)
        .where(
            YandexAccount.status == "active",
            YandexAccount.plus_end_at >= need_cover_until,
            YandexAccount.used_slots < (YandexAccount.max_slots - 1),
        )
        .order_by(YandexAccount.used_slots.asc(), YandexAccount.plus_end_at.asc())
        .with_for_update()
    )
    res = await session.execute(q)
    return res.scalar_one_or_none()
