#!/usr/bin/env python3
"""
Threads quote-repost pipeline for @kawatms — TAVILY-backed source.

This is the production path while we wait on Meta to grant the
``threads_keyword_search`` permission. The keyword_search variant
(``scripts/threads_quote_repost.py``) is intentionally kept for the day
the permission lands.

Pipeline:

    1. Tavily ``site:threads.com <kw>`` searches across a fan-out of
       car-related queries. Extract every Threads URL hit.
    2. Filter / dedup (vs state/threads_reposted_tavily.json) + score
       (video bonus, freshness, Tavily relevance).
    3. ChatLLM picks the best of 3, generates a 整備士目線 comment.
       (Reuses prompts/threads_quote_select.py.)
    4. Resolve shortcode -> ``quote_post_id`` via
       lib.threads_id_resolver. Default strategy: try HTML scrape, fall
       back to passing the raw shortcode.
    5. Threads container + publish via lib.threads_publisher.

Fallback:
    If Tavily yields zero candidates (after filters), we can optionally
    fall back to posting a plain TEXT post with a single car-news
    link_attachment. Enabled with ``--allow-news-fallback``.

CLI:
    --preview-only           Only print the 3 Tavily candidates as JSON.
                             No LLM call, no publish, no state write.
    --dry-run                Search + LLM but skip the Threads publish.
    --fresh-hours N          Default 48.
    --query Q                Add a Tavily query (repeatable). If omitted,
                             tavily_threads_searcher.DEFAULT_QUERIES used.
    --min-viral N            Default 5.
    --min-fit N              Default 6.
    --top-n N                Default 3.
    --resolve-mode           "auto" / "shortcode" / "scrape" (default auto).
    --allow-news-fallback    On 0 Tavily candidates, post a TEXT card.
    --news-domain D          Allow-list domain (repeatable) for fallback.

Env:
    TAVILY_API_KEY           required
    CHATLLM_API_KEY          required (unless --preview-only)
    THREADS_ACCESS_TOKEN     required (unless --preview-only)
    THREADS_USER_ID          required (unless --preview-only)
    THREADS_APP_SECRET       optional
    LOG_LEVEL                optional, default INFO
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.chatllm_client import ChatLLMClient, ChatLLMError  # noqa: E402
from lib.tavily_client import TavilyClient, TavilyError  # noqa: E402
from lib.tavily_threads_searcher import (  # noqa: E402
    ThreadsCandidate,
    TavilyThreadsSearcher,
    TavilyThreadsSearchError,
)
from lib.threads_id_resolver import (  # noqa: E402
    ThreadsIdResolveError,
    resolve_quote_post_id,
)
from lib.threads_publisher import ThreadsError, ThreadsPublisher  # noqa: E402
from prompts.threads_quote_select import (  # noqa: E402
    SYSTEM_PROMPT,
    QuoteSelectError,
    build_user_prompt,
    parse_selection,
)

LOG = logging.getLogger("threads_quote_repost_tavily")

STATE_PATH = REPO_ROOT / "state" / "threads_reposted_tavily.json"
STATE_MAX_ENTRIES = 300

CHATLLM_MODEL = "gemini-3.1-pro-preview"

DEFAULT_FRESH_HOURS = 48
DEFAULT_MIN_VIRAL = 5
DEFAULT_MIN_FIT = 6
DEFAULT_TOP_N = 3
DEFAULT_PER_QUERY = 5

# News-fallback default allow list. Conservative — Japanese auto-news only.
NEWS_DEFAULT_DOMAINS = [
    "response.jp",
    "carview.yahoo.co.jp",
    "kakakumag.com",
    "motor-fan.jp",
    "webcg.net",
    "autoc-one.jp",
    "carsensor.net",
    "bestcarweb.jp",
]

# Tavily query for the news fallback (different from the per-shortcode
# searches; we want a single fresh article).
NEWS_FALLBACK_QUERY = "新型車 試乗 整備 EV F1 軽自動車"


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
def setup_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"reposted": []}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        LOG.warning("State unreadable, treating as empty: %s", e)
        return {"reposted": []}
    if "reposted" not in data or not isinstance(data["reposted"], list):
        data["reposted"] = []
    return data


def save_state(state: dict) -> None:
    reposted = state.get("reposted", [])
    if len(reposted) > STATE_MAX_ENTRIES:
        state["reposted"] = reposted[-STATE_MAX_ENTRIES:]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(STATE_PATH)
    LOG.info("State written: %s (%d entries)", STATE_PATH, len(state["reposted"]))


def reposted_shortcodes(state: dict) -> List[str]:
    codes: List[str] = []
    for entry in state.get("reposted", []):
        if isinstance(entry, dict):
            c = entry.get("quoted_shortcode") or entry.get("shortcode")
            if c:
                codes.append(str(c))
        elif isinstance(entry, str):
            codes.append(entry)
    return codes


def record_repost(
    state: dict,
    candidate: ThreadsCandidate,
    selection,
    post_id: str,
    quote_post_id_used: str,
) -> None:
    state.setdefault("reposted", []).append(
        {
            "quoted_shortcode": candidate.shortcode,
            "quoted_url": candidate.url,
            "quoted_username": candidate.username,
            "quoted_timestamp": candidate.timestamp,
            "matched_keyword": candidate.matched_keyword,
            "quote_post_id_used": quote_post_id_used,
            "viral_score": selection.viral_score,
            "brand_fit_score": selection.brand_fit_score,
            "post_id": post_id,
            "reposted_at": _now_iso(),
        }
    )


def record_news_fallback(state: dict, article_url: str, post_id: str) -> None:
    state.setdefault("reposted", []).append(
        {
            "mode": "news_fallback",
            "article_url": article_url,
            "post_id": post_id,
            "reposted_at": _now_iso(),
        }
    )


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# core pipeline
# ---------------------------------------------------------------------------
def find_candidates(
    queries: Optional[List[str]],
    fresh_hours: int,
    seen_codes: List[str],
    top_n: int,
    per_query_max: int,
) -> List[ThreadsCandidate]:
    searcher = TavilyThreadsSearcher()
    return searcher.find_candidates(
        queries=queries if queries else None,
        fresh_hours=fresh_hours,
        per_query_max=per_query_max,
        top_n=top_n,
        seen_shortcodes=seen_codes,
    )


def select_with_llm(candidates: List[ThreadsCandidate]):
    chat = ChatLLMClient()
    user_msg = build_user_prompt(candidates)
    LOG.info(
        "Asking ChatLLM model=%s to choose from %d candidates",
        CHATLLM_MODEL, len(candidates),
    )
    raw = chat.chat(
        model=CHATLLM_MODEL,
        system=SYSTEM_PROMPT,
        user=user_msg,
        response_format="json",
    )
    LOG.debug("ChatLLM raw selection output: %s", raw[:500])
    return parse_selection(raw, num_candidates=len(candidates))


def publish_quote(
    candidate: ThreadsCandidate,
    comment: str,
    resolve_mode: str,
) -> (str, str):
    """Resolve quote_post_id, post, return (post_id, quote_post_id_used)."""
    try:
        quote_post_id = resolve_quote_post_id(
            candidate.url or candidate.shortcode,
            prefer=resolve_mode,
        )
    except ThreadsIdResolveError as e:
        raise ThreadsError(f"quote_post_id resolution failed: {e}") from e

    LOG.info(
        "Resolved quote_post_id=%s (mode=%s) for shortcode=%s",
        quote_post_id, resolve_mode, candidate.shortcode,
    )
    pub = ThreadsPublisher()
    try:
        post_id = pub.create_quote_post(text=comment, quoted_post_id=quote_post_id)
    except ThreadsError as e:
        # If we used the scraped numeric id and it failed, try one more time
        # with the raw shortcode (Option A fallback). Only when caller
        # explicitly used "auto".
        if resolve_mode == "auto" and quote_post_id != candidate.shortcode:
            LOG.warning(
                "Quote post with scraped media_id %s failed: %s. "
                "Falling back to raw shortcode.", quote_post_id, e,
            )
            try:
                post_id = pub.create_quote_post(
                    text=comment, quoted_post_id=candidate.shortcode,
                )
                quote_post_id = candidate.shortcode
            except ThreadsError as e2:
                raise ThreadsError(
                    f"both scraped media_id and shortcode rejected: {e2}"
                ) from e2
        else:
            raise
    return post_id, quote_post_id


# ---------------------------------------------------------------------------
# news fallback
# ---------------------------------------------------------------------------
def news_fallback(
    allow_domains: List[str],
    dry_run: bool,
) -> Optional[dict]:
    """
    Tavily news search for a Japanese auto article + post it as a TEXT card
    via Threads link_attachment.
    Returns dict with {article_url, post_id} when published; None when
    skipped (dry-run, or no article found).
    """
    LOG.info("News-fallback engaged. Allow domains: %s", allow_domains)
    try:
        client = TavilyClient()
        results = client.search(
            query=NEWS_FALLBACK_QUERY,
            topic="news",
            max_results=5,
            days=3,
            include_domains=allow_domains,
            search_depth="advanced",
        )
    except TavilyError as e:
        LOG.error("Tavily news-fallback search failed: %s", e)
        return None

    if not results:
        LOG.info("News-fallback: 0 articles. Nothing to post.")
        return None

    article = results[0]
    article_url = article.get("url") or ""
    article_title = (article.get("title") or "").strip()
    if not article_url.startswith("https://") or not article_title:
        LOG.warning("News-fallback: first result has no usable url/title: %r", article)
        return None

    # Build a short, on-brand 整備士目線 TEXT post.
    text = (
        f"{article_title}\n"
        f"気になるニュース、ちょっと整備士目線で見てみますね。\n"
        f"続きはこちらからどうぞ。"
    )
    if len(text) > 460:
        text = text[:460].rstrip() + "…"

    LOG.info("News-fallback article: %s", article_url)
    if dry_run:
        LOG.info("Dry-run: skipping news-fallback publish.")
        return {"article_url": article_url, "post_id": "(dry-run)"}

    pub = ThreadsPublisher()
    try:
        post_id = pub.create_text_post(text=text, link_attachment=article_url)
    except ThreadsError as e:
        LOG.error("News-fallback publish failed: %s", e)
        return None
    return {"article_url": article_url, "post_id": post_id}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Quote-repost a popular car-related Threads post (Tavily-sourced) "
            "for @kawatms. 火金 17:00 JST."
        )
    )
    p.add_argument("--dry-run", action="store_true",
                   help="run search + LLM, skip Threads publish + state write")
    p.add_argument("--preview-only", action="store_true",
                   help="run Tavily only; print candidates; skip LLM + publish")
    p.add_argument("--fresh-hours", type=int, default=DEFAULT_FRESH_HOURS,
                   help=f"freshness window (default {DEFAULT_FRESH_HOURS})")
    p.add_argument("--query", action="append", default=[],
                   help="add a Tavily query (repeatable); empty -> defaults")
    p.add_argument("--per-query-max", type=int, default=DEFAULT_PER_QUERY,
                   help=f"Tavily max_results per query (default {DEFAULT_PER_QUERY})")
    p.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                   help=f"shortlist size for LLM (default {DEFAULT_TOP_N})")
    p.add_argument("--min-viral", type=int, default=DEFAULT_MIN_VIRAL,
                   help=f"minimum viral_score to publish (default {DEFAULT_MIN_VIRAL})")
    p.add_argument("--min-fit", type=int, default=DEFAULT_MIN_FIT,
                   help=f"minimum brand_fit_score to publish (default {DEFAULT_MIN_FIT})")
    p.add_argument("--resolve-mode", default="auto",
                   choices=["auto", "shortcode", "scrape"],
                   help="quote_post_id resolution strategy (default auto)")
    p.add_argument("--allow-news-fallback", action="store_true",
                   help="if Tavily Threads pool is empty, post a TEXT card")
    p.add_argument("--news-domain", action="append", default=[],
                   help="news fallback allow-list (repeatable); empty -> defaults")
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    state = load_state()
    seen = reposted_shortcodes(state)
    LOG.info("Loaded %d previously-reposted shortcodes from state", len(seen))

    try:
        candidates = find_candidates(
            queries=args.query or None,
            fresh_hours=args.fresh_hours,
            seen_codes=seen,
            top_n=args.top_n,
            per_query_max=args.per_query_max,
        )
    except TavilyThreadsSearchError as e:
        LOG.error("Tavily Threads search failed: %s", e)
        return 2

    LOG.info("Shortlisted %d candidates", len(candidates))
    for i, c in enumerate(candidates):
        LOG.info(
            "  [%d] @%s %s shortcode=%s score=%.2f reasons=%s",
            i, c.username or "(unknown)", c.media_type, c.shortcode,
            c.score, ",".join(c.score_reasons),
        )

    if args.preview_only:
        out = {"candidates": [c.to_summary() for c in candidates]}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if not candidates:
        if args.allow_news_fallback:
            domains = args.news_domain or NEWS_DEFAULT_DOMAINS
            res = news_fallback(allow_domains=domains, dry_run=args.dry_run)
            if res and not args.dry_run:
                record_news_fallback(state, res["article_url"], res["post_id"])
                save_state(state)
                LOG.info("News-fallback published: %s", res["post_id"])
                return 0
            elif res:
                LOG.info("News-fallback (dry-run): would post %s", res["article_url"])
                return 0
            LOG.info("News-fallback produced nothing. Exiting cleanly.")
            return 0
        LOG.info("No candidates and --allow-news-fallback not set. Exiting cleanly.")
        return 0

    try:
        selection = select_with_llm(candidates)
    except (ChatLLMError, QuoteSelectError) as e:
        LOG.error("LLM selection failed: %s", e)
        return 2

    chosen = candidates[selection.selected_index]
    LOG.info(
        "LLM picked [%d] @%s viral=%d fit=%d reason=%s",
        selection.selected_index, chosen.username or "(unknown)",
        selection.viral_score, selection.brand_fit_score, selection.reason,
    )
    LOG.info("Generated comment (%d chars):\n%s",
             len(selection.comment), selection.comment)

    if selection.viral_score < args.min_viral or selection.brand_fit_score < args.min_fit:
        LOG.warning(
            "Skipping publish: viral=%d (need >=%d), fit=%d (need >=%d)",
            selection.viral_score, args.min_viral,
            selection.brand_fit_score, args.min_fit,
        )
        return 0

    if args.dry_run:
        LOG.info("Dry-run: skipping Threads publish + state update.")
        # Still attempt resolve so we can see if it would have worked.
        try:
            resolved = resolve_quote_post_id(
                chosen.url or chosen.shortcode, prefer=args.resolve_mode,
            )
            LOG.info("Would call create_quote_post with quote_post_id=%s",
                     resolved)
        except ThreadsIdResolveError as e:
            LOG.warning("Dry-run resolve failed: %s", e)
        print(json.dumps({
            "selection": selection.to_dict(),
            "chosen": chosen.to_summary(),
        }, ensure_ascii=False, indent=2))
        return 0

    try:
        post_id, used = publish_quote(chosen, selection.comment, args.resolve_mode)
    except ThreadsError as e:
        LOG.error("Threads quote publish failed: %s", e)
        return 3

    LOG.info("Threads quote-repost id: %s (quote_post_id_used=%s)", post_id, used)
    record_repost(state, chosen, selection, post_id, used)
    save_state(state)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    setup_logging()
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        LOG.warning("Interrupted by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
