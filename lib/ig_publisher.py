"""
Instagram Graph API publisher.

Two-step single-image post:
    1. POST /{ig-business-id}/media          -> creation_id
    2. (poll) GET /{creation_id}?fields=status_code  until FINISHED
    3. POST /{ig-business-id}/media_publish  -> post_id

Docs:
- https://developers.facebook.com/docs/instagram-api/guides/content-publishing
- https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media

Requires Long-lived Access Token + an Instagram Business / Creator account
linked to a Facebook Page.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import requests

LOG = logging.getLogger(__name__)


class IGError(RuntimeError):
    pass


class IGPublisher:
    GRAPH_API_VERSION = "v21.0"

    def __init__(
        self,
        access_token: Optional[str] = None,
        business_id: Optional[str] = None,
        api_version: Optional[str] = None,
    ):
        self.access_token = access_token or os.environ.get("IG_ACCESS_TOKEN")
        self.business_id = business_id or os.environ.get("IG_BUSINESS_ID")
        if not self.access_token:
            raise IGError("IG_ACCESS_TOKEN is not set")
        if not self.business_id:
            raise IGError("IG_BUSINESS_ID is not set")
        if api_version:
            self.GRAPH_API_VERSION = api_version

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/{self.GRAPH_API_VERSION}"

    # ------------------------------------------------------------------
    # low-level
    # ------------------------------------------------------------------
    def _post(self, path: str, params: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
        url = f"{self.base_url}/{path}"
        body = dict(params)
        body["access_token"] = self.access_token
        try:
            resp = requests.post(url, data=body, timeout=timeout)
        except requests.RequestException as e:
            raise IGError(f"Network error on POST {path}: {e}") from e
        if resp.status_code >= 400:
            raise IGError(f"POST {path} HTTP {resp.status_code}: {resp.text[:600]}")
        try:
            return resp.json()
        except ValueError as e:
            raise IGError(f"Invalid JSON from POST {path}: {e}") from e

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Dict[str, Any]:
        url = f"{self.base_url}/{path}"
        q = dict(params or {})
        q["access_token"] = self.access_token
        try:
            resp = requests.get(url, params=q, timeout=timeout)
        except requests.RequestException as e:
            raise IGError(f"Network error on GET {path}: {e}") from e
        if resp.status_code >= 400:
            raise IGError(f"GET {path} HTTP {resp.status_code}: {resp.text[:600]}")
        try:
            return resp.json()
        except ValueError as e:
            raise IGError(f"Invalid JSON from GET {path}: {e}") from e

    # ------------------------------------------------------------------
    # high-level
    # ------------------------------------------------------------------
    def create_container(self, image_url: str, caption: str) -> str:
        """
        Create an IG media container for a single-image post.
        Returns creation_id.
        """
        if not image_url.startswith("https://"):
            raise IGError(f"image_url must be https://: {image_url[:60]}")
        if len(caption) > 2200:
            raise IGError(f"caption too long: {len(caption)} chars (max 2200)")

        result = self._post(
            f"{self.business_id}/media",
            {"image_url": image_url, "caption": caption},
            timeout=60,
        )
        creation_id = result.get("id")
        if not creation_id:
            raise IGError(f"No `id` in /media response: {result}")
        LOG.info("Created IG container: %s", creation_id)
        return creation_id

    def wait_for_container_ready(
        self,
        creation_id: str,
        max_wait: int = 180,
        poll_interval: int = 5,
    ) -> None:
        """
        Poll status_code until FINISHED. Raises on ERROR / EXPIRED / timeout.
        """
        elapsed = 0
        while elapsed < max_wait:
            data = self._get(creation_id, {"fields": "status_code"})
            code = (data.get("status_code") or "").upper()
            LOG.info("Container %s status: %s (%ds)", creation_id, code, elapsed)
            if code == "FINISHED":
                return
            if code in ("ERROR", "EXPIRED"):
                raise IGError(f"Container {creation_id} failed: status_code={code}")
            time.sleep(poll_interval)
            elapsed += poll_interval
        raise IGError(
            f"Container {creation_id} did not reach FINISHED within {max_wait}s"
        )

    def publish_container(self, creation_id: str) -> str:
        """
        Publish a FINISHED container. Returns the published media id.
        """
        result = self._post(
            f"{self.business_id}/media_publish",
            {"creation_id": creation_id},
            timeout=60,
        )
        post_id = result.get("id")
        if not post_id:
            raise IGError(f"No `id` in /media_publish response: {result}")
        LOG.info("Published IG post: %s", post_id)
        return post_id

    def post(self, image_url: str, caption: str) -> str:
        """
        Full single-image publish flow. Returns the post id.
        """
        creation_id = self.create_container(image_url, caption)
        self.wait_for_container_ready(creation_id)
        return self.publish_container(creation_id)
