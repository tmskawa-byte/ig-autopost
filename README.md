# ig-autopost

Instagram 自動投稿システム（@kawatms 用）。
GitHub Actions 上で毎朝 9:00 JST に走り、車関連の最新ニュースを Tavily で取得し、
Gemini 3.1 Pro Preview で記事生成、Nano Banana Pro で画像生成、Instagram Graph API
で投稿します。Make.com の IG シナリオの後継。

## アーキテクチャ

```
[cron 9:00 JST]
       │
       ▼
auto_post.py
       │
       ├── pick_topic() / pick_subtopic()                    (prompts/topics.py)
       │
       ├── Tavily Search (topic=news, days=365, JP domains)  (lib/tavily_client.py)
       │
       ├── Stage 1: 調査メモ生成（Gemini 3.1 Pro Preview）   (prompts/stage1_research.py)
       │       └─ SKIP の場合は静かに終了
       │
       ├── Stage 2: caption + image_prompt JSON              (prompts/stage2_caption.py)
       │
       ├── 画像生成 (Nano Banana Pro, 1:1, 2K)               (lib/chatllm_client.py)
       │
       ├── 画像 download → ImgBB upload (public URL)         (lib/imgbb_uploader.py)
       │
       └── Instagram Graph API 投稿                          (lib/ig_publisher.py)
              ├─ POST /{biz_id}/media        → creation_id
              ├─ poll status_code            → FINISHED
              └─ POST /{biz_id}/media_publish
```

## ファイル構成

```
ig-autopost/
├── .github/workflows/post.yml      # cron + workflow_dispatch
├── auto_post.py                    # メインエントリ
├── lib/
│   ├── chatllm_client.py           # RouteLLM ラッパー（テキスト+画像）
│   ├── tavily_client.py            # Tavily Search
│   ├── image_gen.py                # data URL / http URL → bytes
│   ├── imgbb_uploader.py           # ImgBB アップロード
│   └── ig_publisher.py             # IG Graph API
├── prompts/
│   ├── topics.py                   # 9 テーマ + サブトピック + JP メディア domain
│   ├── stage1_research.py          # 調査メモプロンプト
│   └── stage2_caption.py           # caption+image_prompt プロンプト
├── requirements.txt                # requests のみ
├── README.md                       # この file
└── SETUP_SECRETS.md                # Secrets 設定手順
```

## セットアップ

### 1. リポジトリ作成 & push

```bash
# 解凍したフォルダに移動
cd ig-autopost

# Git 初期化
git init
git add .
git commit -m "Initial commit: ig-autopost"
git branch -M main

# GitHub にリポジトリ作成（gh CLI を使う場合）
gh repo create tmskawa-byte/ig-autopost --private --source=. --remote=origin --push

# gh CLI なしで Web から作る場合: GitHub で空のプライベートリポを作ったあと
git remote add origin https://github.com/tmskawa-byte/ig-autopost.git
git push -u origin main
```

### 2. GitHub Secrets 設定

詳細は [SETUP_SECRETS.md](./SETUP_SECRETS.md) を参照。
5 つの secret を `Settings → Secrets and variables → Actions → New repository secret` から登録:

- `TAVILY_API_KEY`
- `CHATLLM_API_KEY`
- `IMGBB_API_KEY`
- `IG_ACCESS_TOKEN`
- `IG_BUSINESS_ID`

### 3. 手動で初回テスト（dry-run）

GitHub の `Actions → IG Auto Post → Run workflow` から:
- `dry_run`: **true** を選択
- `topic`: 空のまま

これで IG 投稿せずに、Tavily 取得 → 記事生成 → 画像生成 → ImgBB アップロードまで通します。
Actions のログを確認:
- Tavily で記事が取れているか
- Stage 1 が SKIP していないか
- caption が生成されているか（ログ末尾に表示）
- ImgBB の public URL が出ているか（URL をブラウザで開いて画像確認）

### 4. 本番投稿テスト

問題なければ `dry_run`: **false** で再度手動 Run。
IG @kawatms に投稿されることを確認。

### 5. cron 有効化

`.github/workflows/post.yml` は最初から cron がオンです。
push したら 翌 9:00 JST 以降、自動で走り始めます。

> **NOTE**: GitHub の cron は混雑時に数分〜15分ほど遅延することがあります。
> 厳密な 9:00:00 ではなく「9:00 前後」と理解してください。

## 動作確認・運用

### ログ閲覧
GitHub の `Actions` タブ → 各 Run の詳細でステップごとのログが見られます。

### 失敗時の Exit Code
`auto_post.py` の終了コード:
- `0`: 成功（SKIP・記事ゼロも含む正常終了）
- `2`: 引数エラー
- `3`: Tavily エラー
- `4`: Stage 1 失敗
- `5`: Stage 2 失敗
- `6`: Stage 2 JSON parse 失敗
- `7`: 画像生成失敗
- `8`: 画像 download 失敗
- `9`: ImgBB upload 失敗
- `10`: IG publish 失敗

### Make.com 側

> Make.com の IG シナリオは **Inactive にして残してください**（即削除しない）。
> 数日 GitHub Actions の安定稼働を見届けてから削除判断。

### 連投回避
サブトピックを 3-5 個用意して毎日ランダム化しているので、同じテーマでも
切り口がずれるはず。気になる場合は `prompts/topics.py` の subtopics リストを拡張。

## カスタマイズ

### 投稿時刻を変える
`.github/workflows/post.yml` の cron を編集:
```yaml
- cron: '0 0 * * *'  # 9:00 JST
- cron: '30 22 * * *'  # 7:30 JST
```
[crontab.guru](https://crontab.guru/) で表記確認。UTC 基準。

### テーマ追加
`prompts/topics.py` の `TOPICS` 辞書に新エントリを追加するだけ。
`pick_topic()` が自動で対象に含めます。

### モデル変更
`auto_post.py` 冒頭の `TEXT_MODEL` / `IMAGE_MODEL` 定数を書き換え。
thinking モードでないモデルにする場合、`ChatLLMClient.THINKING_MODELS` から除外
すれば `temperature` を渡せます。

## トラブルシューティング

| 症状 | 原因候補 | 対処 |
| --- | --- | --- |
| Tavily で 0 件 | クエリ語が広すぎ/狭すぎ | `prompts/topics.py` の subtopic か `extra_query` を調整 |
| Stage 1 が毎回 SKIP | テーマ vs include_domains の相性悪 | JP_MEDIA_DOMAINS を見直し or テーマ別 domain にする |
| Stage 2 JSON parse 失敗 | LLM が文章で返している | プロンプトの JSON 強制部を強める / 再 Run |
| IG `Invalid image_url` | ImgBB の URL がブロックされている | ImgBB の公開設定確認、または別 CDN（Imgur）への切替を検討 |
| IG `Media container expired` | container 作成後 24h 放置 | 通常起こらないが、`max_wait` 増やす |
| IG `(#10) Application does not have permission` | IG Access Token が古い/権限不足 | Token 再発行（`instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement` 必要） |
| `temperature` is not supported | thinking モデルに temperature 渡してる | `chatllm.chat()` で `temperature=` を指定していないか確認 |

### IG Access Token の更新
Long-lived Token は **60日** で失効します。Meta for Developers の
[Access Token Tool](https://developers.facebook.com/tools/explorer/) で
更新 → GitHub Secret を上書き。
更新忘れ防止のためカレンダーに通知を設定推奨。

## ローカル実行（任意）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export TAVILY_API_KEY="..."
export CHATLLM_API_KEY="..."
export IMGBB_API_KEY="..."
export IG_ACCESS_TOKEN="..."
export IG_BUSINESS_ID="..."

# Dry run（IG 投稿しない）
python auto_post.py --dry-run

# 特定テーマ強制
python auto_post.py --dry-run --topic topic_3 --seed 42
```

## 依存

- Python 3.12
- requests >= 2.31

すべて GitHub Actions の `ubuntu-latest` ランナー + 無料枠で動きます。
追加課金はかかりません。

## 既存 X bot との関係

`tmskawa-byte/x-autopost`（X 自動投稿）とは独立。共有コード・共有 Secrets なし。
互いに影響しません。
