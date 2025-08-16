# MemoryCore 仕様書（Python 実装向け）

## 0) 目的

- 会話ログを**生で完全保存**しつつ、要約・埋め込み・感情ベクトル・トピック（＝経験/事実）で構造化。
- 2種の想起を提供：
　- **自動想起（自然な会話中の想起）**
　- **探索想起（意識的な検索・内省）**
- 低負荷な**軽量LLM**で「トピック割当」の日常運転、**長文耐性LLM**で「再編成（分割/統合）」の定期整備。

---

## 1) 依存関係（候補と根拠）

- **埋め込み**: `sentence-transformers`（SBERT系）
　- 汎用・再学習容易。例: `nomic-ai/nomic-embed-text-v1.5`（オープン、長文強・用途広い）。
　
- **ベクトル保存/検索**
　- **Qdrant**：ペイロード/フィルタ/ハイブリッド検索が強い（RAM節約・量子化・オンディスク）。
　
- **軽量LLM**: ローカル推論（`Ollama`）
　- 例: Smallモデル（Qwen3 30B a3Bを想定）でトピック割当。
- **長文再編成LLM**: **Gemini 2.0 Flash**（最大約100万tokens）
　- 大量記憶の**分割/統合/木構造再編**に最適。
- **スキーマ**: `pydantic` / `dataclasses`
- **永続化**: Qdrant、原文は**オブジェクトストレージ**（S3互換）にも二重保存推奨。

---

## 2) データモデル（Pydantic 擬似）

```
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import datetime

# 感情ベクトル（次元は固定：-1.0〜+1.0 正規化）
EMO_KEYS = ["joy","peace","trust","curiosity","flutter",
            "playfulness","empathy","hope",
            "conflict","anxiety","sadness","anger"]  # 12次元例

class EmotionVector(BaseModel):
    values: Dict[str, float]  # key in EMO_KEYS, -1.0..+1.0
    confidence: float = 0.0   # 0..1

class MemoryEntry(BaseModel):
    id: str
    conversation_id: str
    turn_index: int
    timestamp: datetime
    speaker: str           # "user" | "ai"
    raw_text: str          # 完全原文
    summary: Optional[str] = None
    embedding: Optional[List[float]] = None
    emotion: Optional[EmotionVector] = None
    linked_topics: List[str] = []       # Topic.id
    linked_entries: List[str] = []      # 近接や引用等
    meta: Dict[str, str] = {}           # 任意（client, channel等）
    raw_pointer: Optional[str] = None   # S3/FSの原文ポインタ（冗長保存）

class Topic(BaseModel):
    id: str
    title: str                # 例:「那須塩原へ一緒に旅行した」
    summary: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    strength: float = 0.0     # 0..1 (重要度)
    centroid_embedding: Optional[List[float]] = None
    centroid_emotion: Optional[EmotionVector] = None
    entry_ids: List[str] = [] # メンバー
    parents: List[str] = []   # 上位トピック
    children: List[str] = []  # サブトピック
```

> **原文保存**は絶対条件：`raw_text` を保存し、別途 `raw_pointer` でオブジェクトストレージへも永続化（冗長）。

---

## 3) ストレージ層

### 3.1 Qdrant（推奨）

- **Collections**:
　- `entries`（Mainベクトルコレクション）
　- vector: `embedding`（cosine）
　- payload: `raw_text`, `conversation_id`, `turn_index`, `timestamp`, `speaker`, `emotion.values`, `topic_ids` 等（型安全）
　- **payload index** を定義（speaker/timestamp/topic\_ids などでフィルタ検索を高速化）。
　- `topics`（Topicのメタ＋センチロイド用ベクトル）
- 量子化/オンディスク最適化は大規模時に適用。

---

## 4) 埋め込み・感情

### 4.1 埋め込み

- 既定: `sentence-transformers` + `nomic-embed-text-v1.5`
- 文脈に応じて**query/doc プレフィックス**を活用（検索精度↑）。
　

### 4.2 感情ベクトル推定

- 小型分類器（finetune可） or LLMによるゼロショット→正規化。
- 正規化レンジ：`[-1, +1]`、信頼度 `confidence` 併記。
- `Topic.centroid_emotion` はメンバー平均＋再想起頻度に応じた EMA。

---

## 5) トピック運用（割当／再編成）

### 5.1 割当（オンライン、軽量LLM）

**目的**: 新発話を既存トピックに紐付ける or 新規生成　
**入力**: 直近3〜5往復の会話、既存トピックの `[id, title, summary]`　
**出力**: `topic_id` or `NEW(title, summary)`　
**実行**: API経由でローカルLLM（Ollama）を呼ぶ。

**プロンプト雛形（Pseudo）**

```
SYSTEM: You are a topic assigner for a memory system.
USER:
Recent dialog (last N turns):
- U: ...
- A: ...
Existing topics:
- [id=topic_07] "那須塩原へ一緒に旅行した" — summary: xxx
- [id=topic_12] "ナナシとの出会い" — summary: yyy
Task:
1) Does the recent dialog belong to one of the existing topics? Return BEST_MATCH=topic_id with REASON.
2) If none fits, propose NEW with {title,summary} (Japanese).
Output JSON only: {"decision": "BEST_MATCH"|"NEW", "topic_id": "...", "new_topic": {...}, "reason": "..."}
```

### 5.2 再編成（オフライン/バッチ、Gemini 1.5 Pro）

**タイミング**: 毎晩 or 週次　
**処理**:

- 発話が多すぎるトピック → **クラスタリングしてサブトピック化**
- 孤立トピック → 近縁へ**統合提案**
- 時系列近接＋類似内容 → **親トピック生成**（樹形図化）
- センチロイド・強度・感情の**再計算**

**LLM**: Gemini 2.0 Flash（100万tokenで大規模文脈を俯瞰整形） 。

---

## 6) 想起エンジン（RetrievalEngine）

### 6.1 スコアリング

`score(entry) = w_sim*sim_text + w_time*decay(Δt) + w_topic*strength(topic) + w_em*sim_emotion + w_recency*R`

- `sim_text`: クエリ埋め込みと `entry.embedding` のコサイン
- `decay(Δt) = exp(-Δt/τ)`（時間減衰）
- `strength(topic)`: 紐付トピックの最大/平均強度
- `sim_emotion`: 現在の感情 vs `entry.emotion` のコサイン
- `R`: 直近活性化（再想起）ボーナス
　

**重み既定**（要チューニング）:　
`w_sim=0.45, w_time=0.1, w_topic=0.2, w_em=0.2, w_recency=0.05`

### 6.2 自動想起（会話中）

1. 現在発話を埋め込み → `entries` 類似検索（Top-K）
　
2. Top-K の `topic_id` 集計 → 上位トピックから**周辺エントリを拡張取得**
　
3. 6.1 のスコアで再ランキング → 上位N件を「想起バンドル」に
　
4. **要約＋引用**をCEN側に渡す（長さ制御）
　

### 6.3 探索想起（意識的検索）

- **クエリ種別**：キーワード/自然文/期間/感情類似/トピック名
- 返却：
　- **トピックビュー**（中央=Topic、周囲=主要エントリ）
　- **時間軸ビュー**（期間内の連続エントリ）
　- **引用ビュー**（原文スニペット＋ジャンプポインタ）

---

## 7) API 設計（モジュール構成）

```
memory_core/
  ├─ schemas.py        # Pydanticモデル
  ├─ storage.py        # Qdrant or pgvector 抽象化
  ├─ embeddings.py     # SBERT埋め込み
  ├─ emotion.py        # 感情ベクトル推定
  ├─ topic_assigner.py # 軽量LLMで割当
  ├─ organizer.py      # Geminiで再編成バッチ
  ├─ retriever.py      # 想起ロジック（自動/探索）
  ├─ pipeline.py       # 取込〜保存〜割当のETL
  └─ config.py
```

### 主要関数シグネチャ（例）

```
# pipeline.py
def ingest_turn(conv_id: str, turn_index: int, speaker: str, text: str, meta: dict) -> MemoryEntry: ...
def link_entries(entry_id_a: str, entry_id_b: str, relation: str = "contextual"): ...

# embeddings.py
def embed(texts: list[str]) -> list[list[float]]: ...

# emotion.py
def infer_emotion(text: str) -> EmotionVector: ...

# topic_assigner.py
def assign_topic(recent_dialog: list[dict], candidate_topics: list[Topic]) -> dict: ...
# returns {"decision": "BEST_MATCH"|"NEW", ...}

# organizer.py
def nightly_reorganize() -> dict: ...
# returns summary of split/merge/centroid updates

# retriever.py
def auto_recall(current_utterance: str, k: int = 10) -> list[MemoryEntry]: ...
def explore(query: dict, k: int = 20) -> dict:
    """
    query: {"keywords": "...", "topic_id": "...", "time_range": [t0,t1],
            "emotion_hint": EmotionVector, ...}
    """

```

---

## 8) 取り込みパイプライン（オンライン）

1. `ingest_turn()`
　- `raw_text`保存（DB＋オブジェクトストレージ）
　- `summary`生成（軽量LLM or ルール）
　- `embedding`計算（SBERT）
　- `emotion`推定
　
2. `assign_topic()`（軽量LLM）
　- 既存に割当 or 新規Topic生成
　- Topic側の `strength/centroid` を**インクリメンタル更新**
　
3. Qdrant/pgvectorへ**Upsert**
　
4. Entry間リンク（同会話の前後Turnを `linked_entries` に付与）
　

---

## 9) 再編成パイプライン（オフライン）

- ジョブ `nightly_reorganize()`：
　
　- 入力：全トピック/全要約/統計　
　- LLM: **Gemini 2.0 Flash**（長文）
　- 出力：
 　- 分割：`topic_X -> topic_Xa, Xb ...`
 　- 統合：`topic_Y + topic_Z -> topic_YZ`
 　- 親子付与：`topic_A` parent of `topic_B`
 　- センチロイド再計算、強度再計算
　- 変更差分を**ジャーナル保存**してロールバック可能に。
　

---

## 10) ランキング・UI 提供物（返却形）

- **想起バンドル**（CEN渡し用）
　- `highlights`: 引用（原文スニペット）
　- `topics`: 関連トピックと要約
　- `why`: 採用理由（sim/時間/感情/トピック強度）
- **探索結果**
　- トピック中心ノード＋周辺発話（ジャンプ可能なID）
　

---

## 11) 品質管理

- **評価**：
　- 人手ラベルの「正しい想起セット」とのnDCG/MRR
　- 感情一致精度（人工評価）
- **メトリクス**：
　- 再想起頻度／被参照率→ `Topic.strength` 自動調整
　- オンラインA/B：w重み調整
　

---

## 12) セキュリティ／信頼性

- 原文は**改変不可**保存（WORM相当のストレージ階層 or 署名ハッシュ）
- すべての再編成は**差分ジャーナル**と**バージョン**管理
- PIIは必要に応じて暗号化フィールド化
　

---

## 13) クイックスタート（疑似）

```
mc = MemoryCore(...)  # wraps storage/embeddings/emotion/llm clients

e = mc.ingest_turn(conv_id="c1", turn_index=1, speaker="user",
                   text="那須塩原の吊り橋の写真、送ったよ。めっちゃ揺れたね…", meta={})

bundle = mc.auto_recall("あの旅行、また行きたいな。")
# -> 引用付きで「那須塩原旅行」トピックの記憶が返る

timeline = mc.explore({"topic_id": "topic_nasu_trip"})
# -> 旅行の連続ログを時間順に
```

---

## 14) 実装メモ

- **埋め込み**は将来差し替え可能に（`EmbeddingProvider` 抽象）。 [sbert.net](https://sbert.net/?utm_source=chatgpt.com)
