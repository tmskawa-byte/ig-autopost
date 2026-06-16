#!/usr/bin/env python3
"""
Threads auto-post pipeline.

Flow:
    1. Fetch latest articles from tsushima-motor.com RSS
    2. Filter out articles already posted (state/threads_posted.json)
    3. Pick the newest unposted article
    4. Generate a Japanese "整備士目線" excerpt (<= 500 chars total) via ChatLLM
    5. Post to Threads:
       - IMAGE post when the article has an og:image (preferred)
       - TEXT post with link_attachment otherwise
    6. Update state/threads_posted.json

Env vars:
    THREADS_ACCESS_TOKEN   (required)
    THREADS_USER_ID        (required)
    THREADS_APP_ID         (informational; not used directly here)
    THREADS_APP_SECRET     (optional - enables appsecret_proof)
    CHATLLM_API_KEY        (required)
    BLOG_RSS_URL           (optional, defaults to tsushima-motor.com)
    LOG_LEVEL              (optional, defaults to INFO)

CLI flags:
    --dry-run        run pipeline up to caption generation; do NOT post or
                     update state. Prints the candidate caption + article.
    --preview-only   pick the article and print it; do NOT generate caption,
                     post, or update state.
    --force-slug X   pick the article with this slug regardless of dedup.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# Allow running both as `python scripts/threads_post.py` from repo root and
# as `python -m scripts.threads_post`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.blog_reader import Article, BlogReaderError, fetch_latest_articles  # noqa: E402
from lib.chatllm_client import ChatLLMClient, ChatLLMError  # noqa: E402
from lib.threads_publisher import ThreadsError, ThreadsPublisher  # noqa: E402

LOG = logging.getLogger("threads_post")

STATE_PATH = REPO_ROOT / "state" / "threads_posted.json"
STATE_MAX_ENTRIES = 200  # keep recent history bounded
DEFAULT_RSS_URL = os.environ.get(
    "BLOG_RSS_URL", "https://tsushima-motor.com/rss.xml"
)

# Threads hard limit; we leave a small safety margin for trailing link / hashtags.
TEXT_HARD_LIMIT = 500
CAPTION_BUDGET = 460  # ChatLLM target before we append the link
TRAILING_LINK_RESERVE = 40  # rough budget for " " + URL (will be measured exactly)

CHATLLM_MODEL = "gemini-3.1-pro-preview"

SYSTEM_PROMPT = """あなたは長崎・対馬で整備工場を営む「対馬モータースサービス」(IG/Threads: @kawatms) の整備士です。
親しみやすい兄さん/姉さんのトーンで、ですます調。
専門用語は最小限、現場感のあるちょっとした一言を添えるのが得意。
絵文字は最大 2 個まで。煽り表現・誇張表現は使わない。
ハッシュタグは最後にまとめて 2〜4 個（必ず #対馬モータースサービス と #kawatms を含める）。
"""

USER_PROMPT_TEMPLATE = """以下は当店のブログ最新記事です。
これを「Threads 用の短い切り抜き投稿」に書き直してください。

# 制約
- 全角換算で {budget} 字以内（記事 URL を末尾に貼る予定なので、その分は引いてあります）
- 改行 2〜3 回までで読みやすく
- 記事の中で一番「整備士目線でちょっと面白い／役立つ」 1 ポイントだけ拾う
- 「詳しくはブログで👇」のような誘導 1 行を入れる（URL は最後に自動で付くのでここでは書かない）
- 最後にハッシュタグ 2〜4 個（必ず #対馬モータースサービス と #kawatms）
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
def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"posted": []}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
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


# ---------------------------------------------------------------------------
# article selection
# ---------------------------------------------------------------------------
def pick_article(
    articles: List[Article],
    state: dict,
    force_slug: Optional[str] = None,
) -> Optional[Article]:
    if force_slug:
        for a in articles:
            if a.slug == force_slug:
                return a
        LOG.error("--force-slug %r not found in feed", force_slug)
        return None

    for a in articles:
        if not already_posted(state, a):
            return a
    LOG.info("All %d feed articles are already posted.", len(articles))
    return None


# ---------------------------------------------------------------------------
# caption generation
# ---------------------------------------------------------------------------
def generate_caption(client: ChatLLMClient, article: Article) -> str:
    user_msg = USER_PROMPT_TEMPLATE.format(
        budget=CAPTION_BUDGET - TRAILING_LINK_RESERVE,
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
    description = re.sub(r"\s+", " ", article.description or "").strip()
    if len(description) > 120:
        description = description[:119].rstrip() + "…"

    parts = [
        "ブログを更新しました。",
        article.title or "新しい記事を公開しました。",
    ]
    if description:
        parts.append(description)
    parts.extend(
        [
            "詳しくはブログで。",
            "#対馬モーターサービス #kawatms",
        ]
    )
    return "\n\n".join(parts)


def _clean_caption(raw: str) -> str:
    text = (raw or "").strip()
    # Strip enclosing code fences if model added them.
    if text.startswith("```"):
        text = re.sub(r"^```[^\n]*\n", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    # Normalize excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_final_text(caption: str, link: str) -> str:
    """
    Append the article URL to the caption (separated by blank line),
    truncating the caption side if the combined length would exceed 500 chars.
    """
    suffix = f"\n\n{link}" if link else ""
    available = TEXT_HARD_LIMIT - len(suffix)
    if available < 0:
        # link itself is already longer than the post limit; fall back to link only
        return link[:TEXT_HARD_LIMIT]
    if len(caption) > available:
        # Truncate on a sane boundary
        truncated = caption[: max(0, available - 1)].rstrip()
        truncated = re.sub(r"[、。,.\s]+$", "", truncated)
        caption = truncated + "…"
    return f"{caption}{suffix}"


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------
def publish(article: Article, final_text: str) -> str:
    pub = ThreadsPublisher()
    if article.image_url and article.image_url.startswith("https://"):
        LOG.info("Posting IMAGE with image_url=%s", article.image_url)
        return pub.create_image_post(text=final_text, image_url=article.image_url)
    LOG.info("Posting TEXT with link_attachment=%s", article.link)
    return pub.create_text_post(text=final_text, link_attachment=article.link or None)


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
    p = argparse.ArgumentParser(description="Post a Threads update from the latest blog article.")
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
    article = pick_article(articles, state, force_slug=args.force_slug)
    if article is None:
        LOG.info("Nothing to post. Exiting cleanly.")
        return 0

    LOG.info(
        "Selected article: title=%r slug=%s link=%s image=%s",
        article.title, article.slug, article.link, article.image_url,
    )

    if args.preview_only:
        print(json.dumps(article.to_dict(), ensure_ascii=False, indent=2))
        return 0

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
    LOG.info("Final text (%d chars):\n%s", len(final_text), final_text)

    if args.dry_run:
        LOG.info("Dry run: skipping Threads publish and state update.")
        print(final_text)
        return 0

    try:
        post_id = publish(article, final_text)
    except ThreadsError as e:
        LOG.error("Threads publish failed: %s", e)
        return 3

    LOG.info("Threads post id: %s", post_id)
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
