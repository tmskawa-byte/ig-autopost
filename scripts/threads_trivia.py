#!/usr/bin/env python3
"""
Threads "車の豆知識" auto-post pipeline.

別パイプライン: blog 切り抜き (scripts/threads_post.py) とは独立。
週 5 (火水金土日 朝 7:00 JST) で AI が短い豆知識を生成して Threads に投稿。

Flow:
    1. state/threads_trivia_topics.json から過去 30 件のテーマ履歴を読む
    2. ChatLLM (gemini-3.1-pro-preview, response_format=json) に
       「過去テーマを避けて」豆知識を JSON で生成させる
       {"topic": "<3-12字の短いテーマ>", "text": "<60-150字本文>"}
    3. text の長さと禁止トピック (金融/投資/bot 系) をローカル validate
    4. 失敗時は最大 --max-attempts 回まで再生成
    5. Threads に TEXT で publish (link_attachment なし、純価値提供)
    6. state を append、最新 N 件のみ keep

Env vars:
    THREADS_ACCESS_TOKEN   (required)
    THREADS_USER_ID        (required)
    THREADS_APP_SECRET     (optional - appsecret_proof 用)
    CHATLLM_API_KEY        (required)
    LOG_LEVEL              (optional, default INFO)

CLI flags:
    --dry-run        生成までやって publish と state 更新をスキップ。
    --preview-only   --dry-run と同じ動き（本パイプラインには記事選定が
                     ないので、生成自体が "preview" 単位）。生成 JSON を表示。
    --max-attempts N validate 失敗時の再試行回数 (default 4)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

# Allow running both as `python scripts/threads_trivia.py` from repo root and
# as `python -m scripts.threads_trivia`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.chatllm_client import ChatLLMClient, ChatLLMError  # noqa: E402
from lib.threads_publisher import ThreadsError, ThreadsPublisher  # noqa: E402

LOG = logging.getLogger("threads_trivia")

STATE_PATH = REPO_ROOT / "state" / "threads_trivia_topics.json"
STATE_MAX_ENTRIES = 200       # 全履歴の上限（disk 節約）
DEDUP_WINDOW = 30             # 重複防止で prompt に渡すテーマ数

CHATLLM_MODEL = "gemini-3.1-pro-preview"

TEXT_MIN_LEN = 60
TEXT_MAX_LEN = 150
TEXT_HARD_LIMIT = 500         # Threads API 仕様（念のため）

# 金融・投資・bot 系（防犯リスク／ケンちゃんメモ）
BANNED_SUBSTRINGS = (
    "投資", "金融", "資産運用", "FX", "fx", "仮想通貨", "暗号資産",
    "NFT", "副業", "稼げ", "稼ぐ", "bot", "ボット", "Bot",
)

# 関西弁の典型語尾（標準語フランク厳守、関西弁 NG）
KANSAI_HINTS = (
    "やで", "やわ", "ちゃう", "ほんま", "あかん", "せやで", "せやな",
    "なんやねん", "やねん",
)

SYSTEM_PROMPT = """あなたは長崎・対馬で整備工場を営む「対馬モータースサービス」(IG/Threads: @kawatms) の整備士です。
Threads で「車の豆知識」を 1 投稿だけ作ります。

# トーン（厳密に守る）
- 標準語フランク。ですます調にしない、敬体禁止。短く言い切る。
- 整備士目線、現場感、実用、具体的。専門用語は最小限。
- ボソッと一言つぶやく温度感。煽り・誇張・絵文字過多 NG。
- 絵文字は基本なし、入れても合計 1 個まで。
- 関西弁 NG（標準語のフランク）。
- ハッシュタグ・URL・CTA は付けない。純粋な価値提供だけ。
- 「詳しくはブログで」「DM ください」のような誘導は禁止。

# 長さ
- 本文 60〜150 字（全角換算）。
- 改行は 0〜2 回。

# 内容
- 季節 tips（梅雨、夏、冬、雪、台風、花粉 etc）も歓迎。
- ブレーキ、タイヤ、ワイパー、バッテリー、エンジン、エアコン、
  オイル、燃費、洗車、車検、保険、運転習慣など何でも可。
- ただし「過去のテーマ」と被らないよう、別の切り口を選ぶ。

# 禁止トピック
- 金融、投資、副業、FX、仮想通貨、NFT、bot、自動売買、稼ぐ系の話題は一切禁止。
- 政治、宗教、特定メーカーの誹謗中傷も禁止。

# 出力フォーマット
必ず以下の JSON だけを返す（マークダウン code fence なし、説明文なし）:

{"topic": "<3〜12 字の短いテーマ名>", "text": "<本文 60〜150 字>"}

# トーン例（この温度感で）
例1 (ブレーキ):
ブレーキパッド減ってくると音が変わる。キー → ギー → ガーってだんだんうるさくなる。ガーまで来たらもうローター削ってる、すぐ整備工場へ来て。

例2 (ワイパー):
ワイパー、拭き残しが出てきたら賞味期限切れ。撥水剤入れる前にゴム替えな。雨の日の前夜に気付くと詰む、梅雨前にチェック。

例3 (タイヤ空気圧):
月1で空気圧チェックすると燃費5%違う。スタンドで無料、わざわざ整備工場来なくてOK。サイドウォールに書いてある数値を素直に。
"""

USER_PROMPT_TEMPLATE = """今日の車の豆知識を 1 つ生成してください。

# 過去 30 投稿のテーマ（被らないように別の切り口で）
{avoid_block}

# 季節ヒント
今日は {today} (JST 想定)。季節に合う tip なら自然に絡めて OK（無理に絡めなくても可）。

# 注意
- ハッシュタグ・URL・CTA・絵文字過多 NG
- 標準語フランク・整備士目線・60〜150 字
- 出力は JSON 1 オブジェクトのみ
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
    # 互換: 旧フォーマットで配列のみ保存されていた場合
    if isinstance(data, list):
        return {"posted": data}
    if "posted" not in data or not isinstance(data["posted"], list):
        data["posted"] = []
    return data


def save_state(state: dict) -> None:
    posted = state.get("posted", [])
    if len(posted) > STATE_MAX_ENTRIES:
        state["posted"] = posted[-STATE_MAX_ENTRIES:]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(STATE_PATH)
    LOG.info("State written: %s (%d entries)", STATE_PATH, len(state["posted"]))


def recent_topics(state: dict, n: int = DEDUP_WINDOW) -> List[str]:
    posted = state.get("posted", [])
    topics: List[str] = []
    for entry in posted[-n:]:
        if isinstance(entry, dict):
            t = entry.get("topic") or ""
            if t:
                topics.append(str(t))
        elif isinstance(entry, str):
            topics.append(entry)
    return topics


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------
def _format_avoid_block(topics: List[str]) -> str:
    if not topics:
        return "(まだ投稿履歴なし。自由に選んで OK)"
    return "\n".join(f"- {t}" for t in topics)


def _today_jst_str() -> str:
    # GitHub Actions runner is UTC. JST = UTC+9.
    from datetime import timedelta
    now_utc = datetime.now(timezone.utc)
    jst = now_utc + timedelta(hours=9)
    return jst.strftime("%Y-%m-%d (%a)")


def _parse_json_payload(raw: str) -> Tuple[str, str]:
    """Parse {"topic": "...", "text": "..."} tolerantly."""
    text = (raw or "").strip()
    # Strip code fence if present (model occasionally adds it)
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    # First { ... last }
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    data = json.loads(text)
    topic = str(data.get("topic", "")).strip()
    body = str(data.get("text", "")).strip()
    if not topic or not body:
        raise ValueError(f"Missing topic/text in payload: {data!r}")
    return topic, body


def _validate_text(body: str, recent: List[str]) -> Optional[str]:
    """Return None if OK, else a short error reason string."""
    n = len(body)
    if n < TEXT_MIN_LEN:
        return f"too short ({n} < {TEXT_MIN_LEN})"
    if n > TEXT_MAX_LEN:
        return f"too long ({n} > {TEXT_MAX_LEN})"
    if n > TEXT_HARD_LIMIT:
        return f"exceeds Threads hard limit ({n} > {TEXT_HARD_LIMIT})"
    lowered = body.lower()
    for bad in BANNED_SUBSTRINGS:
        if bad.lower() in lowered:
            return f"banned topic keyword: {bad}"
    for kan in KANSAI_HINTS:
        if kan in body:
            return f"kansai-ben token detected: {kan}"
    if body.count("#") > 0:
        return "must not contain hashtags"
    if "http://" in body or "https://" in body:
        return "must not contain URLs"
    return None


def generate_trivia(
    client: ChatLLMClient,
    recent: List[str],
    max_attempts: int = 4,
) -> Tuple[str, str]:
    """
    Returns (topic, text). Raises ChatLLMError on persistent failure.
    """
    user_msg = USER_PROMPT_TEMPLATE.format(
        avoid_block=_format_avoid_block(recent),
        today=_today_jst_str(),
    )
    last_err = "(none)"
    for attempt in range(1, max_attempts + 1):
        LOG.info("Generation attempt %d/%d", attempt, max_attempts)
        try:
            raw = client.chat(
                model=CHATLLM_MODEL,
                system=SYSTEM_PROMPT,
                user=user_msg,
                response_format="json",
            )
        except ChatLLMError as e:
            last_err = f"ChatLLM error: {e}"
            LOG.warning("Attempt %d failed: %s", attempt, last_err)
            continue

        try:
            topic, body = _parse_json_payload(raw)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = f"parse error: {e} (raw head: {raw[:120]!r})"
            LOG.warning("Attempt %d parse failed: %s", attempt, last_err)
            # 次の試行ではモデルに失敗理由を伝えてリトライ
            user_msg = (
                USER_PROMPT_TEMPLATE.format(
                    avoid_block=_format_avoid_block(recent),
                    today=_today_jst_str(),
                )
                + f"\n\n# 前回失敗理由\n{last_err}\n出力は JSON 1 オブジェクトのみ。"
            )
            continue

        # トピック被りチェック（完全一致のみ。緩い類似は LLM 側に任せる）
        if topic in recent:
            last_err = f"topic dup: {topic!r}"
            LOG.warning("Attempt %d topic duplicate: %s", attempt, last_err)
            user_msg = (
                USER_PROMPT_TEMPLATE.format(
                    avoid_block=_format_avoid_block(recent + [topic]),
                    today=_today_jst_str(),
                )
                + f"\n\n# 前回失敗理由\nテーマ {topic!r} は被ったので別テーマで。"
            )
            continue

        err = _validate_text(body, recent)
        if err:
            last_err = err
            LOG.warning("Attempt %d validation failed: %s", attempt, err)
            user_msg = (
                USER_PROMPT_TEMPLATE.format(
                    avoid_block=_format_avoid_block(recent),
                    today=_today_jst_str(),
                )
                + f"\n\n# 前回失敗理由\n{err}\n再生成してください。"
            )
            continue

        LOG.info("Validation OK: topic=%r len=%d", topic, len(body))
        return topic, body

    raise ChatLLMError(
        f"Generation failed after {max_attempts} attempts. last_err={last_err}"
    )


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------
def publish_text(text: str) -> str:
    pub = ThreadsPublisher()
    return pub.create_text_post(text=text, link_attachment=None)


# ---------------------------------------------------------------------------
# state update
# ---------------------------------------------------------------------------
def record_post(state: dict, topic: str, text: str, post_id: str) -> None:
    state.setdefault("posted", []).append(
        {
            "topic": topic,
            "text": text,
            "post_id": post_id,
            "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate and post a Threads 'car trivia' update."
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="generate text but skip Threads publish and state update",
    )
    p.add_argument(
        "--preview-only",
        action="store_true",
        help="alias for --dry-run in this pipeline (no article selection step)",
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=4,
        help="max generation attempts on validation failure (default 4)",
    )
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    state = load_state()
    recent = recent_topics(state, DEDUP_WINDOW)
    LOG.info("Loaded state: %d total entries, %d recent topics for dedup",
             len(state.get("posted", [])), len(recent))

    try:
        client = ChatLLMClient()
    except ChatLLMError as e:
        LOG.error("ChatLLM init failed: %s", e)
        return 2

    try:
        topic, body = generate_trivia(client, recent, max_attempts=args.max_attempts)
    except ChatLLMError as e:
        LOG.error("Generation failed: %s", e)
        return 2

    LOG.info("Generated: topic=%r len=%d\n%s", topic, len(body), body)

    payload = {"topic": topic, "text": body, "length": len(body)}

    if args.dry_run or args.preview_only:
        LOG.info("Dry run / preview-only: skipping Threads publish and state update.")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    try:
        post_id = publish_text(body)
    except ThreadsError as e:
        LOG.error("Threads publish failed: %s", e)
        return 3

    LOG.info("Threads post id: %s", post_id)
    record_post(state, topic, body, post_id)
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
