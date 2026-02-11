# app/content/rezka_parser.py
import logging
from HdRezkaApi import HdRezkaApi

logger = logging.getLogger(__name__)

class RezkaParser:
    def __init__(self, mirror="https://rezka.ag"):
        # В разных версиях HdRezkaApi аргумент зеркала назывался по-разному.
        # В этом проекте закреплено HdRezkaApi==11.1.0 (ожидает url=),
        # но на всякий случай поддержим и старый mirror=.
        try:
            self.api = HdRezkaApi(url=mirror)
        except TypeError:
            self.api = HdRezkaApi(mirror=mirror)

    def search(self, query, limit=6):
        try:
            results = self.api.search(query)
            return [
                {"url": item.url, "title": item.title, "year": item.year or "—", "poster": item.poster}
                for item in results[:limit]
            ]
        except Exception as e:
            logger.error(f"Rezka search error: {e}")
            return []

    def get_details(self, url):
        try:
            item = self.api.get(url)
            if not item:
                return None

            streams = item.videos if hasattr(item, 'videos') else {}
            if not streams and hasattr(item, 'player'):
                streams = {"default": item.player}

            return {
                "title": item.title,
                "year": item.year,
                "poster": item.poster,
                "description": getattr(item, 'description', ''),
                "streams": streams,
                "seasons": getattr(item, 'seasons', {}) if hasattr(item, 'seasons') else None
            }
        except Exception as e:
            logger.error(f"Rezka details error: {url} → {e}")
            return None
