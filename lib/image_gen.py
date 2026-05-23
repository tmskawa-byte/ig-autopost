"""
Image utility helpers.

`fetch_image_bytes` accepts either a data URL (data:image/png;base64,...)
or a regular http(s) URL and returns (bytes, mime_type).

Nano Banana Pro typically returns data URLs, but we accept either for safety.
"""
from __future__ import annotations

import base64
import logging
from typing import Tuple

import requests

LOG = logging.getLogger(__name__)


class ImageFetchError(RuntimeError):
    pass


def fetch_image_bytes(url: str, timeout: int = 60) -> Tuple[bytes, str]:
    """
    Returns (raw_bytes, mime_type).
    Raises ImageFetchError on any failure.
    """
    if not url:
        raise ImageFetchError("Empty image URL")

    if url.startswith("data:"):
        # data:image/png;base64,XXXX
        try:
            header, payload = url.split(",", 1)
        except ValueError as e:
            raise ImageFetchError(f"Malformed data URL: {e}") from e
        mime = "image/jpeg"
        if header.startswith("data:") and ";" in header:
            mime = header[len("data:"):].split(";")[0] or "image/jpeg"
        try:
            data = base64.b64decode(payload, validate=False)
        except Exception as e:  # broad: base64 errors vary
            raise ImageFetchError(f"Base64 decode failed: {e}") from e
        return data, mime

    if url.startswith("http://") or url.startswith("https://"):
        try:
            resp = requests.get(url, timeout=timeout)
        except requests.RequestException as e:
            raise ImageFetchError(f"HTTP error fetching image: {e}") from e
        if resp.status_code >= 400:
            raise ImageFetchError(
                f"HTTP {resp.status_code} fetching image: {resp.text[:200]}"
            )
        mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        return resp.content, mime or "image/jpeg"

    raise ImageFetchError(f"Unsupported URL scheme: {url[:40]}")
