from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from app.core.config import settings

log = logging.getLogger(__name__)


class PoiskKinoError(RuntimeError):
    pass


@dataclass(frozen=True)
class PoiskKinoClient:
    base_url: str
    api_key: str | None

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"X-API-KEY": self.api_key}

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url.rstrip("/") + path
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=self._headers(), params=params) as resp:
                # Try to decode JSON even for errors (API often returns details).
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = await resp.text()

                if resp.status >= 400:
                    raise PoiskKinoError(f"PoiskKino {resp.status}: {data}")
                return data

    async def search(self, query: str, *, limit: int = 6, page: int = 1) -> dict[str, Any]:
        # Поиск фильмов/сериалов по названию
        # Документация сервиса использует версию v1.4
        return await self._get_json(
            "/v1.4/movie/search",
            params={"query": query, "limit": limit, "page": page},
        )

    async def get_movie(self, movie_id: int) -> dict[str, Any]:
        return await self._get_json(f"/v1.4/movie/{movie_id}")


poiskkino_client = PoiskKinoClient(
    base_url=settings.poiskkino_base_url,
    api_key=settings.poiskkino_api_key,
)
