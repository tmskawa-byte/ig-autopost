"""
Threads Graph API publisher.

Two-step publish (text or single-image):
    1. POST /{user-id}/threads             -> creation_id (container)
    2. (poll) GET /{creation_id}?fields=status,error_message  until FINISHED
    3. POST /{user-id}/threads_publish     -> post_id

Docs:
- https://developers.facebook.com/docs/threads
- https://developers.facebook.com/docs/threads/posts
- https://developers.facebook.com/docs/threads/reference/media

Requires a Threads access token (long-lived, 60-day expiry) issued by Meta
for the Threads-enabled user / business account.

Notes:
- Threads text limit: 500 characters per post.
- IMAGE posts require a publicly fetchable https:// URL.
- ``link_attachment`` produces a tappable preview card; it is only honored
  for TEXT (no image / video / carousel) posts.
- Some accounts may require ``appsecret_proof`` (HMAC-SHA256 of access_token
  with app_secret). This client adds it automatically when THREADS_APP_SECRET
  is set in the environment.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any, Dict, Optional

import requests

LOG = logging.getLogger(__name__)


class ThreadsError(RuntimeError):
    pass


class ThreadsPublisher:
    GRAPH_API_VERSION = "v1.0"
    GRAPH_HOST = "https://graph.threads.net"
    TEXT_LIMIT = 500

    def __init__(
        self,
        access_token: Optional[str] = None,
        user_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        api_version: Optional[str] = None,
    ):
        self.access_token = access_token or os.environ.get("THREADS_ACCESS_TOKEN")
        self.user_id = user_id or os.environ.get("THREADS_USER_ID")
        # app_secret is optional. If provided we send appsecret_proof on every call.
        self.app_secret = app_secret or os.environ.get("THREADS_APP_SECRET")
        if not self.access_token:
            raise ThreadsError("THREADS_ACCESS_TOKEN is not set")
        if not self.user_id:
            raise ThreadsError("THREADS_USER_ID is not set")
        if api_version:
            self.GRAPH_API_VERSION = api_version

    @property
    def base_url(self) -> str:
        return f"{self.GRAPH_HOST}/{self.GRAPH_API_VERSION}"

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _appsecret_proof(self) -> Optional[str]:
        if not self.app_secret:
            return None
        return hmac.new(
            self.app_secret.encode("utf-8"),
            self.access_token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _auth_params(self) -> Dict[str, str]:
        params: Dict[str, str] = {"access_token": self.access_token}
        proof = self._appsecret_proof()
        if proof:
            params["appsecret_proof"] = proof
        return params

    def _post(self, path: str, params: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
        url = f"{self.base_url}/{path}"
        body = dict(params)
        body.update(self._auth_params())
        try:
            resp = requests.post(url, data=body, timeout=timeout)
        except requests.RequestException as e:
            raise ThreadsError(f"Network error on POST {path}: {e}") from e
        if resp.status_code >= 400:
            raise ThreadsError(f"POST {path} HTTP {resp.status_code}: {resp.text[:600]}")
        try:
            return resp.json()
        except ValueError as e:
            raise ThreadsError(f"Invalid JSON from POST {path}: {e}") from e

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Dict[str, Any]:
        url = f"{self.base_url}/{path}"
        q = dict(params or {})
        q.update(self._auth_params())
        try:
            resp = requests.get(url, params=q, timeout=timeout)
        except requests.RequestException as e:
            raise ThreadsError(f"Network error on GET {path}: {e}") from e
        if resp.status_code >= 400:
            raise ThreadsError(f"GET {path} HTTP {resp.status_code}: {resp.text[:600]}")
        try:
            return resp.json()
        except ValueError as e:
            raise ThreadsError(f"Invalid JSON from GET {path}: {e}") from e

    # ------------------------------------------------------------------
    # container creation
    # ------------------------------------------------------------------
    def _validate_text(self, text: str) -> None:
        if text is None:
            raise ThreadsError("text is required")
        if len(text) > self.TEXT_LIMIT:
            raise ThreadsError(
                f"text too long: {len(text)} chars (max {self.TEXT_LIMIT})"
            )

    def create_text_container(
        self,
        text: str,
        link_attachment: Optional[str] = None,
    ) -> str:
        """
        Create a TEXT container. Optionally attach a link preview card.
        Returns creation_id.
        """
        self._validate_text(text)
        params: Dict[str, Any] = {
            "media_type": "TEXT",
            "text": text,
        }
        if link_attachment:
            if not link_attachment.startswith("https://"):
                raise ThreadsError(
                    f"link_attachment must be https://: {link_attachment[:60]}"
                )
            params["link_attachment"] = link_attachment

        result = self._post(f"{self.user_id}/threads", params, timeout=60)
        creation_id = result.get("id")
        if not creation_id:
            raise ThreadsError(f"No `id` in /threads response: {result}")
        LOG.info("Created Threads TEXT container: %s", creation_id)
        return creation_id

    def create_image_container(
        self,
        text: str,
        image_url: str,
        link_attachment: Optional[str] = None,
    ) -> str:
        """
        Create an IMAGE container. ``text`` is the caption (max 500 chars).
        Note: link_attachment is generally ignored by Threads on media posts.
        Returns creation_id.
        """
        self._validate_text(text)
        if not image_url.startswith("https://"):
            raise ThreadsError(f"image_url must be https://: {image_url[:60]}")

        params: Dict[str, Any] = {
            "media_type": "IMAGE",
            "image_url": image_url,
            "text": text,
        }
        if link_attachment:
            # Send it anyway in case the account-level capability accepts it.
            if not link_attachment.startswith("https://"):
                raise ThreadsError(
                    f"link_attachment must be https://: {link_attachment[:60]}"
                )
            params["link_attachment"] = link_attachment

        result = self._post(f"{self.user_id}/threads", params, timeout=60)
        creation_id = result.get("id")
        if not creation_id:
            raise ThreadsError(f"No `id` in /threads response: {result}")
        LOG.info("Created Threads IMAGE container: %s", creation_id)
        return creation_id

    # ------------------------------------------------------------------
    # status polling
    # ------------------------------------------------------------------
    def wait_for_container_ready(
        self,
        creation_id: str,
        max_wait: int = 180,
        poll_interval: int = 5,
    ) -> None:
        """
        Poll ``status`` until FINISHED. Raises on ERROR / EXPIRED / timeout.

        Threads uses ``status`` (not ``status_code`` like IG).
        Values: IN_PROGRESS, FINISHED, ERROR, PUBLISHED, EXPIRED.
        """
        elapsed = 0
        while elapsed < max_wait:
            data = self._get(creation_id, {"fields": "status,error_message"})
            status = (data.get("status") or "").upper()
            LOG.info("Container %s status: %s (%ds)", creation_id, status, elapsed)
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                err = data.get("error_message") or "(no error_message)"
                raise ThreadsError(
                    f"Container {creation_id} failed: status={status} error={err}"
                )
            time.sleep(poll_interval)
            elapsed += poll_interval
        raise ThreadsError(
            f"Container {creation_id} did not reach FINISHED within {max_wait}s"
        )

    # ------------------------------------------------------------------
    # publish
    # ------------------------------------------------------------------
    def publish_container(self, creation_id: str) -> str:
        """
        Publish a FINISHED container. Returns the published thread id.
        """
        result = self._post(
            f"{self.user_id}/threads_publish",
            {"creation_id": creation_id},
            timeout=60,
        )
        post_id = result.get("id")
        if not post_id:
            raise ThreadsError(f"No `id` in /threads_publish response: {result}")
        LOG.info("Published Threads post: %s", post_id)
        return post_id

    # ------------------------------------------------------------------
    # high-level convenience
    # ------------------------------------------------------------------
    def create_text_post(
        self,
        text: str,
        link_attachment: Optional[str] = None,
    ) -> str:
        """Full TEXT publish flow. Returns the post id."""
        creation_id = self.create_text_container(text, link_attachment=link_attachment)
        self.wait_for_container_ready(creation_id)
        return self.publish_container(creation_id)

    def create_image_post(
        self,
        text: str,
        image_url: str,
        link_attachment: Optional[str] = None,
    ) -> str:
        """Full IMAGE publish flow. Returns the post id."""
        creation_id = self.create_image_container(
            text, image_url, link_attachment=link_attachment
        )
        self.wait_for_container_ready(creation_id)
        return self.publish_container(creation_id)
