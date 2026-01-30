from __future__ import annotations

from aiogram.types import Message, CallbackQuery

from app.db.models.user import User


class SubscriptionRequired(Exception):
    """Подписка не активна"""


def require_active_subscription(user: User) -> None:
    """
    Бросает исключение, если подписка не активна.
    """
    if not user:
        raise SubscriptionRequired

    if not user.subscription_active:
        raise SubscriptionRequired
