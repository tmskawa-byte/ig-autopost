#!/usr/bin/env python3
"""
X (Twitter) auto-post pipeline for @tmskawat38u.

This is the X analogue of ``scripts/threads_post.py``. The existing Threads
pipeline is left untouched; this one mirrors its structure but posts to X.

Flow:
    1. Fetch latest articles from tsushima-motor.com RSS
    2. Reserve the latest article for Threads
    3. Pick the newest remaining article not posted to X yet
    4. Generate a Japanese "整備士目線" excerpt (X-sized) via ChatLLM
    5. Post a text tweet (caption + article URL) via the X API v2 (OAuth 1.0a)
    6. Update state/x_posted.json

Env vars:
    KAWATMS_X_CONSUMER_KEY          (required)
    KAWATMS_X_CONSUMER_SECRET       (required)
    KAWATMS_X_ACCESS_TOKEN          (required)
    KAWATMS_X_ACCESS_TOKEN_SECRET   (required)
    CHATLLM_API_KEY                 (required)
    BLOG_RSS_URL                    (optional, defaults to tsushima-motor.com)
    LOG_LEVEL                       (optional, defaults to INFO)

CLI flags:
    --dry-run        run pipeline up to caption generation; do NOT post or
                     update state. Prints the candidate tweet + article.
    --preview-only   pick the article and print it; do NOT generate caption,
                     post, or update state.
    --force-slug X   pick the article with this slug regardless of dedup.

Note on length: X counts most Japanese characters as 2 weighted units and
collapses every URL to 23, so the hard 280-unit budget is roughly "120 全角字
+ 1 URL". We ask ChatLLM for a short body and hard-truncate (by weighted
length) as a safety net before posting.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

# Allow running both as `python scripts/x_post.py` from repo root and
# as `python -m scripts.x_post`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.blog_reader import Article, BlogReaderError, fetch_latest_articles  # noqa: E402
from lib.chatllm_client import ChatLLMClient, ChatLLMError  # noqa: E402
from lib.x_publisher import XError, XPublisher, weighted_len  # noqa: E402

LOG = logging.getLogger("x_post")

STATE_PATH = REPO_ROOT / "state" / "x_posted.json"
THREADS_STATE_PATH = REPO_ROOT / "state" / "threads_posted.json"
STATE_MAX_ENTRIES = 200  # keep recent history bounded
DEFAULT_RSS_URL = os.environ.get(
    "BLOG_RSS_URL", "https://tsushima-motor.com/rss.xml"
)

# X hard limit is 280 *weighted* units (JP char = 2, URL = 23). We ask the
# model for a small full-width budget and enforce the weighted limit on build.
TEXT_HARD_LIMIT = 280
CAPTION_CHAR_BUDGET = 110  # 全角字 target handed to ChatLLM (≈ 220 weighted)

CHATLLM_MODEL = "gemini-3.1-pro-preview"

SYSTEM_PROMPT = """あなたは長崎・対馬で整備工場を営む「対馬モータースサービス」(X: @tmskawat38u) の整備士です。
親しみやすい兄さん/姉さんのトーンで、ですます調。
専門用語は最小限、現場感のあるちょっとした一言を添えるのが得意。
絵文字は最大 2 個まで。煽り表現・誇張表現は使わない。
ハッシュタグは最後にまとめて 1〜2 個（必ず #対馬モータースサービス を含める）。
"""

USER_PROMPT_TEMPLATE = """以下は当店のブログ最新記事です。
これを「X(旧Twitter) 用の短い切り抜き投稿」に書き直してください。

# 制約
- 全角で {budget} 字以内（記事 URL を末尾に自動で貼ります。X は日本語1文字を2カウントで数えるので短めに）
- 改行 1〜2 回までで読みやすく
- 記事の中で一番「整備士目線でちょっと面白い／役立つ」 1 ポイントだけ拾う
- 「詳しくはブログで👇」のような誘導 1 行を入れる（URL は最後に自動で付くのでここでは書かない）
- 最後にハッシュタグ 1〜2 個（必ず #対馬モータースサービス を含める）
- 出力は本文のみ。前置きや説明文は一切付けない

# 記事
タイトル: {title}
URL: {link}
カテゴリ: {categories}
要約: {description}
"""


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
def load_state(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        return {"posted": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        LOG.warning("State unreadable, treating as empty: %s", e)
        return {"posted": []}
    if "posted" not in data or not isinstance(data["posted"], list):
        data["posted"] = []
    return data


def save_state(state: dict) -> None:
    posted = state.get("posted", [])
    # Trim history
    if len(posted) > STATE_MAX_ENTRIES:
        state["posted"] = posted[-STATE_MAX_ENTRIES:]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(STATE_PATH)
    LOG.info("State written: %s (%d entries)", STATE_PATH, len(state["posted"]))


def already_posted(state: dict, article: Article) -> bool:
    key = article.slug or article.link
    posted = state.get("posted", [])
    for entry in posted:
        if isinstance(entry, dict):
            if entry.get("slug") == article.slug or entry.get("link") == article.link:
                return True
        elif isinstance(entry, str):
            if entry == article.slug or entry == article.link:
                return True
    return key == ""  # treat empty-key items as "already handled" to skip them


def _parse_iso_datetime(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def last_posted_at(state: dict, article: Article) -> Optional[datetime]:
    latest: Optional[datetime] = None
    for entry in state.get("posted", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("slug") != article.slug and entry.get("link") != article.link:
            continue
        posted_at = _parse_iso_datetime(str(entry.get("posted_at") or ""))
        if posted_at and (latest is None or posted_at > latest):
            latest = posted_at
    return latest


def recently_posted(state: dict, article: Article, cooldown_days: int) -> bool:
    posted_at = last_posted_at(state, article)
    if posted_at is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, cooldown_days))
    return posted_at >= cutoff


# ---------------------------------------------------------------------------
# article selection
# ---------------------------------------------------------------------------
def pick_article(
    articles: List[Article],
    state: dict,
    avoid_state: Optional[dict] = None,
    force_slug: Optional[str] = None,
    random_pick: bool = False,
    cooldown_days: int = 30,
    seed: Optional[int] = None,
) -> Optional[Article]:
    if force_slug:
        for a in articles:
            if a.slug == force_slug:
                return a
        LOG.error("--force-slug %r not found in feed", force_slug)
        return None

    if len(articles) < 2:
        LOG.warning(
            "Only %d article(s) in feed; skipping X post because the latest "
            "article is reserved for Threads.",
            len(articles),
        )
        return None

    latest = articles[0]
    LOG.info(
        "Reserving latest article for Threads: title=%r slug=%s link=%s",
        latest.title,
        latest.slug,
        latest.link,
    )

    candidates = articles[1:]

    if random_pick:
        rng = random.Random(seed) if seed is not None else random
        avoid_state = avoid_state or {"posted": []}
        eligible = [
            a
            for a in candidates
            if not recently_posted(state, a, cooldown_days)
            and not recently_posted(avoid_state, a, cooldown_days)
        ]
        avoid_safe_pool = [
            a for a in candidates if not recently_posted(avoid_state, a, cooldown_days)
        ]
        pool = eligible or avoid_safe_pool or list(candidates)
        if not eligible:
            LOG.warning(
                "No older article outside the %d-day X/Threads cooldown; falling back to %d older feed articles.",
                cooldown_days,
                len(pool),
            )
        if not pool:
            return None
        article = rng.choice(pool)
        LOG.info(
            "Random selected article: title=%r slug=%s cooldown_days=%d eligible=%d older_total=%d",
            article.title,
            article.slug,
            cooldown_days,
            len(eligible),
            len(candidates),
        )
        return article

    avoid_state = avoid_state or {"posted": []}
    for a in candidates:
        if already_posted(state, a):
            continue
        if recently_posted(avoid_state, a, cooldown_days):
            continue
        return a
    LOG.info(
        "No X candidate found after reserving the latest article; all %d older "
        "feed article(s) are already posted to X.",
        max(0, len(articles) - 1),
    )
    return None


# ---------------------------------------------------------------------------
# caption generation
# ---------------------------------------------------------------------------
def generate_caption(client: ChatLLMClient, article: Article) -> str:
    user_msg = USER_PROMPT_TEMPLATE.format(
        budget=CAPTION_CHAR_BUDGET,
        title=article.title or "(no title)",
        link=article.link,
        categories=", ".join(article.categories) or "(none)",
        description=(article.description or "(no description)")[:600],
    )
    LOG.info("Calling ChatLLM model=%s for caption", CHATLLM_MODEL)
    raw = client.chat(model=CHATLLM_MODEL, system=SYSTEM_PROMPT, user=user_msg)
    return _clean_caption(raw)


def is_credit_exhausted_error(error: ChatLLMError) -> bool:
    text = str(error).lower()
    return "no remaining credits" in text or "remaining credits" in text


def generate_fallback_caption(article: Article) -> str:
    title = article.title or "整備士目線の新しい記事"
    caption = (
        f"{title}\n"
        "整備士目線でポイントを整理しました。\n"
        "#対馬モーターサービス"
    )
    return caption


def _clean_caption(raw: str) -> str:
    text = (raw or "").strip()
    # Strip enclosing code fences if model added them.
    if text.startswith("```"):
        text = re.sub(r"^```[^\n]*\n", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    # Normalize excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate_weighted(text: str, budget: int) -> str:
    """Truncate ``text`` so its weighted length is <= ``budget``."""
    out: List[str] = []
    used = 0
    for ch in text:
        w = 1 if ch.isascii() else 2
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out)


def build_final_text(caption: str, link: str) -> str:
    """
    Append the article URL to the caption (separated by a newline), truncating
    the caption side if the combined *weighted* length would exceed 280.
    """
    suffix = f"\n{link}" if link else ""
    available = TEXT_HARD_LIMIT - weighted_len(suffix)
    if available < 0:
        # URL alone exceeds the limit; fall back to a trimmed link.
        return _truncate_weighted(link, TEXT_HARD_LIMIT)
    if weighted_len(caption) > available:
        # Reserve 2 weighted units for the trailing ellipsis.
        truncated = _truncate_weighted(caption, max(0, available - 2)).rstrip()
        truncated = re.sub(r"[、。,.\s]+$", "", truncated)
        caption = truncated + "…"
    return f"{caption}{suffix}"


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------
def publish(article: Article, final_text: str) -> str:
    pub = XPublisher()
    LOG.info(
        "Posting tweet (%d weighted units) link=%s",
        weighted_len(final_text), article.link,
    )
    return pub.create_text_post(final_text)


# ---------------------------------------------------------------------------
# state update
# ---------------------------------------------------------------------------
def record_post(state: dict, article: Article, post_id: str) -> None:
    state.setdefault("posted", []).append(
        {
            "slug": article.slug,
            "link": article.link,
            "title": article.title,
            "post_id": post_id,
            "posted_at": _now_iso(),
        }
    )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Post an X update from the latest blog article.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="run pipeline up to caption generation, skip publish and state update",
    )
    p.add_argument(
        "--preview-only",
        action="store_true",
        help="pick the article and print it; do not generate caption or post",
    )
    p.add_argument(
        "--force-slug",
        default=None,
        help="post the article matching this slug regardless of dedup",
    )
    p.add_argument(
        "--rss-url",
        default=DEFAULT_RSS_URL,
        help=f"RSS feed URL (default: {DEFAULT_RSS_URL})",
    )
    p.add_argument(
        "--random",
        action="store_true",
        help="pick a random older article, avoiding recent repeats by cooldown",
    )
    p.add_argument(
        "--cooldown-days",
        type=int,
        default=int(os.environ.get("BLOG_CLIP_COOLDOWN_DAYS", "30")),
        help="days before the same article can be randomly clipped again",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=None,
        help="deterministic random seed for testing",
    )
    p.add_argument(
        "--caption-mode",
        choices=("template", "llm"),
        default=os.environ.get("SOCIAL_CAPTION_MODE", "template"),
        help="template avoids LLM credits; llm uses ChatLLM with template fallback on credit exhaustion",
    )
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    LOG.info("Fetching RSS: %s", args.rss_url)
    try:
        articles = fetch_latest_articles(args.rss_url, limit=20)
    except BlogReaderError as e:
        LOG.error("RSS fetch failed: %s", e)
        return 2

    if not articles:
        LOG.error("Feed has no parseable items")
        return 2

    state = load_state()
    threads_state = load_state(THREADS_STATE_PATH)
    article = pick_article(
        articles,
        state,
        avoid_state=threads_state,
        force_slug=args.force_slug,
        random_pick=args.random,
        cooldown_days=args.cooldown_days,
        seed=args.seed,
    )
    if article is None:
        LOG.info("Nothing to post. Exiting cleanly.")
        return 0

    LOG.info(
        "Selected article: title=%r slug=%s link=%s",
        article.title, article.slug, article.link,
    )

    if args.preview_only:
        print(json.dumps(article.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.caption_mode == "template":
        LOG.info("Using deterministic template caption.")
        caption = generate_fallback_caption(article)
    else:
        try:
            chat = ChatLLMClient()
        except ChatLLMError as e:
            LOG.error("ChatLLM init failed: %s", e)
            return 2

        try:
            caption = generate_caption(chat, article)
        except ChatLLMError as e:
            if not is_credit_exhausted_error(e):
                LOG.error("Caption generation failed: %s", e)
                return 2
            LOG.warning(
                "ChatLLM credits exhausted; using deterministic fallback caption: %s",
                e,
            )
            caption = generate_fallback_caption(article)

    final_text = build_final_text(caption, article.link)
    LOG.info(
        "Final tweet (%d weighted units):\n%s",
        weighted_len(final_text), final_text,
    )

    if args.dry_run:
        LOG.info("Dry run: skipping X publish and state update.")
        print(final_text)
        return 0

    try:
        post_id = publish(article, final_text)
    except XError as e:
        LOG.error("X publish failed: %s", e)
        return 3

    LOG.info("X post id: %s", post_id)
    record_post(state, article, post_id)
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
