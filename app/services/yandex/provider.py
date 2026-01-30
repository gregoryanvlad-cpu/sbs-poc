from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime, timedelta


@dataclass
class YandexFamilyMember:
    email: str
    is_admin: bool


@dataclass
class YandexPlusState:
    active: bool
    next_charge_at: Optional[datetime]
    members: List[YandexFamilyMember]
    max_members: int = 4  # админ + 3 участника


class BaseYandexProvider:
    async def get_state(self, *, storage_state_path: str) -> YandexPlusState:
        raise NotImplementedError


class MockYandexProvider(BaseYandexProvider):
    """
    Заглушка. Используется пока YANDEX_PROVIDER=mock
    Нужна, чтобы бот СТАБИЛЬНО запускался на Railway
    """

    async def get_state(self, *, storage_state_path: str) -> YandexPlusState:
        return YandexPlusState(
            active=True,
            next_charge_at=datetime.utcnow() + timedelta(days=9),
            members=[
                YandexFamilyMember(email="admin@yandex.ru", is_admin=True),
                YandexFamilyMember(email="user1@yandex.ru", is_admin=False),
            ],
        )
