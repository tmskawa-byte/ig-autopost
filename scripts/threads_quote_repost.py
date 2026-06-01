#!/usr/bin/env python3
"""
Threads quote-repost pipeline for @kawatms.

Schedule (in GitHub Actions): 火・金 17:00 JST  (cron: 0 8 * * 2,5 UTC)

Flow:
    1. Run multi-keyword search via Threads /keyword_search
       - Prefer VIDEO posts (X RT rule [[x-rt-video-only]])
       - Fresh window: posts within the last 12h
       - Drop replies, quote posts, sketchy accounts / scammy text
    2. Dedup against state/threads_reposted.json
    3. Score + take the top 3 candidates
    4. Ask ChatLLM (gemini-3.1-pro-preview) to:
       - Pick 1 best candidate
       - Generate a 整備士目線 quote comment (<= 480 chars)
    5. Post to Threads as a quote_post via ThreadsPublisher.create_quote_post()
    6. Append the result to state/threads_reposted.json

Env vars:
    THREADS_ACCESS_TOKEN   (required, must include `threads_keyword_search`)
    THREADS_USER_ID        (required)
    THREADS_APP_ID         (informational)
    THREADS_APP_SECRET     (optional - enables appsecret_proof)
    CHATLLM_API_KEY        (required, unless --preview-only)
    LOG_LEVEL              (optional, defaults to INFO)

CLI flags:
    --dry-run        run search + LLM selection, print final quote text, do
                     NOT publish or update state.
    --preview-only   run search only; print the 3 candidates; do NOT call
                     ChatLLM, publish, or update state.
    --fresh-hours N  override the freshness window (default 12).
    --keyword K      add a keyword (repeatable). If none provided, the
                     ThreadsSearcher default list is used.
    --min-viral N    minimum viral_score required to publish (default 5).
    --min-fit N      minimum brand_fit_score required to publish (default 6).
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
from lib.threads_publisher import ThreadsError, ThreadsPublisher  # noqa: E402
from lib.threads_searcher import (  # noqa: E402
    CandidatePost,
    ThreadsSearchError,
    ThreadsSearcher,
)
from prompts.threads_quote_select import (  # noqa: E402
    SYSTEM_PROMPT,
    QuoteSelectError,
    build_user_prompt,
    parse_selection,
)

LOG = logging.getLogger("threads_quote_repost")

STATE_PATH = REPO_ROOT / "state" / "threads_reposted.json"
STATE_MAX_ENTRIES = 300

CHATLLM_MODEL = "gemini-3.1-pro-preview"

DEFAULT_FRESH_HOURS = 12
DEFAULT_MIN_VIRAL = 5
DEFAULT_MIN_FIT = 6
DEFAULT_TOP_N = 3
DEFAULT_PER_KW_LIMIT = 25


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


def reposted_ids(state: dict) -> List[str]:
    ids: List[str] = []
    for entry in state.get("reposted", []):
        if isinstance(entry, dict):
            qid = entry.get("quoted_id")
            if qid:
                ids.append(str(qid))
        elif isinstance(entry, str):
            ids.append(entry)
    return ids


def record_repost(
    state: dict,
    candidate: CandidatePost,
    selection,
    post_id: str,
) -> None:
    state.setdefault("reposted", []).append(
        {
            "quoted_id": candidate.id,
            "quoted_username": candidate.username,
            "quoted_permalink": candidate.permalink,
            "quoted_timestamp": candidate.timestamp,
            "matched_keyword": candidate.matched_keyword,
            "viral_score": selection.viral_score,
            "brand_fit_score": selection.brand_fit_score,
            "post_id": post_id,
            "reposted_at": _now_iso(),
        }
    )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------
def find_candidates(
    keywords: Optional[List[str]],
    fresh_hours: int,
    seen_ids: List[str],
    top_n: int,
) -> List[CandidatePost]:
    searcher = ThreadsSearcher()
    candidates = searcher.find_candidates(
        keywords=keywords if keywords else None,
        fresh_hours=fresh_hours,
        per_kw_limit=DEFAULT_PER_KW_LIMIT,
        video_only=True,
        top_n=top_n,
        seen_ids=seen_ids,
    )
    return candidates


def select_with_llm(candidates: List[CandidatePost]):
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
    selection = parse_selection(raw, num_candidates=len(candidates))
    return selection


def publish_quote(candidate: CandidatePost, comment: str) -> str:
    pub = ThreadsPublisher()
    return pub.create_quote_post(text=comment, quoted_post_id=candidate.id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Quote-repost a popular car-related Threads post (火金 17:00 JST)."
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="run search + LLM selection, do NOT publish or update state",
    )
    p.add_argument(
        "--preview-only",
        action="store_true",
        help="run search only, print 3 candidates, do NOT call LLM / publish",
    )
    p.add_argument(
        "--fresh-hours",
        type=int,
        default=DEFAULT_FRESH_HOURS,
        help=f"freshness window in hours (default {DEFAULT_FRESH_HOURS})",
    )
    p.add_argument(
        "--keyword",
        action="append",
        default=[],
        help="add a keyword (repeatable). If empty, uses searcher defaults.",
    )
    p.add_argument(
        "--top-n",
        type=int,
        default=DEFAULT_TOP_N,
        help=f"number of candidates to shortlist (default {DEFAULT_TOP_N})",
    )
    p.add_argument(
        "--min-viral",
        type=int,
        default=DEFAULT_MIN_VIRAL,
        help=f"minimum viral_score required to publish (default {DEFAULT_MIN_VIRAL})",
    )
    p.add_argument(
        "--min-fit",
        type=int,
        default=DEFAULT_MIN_FIT,
        help=f"minimum brand_fit_score required to publish (default {DEFAULT_MIN_FIT})",
    )
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    state = load_state()
    seen = reposted_ids(state)
    LOG.info("Loaded %d previously-reposted IDs from state", len(seen))

    try:
        candidates = find_candidates(
            keywords=args.keyword or None,
            fresh_hours=args.fresh_hours,
            seen_ids=seen,
            top_n=args.top_n,
        )
    except ThreadsSearchError as e:
        LOG.error("Threads keyword_search failed: %s", e)
        return 2

    if not candidates:
        LOG.info("No suitable candidates found this run. Exiting cleanly.")
        return 0

    LOG.info("Shortlisted %d candidates", len(candidates))
    for i, c in enumerate(candidates):
        LOG.info("  [%d] @%s %s score=%.2f reasons=%s",
                 i, c.username, c.media_type, c.score, ",".join(c.score_reasons))

    if args.preview_only:
        out = {
            "candidates": [c.to_summary() for c in candidates],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    try:
        selection = select_with_llm(candidates)
    except (ChatLLMError, QuoteSelectError) as e:
        LOG.error("LLM selection failed: %s", e)
        return 2

    chosen = candidates[selection.selected_index]
    LOG.info(
        "LLM picked candidate [%d] @%s viral=%d fit=%d reason=%s",
        selection.selected_index, chosen.username,
        selection.viral_score, selection.brand_fit_score, selection.reason,
    )
    LOG.info("Generated comment (%d chars):\n%s", len(selection.comment), selection.comment)

    if selection.viral_score < args.min_viral or selection.brand_fit_score < args.min_fit:
        LOG.warning(
            "Skipping publish: viral=%d (need >=%d), fit=%d (need >=%d)",
            selection.viral_score, args.min_viral,
            selection.brand_fit_score, args.min_fit,
        )
        return 0

    if args.dry_run:
        LOG.info("Dry run: skipping Threads publish + state update.")
        print(json.dumps({
            "selection": selection.to_dict(),
            "chosen": chosen.to_summary(),
        }, ensure_ascii=False, indent=2))
        return 0

    try:
        post_id = publish_quote(chosen, selection.comment)
    except ThreadsError as e:
        LOG.error("Threads quote publish failed: %s", e)
        return 3

    LOG.info("Threads quote-repost id: %s", post_id)
    record_repost(state, chosen, selection, post_id)
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
