"""
IG Auto Post — entry point.

Pipeline:
    1. Pick topic + subtopic (random)
    2. Tavily news search (JP media only, last 365 days)
    3. Stage 1: research memo (Gemini 3.1 Pro Preview, thinking mode)
       - may return SKIP -> abort cleanly
    4. Stage 2: caption + image_prompt JSON (same model)
    5. Image generation (Nano Banana Pro, 1:1, 2K)
    6. Upload image to ImgBB -> public URL
    7. Post to Instagram via Graph API

Env vars required:
    TAVILY_API_KEY, CHATLLM_API_KEY, IMGBB_API_KEY,
    IG_ACCESS_TOKEN, IG_BUSINESS_ID

CLI flags:
    --dry-run         Do everything except the final IG publish.
    --topic TOPIC_ID  Force a specific topic (e.g. topic_3).
    --subtopic NAME   Force a specific subtopic string (must exist in topic).
    --seed N          Deterministic random selection (for testing).
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
from typing import Any, Dict

from lib.chatllm_client import ChatLLMClient, ChatLLMError
from lib.tavily_client import TavilyClient, TavilyError
from lib.image_gen import fetch_image_bytes, ImageFetchError
from lib.imgbb_uploader import upload as imgbb_upload, ImgBBError
from lib.ig_publisher import IGPublisher, IGError

from prompts.topics import (
    JP_MEDIA_DOMAINS,
    TOPICS,
    pick_topic,
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
        help="Run pipeline but do NOT publish to Instagram (image is still generated + uploaded).",
    )
    p.add_argument("--topic", default=None, help="Force topic id (e.g. topic_3)")
    p.add_argument("--subtopic", default=None, help="Force subtopic string")
    p.add_argument("--seed", type=int, default=None, help="Seed for random selection")
    return p.parse_args()


def main() -> int:
    setup_logging()
    args = parse_args()

    LOG.info("=== IG Auto Post start: %s ===", datetime.now(timezone.utc).isoformat())
    if args.dry_run:
        LOG.warning("DRY RUN: IG publish step will be skipped.")

    rng = random.Random(args.seed) if args.seed is not None else random

    # ----- 1. Topic / subtopic -----
    if args.topic:
        if args.topic not in TOPICS:
            LOG.error("Unknown topic: %s (valid: %s)", args.topic, list(TOPICS.keys()))
            return 2
        topic_id = args.topic
    else:
        topic_id, _ = pick_topic(rng)

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
    else:
        subtopic = pick_subtopic(topic_id, rng)

    LOG.info("Topic: %s (%s)", topic_id, topic_name(topic_id))
    LOG.info("Subtopic: %s", subtopic)

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
    user_input_2 = build_stage2_user_input(research_memo, topic_id, subtopic)
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
        return 0

    try:
        ig = IGPublisher()
        post_id = ig.post(image_url=public_url, caption=caption)
    except IGError as e:
        LOG.error("IG publish failed: %s", e)
        return 10
    LOG.info("=== Posted successfully: post_id=%s ===", post_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
