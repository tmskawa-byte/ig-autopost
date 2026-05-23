"""
9 topics + subtopics + Tavily query builder + JP media domain whitelist.

Each topic has:
  - name (Japanese label)
  - subtopics (3-5 rotation candidates, to avoid repetition across days)
  - tone (LLM instruction snippet on angle / voice for this topic)
  - extra_query (optional extra keywords appended to the Tavily query)
"""
from __future__ import annotations

import random
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# Japanese media + government / industry sources for Tavily include_domains.
# Curated for accuracy, recency, and automotive relevance.
# ---------------------------------------------------------------------------
JP_MEDIA_DOMAINS: List[str] = [
    # automotive specialty
    "response.jp",
    "car.watch.impress.co.jp",
    "carview.yahoo.co.jp",
    "autocar.jp",
    "motor-fan.jp",
    "bestcarweb.jp",
    "car-me.jp",
    "web.motor-magazine.co.jp",
    "as-web.jp",
    "automesseweb.jp",
    "kuruma-news.jp",
    "minkara.carview.co.jp",
    "forride.jp",
    # general news (national papers + broadcasters)
    "nhk.or.jp",
    "asahi.com",
    "mainichi.jp",
    "yomiuri.co.jp",
    "nikkei.com",
    "jiji.com",
    "sankei.com",
    "kyodonews.jp",
    # government / industry bodies
    "mlit.go.jp",        # 国土交通省 — リコール、標識など公式情報
    "npa.go.jp",         # 警察庁 — 道交法
    "jaf.or.jp",         # JAF
    "sonpo.or.jp",       # 日本損害保険協会
    "nasva.go.jp",       # 自動車事故対策機構
]


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------
TOPICS: Dict[str, Dict] = {
    "topic_1": {
        "name": "新技術（EV・自動運転・ADAS）",
        "subtopics": [
            "EV 充電インフラ",
            "自動運転レベル4",
            "ADAS 衝突被害軽減ブレーキ",
            "全固体電池",
            "V2H 双方向充電",
        ],
        "extra_query": "",
        "tone": (
            "新技術トピック。読者は『最新動向は気になるけど専門用語は苦手』なドライバー層。"
            "技術の概要だけでなく『自分の生活/車選びにどう影響するか』を必ず最後に書くこと。"
            "メーカー名・モデル名・年式・標準装備かオプションかなど、検証可能な事実を優先。"
        ),
    },
    "topic_2": {
        "name": "道路交通法（改正・罰則・新ルール）",
        "subtopics": [
            "道路交通法改正",
            "電動キックボード 新ルール",
            "あおり運転 罰則",
            "ながら運転 罰則",
            "高齢者運転免許 更新",
        ],
        "extra_query": "罰則 違反点数",
        "tone": (
            "道交法トピック。読者は『うっかり違反したくない』一般ドライバー。"
            "施行日・罰則（反則金/点数/懲役）・対象行為を具体的かつ正確に。"
            "推測や曖昧な書き方は厳禁。出典は警察庁・国交省・大手紙を優先。"
        ),
    },
    "topic_3": {
        "name": "交通事故判例（高額賠償）",
        "subtopics": [
            "交通事故 高額賠償 判例",
            "歩行者 飛び出し 過失割合",
            "自転車 事故 賠償",
            "通勤災害 自動車事故 判例",
            "後遺障害 慰謝料 判例",
        ],
        "extra_query": "判決",
        "tone": (
            "判例トピック。読者は『自分は加害者にも被害者にもなりうる』ドライバー。"
            "事故態様 → 争点 → 判決（賠償額・過失割合）→ 教訓 の順で整理。"
            "個人特定は避け、判決日・裁判所・賠償額など事実ベース。煽らず淡々と。"
        ),
    },
    "topic_4": {
        "name": "車関係ガジェット",
        "subtopics": [
            "ドライブレコーダー 360度",
            "カーナビ 最新モデル",
            "Apple CarPlay 対応",
            "車載 USB-C 急速充電",
            "OBD2 診断機",
        ],
        "extra_query": "比較 おすすめ",
        "tone": (
            "ガジェットトピック。読者は『使えるものなら買い替えたい』実用派。"
            "製品ジャンルの最新トレンド + 選び方の軸（解像度/対応規格/価格帯）。"
            "特定製品の宣伝にならないよう、複数モデルを比較する視点で書く。"
        ),
    },
    "topic_5": {
        "name": "新型車（トヨタ/ホンダ/日産発表）",
        "subtopics": [
            "トヨタ 新型車 発表",
            "ホンダ 新型 SUV",
            "日産 新型 EV",
            "ハイブリッド 新型",
            "ミニバン 新型",
        ],
        "extra_query": "発売",
        "tone": (
            "新型車トピック。読者は『次の車そろそろ…』と考えているユーザー。"
            "発表日・発売予定・パワートレイン・価格・先代との違いを中心に。"
            "ライバル車との位置づけにも触れると深みが出る。"
        ),
    },
    "topic_6": {
        "name": "任意保険（補償・見直し）",
        "subtopics": [
            "自動車保険 見直し ポイント",
            "弁護士特約 必要性",
            "対人対物 補償限度額",
            "車両保険 エコノミー 一般",
            "ロードサービス 任意保険",
        ],
        "extra_query": "",
        "tone": (
            "任意保険トピック。読者は『何となく入ってるけど中身は曖昧』な層。"
            "ありがちな勘違い → 正しい知識 → 見直しのチェックポイント の流れで。"
            "特定保険会社のおすすめは避け、契約者目線で中立に。"
        ),
    },
    "topic_7": {
        "name": "リコール",
        "subtopics": [
            "リコール 国交省",
            "エンジン リコール",
            "エアバッグ リコール",
            "ブレーキ リコール",
            "ハイブリッド リコール",
        ],
        "extra_query": "",
        "tone": (
            "リコールトピック。読者は『自分の車は大丈夫？』が気になるオーナー。"
            "対象車種・型式・期間・不具合内容・対処法（販売店連絡）を正確に。"
            "煽らず冷静に、ただし重要性は伝える。公式情報を最優先。"
        ),
    },
    "topic_8": {
        "name": "警告灯・メーター",
        "subtopics": [
            "エンジン警告灯 点灯 原因",
            "バッテリー警告灯 対処",
            "ABS警告灯 走行可能",
            "オイル警告灯 緊急",
            "タイヤ空気圧 警告",
        ],
        "extra_query": "対処法",
        "tone": (
            "警告灯トピック。読者は『今まさに点いた』焦っているオーナーかもしれない。"
            "見出しで結論（走行可 or 即停止）。次にその警告灯の意味、原因の典型、"
            "セルフチェックと整備工場に行くタイミングを実用的に。"
        ),
    },
    "topic_9": {
        "name": "道路標識・路面表示",
        "subtopics": [
            "新しい道路標識",
            "ゾーン30 路面表示",
            "ラウンドアバウト 走り方",
            "自転車レーン 標示",
            "止まれ 路面標示",
        ],
        "extra_query": "ルール",
        "tone": (
            "標識・路面表示トピック。読者は『見たことあるけど意味は曖昧』なドライバー。"
            "標識/標示の名称 → 意味 → 守らないとどうなるか（罰則含む）を明確に。"
            "新設・改正されたものを優先。画像で伝えやすいので視覚的描写も具体的に。"
        ),
    },
}


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------
def pick_topic(rng: random.Random | None = None) -> Tuple[str, Dict]:
    """Pick one topic uniformly at random. Returns (topic_id, topic_dict)."""
    rng = rng or random
    topic_id = rng.choice(list(TOPICS.keys()))
    return topic_id, TOPICS[topic_id]


def pick_subtopic(topic_id: str, rng: random.Random | None = None) -> str:
    rng = rng or random
    return rng.choice(TOPICS[topic_id]["subtopics"])


def build_query(topic_id: str, subtopic: str) -> str:
    """
    Build the Tavily query. We rely on topic=news + days=365 to filter for
    recent news, so the query itself stays keyword-focused (Japanese).
    """
    topic = TOPICS[topic_id]
    extra = topic.get("extra_query", "") or ""
    parts = [subtopic.strip(), extra.strip(), "最新"]
    return " ".join(p for p in parts if p)


def topic_tone(topic_id: str) -> str:
    return TOPICS[topic_id]["tone"]


def topic_name(topic_id: str) -> str:
    return TOPICS[topic_id]["name"]
