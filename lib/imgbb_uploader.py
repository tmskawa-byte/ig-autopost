"""
ImgBB upload wrapper.

Docs: https://api.imgbb.com/

We always upload as base64 (multipart 'image' field). Returns the direct CDN URL
(NOT the viewer page URL) which Instagram can fetch.

Free tier limit: 32 MB. Our 2K JPG/PNG output is well under that.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional

import requests

LOG = logging.getLogger(__name__)


class ImgBBError(RuntimeError):
    pass


def upload(
    image_bytes: bytes,
    api_key: Optional[str] = None,
    name: Optional[str] = None,
    expiration_seconds: Optional[int] = None,
    timeout: int = 120,
) -> str:
    """
    Upload image bytes to ImgBB. Returns the public direct URL.
    """
    api_key = api_key or os.environ.get("IMGBB_API_KEY")
    if not api_key:
        raise ImgBBError("IMGBB_API_KEY is not set")
    if not image_bytes:
        raise ImgBBError("Empty image bytes")

    encoded = base64.b64encode(image_bytes).decode("ascii")
    data = {"key": api_key, "image": encoded}
    if name:
        data["name"] = name
    params = {}
    if expiration_seconds:
        # ImgBB accepts ?expiration=SECONDS (60..15552000).
        params["expiration"] = str(int(expiration_seconds))

    try:
        resp = requests.post(
            "https://api.imgbb.com/1/upload",
            data=data,
            params=params,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise ImgBBError(f"Network error: {e}") from e

    if resp.status_code >= 400:
        raise ImgBBError(f"HTTP {resp.status_code}: {resp.text[:400]}")

    try:
        body = resp.json()
    except ValueError as e:
        raise ImgBBError(f"Invalid JSON: {e}") from e

    if not body.get("success"):
        raise ImgBBError(f"Upload not successful: {body}")

    try:
        url = body["data"]["url"]
    except (KeyError, TypeError) as e:
        raise ImgBBError(f"Unexpected response shape: {e} body={body}") from e
    LOG.info("ImgBB upload OK: %s", url)
    return url
