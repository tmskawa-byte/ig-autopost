"""
IG Auto Post — entry point.

Pipeline:
    1. Pick topic (rotation: state/topic_index.txt, 1〜9 ループ)
       + subtopic (random、直近5件除外: state/recent_subtopics.json)
    2. Tavily news search (JP media only, last 365 days)
    3. Stage 1: research memo (Gemini 3.1 Pro Preview, thinking mode)
       - may return SKIP -> abort cleanly
    4. Stage 2: caption + image_prompt JSON (same model)
    5. Image generation (Nano Banana Pro, 1:1, 2K)
    6. Upload image to ImgBB -> public URL
    7. Post to Instagram via Graph API
    8. State update (topic_index 進める / recent_subtopics 追記)
       — dry_run/失敗時はスキップ。workflow が次 step で commit & push。

Env vars required:
    TAVILY_API_KEY, CHATLLM_API_KEY, IMGBB_API_KEY,
    IG_ACCESS_TOKEN, IG_BUSINESS_ID

CLI flags:
    --dry-run         Do everything except the final IG publish (state も更新しない).
    --topic TOPIC_ID  Force a specific topic (e.g. topic_3). 順番制を bypass、
                      topic_index は進めない（rotation の流れを乱さない）。
    --subtopic NAME   Force a specific subtopic string (must exist in topic).
                      直近除外フィルタも bypass。
    --seed N          Deterministic random selection (for testing).
    --preview-only    Pick topic+subtopic を表示して即終了（dry_run より浅い確認用）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

from lib.chatllm_client import ChatLLMClient, ChatLLMError
from lib.tavily_client import TavilyClient, TavilyError
from lib.image_gen import fetch_image_bytes, ImageFetchError
from lib.imgbb_uploader import upload as imgbb_upload, ImgBBError
from lib.ig_publisher import IGPublisher, IGError

from prompts.topics import (
    JP_MEDIA_DOMAINS,
    TOPICS,
    pick_subtopic,
    build_query,
    topic_name,
)
from prompts.stage1_research import (
    STAGE1_SYSTEM_PROMPT,
    format_articles_for_llm,
    is_skip,
)
from prompts.stage2_caption import (
    STAGE2_SYSTEM_PROMPT,
    build_stage2_user_input,
)

LOG = logging.getLogger("auto_post")

TEXT_MODEL = "gemini-3.1-pro-preview"
IMAGE_MODEL = "nano_banana_pro"

# Hard ceiling for IG caption (IG limit is 2200; we built prompt for 2100).
CAPTION_HARD_MAX = 2200

# ---------------------------------------------------------------------------
# State files (topic rotation + subtopic dedup) — Pattern C
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(_REPO_ROOT, "state")
TOPIC_INDEX_FILE = os.path.join(STATE_DIR, "topic_index.txt")
RECENT_SUBTOPICS_FILE = os.path.join(STATE_DIR, "recent_subtopics.json")

# Subtopic dedup window: pick from candidates that haven't been used in the last N posts.
SUBTOPIC_EXCLUDE_WINDOW = 5
# Keep at most this many entries in recent_subtopics.json history.
SUBTOPIC_HISTORY_KEEP = 10


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
def read_topic_index() -> int:
    try:
        with open(TOPIC_INDEX_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except FileNotFoundError:
        LOG.warning("topic_index.txt not found; defaulting to 0")
        return 0
    except (ValueError, OSError) as e:
        LOG.warning("topic_index.txt unreadable (%s); defaulting to 0", e)
        return 0


def write_topic_index(n: int) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(TOPIC_INDEX_FILE, "w", encoding="utf-8") as f:
        f.write(str(n))


def read_recent_subtopics() -> List[Dict[str, Any]]:
    try:
        with open(RECENT_SUBTOPICS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        hist = data.get("history", []) or []
        if not isinstance(hist, list):
            LOG.warning("recent_subtopics.json history not a list; resetting")
            return []
        return hist
    except FileNotFoundError:
        LOG.warning("recent_subtopics.json not found; starting empty")
        return []
    except (json.JSONDecodeError, OSError) as e:
        LOG.warning("recent_subtopics.json unreadable (%s); starting empty", e)
        return []


def write_recent_subtopics(history: List[Dict[str, Any]]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    trimmed = history[-SUBTOPIC_HISTORY_KEEP:]
    with open(RECENT_SUBTOPICS_FILE, "w", encoding="utf-8") as f:
        json.dump({"history": trimmed}, f, ensure_ascii=False, indent=2)
        f.write("\n")


def rotation_topic_id(index: int) -> str:
    """topic_1〜topic_9 を順番にループ。

    TOPICS は Python 3.7+ で insertion order を保持するため、
    list(TOPICS.keys()) は ['topic_1', ..., 'topic_9'] になる。
    """
    keys = list(TOPICS.keys())
    return keys[index % len(keys)]


def pick_subtopic_with_dedup(
    topic_id: str,
    history: List[Dict[str, Any]],
    rng: random.Random,
) -> str:
    """直近 SUBTOPIC_EXCLUDE_WINDOW 件の subtopic を除外して random 抽選。

    全候補が直近に出ていたら fallback で全候補から random。
    """
    candidates = list(TOPICS[topic_id]["subtopics"])
    recent_subtopics = {
        h.get("subtopic") for h in history[-SUBTOPIC_EXCLUDE_WINDOW:]
        if isinstance(h, dict) and h.get("subtopic")
    }
    available = [s for s in candidates if s not in recent_subtopics]
    if not available:
        LOG.warning(
            "All %d subtopics of %s appear in last %d history entries; "
            "falling back to full pool.",
            len(candidates), topic_id, SUBTOPIC_EXCLUDE_WINDOW,
        )
        available = candidates
    chosen = rng.choice(available)
    LOG.info(
        "Subtopic pick: %r from %d available (excluded %d recent: %s)",
        chosen, len(available), len(recent_subtopics),
        sorted(recent_subtopics) if recent_subtopics else "[]",
    )
    return chosen

def format_recent_subtopics_for_prompt(
    history: List[Dict[str, Any]],
    window: int = SUBTOPIC_EXCLUDE_WINDOW,
) -> str:
    """直近 ``window`` 件の subtopic を Stage 2 プロンプト用にフォーマット。

    Pattern D: subtopic 文字列マッチ dedup (Pattern C) では「自転車事故 賠償」と
    「後遺障害 慰謝料 判例」のように字面が違っても意味が被るケースを取れない。
    そこで AI 自身に直近投稿の subtopic 一覧を渡し、意味レベルで「これと
    被らない角度で書け」と判断させる。

    history が空 or 全エントリが無効なら空文字 (= 注入なし)。
    """
    recent = [
        h for h in history[-window:]
        if isinstance(h, dict) and h.get("subtopic")
    ]
    if not recent:
        return ""
    lines = ["## 直近の IG 投稿サブトピック（これらと内容が被らないこと）"]
    # 新しい順に並べる方が AI にとって読みやすい
    for entry in reversed(recent):
        date_str = entry.get("date", "") or ""
        try:
            parts = date_str.split("-")
            md = f"{int(parts[1])}/{int(parts[2])}"
        except (ValueError, IndexError):
            md = date_str
        topic_id = entry.get("topic_id", "")
        subtopic = entry.get("subtopic", "")
        lines.append(f"- {md}: {subtopic}（{topic_id}）")
    lines.append("")
    lines.append(
        "【重要】上記の subtopic と意味的に重なる内容、似た判例、同じキーワード"
        "（例: 直近が「自転車事故 9500万」なら別の自転車事故判例も避ける）を"
        "再掲しない。新しい角度・別の切り口で書くこと。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def extract_json(text: str) -> Dict[str, Any]:
    """
    Parse a JSON object from the model's response. Tolerates:
      - markdown code fences (```json ... ```)
      - leading/trailing whitespace or commentary
    """
    if not text or not text.strip():
        raise ValueError("Empty response")

    s = text.strip()

    # Strip markdown fences if present.
    fence = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()

    # Try direct parse first.
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Fallback: find the first { ... } block.
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"No JSON object found in response: {s[:200]}")
    candidate = s[start : end + 1]
    return json.loads(candidate)


def validate_caption(caption: str) -> str:
    caption = caption.strip()
    if not caption:
        raise ValueError("Caption is empty")
    if len(caption) > CAPTION_HARD_MAX:
        # Trim hashtags from the end conservatively rather than mid-sentence.
        LOG.warning(
            "Caption is %d chars (max %d); trimming.",
            len(caption),
            CAPTION_HARD_MAX,
        )
        caption = caption[:CAPTION_HARD_MAX].rstrip()
    return caption


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="IG auto-post pipeline")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pipeline but do NOT publish to Instagram (image is still generated + uploaded). State も更新しない。",
    )
    p.add_argument("--topic", default=None, help="Force topic id (e.g. topic_3). 順番制 bypass・index は進めない。")
    p.add_argument("--subtopic", default=None, help="Force subtopic string. 直近除外フィルタも bypass。")
    p.add_argument("--seed", type=int, default=None, help="Seed for random selection")
    p.add_argument(
        "--preview-only",
        action="store_true",
        help="Pick topic+subtopic を表示して即終了。state は更新しない（rotation の流れを乱さない確認用）。",
    )
    return p.parse_args()


def main() -> int:
    setup_logging()
    args = parse_args()

    LOG.info("=== IG Auto Post start: %s ===", datetime.now(timezone.utc).isoformat())
    if args.dry_run:
        LOG.warning("DRY RUN: IG publish step will be skipped. State も更新しない。")
    if args.preview_only:
        LOG.warning("PREVIEW ONLY: topic/subtopic を表示して即終了。")

    rng = random.Random(args.seed) if args.seed is not None else random

    # ----- 1. Topic / subtopic -----
    current_index = read_topic_index()
    history = read_recent_subtopics()
    recent_posts_block = format_recent_subtopics_for_prompt(history)
    LOG.info(
        "State: topic_index=%d, recent_subtopics_history=%d entries",
        current_index, len(history),
    )
    if recent_posts_block:
        injected_count = sum(
            1 for line in recent_posts_block.splitlines() if line.startswith("- ")
        )
        LOG.info(
            "Pattern D: injecting %d recent post(s) into Stage 2 prompt",
            injected_count,
        )
        LOG.info("Pattern D recent_posts_block:\n%s", recent_posts_block)
    else:
        LOG.info("Pattern D: no recent subtopics to inject (history empty).")

    if args.topic:
        if args.topic not in TOPICS:
            LOG.error("Unknown topic: %s (valid: %s)", args.topic, list(TOPICS.keys()))
            return 2
        topic_id = args.topic
        topic_from_rotation = False
        LOG.info("Topic forced via --topic; rotation index will NOT advance.")
    else:
        topic_id = rotation_topic_id(current_index)
        topic_from_rotation = True
        LOG.info(
            "Topic from rotation: index=%d -> %s", current_index, topic_id,
        )

    if args.subtopic:
        if args.subtopic not in TOPICS[topic_id]["subtopics"]:
            LOG.error(
                "Subtopic %r not in topic %s. Valid: %s",
                args.subtopic,
                topic_id,
                TOPICS[topic_id]["subtopics"],
            )
            return 2
        subtopic = args.subtopic
        LOG.info("Subtopic forced via --subtopic; dedup filter bypassed.")
    else:
        subtopic = pick_subtopic_with_dedup(topic_id, history, rng)

    LOG.info("Topic: %s (%s)", topic_id, topic_name(topic_id))
    LOG.info("Subtopic: %s", subtopic)

    if args.preview_only:
        LOG.info("=== PREVIEW ONLY: exiting before Tavily search ===")
        return 0

    # ----- 2. Tavily search -----
    try:
        tavily = TavilyClient()
        query = build_query(topic_id, subtopic)
        LOG.info("Tavily query: %s", query)
        articles = tavily.search(
            query=query,
            topic="news",
            max_results=10,
            days=365,
            include_domains=JP_MEDIA_DOMAINS,
        )
    except TavilyError as e:
        LOG.error("Tavily failure: %s", e)
        return 3
    LOG.info("Got %d article(s)", len(articles))
    if not articles:
        LOG.warning("Tavily returned 0 articles; aborting (nothing to write about).")
        return 0  # not an error, just no signal today

    # ----- 3. Stage 1: research memo -----
    chatllm = ChatLLMClient()
    user_input_1 = format_articles_for_llm(articles, topic_id, subtopic)
    try:
        LOG.info("Stage 1 (research memo) starting...")
        research_memo = chatllm.chat(
            model=TEXT_MODEL,
            system=STAGE1_SYSTEM_PROMPT,
            user=user_input_1,
            timeout=240,
        )
    except ChatLLMError as e:
        LOG.error("Stage 1 failed: %s", e)
        return 4
    LOG.info("Stage 1 output length: %d chars", len(research_memo))
    LOG.info("Stage 1 (head): %s", research_memo[:300].replace("\n", " "))

    if is_skip(research_memo):
        LOG.warning("Stage 1 returned SKIP. Aborting cleanly. memo=%r", research_memo[:200])
        return 0

    # ----- 4. Stage 2: caption + image_prompt -----
    user_input_2 = build_stage2_user_input(
        research_memo,
        topic_id,
        subtopic,
        recent_posts_block=recent_posts_block,
    )
    try:
        LOG.info("Stage 2 (caption + image_prompt) starting...")
        stage2_raw = chatllm.chat(
            model=TEXT_MODEL,
            system=STAGE2_SYSTEM_PROMPT,
            user=user_input_2,
            response_format="json",
            timeout=240,
        )
    except ChatLLMError as e:
        LOG.error("Stage 2 failed: %s", e)
        return 5
    LOG.info("Stage 2 raw length: %d chars", len(stage2_raw))

    try:
        stage2 = extract_json(stage2_raw)
    except (ValueError, json.JSONDecodeError) as e:
        LOG.error("Stage 2 JSON parse failed: %s\nraw=%s", e, stage2_raw[:1000])
        return 6

    caption = stage2.get("caption", "")
    image_prompt = stage2.get("image_prompt", "")
    if not caption or not image_prompt:
        LOG.error("Stage 2 missing fields: caption=%r image_prompt=%r", caption[:60], image_prompt[:60])
        return 6

    try:
        caption = validate_caption(caption)
    except ValueError as e:
        LOG.error("Caption invalid: %s", e)
        return 6
    LOG.info("Caption: %d chars", len(caption))
    LOG.info("Image prompt (head): %s", image_prompt[:240].replace("\n", " "))

    # ----- 5. Image generation -----
    try:
        LOG.info("Generating image via %s ...", IMAGE_MODEL)
        image_url = chatllm.generate_image(
            model=IMAGE_MODEL,
            prompt=image_prompt,
            aspect_ratio="1:1",
            resolution="2K",
            num_images=1,
            timeout=360,
        )
    except ChatLLMError as e:
        LOG.error("Image generation failed: %s", e)
        return 7
    LOG.info("Image URL kind: %s", "data-url" if image_url.startswith("data:") else "http")

    # ----- 6. Download + ImgBB upload -----
    try:
        image_bytes, mime = fetch_image_bytes(image_url, timeout=120)
    except ImageFetchError as e:
        LOG.error("Image fetch failed: %s", e)
        return 8
    LOG.info("Image bytes: %d, mime: %s", len(image_bytes), mime)
    if len(image_bytes) > 8 * 1024 * 1024:
        LOG.warning("Image is %d bytes, may exceed IG's 8MB limit", len(image_bytes))

    try:
        public_url = imgbb_upload(image_bytes, name=f"kawatms_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}")
    except ImgBBError as e:
        LOG.error("ImgBB upload failed: %s", e)
        return 9
    LOG.info("ImgBB public URL: %s", public_url)

    # ----- 7. Publish to Instagram -----
    if args.dry_run:
        LOG.warning("DRY RUN: skipping IG publish. Would post:\n--- CAPTION ---\n%s\n--- /CAPTION ---", caption)
        LOG.info("DRY RUN: image available at %s", public_url)
        LOG.info("DRY RUN: state は更新しない（rotation 進めない）。")
        return 0

    try:
        ig = IGPublisher()
        post_id = ig.post(image_url=public_url, caption=caption)
    except IGError as e:
        LOG.error("IG publish failed: %s", e)
        return 10
    LOG.info("=== Posted successfully: post_id=%s ===", post_id)

    # ----- 8. State update (only on real, successful publish) -----
    try:
        if topic_from_rotation:
            write_topic_index(current_index + 1)
            LOG.info("topic_index advanced: %d -> %d", current_index, current_index + 1)
        else:
            LOG.info("topic_index NOT advanced (--topic was forced).")
        history.append({
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "topic_id": topic_id,
            "subtopic": subtopic,
        })
        write_recent_subtopics(history)
        LOG.info("recent_subtopics updated; history length=%d (kept %d).",
                 len(history), min(len(history), SUBTOPIC_HISTORY_KEEP))
    except OSError as e:
        # 投稿は成功しているので終了コードは 0。state は workflow が次回見れば
        # 古いままなので、同じ topic_index で動く（rotation が1日ズレる程度の影響）。
        LOG.warning("State update failed AFTER successful post: %s. Post id=%s",
                    e, post_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
