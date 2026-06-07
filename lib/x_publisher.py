"""
X (Twitter) API v2 publisher using OAuth 1.0a user-context auth.

Single text tweet publish:
    POST https://api.twitter.com/2/tweets   body: {"text": "..."}

Auth: OAuth 1.0a (consumer key/secret + access token/secret), signed with
HMAC-SHA1 via ``requests-oauthlib``. This is the X analogue of
``lib/threads_publisher.py`` (which uses a Graph API bearer token).

Env vars (all required):
    KAWATMS_X_CONSUMER_KEY
    KAWATMS_X_CONSUMER_SECRET
    KAWATMS_X_ACCESS_TOKEN
    KAWATMS_X_ACCESS_TOKEN_SECRET

Character counting:
- X does NOT use unicode code-point counts. Most CJK / full-width / emoji
  characters weigh 2, ASCII weighs 1, and every URL is shortened by t.co to a
  fixed weight of 23 regardless of its real length. The hard limit is 280
  *weighted* units. ``weighted_len`` implements a conservative approximation
  (non-ASCII = 2, URL = 23) so we never build a tweet the API will reject.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

import requests

LOG = logging.getLogger(__name__)

# Every URL is collapsed to a fixed t.co weight by X, independent of its length.
URL_RE = re.compile(r"https?://\S+")
URL_WEIGHT = 23


class XError(RuntimeError):
    pass


def weighted_len(text: str) -> int:
    """
    Approximate X's weighted character count for ``text``.

    - Each URL counts as a fixed ``URL_WEIGHT`` (t.co shortening).
    - Each remaining ASCII character counts as 1.
    - Each remaining non-ASCII character (CJK, kana, full-width, emoji) counts
      as 2. This over-counts a few BMP symbols but never under-counts, which
      keeps us safely under the 280 limit.
    """
    if not text:
        return 0
    url_count = len(URL_RE.findall(text))
    without_urls = URL_RE.sub("", text)
    total = url_count * URL_WEIGHT
    for ch in without_urls:
        total += 1 if ch.isascii() else 2
    return total


class XPublisher:
    API_URL = "https://api.twitter.com/2/tweets"
    TEXT_LIMIT = 280  # weighted units, NOT unicode code points

    def __init__(
        self,
        consumer_key: Optional[str] = None,
        consumer_secret: Optional[str] = None,
        access_token: Optional[str] = None,
        access_token_secret: Optional[str] = None,
    ):
        self.consumer_key = consumer_key or os.environ.get("KAWATMS_X_CONSUMER_KEY")
        self.consumer_secret = consumer_secret or os.environ.get(
            "KAWATMS_X_CONSUMER_SECRET"
        )
        self.access_token = access_token or os.environ.get("KAWATMS_X_ACCESS_TOKEN")
        self.access_token_secret = access_token_secret or os.environ.get(
            "KAWATMS_X_ACCESS_TOKEN_SECRET"
        )
        missing = [
            name
            for name, val in (
                ("KAWATMS_X_CONSUMER_KEY", self.consumer_key),
                ("KAWATMS_X_CONSUMER_SECRET", self.consumer_secret),
                ("KAWATMS_X_ACCESS_TOKEN", self.access_token),
                ("KAWATMS_X_ACCESS_TOKEN_SECRET", self.access_token_secret),
            )
            if not val
        ]
        if missing:
            raise XError(f"Missing X credentials: {', '.join(missing)}")

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _auth(self):
        try:
            from requests_oauthlib import OAuth1  # type: ignore
        except ImportError as e:  # pragma: no cover - dependency missing
            raise XError(
                "requests-oauthlib is required for X posting "
                "(pip install requests-oauthlib)"
            ) from e
        return OAuth1(
            self.consumer_key,
            self.consumer_secret,
            self.access_token,
            self.access_token_secret,
            signature_type="auth_header",
        )

    def _validate_text(self, text: str) -> None:
        if not text:
            raise XError("text is required")
        weight = weighted_len(text)
        if weight > self.TEXT_LIMIT:
            raise XError(
                f"text too long: {weight} weighted units (max {self.TEXT_LIMIT})"
            )

    # ------------------------------------------------------------------
    # publish
    # ------------------------------------------------------------------
    def create_text_post(self, text: str, timeout: int = 60) -> str:
        """
        Publish a single text tweet. Returns the published tweet id.
        """
        self._validate_text(text)
        try:
            resp = requests.post(
                self.API_URL,
                json={"text": text},
                auth=self._auth(),
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise XError(f"Network error on POST /2/tweets: {e}") from e
        if resp.status_code >= 400:
            raise XError(
                f"POST /2/tweets HTTP {resp.status_code}: {resp.text[:600]}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise XError(f"Invalid JSON from POST /2/tweets: {e}") from e
        tweet_id = (data.get("data") or {}).get("id")
        if not tweet_id:
            raise XError(f"No tweet id in /2/tweets response: {data}")
        LOG.info("Published tweet: %s", tweet_id)
        return tweet_id
