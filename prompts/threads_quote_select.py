"""
Prompt + parsing helpers for the Threads quote-repost LLM call.

We send Gemini 3.1 Pro Preview (thinking) the 3 candidates that came out
of ThreadsSearcher.find_candidates() and ask it to:
    1. Pick the best one for an @kawatms quote repost.
    2. Generate a short, 整備士目線 comment (<= 480 chars to leave
       margin for the 500-char Threads cap).

The model MUST return a single JSON object:
    {
      "selected_index": 0|1|2,
      "comment": "...",
      "viral_score": 0-10,            # how viral-feeling the source post is
      "brand_fit_score": 0-10,        # how well it fits @kawatms brand
      "reason": "..."                  # 1-2 sentences in Japanese
    }
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

LOG = logging.getLogger(__name__)


# Threads enforces a 500-char text cap. We reserve some margin in case the
# downstream caller wants to append anything, plus to keep the model's output
# tight.
COMMENT_HARD_LIMIT = 480


SYSTEM_PROMPT = """あなたは長崎・対馬で整備工場を営む「対馬モータースサービス」(IG/Threads: @kawatms) の整備士です。
親しみやすい兄さん/姉さんのトーンで、ですます調。
専門用語は最小限、現場感のあるちょっとした一言を添えるのが得意。

# あなたの仕事
他のクリエイターさんの良い車関連投稿を Threads で引用リポストするときに、
候補3つの中から「一番うちのフィードに合いそうな投稿」を1つ選び、
その投稿に添える整備士目線のコメントを書きます。

# 評価軸
1. バズりそう度 (viral_score): いいね・閲覧・リポストが伸びそうか
2. うちのブランド合致度 (brand_fit_score): 整備工場の SNS として違和感ないか
   - 違和感が出るパターン: 過度な煽り、過激な意見、不正確な情報、政治色、暗号資産、宗教
   - 合うパターン: 走り好き、車好きの素直な投稿、新型車情報、レース、整備・メンテ、ドライブ風景

# コメントのルール
- ですます調・親しみやすい兄さん/姉さんトーン
- 整備士目線のひと言を必ず1つ入れる（実体験ベースの観察や、現場あるある）
- 元投稿の中身に寄り添う・けなさない・絡まない
- ハッシュタグは付けない（引用元の流入を妨げないため）
- 絵文字は最大1個
- 全角換算で 480 字以内（厳守）
- 出力は本文のみ、説明や前置きや改行 4 個以上の連発はしない

# 出力フォーマット
以下の JSON だけを返してください。説明文・前置きは不要。

{
  "selected_index": 0|1|2,
  "viral_score": 0-10 の整数,
  "brand_fit_score": 0-10 の整数,
  "reason": "短い理由 (1〜2文の日本語)",
  "comment": "整備士目線の Threads コメント本文 (480 字以内)"
}
"""


USER_PROMPT_TEMPLATE = """以下の 3 投稿は、@kawatms の Threads で引用リポストする候補です。
それぞれの投稿者・本文・投稿日時・検索ヒット keyword を見て、
- 一番バズりそうで、かつうちのブランドに合うものを 1 つ選び (selected_index)
- その投稿への整備士目線のコメントを生成してください

# 注意
- 元投稿の話題に寄り添うコメント
- 整備士目線のちょっとした観察を必ず混ぜる
- 絵文字は最大 1 個まで
- ハッシュタグ NG
- 480 字以内

# 候補
{candidates_block}

# 出力
（前置きなしで JSON のみ）
"""


def build_candidates_block(candidates: Sequence[Any]) -> str:
    """
    Render the candidate list into a compact Japanese block for the prompt.
    ``candidates`` items are CandidatePost (from lib.threads_searcher) but we
    only rely on attributes by name.
    """
    chunks: List[str] = []
    for idx, c in enumerate(candidates):
        chunks.append(_render_candidate(idx, c))
    return "\n\n".join(chunks)


def _render_candidate(idx: int, c: Any) -> str:
    text = (getattr(c, "text", "") or "").strip()
    # Trim very long source text; the comment is what we care about generating.
    if len(text) > 700:
        text = text[:700].rstrip() + "…"

    media_type = getattr(c, "media_type", "?") or "?"
    username = getattr(c, "username", "?") or "?"
    timestamp = getattr(c, "timestamp", "?") or "?"
    permalink = getattr(c, "permalink", "") or ""
    score = getattr(c, "score", None)
    matched_kw = getattr(c, "matched_keyword", "") or ""

    lines = [
        f"## 候補 [{idx}]",
        f"- 投稿者: @{username}",
        f"- 種別: {media_type}",
        f"- 投稿日時 (UTC): {timestamp}",
        f"- 検索ヒット keyword: {matched_kw}",
    ]
    if score is not None:
        lines.append(f"- スコア (鮮度+動画優先): {score}")
    if permalink:
        lines.append(f"- permalink: {permalink}")
    lines.append("- 本文:")
    lines.append(text or "(本文なし)")
    return "\n".join(lines)


def build_user_prompt(candidates: Sequence[Any]) -> str:
    return USER_PROMPT_TEMPLATE.format(
        candidates_block=build_candidates_block(candidates),
    )


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

@dataclass
class QuoteSelection:
    selected_index: int
    viral_score: int
    brand_fit_score: int
    reason: str
    comment: str
    raw: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected_index": self.selected_index,
            "viral_score": self.viral_score,
            "brand_fit_score": self.brand_fit_score,
            "reason": self.reason,
            "comment": self.comment,
        }


class QuoteSelectError(RuntimeError):
    pass


_FENCE_RE = re.compile(r"^```[^\n]*\n|\n?```\s*$")


def _strip_code_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[^\n]*\n", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _extract_json_object(text: str) -> str:
    """
    Pull the first balanced { ... } block from text. Handles models that
    leak a leading paragraph before the JSON.
    """
    s = _strip_code_fence(text)
    start = s.find("{")
    if start == -1:
        raise QuoteSelectError(f"No '{{' found in model output: {s[:200]!r}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    raise QuoteSelectError(f"Unbalanced JSON in model output: {s[:200]!r}")


def parse_selection(
    raw: str,
    num_candidates: int,
    hard_limit: int = COMMENT_HARD_LIMIT,
) -> QuoteSelection:
    """
    Parse the LLM JSON output. Trims comment to ``hard_limit`` and validates
    selected_index is within range.
    """
    payload_str = _extract_json_object(raw)
    try:
        obj = json.loads(payload_str)
    except json.JSONDecodeError as e:
        raise QuoteSelectError(f"JSON parse failed: {e} body={payload_str[:200]!r}") from e

    try:
        idx = int(obj.get("selected_index"))
    except (TypeError, ValueError) as e:
        raise QuoteSelectError(f"Bad selected_index: {obj.get('selected_index')!r}") from e
    if idx < 0 or idx >= num_candidates:
        raise QuoteSelectError(
            f"selected_index out of range: {idx} (have {num_candidates} candidates)"
        )

    comment_raw = str(obj.get("comment") or "").strip()
    if not comment_raw:
        raise QuoteSelectError("Empty comment in model output")
    comment = _trim_comment(comment_raw, hard_limit)

    return QuoteSelection(
        selected_index=idx,
        viral_score=int(obj.get("viral_score") or 0),
        brand_fit_score=int(obj.get("brand_fit_score") or 0),
        reason=str(obj.get("reason") or "").strip(),
        comment=comment,
        raw=obj,
    )


def _trim_comment(text: str, hard_limit: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= hard_limit:
        return text
    truncated = text[: max(0, hard_limit - 1)]
    truncated = re.sub(r"[、。,.\s]+$", "", truncated)
    return truncated + "…"


__all__ = [
    "SYSTEM_PROMPT",
    "USER_PROMPT_TEMPLATE",
    "COMMENT_HARD_LIMIT",
    "QuoteSelection",
    "QuoteSelectError",
    "build_candidates_block",
    "build_user_prompt",
    "parse_selection",
]
