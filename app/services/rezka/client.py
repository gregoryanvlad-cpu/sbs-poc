from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from HdRezkaApi import HdRezkaApi
from HdRezkaApi.search import HdRezkaSearch

from app.core.config import settings

log = logging.getLogger(__name__)


class RezkaError(RuntimeError):
    pass


@dataclass(frozen=True)
class RezkaClient:
    origin: str

    def _search_sync(self, query: str, limit: int) -> list[dict[str, Any]]:
        # HdRezkaSearch returns list of {"title","url","rating"}
        try:
            search = HdRezkaSearch(self.origin)
            results = search.fast_search(query) or []
            return results[:limit]
        except Exception as e:
            raise RezkaError(f"Rezka search failed: {type(e).__name__}: {e}") from e

    async def search(self, query: str, *, limit: int = 6) -> list[dict[str, Any]]:
        # Run blocking requests/bs4 in thread to avoid blocking aiogram loop
        return await asyncio.to_thread(self._search_sync, query, limit)

    def _get_info_sync(self, url: str) -> dict[str, Any]:
        try:
            api = HdRezkaApi(url)
            if not api.ok:
                raise RezkaError(f"Rezka open failed: {api.exception}")
            rating = None
            try:
                if getattr(api, "rating", None) and hasattr(api.rating, "value"):
                    rating = api.rating.value
            except Exception:
                rating = None

            return {
                "url": url,
                "name": getattr(api, "name", None),
                "orig_name": getattr(api, "origName", None),
                "description": getattr(api, "description", None),
                "thumbnail": getattr(api, "thumbnail", None),
                "thumbnail_hq": getattr(api, "thumbnailHQ", None),
                "rating": rating,
                "category": str(getattr(api, "category", "")) if getattr(api, "category", None) else None,
                "year": getattr(api, "year", None),
            }
        except RezkaError:
            raise
        except Exception as e:
            raise RezkaError(f"Rezka get_info failed: {type(e).__name__}: {e}") from e

    async def get_info(self, url: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_info_sync, url)


rezka_client = RezkaClient(origin=settings.rezka_origin)
