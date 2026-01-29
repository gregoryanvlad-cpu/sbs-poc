from app.core.config import settings


def is_owner(tg_id: int) -> bool:
    return int(tg_id) == int(settings.owner_tg_id)
