"""Puts the final JPEGs / MP4 at a public URL, which Meta needs to download them.

Production: Cloudflare R2 bucket served on media.abonix.net (a custom domain
on the bucket), so nothing on the Abonix server is exposed or changed.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Protocol

from app.config import get_settings


class StorageError(RuntimeError):
    pass


def content_type(path: Path) -> str:
    return {".mp4": "video/mp4", ".png": "image/png"}.get(path.suffix.lower(), "image/jpeg")


class MediaStore(Protocol):
    async def put(self, local: Path, key: str) -> str: ...


class R2Store:
    def __init__(self, account_id: str, access_key: str, secret_key: str, bucket: str, public_base: str):
        if not (account_id and access_key and secret_key):
            raise StorageError("R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are not set")
        import boto3  # imported lazily: only needed in production

        self.bucket = bucket
        self.public_base = public_base.rstrip("/")
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )

    def _put_sync(self, local: Path, key: str) -> None:
        self.s3.upload_file(
            str(local), self.bucket, key,
            ExtraArgs={"ContentType": content_type(local), "CacheControl": "public, max-age=31536000, immutable"},
        )

    async def put(self, local: Path, key: str) -> str:
        await asyncio.to_thread(self._put_sync, local, key)
        return f"{self.public_base}/{key}"


class LocalStore:
    """Dev only: copies into data/media/public. The URL is only reachable if
    something serves that folder; publishing to Meta will not work with it."""

    def __init__(self, root: Path, public_base: str):
        self.root = root
        self.public_base = public_base.rstrip("/")

    async def put(self, local: Path, key: str) -> str:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        if local.resolve() != dest.resolve():
            shutil.copyfile(local, dest)
        return f"{self.public_base}/{key}"


def get_store() -> MediaStore:
    s = get_settings()
    if s.storage_backend == "r2":
        return R2Store(s.r2_account_id, s.r2_access_key_id, s.r2_secret_access_key, s.r2_bucket, s.media_public_base_url)
    return LocalStore(s.media_dir / "public", s.media_public_base_url)
