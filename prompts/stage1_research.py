"""
Stage 1: research memo generator.

Input: 5 articles from Tavily + topic context.
Output: 800-1200字 の調査メモ (Japanese), OR a single SKIP line if the
articles are too thin / off-topic / stale to support a quality post.

The memo feeds Stage 2 (caption + image_prompt). It is NOT the caption itself;
it is the editorial brief — synthesized facts, the angle, the takeaway.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .topics import topic_name, topic_tone


STAGE1_SYSTEM_PROMPT = """\
あなたは日本の自動車ジャーナリスト兼整備士です。Instagram に投稿する記事の
「調査メモ」を作成します。読者は『対馬モータースサービス』のフォロワー、
すなわち日本の一般ドライバー（車に関心はあるが専門家ではない）。

【あなたの仕事】
1. 与えられた最大5本の日本語記事を読む
2. テーマに即した『1本の投稿に値する切り口』を見つける
3. 800〜1200字の日本語『調査メモ』を出力する

【調査メモに必ず含めるもの】
- 何が起きたか（事実、できれば日付・数字・固有名詞）
- なぜ重要か（読者にとっての意味）
- 一段深い視点（他媒体の見出しを並べただけにしない）
- 実用的な示唆（読者が今日からできる/気を付けるべきこと）

【厳守事項】
- 記事から離れた創作・推測・憶測は禁止。事実は記事に書いてあることだけ。
- 賠償額・施行日・車種・型式・罰則などの数字や固有名詞は記事から正確に引く。
- 複数記事を統合する。1本だけを要約しない。
- 出力はプレーンテキスト（マークダウンの見出し・箇条書きも可、ただし簡潔に）。
- 文字数 800〜1200字を目安に。1500字を超えないこと。

【SKIP 条件】
以下に該当する場合は、メモを書かず、最初の行に
    SKIP: <理由>
とだけ書いて終了してください。

- 関連性のある記事が3本未満
- すべての記事がテーマからずれている
- 記事が1年以上前のニュースしかない（topic=news, days=365 で取得しているが
  念のため確認）
- 内容が薄く、投稿1本を構成するに足る情報が引き出せない
- センシティブで配慮が必要（死亡事故の個人情報など）と判断した場合

SKIP の判断は控えめにしてください（10本に1本程度の頻度を想定）。
迷うときは書く方を選んでください。ただし事実が足りないのに無理に書くのは禁止。

最終出力はメモ本文だけ。前置き・挨拶・自己説明は不要。
"""


def format_articles_for_llm(
    articles: List[Dict[str, Any]],
    topic_id: str,
    subtopic: str,
) -> str:
    """
    Render the article list + topic context as the Stage-1 user message.
    """
    lines: List[str] = []
    lines.append(f"テーマ: {topic_name(topic_id)}")
    lines.append(f"サブトピック: {subtopic}")
    lines.append("")
    lines.append("【このテーマの編集方針】")
    lines.append(topic_tone(topic_id))
    lines.append("")
    lines.append(f"【取得した記事 ({len(articles)}本)】")
    lines.append("")

    if not articles:
        lines.append("(該当記事なし)")
    else:
        for i, art in enumerate(articles, 1):
            title = (art.get("title") or "").strip()
            url = (art.get("url") or "").strip()
            published = art.get("published_date") or art.get("published") or ""
            content = (art.get("content") or "").strip()
            # Truncate per-article content to keep token usage sane.
            if len(content) > 1800:
                content = content[:1800] + "…"
            lines.append(f"--- 記事 {i} ---")
            lines.append(f"タイトル: {title}")
            lines.append(f"URL: {url}")
            if published:
                lines.append(f"公開日: {published}")
            lines.append("本文抜粋:")
            lines.append(content)
            lines.append("")

    lines.append("---")
    lines.append("上記を踏まえ、システム指示に従って調査メモ（または SKIP）を出力してください。")
    return "\n".join(lines)


def is_skip(memo: str) -> bool:
    """
    Detect a SKIP response. We accept either:
      - "SKIP: ..." on the first non-empty line
      - "SKIP\n..." at the very start
    """
    if not memo:
        return True
    for line in memo.splitlines():
        s = line.strip()
        if not s:
            continue
        return s.upper().startswith("SKIP")
    return False
