from app.core.config import settings


def is_owner(tg_id: int) -> bool:
    tid = int(tg_id)
    if tid == int(settings.owner_tg_id):
        return True
    return tid in set(settings.admin_tg_ids)


def is_admin(tg_id: int) -> bool:
    """Alias for backward/forward compatibility."""
    return is_owner(tg_id)
