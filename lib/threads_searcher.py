"""
Threads keyword search + candidate filtering.

Wraps the Threads Graph API ``/keyword_search`` endpoint and produces a
shortlist of repost candidates suitable for @kawatms (整備士目線).

Selection priorities (in order):
    1. Video posts (Threads media_type=VIDEO) — per the X RT rule
       [[x-rt-video-only]]: we strongly prefer video content.
    2. Freshness: posted within the last ``fresh_hours`` (default 12h).
    3. Account quality: skip suspicious / scammy / borderline accounts via
       both username heuristics and text-content keyword blocklists.

Docs:
- https://developers.facebook.com/docs/threads/keyword-search
- https://developers.facebook.com/docs/threads/threads-media

Requires:
- THREADS_ACCESS_TOKEN with the ``threads_keyword_search`` permission.
- THREADS_USER_ID is not strictly required for keyword search itself,
  but ThreadsPublisher inherits it from env so we just rely on the same
  client config layer.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# blocklists - keep in sync with the X RT spec
#   [[x-rt-no-sketchy-accounts]]
# ---------------------------------------------------------------------------

# Words in the POST TEXT that almost always indicate scammy / spammy / no-go
# content. If any of these appear we drop the candidate.
SCAM_TEXT_TERMS: List[str] = [
    # crypto / investment shilling
    "暗号資産", "仮想通貨", "ビットコイン", "BTC", "ETH", "イーサ",
    "億り人", "FX", "バイナリー", "バイナリーオプション",
    "投資助言", "投資顧問", "資産運用", "資産形成",
    "稼げる", "稼ぎ方", "副業で稼", "今だけ", "限定公開",
    "DMください", "DM下さい", "LINE@", "LINE登録", "公式LINE",
    "コミュニティ参加", "オンラインサロン", "メルマガ登録",
    "コピートレード", "シグナル配信",
    # MLM / aggressive sales
    "ネットワークビジネス", "MLM", "在宅で月収", "月収100万",
    # sexual / suggestive (off-brand for an auto shop)
    "出会い", "援", "セクシー",
    # divisive / extreme
    "陰謀", "覚醒", "目覚めよ",
]

# Words/phrases that indicate clickbait / inflammatory tone. Tolerated at low
# density but multiple hits trigger a drop. The X RT spec calls these out.
CLICKBAIT_TERMS: List[str] = [
    "衝撃", "驚愕", "ヤバすぎ", "やばすぎ", "ガチで終了", "オワコン",
    "炎上中", "暴露", "闇", "知らないと損", "必見！", "見ないと損",
    "拡散希望", "緊急", "速報級", "完全終了", "全員見て",
]

# Username patterns / substrings that are typical for scam / lottery /
# crypto promotion accounts. Match case-insensitively on the username.
SUSPECT_USERNAME_SUBSTRINGS: List[str] = [
    "fx", "bitcoin", "crypto", "btc", "eth", "trader", "trading",
    "invest", "rich", "money", "earn", "millionaire", "billionaire",
    "lottery", "lotto", "loan", "sale", "discount", "coupon", "gift",
    "press_release", "_pr_",
]

# Username regex patterns that look auto-generated / throwaway:
#   - 8+ trailing digits
#   - many underscores
#   - all-numeric
SUSPECT_USERNAME_PATTERNS: List[re.Pattern] = [
    re.compile(r"\d{8,}$"),
    re.compile(r"_.+_.+_"),
    re.compile(r"^\d+$"),
]


class ThreadsSearchError(RuntimeError):
    """Raised for any failure talking to the keyword_search endpoint."""


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

@dataclass
class ThreadsPost:
    id: str
    text: str
    media_type: str  # TEXT / IMAGE / VIDEO / CAROUSEL_ALBUM / REPOST_FACADE / ...
    permalink: str
    timestamp: str  # ISO8601
    username: str
    has_replies: bool = False
    is_quote_post: bool = False
    is_reply: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, obj: Dict[str, Any]) -> "ThreadsPost":
        return cls(
            id=str(obj.get("id") or ""),
            text=str(obj.get("text") or ""),
            media_type=str(obj.get("media_type") or "").upper(),
            permalink=str(obj.get("permalink") or ""),
            timestamp=str(obj.get("timestamp") or ""),
            username=str(obj.get("username") or ""),
            has_replies=bool(obj.get("has_replies") or False),
            is_quote_post=bool(obj.get("is_quote_post") or False),
            is_reply=bool(obj.get("is_reply") or False),
            raw=obj,
        )

    def to_summary(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "media_type": self.media_type,
            "timestamp": self.timestamp,
            "permalink": self.permalink,
            "text": self.text[:280],
        }


@dataclass
class CandidatePost(ThreadsPost):
    score: float = 0.0
    score_reasons: List[str] = field(default_factory=list)
    matched_keyword: str = ""

    def to_summary(self) -> Dict[str, Any]:
        s = super().to_summary()
        s["score"] = round(self.score, 3)
        s["score_reasons"] = list(self.score_reasons)
        s["matched_keyword"] = self.matched_keyword
        return s


# ---------------------------------------------------------------------------
# searcher
# ---------------------------------------------------------------------------

class ThreadsSearcher:
    GRAPH_API_VERSION = "v1.0"
    GRAPH_HOST = "https://graph.threads.net"

    # Default queries — broad coverage of car-related content.
    # Order matters slightly: earlier queries are tried first, but we still
    # fan out across all of them when fanout=True.
    DEFAULT_KEYWORDS: List[str] = [
        "自動車", "新車", "EV", "F1", "モータースポーツ",
        "ドライブ", "整備", "カスタム", "車", "レース",
        "国産車", "輸入車", "スポーツカー",
    ]

    def __init__(
        self,
        access_token: Optional[str] = None,
        app_secret: Optional[str] = None,
        api_version: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        self.access_token = access_token or os.environ.get("THREADS_ACCESS_TOKEN")
        self.app_secret = app_secret or os.environ.get("THREADS_APP_SECRET")
        if not self.access_token:
            raise ThreadsSearchError("THREADS_ACCESS_TOKEN is not set")
        if api_version:
            self.GRAPH_API_VERSION = api_version
        self._session = session or requests.Session()

    @property
    def base_url(self) -> str:
        return f"{self.GRAPH_HOST}/{self.GRAPH_API_VERSION}"

    # ------------------------------------------------------------------
    # internal
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

    def _get(self, path: str, params: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
        url = f"{self.base_url}/{path}"
        q = dict(params)
        q.update(self._auth_params())
        try:
            resp = self._session.get(url, params=q, timeout=timeout)
        except requests.RequestException as e:
            raise ThreadsSearchError(f"Network error on GET {path}: {e}") from e
        if resp.status_code >= 400:
            raise ThreadsSearchError(
                f"GET {path} HTTP {resp.status_code}: {resp.text[:600]}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise ThreadsSearchError(f"Invalid JSON from GET {path}: {e}") from e

    # ------------------------------------------------------------------
    # raw search
    # ------------------------------------------------------------------
    DEFAULT_FIELDS = (
        "id,text,media_type,permalink,timestamp,username,"
        "has_replies,is_quote_post,is_reply"
    )

    def keyword_search(
        self,
        q: str,
        search_type: str = "TOP",
        media_type: Optional[str] = None,
        since: Optional[int] = None,
        until: Optional[int] = None,
        limit: int = 25,
        fields: Optional[str] = None,
    ) -> List[ThreadsPost]:
        """
        Call the Threads keyword_search endpoint and return parsed posts.
        """
        params: Dict[str, Any] = {
            "q": q,
            "search_type": search_type,
            "limit": int(limit),
            "fields": fields or self.DEFAULT_FIELDS,
        }
        if media_type:
            params["media_type"] = media_type
        if since is not None:
            params["since"] = int(since)
        if until is not None:
            params["until"] = int(until)
        LOG.info(
            "keyword_search q=%r media_type=%s limit=%d since=%s until=%s",
            q, media_type, limit, since, until,
        )
        data = self._get("keyword_search", params)
        items = data.get("data") or []
        out = [ThreadsPost.from_api(o) for o in items]
        LOG.info("keyword_search q=%r returned %d posts", q, len(out))
        return out

    # ------------------------------------------------------------------
    # filtering / scoring
    # ------------------------------------------------------------------
    def is_account_sketchy(self, post: ThreadsPost) -> Optional[str]:
        """
        Return a reason string if the account looks sketchy, else None.
        Heuristic — biased toward false-positive (skip) for safety.
        """
        uname = (post.username or "").lower()
        if not uname:
            return "no username"
        for sub in SUSPECT_USERNAME_SUBSTRINGS:
            if sub in uname:
                return f"username contains {sub!r}"
        for pat in SUSPECT_USERNAME_PATTERNS:
            if pat.search(uname):
                return f"username matches {pat.pattern!r}"
        return None

    def is_text_sketchy(self, post: ThreadsPost) -> Optional[str]:
        """
        Return a reason string if the text looks like scam / inflammatory
        content, else None.
        """
        text = post.text or ""
        if not text.strip():
            return "empty text"
        for term in SCAM_TEXT_TERMS:
            if term in text:
                return f"text contains scam term {term!r}"
        hits = [w for w in CLICKBAIT_TERMS if w in text]
        if len(hits) >= 2:
            return f"text contains {len(hits)} clickbait terms: {hits[:3]}"
        return None

    @staticmethod
    def _parse_ts(ts: str) -> Optional[int]:
        """Parse ISO8601 timestamp string -> epoch seconds (UTC)."""
        if not ts:
            return None
        try:
            from datetime import datetime
            # Handles "2023-10-17T05:42:03+0000" and "...+00:00"
            if len(ts) >= 5 and (ts[-5] == "+" or ts[-5] == "-") and ts[-3] != ":":
                # Insert a colon to be ISO compliant (e.g. +0000 -> +00:00)
                ts = ts[:-2] + ":" + ts[-2:]
            return int(datetime.fromisoformat(ts).timestamp())
        except (ValueError, TypeError):
            return None

    def score_candidate(
        self,
        post: ThreadsPost,
        now_epoch: int,
        fresh_hours: int = 12,
    ) -> CandidatePost:
        """
        Score a post for repost-worthiness.

        Base score 0. Bonuses:
          + video posts                           +3.0
          + within fresh_hours                    +2.0
          + within 2*fresh_hours                  +0.7  (fallback)
          + has_replies                           +0.4
          - is_reply                              -2.0
          - is_quote_post                         -1.0
        """
        cand = CandidatePost(**{k: getattr(post, k) for k in post.__dict__})
        score = 0.0
        reasons: List[str] = []

        if post.media_type == "VIDEO":
            score += 3.0
            reasons.append("video +3.0")
        elif post.media_type == "IMAGE" or post.media_type == "CAROUSEL_ALBUM":
            score += 1.0
            reasons.append(f"{post.media_type.lower()} +1.0")
        else:
            score += 0.0
            reasons.append("text +0.0")

        ts = self._parse_ts(post.timestamp)
        if ts is not None:
            age_h = (now_epoch - ts) / 3600.0
            if age_h <= fresh_hours:
                score += 2.0
                reasons.append(f"age={age_h:.1f}h fresh +2.0")
            elif age_h <= 2 * fresh_hours:
                score += 0.7
                reasons.append(f"age={age_h:.1f}h ok +0.7")
            else:
                reasons.append(f"age={age_h:.1f}h stale +0.0")
        else:
            reasons.append("no timestamp +0.0")

        if post.has_replies:
            score += 0.4
            reasons.append("has_replies +0.4")
        if post.is_reply:
            score -= 2.0
            reasons.append("is_reply -2.0")
        if post.is_quote_post:
            score -= 1.0
            reasons.append("is_quote_post -1.0")

        cand.score = score
        cand.score_reasons = reasons
        return cand

    # ------------------------------------------------------------------
    # public: find_candidates
    # ------------------------------------------------------------------
    def find_candidates(
        self,
        keywords: Optional[Sequence[str]] = None,
        fresh_hours: int = 12,
        per_kw_limit: int = 25,
        video_only: bool = True,
        top_n: int = 3,
        seen_ids: Optional[Iterable[str]] = None,
        min_score: float = 1.5,
    ) -> List[CandidatePost]:
        """
        Search across multiple keywords, filter, score, and return the top N
        candidates suitable for an @kawatms quote-repost.

        - ``video_only``: if True, request media_type=VIDEO from the API to
          stay within the X RT rule. If we cannot fill ``top_n`` candidates
          we fall back to allowing other media_types in a second pass.
        - ``seen_ids``: post IDs to skip (already-reposted, deduped via
          state/threads_reposted.json).
        - ``min_score``: minimum score required for a candidate to be eligible.
        """
        keywords = list(keywords) if keywords else list(self.DEFAULT_KEYWORDS)
        seen = set(seen_ids or [])
        now = int(time.time())
        since = now - max(int(fresh_hours), 1) * 3600

        pool: Dict[str, CandidatePost] = {}
        bad: List[Dict[str, Any]] = []

        def _ingest(posts: List[ThreadsPost], matched_kw: str) -> None:
            for p in posts:
                if not p.id:
                    continue
                if p.id in seen:
                    continue
                if p.id in pool:
                    # already evaluated — keep the higher-priority keyword's row
                    continue
                # Drop replies and quote-posts upfront, they almost never make
                # good targets for OUR quote repost.
                if p.is_reply:
                    bad.append({"id": p.id, "reason": "is_reply", "username": p.username})
                    continue
                if p.is_quote_post:
                    bad.append({"id": p.id, "reason": "is_quote_post", "username": p.username})
                    continue
                # Account / text sketchiness checks
                reason = self.is_account_sketchy(p)
                if reason:
                    bad.append({"id": p.id, "reason": f"account: {reason}", "username": p.username})
                    continue
                reason = self.is_text_sketchy(p)
                if reason:
                    bad.append({"id": p.id, "reason": f"text: {reason}", "username": p.username})
                    continue
                cand = self.score_candidate(p, now_epoch=now, fresh_hours=fresh_hours)
                cand.matched_keyword = matched_kw
                if cand.score < min_score:
                    bad.append({
                        "id": p.id,
                        "reason": f"low score {cand.score:.2f}",
                        "username": p.username,
                    })
                    continue
                pool[p.id] = cand

        # ------ pass 1: video-only TOP per keyword ------
        for kw in keywords:
            try:
                posts = self.keyword_search(
                    q=kw,
                    search_type="TOP",
                    media_type="VIDEO" if video_only else None,
                    since=since,
                    limit=per_kw_limit,
                )
            except ThreadsSearchError as e:
                LOG.warning("keyword_search failed for %r: %s", kw, e)
                continue
            _ingest(posts, kw)

        ranked = sorted(pool.values(), key=lambda c: c.score, reverse=True)
        LOG.info(
            "Pass1 (video=%s): %d candidates after filter (rejected %d)",
            video_only, len(ranked), len(bad),
        )

        # ------ pass 2: fallback to any media type if we need more ------
        if video_only and len(ranked) < top_n:
            LOG.info("Pass1 yielded %d < %d candidates, falling back to any media_type",
                     len(ranked), top_n)
            for kw in keywords:
                try:
                    posts = self.keyword_search(
                        q=kw,
                        search_type="TOP",
                        media_type=None,
                        since=since,
                        limit=per_kw_limit,
                    )
                except ThreadsSearchError as e:
                    LOG.warning("keyword_search (fallback) failed for %r: %s", kw, e)
                    continue
                _ingest(posts, kw)
            ranked = sorted(pool.values(), key=lambda c: c.score, reverse=True)
            LOG.info("Pass2: %d candidates after fallback", len(ranked))

        return ranked[:top_n]


__all__ = [
    "ThreadsSearcher",
    "ThreadsSearchError",
    "ThreadsPost",
    "CandidatePost",
    "SCAM_TEXT_TERMS",
    "CLICKBAIT_TERMS",
    "SUSPECT_USERNAME_SUBSTRINGS",
]
