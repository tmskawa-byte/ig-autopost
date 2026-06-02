"""
Tavily-backed Threads candidate searcher for @kawatms quote-repost.

This is the alternate source path for ``scripts/threads_quote_repost_tavily.py``.
Compared to ``lib/threads_searcher.py`` (which calls Threads
``/keyword_search`` directly and requires ``threads_keyword_search`` perm
— not yet granted to us), this module:

    1. Hits Tavily Search with ``site:threads.com <car keyword>`` queries.
    2. Extracts ``threads.com/@user/post/<shortcode>`` URLs from the
       Tavily results.
    3. Returns ``ThreadsCandidate`` rows (URL, shortcode, author, snippet,
       published_date, has_video hint).

Filters (kept intentionally lenient per parent-agent instructions):
    - Freshness: drop items older than ``fresh_hours`` (default 48h).
      When Tavily does not surface ``published_date`` we keep the row,
      since dropping all date-less rows would empty most runs.
    - Video preference: ``has_video`` is a hint; we sort video-first but
      do NOT discard non-video.
    - Account sketchiness: only username substring blocklist; we DO NOT
      drop on text scam/click-bait terms here (the downstream LLM
      ``threads_quote_select`` prompt is the heavier filter, and dropping
      too aggressively in Tavily-land starves the candidate pool).

This module never calls the Threads Graph API — getting a quote_post_id
from a shortcode is handled by ``lib/threads_id_resolver.py``.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from lib.tavily_client import TavilyClient, TavilyError

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# defaults / blocklists
# ---------------------------------------------------------------------------

# Broad fan-out queries. Each is run as a separate Tavily search. We keep
# the list moderate (10 queries) to stay within Tavily quota.
DEFAULT_QUERIES: List[str] = [
    "site:threads.com 自動車 動画",
    "site:threads.com 新車 試乗",
    "site:threads.com EV 整備",
    "site:threads.com 軽トラ",
    "site:threads.com スポーツカー",
    "site:threads.com F1 動画",
    "site:threads.com 旧車 レストア",
    "site:threads.com ドライブ",
    "site:threads.com 輸入車",
    "site:threads.com モータースポーツ",
]

# Username substrings that are typical for scam/promo accounts. Loose by
# design — the parent agent asked us to keep filtering gentle.
SUSPECT_USERNAME_SUBSTRINGS: List[str] = [
    "bitcoin", "crypto", "btc_", "_btc", "_fx_", "fxtrader",
    "lottery", "lotto", "casino", "millionaire", "billionaire",
    "press_release",
]

# Hints that the Tavily snippet / title contains scam-shaped content. Two or
# more hits drop the row. Single hit is allowed.
SCAM_TEXT_TERMS: List[str] = [
    "暗号資産", "仮想通貨", "バイナリーオプション", "投資助言",
    "コピートレード", "シグナル配信", "億り人",
    "公式LINE", "LINE@", "DMください",
]

# Threads URL shapes we accept:
#   https://www.threads.com/@user/post/SHORTCODE
#   https://www.threads.net/@user/post/SHORTCODE
#   https://www.threads.com/t/SHORTCODE
# Shortcode is base64-url-ish; in practice [A-Za-z0-9_-]{6,}.
_URL_RE = re.compile(
    r"https?://(?:www\.)?threads\.(?:com|net)/"
    r"(?:@([A-Za-z0-9_.]+)/post/|t/)"
    r"([A-Za-z0-9_\-]{6,})"
)


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

@dataclass
class ThreadsCandidate:
    """
    Row returned from TavilyThreadsSearcher.find_candidates.

    Mirrors the fields used by prompts/threads_quote_select.py so the same
    LLM prompt builder works on both sources (threads_searcher.CandidatePost
    and this class).
    """
    id: str  # populated only after threads_id_resolver runs (= quote_post_id)
    shortcode: str
    url: str
    username: str
    text: str  # snippet from Tavily (NOT full post text)
    timestamp: str  # ISO8601; "" if unknown
    media_type: str  # VIDEO / IMAGE / TEXT — guessed from Tavily hints
    permalink: str  # same as url, kept for prompt-template compatibility
    has_video: bool = False
    matched_keyword: str = ""
    score: float = 0.0
    score_reasons: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_summary(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "shortcode": self.shortcode,
            "url": self.url,
            "username": self.username,
            "media_type": self.media_type,
            "timestamp": self.timestamp,
            "permalink": self.permalink,
            "has_video": self.has_video,
            "matched_keyword": self.matched_keyword,
            "score": round(self.score, 3),
            "score_reasons": list(self.score_reasons),
            "text": (self.text or "")[:280],
        }


class TavilyThreadsSearchError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_published_date(s: Optional[str]) -> Optional[int]:
    """Tavily published_date is typically an ISO8601 string. Return epoch
    seconds or None."""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    # Try a few formats
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            pass
    try:
        # ISO 8601 with timezone or offset
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _to_iso(epoch: Optional[int]) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _extract_threads_urls(item: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Pull every Threads URL we can find from a Tavily result item.

    Returns a list of dicts {url, shortcode, username}. We look in:
        - item["url"]                (primary)
        - item["content"], item["title"]   (text snippets sometimes
                                            include the canonical URL)
    """
    found: List[Dict[str, str]] = []
    seen_codes: set = set()

    def _scan(text: str) -> None:
        if not text:
            return
        for m in _URL_RE.finditer(text):
            uname = m.group(1) or ""
            code = m.group(2) or ""
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            found.append({
                "url": m.group(0),
                "shortcode": code,
                "username": uname,
            })

    for key in ("url", "content", "title", "raw_content"):
        v = item.get(key)
        if isinstance(v, str):
            _scan(v)
    return found


def _guess_has_video(item: Dict[str, Any]) -> bool:
    """
    Tavily sometimes surfaces a ``video`` field or a video URL inside images.
    URL slug like /post/<code> does NOT distinguish video vs image, so we
    fall back to keyword sniffing.
    """
    if item.get("video"):
        return True
    images = item.get("images")
    if isinstance(images, list):
        for im in images:
            if isinstance(im, str) and (im.endswith(".mp4") or "video" in im.lower()):
                return True
            if isinstance(im, dict) and any(
                (str(im.get(k) or "")).lower().endswith(".mp4")
                for k in ("url", "src")
            ):
                return True
    # text sniff
    blob = " ".join(str(item.get(k) or "") for k in ("title", "content")).lower()
    if any(w in blob for w in ("video", "動画", "watch", "再生")):
        return True
    return False


def _is_account_sketchy(username: str) -> Optional[str]:
    u = (username or "").lower()
    if not u:
        return None  # Tavily sometimes lacks username; don't penalise
    for sub in SUSPECT_USERNAME_SUBSTRINGS:
        if sub in u:
            return f"username contains {sub!r}"
    return None


def _is_text_sketchy(text: str) -> Optional[str]:
    if not text:
        return None
    hits = [w for w in SCAM_TEXT_TERMS if w in text]
    if len(hits) >= 2:
        return f"text contains scam terms {hits[:3]}"
    return None


# ---------------------------------------------------------------------------
# searcher
# ---------------------------------------------------------------------------

class TavilyThreadsSearcher:
    """Tavily-backed alternate to ThreadsSearcher.

    Public surface mirrors the keyword_search version:
        searcher = TavilyThreadsSearcher()
        candidates = searcher.find_candidates(
            queries=None,                # falls back to DEFAULT_QUERIES
            fresh_hours=48,
            top_n=3,
            seen_shortcodes=[...],
            per_query_max=5,
        )
    """

    def __init__(
        self,
        tavily: Optional[TavilyClient] = None,
    ):
        try:
            self.tavily = tavily or TavilyClient()
        except TavilyError as e:
            raise TavilyThreadsSearchError(f"TavilyClient init failed: {e}") from e

    # ------------------------------------------------------------------
    # search + parse
    # ------------------------------------------------------------------
    def _search_one(self, query: str, max_results: int, days: int) -> List[Dict[str, Any]]:
        # Tavily ``news`` topic supports ``days`` but heavily prefers news-site
        # domains. For site:threads.com we explicitly use the general topic.
        # We pass include_domains to nudge the engine.
        try:
            return self.tavily.search(
                query=query,
                topic="general",
                max_results=max_results,
                # ``days`` is ignored unless topic == "news" inside our client;
                # we'll filter by published_date / time downstream anyway.
                days=days,
                include_domains=["threads.com", "threads.net"],
                search_depth="advanced",
            )
        except TavilyError as e:
            LOG.warning("Tavily search failed for %r: %s", query, e)
            return []

    # ------------------------------------------------------------------
    # scoring
    # ------------------------------------------------------------------
    def _score(
        self,
        item: Dict[str, Any],
        has_video: bool,
        age_hours: Optional[float],
        fresh_hours: int,
    ) -> (float, List[str]):
        score = 0.0
        reasons: List[str] = []

        if has_video:
            score += 3.0
            reasons.append("video +3.0")
        else:
            reasons.append("non-video +0.0")

        if age_hours is None:
            reasons.append("no published_date +0.0")
        elif age_hours <= fresh_hours:
            score += 2.0
            reasons.append(f"age={age_hours:.1f}h fresh +2.0")
        elif age_hours <= 2 * fresh_hours:
            score += 0.7
            reasons.append(f"age={age_hours:.1f}h ok +0.7")
        else:
            # past 2x window: we shouldn't even have reached here, but be safe
            reasons.append(f"age={age_hours:.1f}h stale +0.0")

        # Tavily's relevance score (0..1). Small bump.
        try:
            tscore = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            tscore = 0.0
        if tscore > 0:
            score += min(tscore, 1.0)
            reasons.append(f"tavily_rel +{min(tscore, 1.0):.2f}")

        return score, reasons

    # ------------------------------------------------------------------
    # main entry
    # ------------------------------------------------------------------
    def find_candidates(
        self,
        queries: Optional[Sequence[str]] = None,
        fresh_hours: int = 48,
        per_query_max: int = 5,
        top_n: int = 3,
        seen_shortcodes: Optional[Iterable[str]] = None,
        min_score: float = 0.0,
        require_fresh: bool = False,
    ) -> List[ThreadsCandidate]:
        """
        Run Tavily searches across ``queries`` (default: DEFAULT_QUERIES),
        extract Threads URLs from each result, dedup by shortcode, filter,
        score, return top N.

        - ``require_fresh``: if True, drop rows lacking a published_date.
          Default False because most Tavily Threads hits do not carry one
          and we'd starve the pool.
        """
        queries = list(queries) if queries else list(DEFAULT_QUERIES)
        seen = set(seen_shortcodes or [])
        now = int(time.time())
        oldest_epoch = now - max(int(fresh_hours), 1) * 3600
        oldest_relaxed = now - max(int(fresh_hours), 1) * 3600 * 2

        pool: Dict[str, ThreadsCandidate] = {}
        rejected: List[Dict[str, Any]] = []

        for q in queries:
            results = self._search_one(q, max_results=per_query_max, days=max(1, fresh_hours // 24))
            LOG.info("Tavily q=%r -> %d raw results", q, len(results))
            for item in results:
                urls = _extract_threads_urls(item)
                if not urls:
                    continue
                # tavily relevance & published_date are per-item, not per-URL.
                pub_epoch = _parse_published_date(item.get("published_date"))
                age_hours = None if pub_epoch is None else (now - pub_epoch) / 3600.0
                has_video = _guess_has_video(item)
                snippet = (item.get("content") or item.get("title") or "").strip()

                for u in urls:
                    code = u["shortcode"]
                    if code in seen or code in pool:
                        continue
                    # Freshness gate
                    if pub_epoch is not None and pub_epoch < oldest_relaxed:
                        rejected.append({"shortcode": code, "reason": f"too old age={age_hours:.1f}h"})
                        continue
                    if require_fresh and pub_epoch is None:
                        rejected.append({"shortcode": code, "reason": "no published_date"})
                        continue
                    # Account / text checks
                    reason = _is_account_sketchy(u["username"])
                    if reason:
                        rejected.append({"shortcode": code, "reason": f"account: {reason}"})
                        continue
                    reason = _is_text_sketchy(snippet)
                    if reason:
                        rejected.append({"shortcode": code, "reason": f"text: {reason}"})
                        continue

                    score, reasons = self._score(item, has_video, age_hours, fresh_hours)
                    if score < min_score:
                        rejected.append({"shortcode": code, "reason": f"low score {score:.2f}"})
                        continue

                    cand = ThreadsCandidate(
                        id="",  # filled later by threads_id_resolver
                        shortcode=code,
                        url=u["url"],
                        username=u["username"],
                        text=snippet,
                        timestamp=_to_iso(pub_epoch),
                        media_type="VIDEO" if has_video else "UNKNOWN",
                        permalink=u["url"],
                        has_video=has_video,
                        matched_keyword=q,
                        score=score,
                        score_reasons=reasons,
                        raw=item,
                    )
                    pool[code] = cand

        ranked = sorted(pool.values(), key=lambda c: c.score, reverse=True)
        LOG.info(
            "Tavily pool: %d candidates after filter (rejected %d)",
            len(ranked), len(rejected),
        )
        for r in rejected[:10]:
            LOG.debug("  reject %s: %s", r.get("shortcode"), r.get("reason"))
        return ranked[:top_n]


__all__ = [
    "TavilyThreadsSearcher",
    "TavilyThreadsSearchError",
    "ThreadsCandidate",
    "DEFAULT_QUERIES",
    "SUSPECT_USERNAME_SUBSTRINGS",
    "SCAM_TEXT_TERMS",
]
