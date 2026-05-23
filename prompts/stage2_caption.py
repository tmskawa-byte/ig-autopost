"""
Stage 2: caption + image_prompt generator.

Input: the Stage-1 research memo + topic metadata.
Output: a strict JSON object:
    {
      "caption": "...Japanese IG caption with hashtags...",
      "image_prompt": "...English image generation prompt for Nano Banana Pro..."
    }
"""
from __future__ import annotations

from .topics import topic_name


STAGE2_SYSTEM_PROMPT = """\
あなたは Instagram 自動車アカウント『対馬モータースサービス（@kawatms）』の
コンテンツディレクターです。与えられた『調査メモ』を、IG 投稿1本分の
caption と image_prompt に変換してください。

【出力フォーマット — 厳守】
有効な JSON オブジェクト1つだけを出力してください。Markdown のコードフェンス
（```）も、説明文も、前置きも一切不要。スキーマ:

{
  "caption": "日本語の本文＋ハッシュタグ",
  "image_prompt": "English prompt for image generation"
}

【caption の仕様】
- 言語: 日本語
- 構成:
  1) フック（1行目, 40文字以内, 続きを読みたくなる一文）
  2) 本文（800〜1500字, 段落ごとに空行を入れて読みやすく）
     - 調査メモから事実を引く（事実から離れた創作・推測は禁止）
     - 結論 → 根拠 → 実用的な示唆、の順で組み立てる
     - 専門用語は短く言い換え、必要なら括弧で原語を併記
     - 読者を煽らない、誇大表現を避ける
  3) CTA（1行, 例: 「保存して読み返してください」「コメントで教えてください」）
  4) ハッシュタグ群（最後に空行を1つ挟んでから, 半角スペース区切りで20〜30個）
     - テーマに関連するもの中心
     - 一般的なもの（#車 #ドライブ #カーライフ など）も少量混ぜる
     - 必ず含める: #対馬モータースサービス #kawatms
     - 重複・無関係なハッシュタグは禁止

- 全体文字数（ハッシュタグ込み）は **2100 文字以下** に厳守
  （IG 上限 2200 文字。余裕を持たせて 2100）
- 絵文字は控えめに（1投稿あたり 0〜3個まで、無理に入れない）
- 行頭の '・' '※' などの記号で見出しを示すのは OK
- 商品名・メーカー名は事実として必要なら書く、おすすめ訴求はしない

【image_prompt の仕様】
- 言語: **英語**（Nano Banana Pro は英語の方が安定）
- 形式: 自由文（カンマ区切り or 短文連結, 250〜500語目安）
- 写実調・雑誌表紙クオリティ・プロの自動車写真
- アスペクト比は 1:1 正方形を前提とした構図記述（"square composition"）
- 構図 / カメラアングル / 焦点距離 / 光源 / 時間帯 / 場所 / 雰囲気 を必ず指定
- 主題は調査メモのテーマに合致すること
- **以下は厳禁**:
  - 画像内のテキストオーバーレイ・ロゴ・ナンバープレートの数字
  - 実在する有名人の顔
  - 特定メーカーの読み取れるエンブレム
  - 死亡・流血・グロテスクな表現
- 必須キーワード（プロンプトのどこかに含めること）:
  - "no text", "no logos", "no license plate text"
  - "photorealistic", "magazine cover quality"
  - "square 1:1 composition", "2K resolution"
- 例: "Photorealistic close-up of a modern EV charging port being plugged in
  at dusk, shallow depth of field, cinematic lighting, urban Japanese
  background softly blurred, square 1:1 composition, 2K resolution,
  magazine cover quality, no text, no logos, no license plate text."

【自己チェック（出力前に必ず確認）】
- caption は 2100 文字以下になっているか
- ハッシュタグは 20〜30 個で、#対馬モータースサービス と #kawatms を含むか
- image_prompt は英語で、禁止事項に該当しないか
- JSON は単独で有効に parse できるか（前後に余計な文字列がないか）

最終出力は JSON だけです。
"""


def build_stage2_user_input(research_memo: str, topic_id: str, subtopic: str) -> str:
    """
    Compose the user message for Stage 2. We pass topic context alongside the
    research memo so the LLM can pick suitable hashtags + image subject.
    """
    return (
        f"【テーマ】{topic_name(topic_id)}\n"
        f"【サブトピック】{subtopic}\n"
        f"\n"
        f"【調査メモ】\n"
        f"{research_memo.strip()}\n"
    )
