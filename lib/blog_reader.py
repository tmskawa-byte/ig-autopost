"""
Blog RSS reader for tsushima-motor.com.

Parses the site's RSS 2.0 feed at https://tsushima-motor.com/rss.xml
and returns a list of Article dataclass instances ordered newest-first.

Stdlib only (xml.etree.ElementTree, urllib.request). Falls back to ``requests``
if available, but does not require it.
"""
from __future__ import annotations

import dataclasses
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)

DEFAULT_RSS_URL = "https://tsushima-motor.com/rss.xml"
DEFAULT_TIMEOUT = 30
USER_AGENT = "social-autopost-bot/1.0 (+https://github.com/tmskawa-byte/ig-autopost)"


class BlogReaderError(RuntimeError):
    pass


@dataclasses.dataclass
class Article:
    title: str
    link: str
    description: str
    pub_date: Optional[datetime]
    image_url: Optional[str]
    slug: str
    categories: List[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "link": self.link,
            "description": self.description,
            "pub_date": self.pub_date.isoformat() if self.pub_date else None,
            "image_url": self.image_url,
            "slug": self.slug,
            "categories": list(self.categories),
        }


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------
def _fetch_bytes(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """Fetch raw bytes from ``url``. Uses ``requests`` if available, else urllib."""
    try:
        import requests  # type: ignore
    except ImportError:
        requests = None  # type: ignore

    if requests is not None:
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        except requests.RequestException as e:  # type: ignore[attr-defined]
            raise BlogReaderError(f"Network error fetching {url}: {e}") from e
        if resp.status_code >= 400:
            raise BlogReaderError(f"HTTP {resp.status_code} fetching {url}")
        return resp.content

    # urllib fallback
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as r:  # nosec: B310 (known https URL)
            return r.read()
    except Exception as e:  # pragma: no cover - network error path
        raise BlogReaderError(f"Network error fetching {url}: {e}") from e


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "media": "http://search.yahoo.com/mrss/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "atom": "http://www.w3.org/2005/Atom",
}


def _text(elem: Optional[ET.Element]) -> str:
    if elem is None:
        return ""
    return (elem.text or "").strip()


def _slug_from_link(link: str) -> str:
    # Strip query / fragment / trailing slash, take last path segment
    cleaned = re.sub(r"[?#].*$", "", link).rstrip("/")
    if not cleaned:
        return ""
    return cleaned.rsplit("/", 1)[-1]


_IMG_TAG = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)


def _extract_image_from_html(html: str) -> Optional[str]:
    if not html:
        return None
    m = _IMG_TAG.search(html)
    return m.group(1) if m else None


def _strip_html(text: str) -> str:
    if not text:
        return ""
    no_tags = re.sub(r"<[^>]+>", "", text)
    no_tags = re.sub(r"\s+", " ", no_tags).strip()
    return no_tags


def _parse_pub_date(raw: str) -> Optional[datetime]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_item(item: ET.Element) -> Article:
    title = _text(item.find("title"))
    link = _text(item.find("link"))
    raw_desc = _text(item.find("description"))
    description = _strip_html(raw_desc)

    pub_date = _parse_pub_date(_text(item.find("pubDate")))

    # Image candidates, in priority order:
    #   1. media:content[url]
    #   2. enclosure[url]
    #   3. <img> in content:encoded
    #   4. <img> in description
    image_url: Optional[str] = None
    media = item.find("media:content", NS)
    if media is not None:
        image_url = media.attrib.get("url") or None
    if not image_url:
        enclosure = item.find("enclosure")
        if enclosure is not None:
            image_url = enclosure.attrib.get("url") or None
    if not image_url:
        encoded = item.find("content:encoded", NS)
        if encoded is not None and encoded.text:
            image_url = _extract_image_from_html(encoded.text)
    if not image_url and raw_desc:
        image_url = _extract_image_from_html(raw_desc)

    categories = [_text(c) for c in item.findall("category") if _text(c)]

    return Article(
        title=title,
        link=link,
        description=description,
        pub_date=pub_date,
        image_url=image_url,
        slug=_slug_from_link(link),
        categories=categories,
    )


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------
def fetch_latest_articles(
    rss_url: str = DEFAULT_RSS_URL,
    limit: Optional[int] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> List[Article]:
    """
    Fetch the blog RSS and return parsed Article objects, newest first.

    The ``limit`` is applied after sorting; pass None to return everything.
    Articles missing ``link`` are filtered out (they cannot be deduped).
    """
    raw = _fetch_bytes(rss_url, timeout=timeout)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise BlogReaderError(f"RSS XML parse error: {e}") from e

    # RSS 2.0: <rss><channel><item>...</item></channel></rss>
    channel = root.find("channel") if root.tag.lower() != "channel" else root
    if channel is None:
        raise BlogReaderError("RSS feed has no <channel> element")

    articles: List[Article] = []
    for item in channel.findall("item"):
        try:
            article = _parse_item(item)
        except Exception as e:  # pragma: no cover - defensive
            LOG.warning("Skipping item due to parse error: %s", e)
            continue
        if not article.link:
            LOG.warning("Skipping item with empty link: title=%r", article.title)
            continue
        articles.append(article)

    # Newest first. Articles without pub_date are sorted last.
    articles.sort(
        key=lambda a: a.pub_date or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    if limit is not None:
        articles = articles[:limit]
    LOG.info("Parsed %d articles from %s", len(articles), rss_url)
    return articles
