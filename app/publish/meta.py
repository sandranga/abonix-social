"""Facebook Page + Instagram publishing through the Graph API, with httpx.

Flows (Meta docs, content publishing):
  Instagram: POST /{ig-user-id}/media (container) -> poll status_code until
             FINISHED -> POST /{ig-user-id}/media_publish
  Facebook:  POST /{page-id}/photos with url + message (Page access token)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class MetaError(RuntimeError):
    def __init__(self, msg: str, *, retryable: bool = True, payload: Any = None):
        super().__init__(msg)
        self.retryable = retryable
        self.payload = payload


# Graph error codes that will not fix themselves by retrying.
NON_RETRYABLE_CODES = {10, 100, 190, 200, 210, 803, 9004, 36003}


class MetaClient:
    def __init__(self, token: str, page_id: str, ig_user_id: str, version: str,
                 http: httpx.AsyncClient | None = None):
        if not token:
            raise MetaError("META_ACCESS_TOKEN is not set", retryable=False)
        self.token = token
        self.page_id = page_id
        self.ig_user_id = ig_user_id
        self.base = f"https://graph.facebook.com/{version}"
        self.http = http or httpx.AsyncClient(timeout=60)
        self._page_token: str | None = None

    async def _call(self, method: str, path: str, *, token: str | None = None, **params: Any) -> dict[str, Any]:
        params["access_token"] = token or self.token
        url = f"{self.base}/{path.lstrip('/')}"
        try:
            if method == "GET":
                r = await self.http.get(url, params=params)
            else:
                r = await self.http.post(url, data=params)
        except httpx.HTTPError as e:
            raise MetaError(f"network error calling {path}: {e}") from e
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:500]}
        if r.status_code >= 400 or "error" in body:
            err = body.get("error", {}) if isinstance(body, dict) else {}
            code = err.get("code")
            msg = f"Graph {path} {r.status_code}: {err.get('message', body)} (code {code}, subcode {err.get('error_subcode')})"
            retryable = r.status_code >= 500 or code not in NON_RETRYABLE_CODES
            raise MetaError(msg, retryable=retryable, payload=body)
        return body

    # --- health ------------------------------------------------------------
    async def check(self) -> dict[str, Any]:
        """Plan step 1 verification: the token sees the Page and its IG account."""
        me = await self._call("GET", "me", fields="id,name")
        if not self.page_id:
            # First run: show which Pages this token can see, with their IG ids,
            # so META_PAGE_ID / META_IG_USER_ID can be filled in.
            body = await self._call("GET", "me/accounts", fields="id,name,instagram_business_account")
            pages = [{"page_id": p.get("id"), "name": p.get("name"),
                      "instagram_business_account": (p.get("instagram_business_account") or {}).get("id")}
                     for p in body.get("data", [])]
            hint = ("Copy page_id into META_PAGE_ID and instagram_business_account into META_IG_USER_ID, "
                    "then run meta-check again." if pages else
                    "The token sees no Pages: assign the Page to the System User (Business settings -> "
                    "System users -> Assign assets), then generate a new token.")
            return {"token_owner": me, "pages": pages, "next_step": hint}
        page = await self._call("GET", self.page_id, fields="id,name,instagram_business_account")
        ig_id = (page.get("instagram_business_account") or {}).get("id")
        out: dict[str, Any] = {"token_owner": me, "page": {"id": page.get("id"), "name": page.get("name")},
                               "instagram_business_account": ig_id}
        if self.ig_user_id:
            out["ig_matches_config"] = ig_id == self.ig_user_id
            out["publishing_limit"] = await self.publishing_limit()
        return out

    async def publishing_limit(self) -> dict[str, Any]:
        body = await self._call("GET", f"{self.ig_user_id}/content_publishing_limit", fields="config,quota_usage")
        data = (body.get("data") or [{}])[0]
        return {"quota_usage": data.get("quota_usage", 0), "quota_total": (data.get("config") or {}).get("quota_total", 100)}

    async def page_token(self) -> str:
        if self._page_token is None:
            body = await self._call("GET", self.page_id, fields="access_token")
            tok = body.get("access_token")
            if not tok:
                raise MetaError("could not get a Page access token: is the Page assigned to the System User?",
                                retryable=False, payload=body)
            self._page_token = tok
        return self._page_token

    # --- Facebook ----------------------------------------------------------
    async def publish_facebook_photo(self, image_url: str, message: str) -> str:
        body = await self._call("POST", f"{self.page_id}/photos", token=await self.page_token(),
                                url=image_url, message=message, published="true")
        return body.get("post_id") or body["id"]

    # --- Instagram ---------------------------------------------------------
    async def create_ig_container(self, image_url: str, caption: str, alt_text: str | None = None) -> str:
        params: dict[str, Any] = {"image_url": image_url, "caption": caption}
        if alt_text:
            params["alt_text"] = alt_text
        body = await self._call("POST", f"{self.ig_user_id}/media", **params)
        return body["id"]

    async def wait_ig_container(self, container_id: str, timeout_s: float = 120, every_s: float = 5) -> None:
        waited = 0.0
        while True:
            body = await self._call("GET", container_id, fields="status_code,status")
            status = body.get("status_code")
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise MetaError(f"IG container {container_id} {status}: {body.get('status')}",
                                retryable=status == "EXPIRED", payload=body)
            if waited >= timeout_s:
                raise MetaError(f"IG container {container_id} still {status} after {timeout_s}s")
            await asyncio.sleep(every_s)
            waited += every_s

    async def publish_ig_container(self, container_id: str) -> str:
        body = await self._call("POST", f"{self.ig_user_id}/media_publish", creation_id=container_id)
        return body["id"]

    async def delete(self, object_id: str) -> None:
        """Used by `cli.py meta-test --delete` (FB posts only; IG media cannot be deleted via API)."""
        await self._call("POST", object_id, token=await self.page_token(), method="delete")


def get_meta() -> MetaClient:
    s = get_settings()
    return MetaClient(s.meta_access_token, s.meta_page_id, s.meta_ig_user_id, s.meta_graph_version)
