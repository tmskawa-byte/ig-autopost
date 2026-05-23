"""
Stage 1: research memo generator.

Input: up to 10 articles from Tavily + topic context.
Output: 800-1200字 の調査メモ (Japanese) with cited URLs at the end,
OR a single SKIP line if articles are too thin to support a quality post.

The memo feeds Stage 2 (caption + image_prompt). It is NOT the caption itself;
it is the editorial brief — synthesized facts, the angle, the takeaway,
plus the URLs that Stage 2 should cite at the end of the caption.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .topics import topic_name, topic_tone

STAGE1_SYSTEM_PROMPT = """\
あなたは日本の自動車ジャーナリスト兼整備士です。Instagram に投稿する記事の
「調査メモ」を作成します。読者は『対馬モータースサービス』のフォロワー、
すなわち **日本の一般ドライバー、特に対馬・長崎・離島・地方在住者**。

【あなたの仕事】
1. 与えられた日本語記事を読む（最大10本）
2. **日本市場で関係のあるテーマ**を中心に切り口を見つける
3. 800〜1200字の日本語『調査メモ』を出力する
4. メモ末尾に **出典 URL 1〜2 件** を必ず付ける

【日本市場フォーカス（重要）】
- 日本で買える・関わる話題を **主題に据える**。
- 海外モデル（北米・欧州限定など）は『ちなみに海外では〜』程度の
  添え物扱いにする。
- 海外モデルに言及する場合は **必ず市場ステータスを明記**:
  - 「※日本未発売、海外モデル」
  - 「※北米向け、日本での販売予定なし」
  - 「※20XX年春日本発売予定」
- 日本未発売の海外モデルを主題にして「次の愛車候補に！」みたいな
  書き方は **致命的に NG**（読者は買えない）。

【調査メモに必ず含めるもの】
- 何が起きたか（事実、できれば日付・数字・固有名詞）
- 日本市場での扱い（販売中 / 未発売 / 発売予定 / 終了モデル）
- なぜ重要か（日本の読者にとっての意味）
- 一段深い視点（他媒体の見出しを並べただけにしない）
- 実用的な示唆（日本のドライバーが今日からできる/気を付けるべきこと）

【厳守事項】
- 記事から離れた創作・推測・憶測は禁止。事実は記事に書いてあることだけ。
- 賠償額・施行日・車種・型式・罰則などの数字や固有名詞は記事から正確に引く。
- 複数記事を統合する。1本だけを要約しない。
- 出力はプレーンテキスト（マークダウンの見出し・箇条書きも可、ただし簡潔に）。
- 文字数 800〜1200字を目安に。1500字を超えないこと。

【記事の使い方】
- テーマと完全一致しなくても、関連する周辺情報として活用してよい
- リコール・道路交通法・判例など『情報が長く有効』なジャンルでは、
  数年前の記事も「今でも有効な知識」として活用してよい
- 取得した記事のうち、テーマに直接関連するものが1本でもあれば
  メモを書く方針で進める

【🚨 出典 URL セクション（必須）】
調査メモの最後に必ず次の形式で出典 URL を付ける:

出典:
- https://example.com/article1
- https://example.com/article2

ルール:
- 提供された記事の URL から **実際に存在するものだけ** を貼る（捏造禁止）
- 1〜2 件で十分（多すぎは不要）
- 最もメモの中核になった記事を優先

【SKIP 条件（限定的）】
以下に該当する場合のみ、メモを書かず、最初の行に
SKIP: <理由>
とだけ書いて終了してください。

- 取得記事 0 本
- すべての記事がテーマから完全に無関係
- 内容が極端に薄い（合計しても3行も書けない）
- センシティブで配慮が必要（死亡事故の個人情報など）

【SKIP 判断ポリシー】
SKIP は最終手段。20本に1本程度の頻度を想定。
- 「関連記事が3本未満」だけでは SKIP しない。1本でも関連があれば書く
- 「1年以上前」だけでは SKIP しない。リコール・法改正・判例は
  数年前の情報でも有効

最終出力はメモ本文 + 出典URLセクションだけ。前置き・挨拶・自己説明は不要。
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
    lines.append("最後に必ず『出典:』セクションで使った URL を貼ること。")
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
