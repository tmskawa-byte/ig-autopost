# Secrets 設定手順

GitHub Actions が動くために必要な 5 つの secret を、リポジトリの
`Settings → Secrets and variables → Actions → New repository secret`
から登録します。

> **絶対にチャットや Issue / PR の本文に secret を貼らないこと。**
> GitHub の Secrets UI に直接入力するだけです。値はマスクされます。

## チェックリスト

| Secret 名 | 用途 | 取得方法 | ケンちゃんの保有状況 |
| --- | --- | --- | --- |
| `TAVILY_API_KEY` | ニュース検索 | [tavily.com](https://tavily.com) の Dashboard | 取得済み（メモ参照） |
| `CHATLLM_API_KEY` | Gemini / Nano Banana Pro 呼出 | Abacus.AI RouteLLM Dashboard | 取得済み（メモ参照） |
| `IMGBB_API_KEY` | 画像ホスティング | [imgbb.com/api](https://api.imgbb.com/) で無料登録 | **要取得** |
| `IG_ACCESS_TOKEN` | Instagram Graph API 認証 | Make.com で使っているものを流用 | 取得済み |
| `IG_BUSINESS_ID` | 投稿先 IG ビジネス ID | 対馬モータースサービスの IG ビジネス ID | 既知 |

---

## 1. TAVILY_API_KEY

すでに取得済み。

1. https://app.tavily.com にログイン
2. `API Keys` から既存キーをコピー、または新規発行
3. GitHub Secrets に `TAVILY_API_KEY` として登録

> 無料プランの月間クォータ (1,000 リクエスト/月) で十分。
> 毎日 1 回投稿 × 30 日 = 30 リクエスト/月 程度。

---

## 2. CHATLLM_API_KEY (Abacus.AI RouteLLM)

すでに取得済み。

1. Abacus.AI ダッシュボードにログイン
2. RouteLLM / ChatLLM の API Key を確認 or 新規発行
3. GitHub Secrets に `CHATLLM_API_KEY` として登録

> Gemini 3.1 Pro Preview と Nano Banana Pro が同じキーで叩けることを確認。
> 別契約だと別キーになる可能性あり。

---

## 3. IMGBB_API_KEY（要取得）

無料登録のみ、追加課金なし。

1. https://imgbb.com にアクセスし、メールアドレスでアカウント作成
2. ログイン後、https://api.imgbb.com/ にアクセス
3. `Get API Key` → 黒い API キー文字列をコピー
4. GitHub Secrets に `IMGBB_API_KEY` として登録

> 無料プランで 32 MB/画像 まで OK。本システムが生成する画像は 1-3 MB 程度なので余裕。
> 画像は ImgBB の CDN に置かれ、IG が直接 fetch する。

---

## 4. IG_ACCESS_TOKEN

Make.com の IG シナリオで使っている **Long-lived Page/User Access Token** を流用します。

### Make.com から取り出す方法

1. Make.com の IG シナリオを開く
2. Instagram モジュールの設定 → Connection の詳細表示
3. Access Token フィールドに表示される長い文字列をコピー
   （見えない場合は新規発行: 下記「再発行」参照）
4. GitHub Secrets に `IG_ACCESS_TOKEN` として登録

### 再発行が必要な場合

1. https://developers.facebook.com/tools/explorer/ にアクセス
2. 対馬モータースサービスの IG ビジネスにリンクされた Facebook Page を選択
3. 以下の権限を付与:
   - `instagram_basic`
   - `instagram_content_publish`
   - `pages_show_list`
   - `pages_read_engagement`
   - `business_management`
4. 短期トークンが発行される。これを **Long-lived Token** に変換:
   - https://developers.facebook.com/tools/debug/accesstoken/ で「Extend Access Token」
   - 60 日有効になる
5. GitHub Secrets に登録

> Long-lived Token は **60日で失効**。カレンダーに更新リマインダー必須。

---

## 5. IG_BUSINESS_ID

対馬モータースサービスの IG ビジネスアカウントの数字 ID（17桁前後）。

### 確認方法

知らない場合の取得方法:

```bash
curl "https://graph.facebook.com/v21.0/me/accounts?access_token=YOUR_TOKEN"
```
レスポンスから Facebook Page ID を取得し、

```bash
curl "https://graph.facebook.com/v21.0/PAGE_ID?fields=instagram_business_account&access_token=YOUR_TOKEN"
```
の `instagram_business_account.id` がそれ。

ケンちゃんは既に知ってるはず（Make.com に設定済み）。
GitHub Secrets に `IG_BUSINESS_ID` として登録。

---

## 登録後の確認

すべて登録すると、GitHub の Settings 画面で:

```
Repository secrets
  CHATLLM_API_KEY      Updated XX seconds ago
  IG_ACCESS_TOKEN      Updated XX seconds ago
  IG_BUSINESS_ID       Updated XX seconds ago
  IMGBB_API_KEY        Updated XX seconds ago
  TAVILY_API_KEY       Updated XX seconds ago
```
の 5 行が見えれば OK。

## 次のステップ

→ README.md の「3. 手動で初回テスト（dry-run）」へ。
