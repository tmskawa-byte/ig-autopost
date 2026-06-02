"""
Resolve a Threads URL / shortcode -> numeric ``quote_post_id`` usable in
``POST /{user-id}/threads`` with ``quote_post_id=...``.

This is the single biggest technical unknown of the Tavily-quote-repost
pipeline. We support three strategies, tried in order:

  Option A (default): pass the shortcode directly to ``quote_post_id``.
      Rationale: Meta's IG / FB Graph endpoints sometimes accept the
      shortcode in place of the numeric media_id. This is the cheapest
      attempt; if Threads rejects it the container creation call returns
      a clear error message and we fall through.

  Option B (skipped, documented only): the public oEmbed endpoint
      ``https://www.threads.com/oembed.json?url=<post-url>`` typically
      embeds ``media_id`` in its iframe HTML. Meta's TOS for oEmbed
      explicitly forbids using it as a way to derive media_id, so we do
      NOT implement this — but record the URL shape here for posterity.
      See https://developers.facebook.com/docs/threads/oembed/.

  Option C: fetch the public Threads post page (HTML) and scrape one of
      the JSON-LD / ``__additionalData`` blobs Threads injects on
      server-rendered pages. Most posts include a 19-digit numeric id
      in a "pk" / "code"-keyed object. Risky and may break without
      notice; only used when Option A fails.

Public surface:

    from lib.threads_id_resolver import resolve_quote_post_id

    media_id = resolve_quote_post_id(url_or_shortcode, prefer="auto")

``prefer`` choices: ``"auto"`` (A->C), ``"shortcode"`` (A only),
``"scrape"`` (C only). Returns the input shortcode unchanged when only
A is allowed and A is supposed to be tried by the caller — the actual
"does Threads API accept this?" check happens during container creation.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import requests

LOG = logging.getLogger(__name__)


class ThreadsIdResolveError(RuntimeError):
    pass


_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_\-]{6,}$")
_URL_RE = re.compile(
    r"https?://(?:www\.)?threads\.(?:com|net)/"
    r"(?:@[A-Za-z0-9_.]+/post/|t/)"
    r"([A-Za-z0-9_\-]+)"
)


def normalize_to_shortcode(url_or_code: str) -> str:
    """Accept either a shortcode or a full URL; return shortcode."""
    s = (url_or_code or "").strip()
    if not s:
        raise ThreadsIdResolveError("empty input")
    if _SHORTCODE_RE.match(s):
        return s
    m = _URL_RE.match(s)
    if m:
        return m.group(1)
    raise ThreadsIdResolveError(f"not a Threads URL or shortcode: {s[:80]!r}")


def build_canonical_url(shortcode: str, username: Optional[str] = None) -> str:
    """Return the canonical web URL we'll show to the user.

    /t/<code> works for any shortcode regardless of whether we know the
    author; /@user/post/<code> is prettier but optional.
    """
    if username:
        return f"https://www.threads.com/@{username}/post/{shortcode}"
    return f"https://www.threads.com/t/{shortcode}"


def _try_scrape(shortcode: str, timeout: int = 10) -> Optional[str]:
    """
    Option C: fetch the public page and look for a numeric media id.

    Conservative: any failure -> None. We never raise from here; the
    caller decides what to do.
    """
    url = f"https://www.threads.com/t/{shortcode}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; kawatms-quote-bot/1.0; "
            "+https://github.com/tmskawa-byte/ig-autopost)"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    except requests.RequestException as e:
        LOG.warning("scrape: network error for %s: %s", shortcode, e)
        return None
    if resp.status_code >= 400:
        LOG.warning("scrape: HTTP %d for %s", resp.status_code, shortcode)
        return None
    html = resp.text or ""

    # Look for "pk":"<19digit>" or "media_id":"<19digit>" patterns. Threads
    # SSR usually serialises both. 17-20 digits is a safe range for Meta IDs.
    for pat in (
        r'"pk":"(\d{15,21})"',
        r'"pk_id":"(\d{15,21})"',
        r'"media_id":"(\d{15,21})"',
        r'\\"pk\\":\\"(\d{15,21})\\"',
    ):
        m = re.search(pat, html)
        if m:
            mid = m.group(1)
            LOG.info("scrape: found media_id=%s for shortcode=%s via /%s/", mid, shortcode, pat)
            return mid
    LOG.warning("scrape: no media_id pattern matched for shortcode=%s", shortcode)
    return None


def resolve_quote_post_id(
    url_or_code: str,
    prefer: str = "auto",
    scrape: bool = True,
) -> str:
    """
    Resolve to a string usable as ``quote_post_id`` in the Threads /threads
    container endpoint.

    ``prefer``:
      - ``"shortcode"``: return the shortcode verbatim (Option A).
      - ``"scrape"``:    only attempt Option C; raise if it fails.
      - ``"auto"``:      try Option A path (return shortcode) and let the
                         caller fall back to scrape on Threads API error;
                         OR if ``scrape=True`` we *pre-resolve* via Option C
                         so the API call uses a numeric id from the start.
                         Default behaviour: pre-resolve, falling back to
                         shortcode if scraping fails.
    """
    code = normalize_to_shortcode(url_or_code)

    if prefer == "shortcode":
        return code

    if prefer == "scrape":
        mid = _try_scrape(code)
        if not mid:
            raise ThreadsIdResolveError(
                f"scrape failed for shortcode {code!r}"
            )
        return mid

    # auto: try scrape, fall back to shortcode
    if scrape:
        mid = _try_scrape(code)
        if mid:
            return mid
        LOG.info("auto-resolve: scrape gave nothing for %s; returning shortcode", code)
    return code


__all__ = [
    "ThreadsIdResolveError",
    "normalize_to_shortcode",
    "build_canonical_url",
    "resolve_quote_post_id",
]
