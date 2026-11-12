"""Background images behind the `ImageProvider` interface: Pixabay or Pexels
stock photos, whichever has a key (Pixabay wins if both do). An AI image model
can be added later as another provider without touching the pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class ImageError(RuntimeError):
    pass


@dataclass
class Photo:
    path: Path
    source: str
    source_id: str
    credit: str
    page_url: str
    alt: str = ""


class ImageProvider(Protocol):
    async def find(self, query: str, out_dir: Path, exclude_ids: set[str]) -> Photo: ...


class PexelsProvider:
    API = "https://api.pexels.com/v1/search"

    def __init__(self, api_key: str, http: httpx.AsyncClient | None = None):
        if not api_key:
            raise ImageError("PEXELS_API_KEY is not set")
        self.api_key = api_key
        self.http = http or httpx.AsyncClient(timeout=30)

    async def _search(self, query: str) -> list[dict]:
        r = await self.http.get(
            self.API,
            params={"query": query, "orientation": "portrait", "per_page": 20, "size": "large"},
            headers={"Authorization": self.api_key},
        )
        if r.status_code != 200:
            raise ImageError(f"Pexels {r.status_code}: {r.text[:200]}")
        return r.json().get("photos", [])

    async def find(self, query: str, out_dir: Path, exclude_ids: set[str]) -> Photo:
        photos = await self._search(query)
        if not photos and " " in query:
            # Retry broader: drop the last word.
            photos = await self._search(query.rsplit(" ", 1)[0])
        # Tall enough to fill 1080x1350 without upscaling, and not used before.
        fresh = [p for p in photos if str(p["id"]) not in exclude_ids and p.get("height", 0) >= 1350]
        if not fresh:
            raise ImageError(f"No usable Pexels photo for '{query}'")
        pick = fresh[0]
        url = pick["src"].get("large2x") or pick["src"]["original"]
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"pexels-{pick['id']}.jpg"
        if not path.exists():
            img = await self.http.get(url, follow_redirects=True)
            img.raise_for_status()
            path.write_bytes(img.content)
        return Photo(
            path=path, source="pexels", source_id=str(pick["id"]),
            credit=pick.get("photographer", ""), page_url=pick.get("url", ""), alt=pick.get("alt") or "",
        )


class PixabayProvider:
    """Pixabay terms: download the image to your own server (no hotlinking)
    and keep requests modest (100 per minute). One search a week is fine."""

    API = "https://pixabay.com/api/"
    MIN_HEIGHT = 1350  # original size; the downloadable copy is at most 1280 px

    def __init__(self, api_key: str, http: httpx.AsyncClient | None = None):
        if not api_key:
            raise ImageError("PIXABAY_API_KEY is not set")
        self.api_key = api_key
        self.http = http or httpx.AsyncClient(timeout=30)

    async def _search(self, query: str) -> list[dict]:
        r = await self.http.get(
            self.API,
            params={"key": self.api_key, "q": query[:100], "image_type": "photo", "orientation": "vertical",
                    "safesearch": "true", "per_page": 20},
        )
        if r.status_code != 200:
            raise ImageError(f"Pixabay {r.status_code}: {r.text[:200]}")
        return r.json().get("hits", [])

    async def find(self, query: str, out_dir: Path, exclude_ids: set[str]) -> Photo:
        photos = await self._search(query)
        if not photos and " " in query:
            photos = await self._search(query.rsplit(" ", 1)[0])
        fresh = [p for p in photos
                 if str(p["id"]) not in exclude_ids and p.get("imageHeight", 0) >= self.MIN_HEIGHT
                 and p.get("largeImageURL")]
        if not fresh:
            raise ImageError(f"No usable Pixabay photo for '{query}'")
        pick = fresh[0]
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"pixabay-{pick['id']}.jpg"
        if not path.exists():
            img = await self.http.get(pick["largeImageURL"], follow_redirects=True)
            img.raise_for_status()
            path.write_bytes(img.content)
        return Photo(
            path=path, source="pixabay", source_id=str(pick["id"]),
            credit=pick.get("user", ""), page_url=pick.get("pageURL", ""), alt=pick.get("tags") or "",
        )


def get_provider() -> ImageProvider | None:
    """The configured photo source, or None when no key is set."""
    s = get_settings()
    if s.pixabay_api_key:
        return PixabayProvider(s.pixabay_api_key)
    if s.pexels_api_key:
        return PexelsProvider(s.pexels_api_key)
    return None


def credit_line(photo: dict | None) -> str | None:
    """'Ana / Pixabay' for the image and the Telegram draft."""
    if not photo or not photo.get("credit"):
        return None
    return f"{photo['credit']} / {(photo.get('source') or 'pexels').capitalize()}"
