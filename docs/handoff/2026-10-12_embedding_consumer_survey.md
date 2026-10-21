# 想起用埋め込み (e5-small 384 次元 → EmbeddingGemma 2 768 次元) 移行のための事実調査

調査日: 2026-10-12。対象: `C:\Users\shuhe\workspace\SAIVerse` (develop, 8d1eec26 時点の作業ツリー)。読み取りのみ。
書式: 「パス:行 — 事実」。推測は「未確認:」を付けて分けた。

---

## 0. 全体の形 (先に要点)

- 埋め込みを持つテーブルは memory.db (ペルソナごと) に **4 系統 + 旧 1** ある: `message_embeddings` (会話メッセージのチャンク)、`arasuji_embeddings` (Chronicle)、`memopedia_embeddings` (Memopedia ページ)、`memopedia_fragment_embeddings` (Fragment)、旧 `embeddings` (レガシー)。
- どれも `vector TEXT` 列に **JSON 配列文字列**で入っており、**次元の列・次元のメタデータはどこにも無い**。
- 次元が合わない保存ベクトルの扱いは経路ごとにバラバラ (WARNING を行ごとに出してスキップ / 黙ってスキップ / 0.0 扱いで残す)。
- 既存の再計算 (API と CLI) は **message_embeddings だけ**を作り直す。Chronicle / Memopedia / Fragment は「行が無いものを埋める」機構しか無く、**古い次元の行を作り直す機構は存在しない**。

---

## 1. ベクトルを作って保存する全経路

### 1.1 保存先テーブルと形式

- sai_memory/memory/storage.py:90-95 — 旧テーブル `embeddings(message_id TEXT PRIMARY KEY, vector TEXT)`。
- sai_memory/memory/storage.py:98-108 — 現行 `message_embeddings(message_id TEXT, chunk_index INTEGER, vector TEXT, PRIMARY KEY(message_id, chunk_index))` と message_id 索引。
- sai_memory/memory/storage.py:110-115 — 起動のたび (`_init_schema`) に旧 `embeddings` の行を chunk 0 として `INSERT OR IGNORE` で `message_embeddings` へ写す。
- sai_memory/memory/storage.py:704 — メッセージのベクトルは `json.dumps(list(map(float, vec)))` で JSON 文字列化して保存する (blob ではない)。
- sai_memory/arasuji/storage.py:360-364 — `arasuji_embeddings(entry_id TEXT PRIMARY KEY, vector TEXT NOT NULL)`。
- sai_memory/memopedia/storage.py:391-396 — `memopedia_embeddings(page_id TEXT PRIMARY KEY, vector TEXT NOT NULL)`。
- sai_memory/memopedia/storage.py:427-432 — `memopedia_fragment_embeddings(fragment_id TEXT PRIMARY KEY, vector TEXT NOT NULL)`。
- sai_memory/unified_recall.py:63-66, 144-147, 233-236 — Chronicle / ページ / Fragment のベクトルも `json.dumps(vector)` で JSON 文字列として `INSERT OR REPLACE` する。
- sai_memory/memory/storage.py:216-224 — `embed_metadata(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)` という汎用 KV テーブルがあり、埋め込み関連で使うキーは `embed_model` (モデル名文字列) の 1 つだけ。次元は記録していない。
- sea/sluice.py:2097-2134, 2187-2217 / saiverse/v3_shape_migration.py:288, 316, 413 — 同じ `embed_metadata` テーブルを、スルースのパンマーカー・捕獲保留の要約・v3 形状移行の印の置き場としても使っている (テーブル名に反して埋め込み専用ではない)。

### 1.2 会話メッセージ (message_embeddings) — 書き込み時に同期で計算

- saiverse_memory/adapter.py:268-281 — adapter の初期化で `Embedder(model=settings.embed_model, local_model_path=..., model_dim=...)` を 1 回作る。失敗したら WARNING を出して `self.embedder = None` (メッセージ保存は続くが埋め込みは作られない)。
- saiverse_memory/adapter.py:2920 (`_append_message`) — `append_building_message` (455) / `append_persona_message` (463) / `append_ledger_message` (893) がすべてここを通る。
- saiverse_memory/adapter.py:2987-3029 — メッセージ行を `add_message` した直後、**同じ `_db_lock` の内側で**本文を `chunk_text` (min 120 / max 480 字) に切り、`embedder.embed(payload, is_query=False)` → `replace_message_embeddings` する。対象はタグを問わず本文が空でない全行 (internal / spell / event_message 等も埋め込む)。
- saiverse_memory/adapter.py:2974-2980 — message dict に `embedding_chunks=0` があるときだけ埋め込みを飛ばす。使う側: sea/runtime_nodes.py:406 (Stelis アンカー)、builtin_data/tools/thread_switch.py:73、api/routes/people/import_chatlog.py:81-82, 193-194 (取り込みで「スキップ」を選んだとき)、scripts/import_chatlog_json.py:149。
- saiverse_memory/adapter.py:3014-3029 — 埋め込み計算の失敗は WARNING を出してメッセージ保存は成功扱い (索引だけ欠ける)。
- saiverse_memory/adapter.py:1640-1653 — `update_message` で本文が変わったら、そのメッセージの埋め込みを削除して作り直す。
- saiverse_memory/adapter.py:1666 / sai_memory/memory/storage.py:1510-1527 — メッセージ削除・スレッド削除で埋め込みも消す。
- api/routes/people/memory.py:106-146 — `POST /{persona_id}/threads/{thread_id}/messages` (メッセージの手動追加) は adapter を通さず、自前で `add_message` → `chunk_text` → `adapter.embedder.embed` → `replace_message_embeddings` する。
- saiverse_memory/adapter.py:1957-2017 — `backfill_missing_message_embeddings`: 埋め込みが 1 件も無い非空メッセージをまとめて埋める。呼ぶのは scripts/import_persona_logs_to_saimemory.py:337 と scripts/import_chatgpt_conversations.py:222 の取り込みスクリプトだけ。
- saiverse_memory/native_export.py:722-729, 743-799 — Native インポートの commit 後に、取り込んだメッセージの埋め込みを設定から作った `Embedder` (767-770) で生成する (`skip_embed` で飛ばせる)。エクスポートにはベクトルを含めない (native_export.py に vector を SELECT する箇所は無い)。
- 未確認: 書き込み経路で埋め込みを `_db_lock` の内側で計算しているため、モデルが重くなるとメッセージ保存 (会話の同期経路) の待ちがそのまま伸びる可能性がある。e5-small と gemma の CPU 速度差は本調査では計測していない。

### 1.3 Chronicle / Memopedia ページ / Fragment — 書き込み時には作らない。後からまとめて埋める

- sai_memory/unified_recall.py:344-385 — `embed_chronicle_entries`: `arasuji_entries` のうち指定 level (既定 1) で埋め込み行が無いもの (113-131 の LEFT JOIN … IS NULL) を 64 件ずつ埋める。埋め込む文は Chronicle の本文そのまま。
- sai_memory/unified_recall.py:388-427 — `embed_memopedia_pages`: 埋め込み行の無いページ (root_ と trunk と category='chronicle' を除く、188-208) を `"{title}: {summary}"` (summary が無ければ title) で埋める。
- sai_memory/unified_recall.py:430-471 — `embed_memopedia_fragments`: 埋め込み行の無い Fragment を `"{親ページ title}: {content}"` (孤児なら content だけ) で埋める。
- sea/session_lifecycle.py:7232-7279 — `ensure_recall_embeddings`: 上の 3 関数を Chronicle level=1 で順に呼ぶ。計算は錠の外、保存だけ `db_lock` の内側。
- sea/session_lifecycle.py:5129, 5250 — Metabolism (記憶の整理) のたびに無条件で `ensure_recall_embeddings` を呼ぶ (印戻しだけで終わる回 5129 と、通常の回 5250)。
- api/routes/people/arasuji.py:1171 (`_run_chronicle_generation`)、1337 (`_run_coverage_repair_job`) — 手動の Chronicle 生成ジョブと網羅修復ジョブの末尾でも呼ぶ。
- api/routes/people/debug.py:42-88 — `POST /{persona_id}/debug/generate-embeddings` が同じ 3 関数を `_db_lock` の内側で呼ぶ。UI の入口は frontend/src/components/memory/MemoryRecall.tsx:152-168, 685 と frontend/src/components/DebugPanel.tsx:83-85。
- scripts/embed_recall_sources.py:41-95 — CLI で同じ 3 関数を呼ぶ。
- Chronicle の埋め込みは level=1 しか作られない (全呼び出しが `level=1`。session_lifecycle.py:7265、debug.py:76、embed_recall_sources.py:85)。
- 新しく作られた Chronicle / ページ / Fragment は、次の Metabolism (または上記の手動の口) まで埋め込みが無く、埋め込み検索には出ない (書き込み時に `store_*_embedding` を呼ぶ箇所はバッチ関数以外に無い)。

### 1.4 Chronicle / ページ / Fragment の埋め込みが消える (= 次回作り直される) 条件

- sai_memory/arasuji/absorption.py:843-857, sai_memory/arasuji/storage.py:796, 837-847, 903, 1249, 1313 — Chronicle は本文が変わった / 消えたエントリの埋め込みを削除する。
- builtin_data/tools/memopedia_edit_fragment.py:49、builtin_data/tools/memopedia_delete_fragment.py:39、sai_memory/memopedia/storage.py:799、sai_memory/memopedia/body_to_fragment.py:1453、sai_memory/curation_ops.py:572 — Fragment は編集・削除・本文の Fragment 化・編纂の統合で埋め込みを削除する。
- `memopedia_embeddings` を削除するのは scripts/arasuji/persona_chronicle_cleanup.py:325 だけ。ページの title / summary を書き換えても埋め込みは消えない (非 test コードに他の DELETE が無い)。→ 副次の発見: ページ要約を編集しても古い要約のベクトルのまま検索される。

### 1.5 manager/persona.py

- manager/persona.py:150-167 — 埋め込みを作る処理は無い。全ペルソナの `sai_memory.embed_model_changed` を集めて起動時警告 `embed_model_mismatch` を積むだけ (§3.3)。

---

## 2. ベクトルを読む全経路

### 2.1 照合の中核は 2 系統

**(A) sai_memory/memory/recall.py の semantic_recall / semantic_recall_groups (message_embeddings だけを見る)**

- sai_memory/memory/recall.py:358-360, 436-438 — クエリを `embedder.embed([query_text], is_query=True)` で埋め込み、その長さを `vector_dim` とする。
- sai_memory/memory/storage.py:935-992 — `get_embeddings_for_scope` が `messages JOIN message_embeddings` を全件 (タグ条件だけ SQL) 読み、各行の JSON を `json.loads` して返す。旧形式の「多重ベクトル」(配列の配列) も展開する (983-987)。
- sai_memory/memory/recall.py:368-383, 446-461 — Python で全行を cosine し、メッセージごとの最良チャンクを取って top-k。しきい値は無い。
- sai_memory/memory/recall.py:488-581 — `build_context_payload` / `build_context` も semantic_recall を使うが、非 test コードからの呼び出しは recall.py 内部以外に見つからなかった。

**(B) sai_memory/unified_recall.py の unified_recall (4 系統すべて)**

- sai_memory/unified_recall.py:1050-1052 — クエリを `is_query=True` で埋め込み、長さを `vector_dim` とする。
- sai_memory/unified_recall.py:1054-1170 — Chronicle (level 既定 1)・ページ・Fragment・メッセージの各保存ベクトルを全件読み、cosine を計算してソースごとに上位 `total_cap*2` 件を残す。
- sai_memory/unified_recall.py:640-648 — ソースごとの枠 `SOURCE_ALLOCATIONS = {chronicle:3, memopedia:1, fragment:5, message:1, perception:1}`、`FOCUS_MULTIPLIER = 4`。
- sai_memory/unified_recall.py:754-771 — 枠の合計 (または引数 `topk`) が上限。
- sai_memory/unified_recall.py:1173-1199 — キーワード順位と埋め込み順位を RRF (K=60) で融合する。融合後の `score` は ~1/60 スケールで、生の cosine は `embed_score` に別途残す (31-38)。しきい値はこの関数の中には無い。
- sai_memory/unified_recall.py:299-337 — メッセージは取得時に「実会話」フィルタ (`real_conversation_filter`) をかけて読む。

**(C) 自動想起の鮮明想起だけは保存ベクトル同士を比べる**

- sea/auto_recall.py:818-936 — `_vivid_source_messages`: Fragment の保存ベクトル (845) と、その Fragment の元 Chronicle が束ねたメッセージのチャンクの保存ベクトル (864) を、自前の `_cosine` (760-772) で比べて最も近いチャンクを抜粋する。クエリは使わない (passage 対 passage)。

### 2.2 消費者ごとの一覧 (クエリをどこで作り、何と照合し、しきい値と件数はどこから来るか)

| 消費者 | 照合先 | クエリの出どころ | しきい値 | 件数 |
|---|---|---|---|---|
| 自動想起 sea/auto_recall.py:1332-1600 (呼び出しは sea/runtime_context.py:1203-1207, 1266) | unified_recall の memopedia / fragment / message (1451-1459、Chronicle は `search_chronicle=False`) | `build_query` (451-490): 最新ユーザー発話 1 件 (+ メディア想起 ON 時は添付の要約) | 0.86 (78-79)、message は +0.02 (119-120)。強化 ON 時は floor 0.78 (190) と Noul 確率 0.5 (193) | topk 8 (99-100)、強化時の枠 `_ENHANCED_SOURCE_ALLOCATIONS` (631) |
| 鮮明想起 sea/auto_recall.py:818-936 | Fragment ベクトル vs 元メッセージのチャンクベクトル | クエリ無し | 無し | `_VIVID_MAX_MESSAGES_REST` 等 (750-757) |
| 記憶想起スペル builtin_data/tools/memory_recall_unified.py:48-65 | unified_recall の Chronicle / ページ / Fragment (`search_messages=False`) | スペル引数 `query` | 無し | 既定枠 |
| memory_recall ツール builtin_data/tools/memory_recall.py:53-68 → adapter.recall_hybrid / recall_snippet | semantic_recall_groups (message, `required_tags=["conversation"]`) | ツール引数 | 無し | `topk` 引数 or `SAIMEMORY_MEMORY_TOPK` (config.py:147) |
| adapter.recall_snippet saiverse_memory/adapter.py:1685-1785 | semantic_recall_groups (1717-1730) | 引数 | 無し | settings.topk + 直近ガード件数 (1709-1716) |
| adapter.recall_hybrid saiverse_memory/adapter.py:1787-1937 | キーワード + semantic_recall_groups (1854-1867)、RRF k=60 (1810) | 引数 | 無し | topk×2 + ガード |
| memory_search_brief ツール builtin_data/tools/memory_search_brief.py:140-156 | semantic_recall_groups | ツール引数 | 無し | topk×2 + ガード |
| 想起の散歩 saiverse/recall_walk.py:467-499 | semantic_recall (`scope="thread"`、thread_id=None で全件) | 起点ノードの title + intent | 無し | `SEMANTIC_TOPK = 4` (102) |
| Memopedia 生成時の検索 sai_memory/memopedia/generator.py:168-224 | semantic_recall_groups (`scope="all"`) | 生成側のクエリ | 無し | topk×2。ここだけ adapter の embedder でなく `load_settings()` から `Embedder` を新しく作る (200-204、実体はモジュールのキャッシュで共有) |
| Memory タブ「横断検索」 api/routes/people/recall.py:497-597 (`POST /unified-recall`、UI は frontend/src/components/memory/MemoryRecall.tsx:197) | unified_recall の全ソース + perception | 画面の入力 | 無し | リクエストの topk |
| コア記憶シーンの会話検索 api/routes/people/core_memory.py:171-260 (UI は CoreMemoryScene.tsx:236) | LIKE 0 件のときだけ unified_recall の message (224-244) | 画面のキーワード | 無し | limit×3 |
| `POST /recall` api/routes/people/recall.py:15-41 | adapter.recall_snippet | リクエスト | 無し | リクエスト |
| `POST /recall-debug` api/routes/people/recall.py:66-449 | semantic_recall_groups (単発 / 空白分割 RRF / ハイブリッド) | リクエスト | 無し | リクエスト |

- `/recall` と `/recall-debug` を呼ぶ箇所は frontend/src に見つからなかった (API だけが残っている)。

---

## 3. 次元 (384) への依存

### 3.1 次元のハードコード・保存・検証

- 非 test のコードに `384` という定数は無い (Grep で確認)。「384次元」はドキュメント docs/concepts/legacy/saimemory.md:90 にだけある。
- DB に次元は保存されていない。全テーブルの `vector` は JSON 文字列で、次元の列も `embed_metadata` のキーも無い (§1.1)。
- sai_memory/config.py:103-109 — `SAIMEMORY_EMBED_MODEL_DIM` (整数) を読む。使われるのは sai_memory/memory/recall.py:214, 233 の 1 か所だけで、fastembed が知らないモデルをローカルパスから `add_custom_model` で登録するときの `dim`。値が無ければ `1_Pooling/config.json` の `word_embedding_dimension` か `config.json` の `hidden_size` 等から推定する (258-284)。
- api/routes/people/reembed.py:41、scripts/reembed_memory.py:54 — 再計算は「期待する次元」を `embedder.model.embedding_size` (fastembed `TextEmbedding` の属性) から取る。fastembed をやめる場合、ここは置き換えた実行体に同じ属性 (または代替) が要る。
- scripts/embed_recall_sources.py:81 — `embedder.model_name` を表示に使う。
- 次元の照合はすべて「クエリベクトルの長さ」との比較で、保存側の申告値とは比べていない (recall.py:371, 449 / unified_recall.py:1058, 1085, 1109, 1138 / auto_recall.py:761)。

### 3.2 384 と 768 が混ざったときに起きること (経路ごと)

- sai_memory/memory/recall.py:371-378, 449-456 — semantic_recall / semantic_recall_groups は長さ不一致の行を**行ごとに WARNING を出して**スキップする。全メッセージが旧次元なら、1 回の検索でチャンク行の数だけ WARNING が出て結果は 0 件になる。
- sai_memory/unified_recall.py:1058-1059, 1085-1086, 1109-1110, 1138-1139 — unified_recall は長さ不一致の行を**ログ無しで** `continue` する。旧次元のソースは黙って検索対象から消え、キーワード経路のヒットだけが残る。
- sea/auto_recall.py:1536-1541 — 自動想起はキーワードだけのヒット (`embed_score is None`) を採用しない。したがって旧次元の記憶は自動想起から黙って消える (DEBUG ログ `skip keyword-only hit` だけ)。
- sea/auto_recall.py:760-762 — 鮮明想起の `_cosine` は長さ不一致を **0.0 として返し、候補から外さない**。Fragment (新次元) と元メッセージ (旧次元) が混ざると、旧次元のメッセージは 0.0 で並ぶ。未確認: 新次元の候補の cosine が負になる組み合わせでは、旧次元 (0.0) の抜粋が選ばれる可能性がある。
- `json.loads` に失敗した行は unified_recall の取得関数 (101-102, 176-177, 264-265, 335-336) で黙ってスキップされる。
- 例外になる経路は見つからなかった。混在は「エラー」ではなく「黙って見つからない / ログが溢れる」の形で現れる。

### 3.3 起動時警告の仕組み (tests/test_embed_model_mismatch_startup_warning.py の対象)

- saiverse_memory/adapter.py:283-285 — Embedder の読み込みに**成功したときだけ** `_check_embed_model_change` を呼ぶ。新モデルの読み込みに失敗した場合は警告も出ず、埋め込み検索が全部止まる (adapter.py:274-281 の WARNING だけ)。
- saiverse_memory/adapter.py:300-331 — 判定は**モデル名の文字列比較**だけ。(a) `embed_metadata.embed_model` が無く `message_embeddings` に行がある → 変更扱い、(b) 記録名と現在名が違う → 変更扱い、(c) `message_embeddings` が空 → 現在名をその場で記録。見ているのは `message_embeddings` だけで、Chronicle / ページ / Fragment の有無も次元も見ない。
- manager/persona.py:150-167 — 旗が立ったペルソナを集めて `startup_warnings` に `source: "embed_model_mismatch"` と persona_ids を積む。
- api/routes/config.py:1075-1086 — `GET /api/config/reembed-check` がその警告を返す。
- frontend/src/app/page.tsx:1267-1275, 3763-3780 — 起動時にバナーを出す。page.tsx:1683-1719 — 「実行」は対象ペルソナごとに `POST /reembed` を **`force: true`** で順に投げ、終わるまでポーリングする。
- api/routes/people/reembed.py — 再計算が終わっても `manager.startup_warnings` と adapter の `embed_model_changed` は書き換えない。バナーを消すのは画面側の状態だけ (page.tsx:1718) なので、再読み込みすると再起動まで同じバナーが出る (コードを読んだ結果。実機では未確認)。
- tests/test_embed_model_mismatch_startup_warning.py:86-119 — テストしているのは manager の集計だけ (adapter は `SimpleNamespace(embed_model_changed=...)` のスタブ) と、無効化した adapter が旗を持つこと。`_check_embed_model_change` 自体の判定と再計算 API のテストは tests/ に無い (`embed_model` / `reembed` を含む test は sluice・v3 移行・この 1 本だけ)。
- sai_memory/config.py:132-142 — `SAIMEMORY_EMBED_MODEL` 未設定のとき、`sbert/multilingual-e5-small` が無くて `sbert/multilingual-e5-base` があると、モデル名が黙って e5-base に切り替わる (起動時警告はここでモデル名の変化として検出される)。

---

## 4. しきい値の住所録

類似度 (生 cosine) のしきい値を持つのは自動想起だけだった。他の検索経路は top-k と RRF だけで、cosine の下限を持たない。

| 値 | 置き場 | 種類 | 意味 |
|---|---|---|---|
| 0.86 | sea/auto_recall.py:78-79 `SAIVERSE_AUTO_RECALL_THRESHOLD` | env (既定値はコード) | 採用しきい値。68-77 のコメントに e5-small での実測分布 (オントピック 0.86〜0.89 / 無関係 max 0.842 / 「うん」max 0.837) |
| +0.02 | sea/auto_recall.py:119-120 `SAIVERSE_AUTO_RECALL_MSG_THRESHOLD_OFFSET` | env | message ソースだけ実効 0.88 (1560) |
| 0.78 | sea/auto_recall.py:190 `_REFLEX_FLOOR` | モジュール定数 (env なし) | 強化 (反射判断) ON 時に判断へ渡す候補の下限 (1116) |
| 0.5 | sea/auto_recall.py:193 `_REFLEX_THRESHOLD` | モジュール定数 | Noul 確率の採用下限 (cosine ではない) |
| 1.0 | sea/auto_recall.py:1419, 1429 | 直書き | エンティティトリガー (タイトル一致) の記憶に与える `best_score`。cosine の上限 1 を前提にした値 |

- sea/auto_recall.py:1238-1245 — `_REFLEX_FLOOR > しきい値` になると、毎ターン WARNING を出し、その帯の記憶が判断に届かず落ちる。しきい値を env で 0.78 未満に下げると (例えば gemma 向けに) この警告が常時出る。floor は env で動かせない。
- しきい値に関係する他の env (cosine 値ではない): `SAIVERSE_AUTO_RECALL_STICKY_TURNS` 3 (83-84)、`SAIVERSE_AUTO_RECALL_QUERY_MESSAGES` 1 (94-95)、`SAIVERSE_AUTO_RECALL_TOPK` 8 (99-100)、`SAIVERSE_AUTO_RECALL_ENTITY_AMBIENT_COUNT` 3 (126-127)、`SAIVERSE_AUTO_RECALL_VIVID_CHARS` 400 (133-134)。
- 想起の件数系 (cosine ではない): sai_memory/unified_recall.py:640-648 (ソース枠と ×4)、sea/auto_recall.py:631 (強化時の枠)、saiverse/recall_walk.py:102 (`SEMANTIC_TOPK=4`)、sai_memory/config.py:147-149 (`SAIMEMORY_MEMORY_TOPK` 5、`RANGE_BEFORE/AFTER` 1)。
- UI から変えられるしきい値は無い (frontend/src に `AUTO_RECALL_THRESHOLD` 等の参照なし。画面から変えられるのは反射判断の待ち秒数 `SAIVERSE_REFLEX_TIMEOUT_SECONDS` だけ、api/routes/config.py:850-895)。
- docs/reference/environment-vars.md に `SAIVERSE_AUTO_RECALL_*` 系の env は載っていない (docs 全体を Grep して 0 件)。0.78 / 0.5 への言及は environment-vars.md:78 にある。
- テストが既定値を前提にしている: tests/test_auto_recall_jev.py:482 (`_REFLEX_FLOOR == 0.78` を assert)、tests/test_auto_recall_jev.py:250, 297, 369, 378, 418, 533, 568 と tests/test_auto_recall.py:369 (0.86 / 実効 0.88 前提のコメントと値)。
- 文書側の記述: docs/intent/memory_architecture_v2.md:276, 290 (0.86 の実測根拠)、docs/intent/auto_recall_jev_rerank.md:10, 46, 61-65, 234, 261, 290、docs/intent/ultramemory.md:27、docs/overview/ideas.md:64。

---

## 5. 再計算の既存機構

### 5.1 api/routes/people/reembed.py (`POST /{persona_id}/reembed`、`GET .../reembed/status`)

- reembed.py:35-41 — `load_settings()` から `Embedder` を新しく作り、期待次元を `embedder.model.embedding_size` から取る。
- reembed.py:43 — adapter を使わず `init_db` で別の接続を開く。adapter の `_db_lock` は取らない。未確認: 会話中のペルソナに対して走らせたときの書き込み競合 (SQLite のロック待ちで済むか) は確かめていない。
- reembed.py:46-49 — `force=true`: `messages` の全 id を対象にする。
- reembed.py:50-69 — `force=false`: 埋め込みが無い id と、どれかのチャンクの長さが期待次元と違う (または JSON が壊れた) id を対象にする。384 の行は 768 のモデルでは全部「違う」に入るので、force なしでも全件拾える。
- reembed.py:82-98 — 本文が空のものは飛ばし、チャンクが空なら本文全体を 1 チャンクにして埋める。
- reembed.py:105-108 — 終わったら `embed_metadata.embed_model` を現在のモデル名にする。
- **再計算するのは `message_embeddings` だけ**。`arasuji_embeddings` / `memopedia_embeddings` / `memopedia_fragment_embeddings` には触れない。
- UI: 起動時バナー (page.tsx:1683-1719、force=true) と、メモリ管理画面の「未処理を埋める / 全て再作成」(frontend/src/components/memory/MemoryImport.tsx:75-84、useMemoryImport.ts:148、frontend/src/lib/memory/importClient.ts:63-72)。

### 5.2 scripts/reembed_memory.py (CLI)

- reembed_memory.py:38-55 — `--model` / `--model-path` / `--model-dim` で上書きでき、期待次元は `embedder.model.embedding_size`。
- reembed_memory.py:67 — DB の場所は `SAIVERSE_HOME` (無ければ `~/.saiverse`) / personas / id / memory.db。
- reembed_memory.py:84-93 — `--force` で全メッセージ。
- reembed_memory.py:94-124 — 既定は「長さが期待次元と違う」メッセージだけ。保存済みの最大次元が期待次元より大きいと「小さい次元への再計算は拒否」して止まる (112-119)。384→768 は通るが、768→384 へ戻すときは `--force` が要る。
- **このスクリプトは `embed_metadata.embed_model` を更新しない** (API 版の reembed.py:108 に当たる処理が無い)。CLI で再計算しても次回起動時に「モデルが変わった」警告が出続ける。
- 対象は `message_embeddings` だけ。

### 5.3 Chronicle / Memopedia / Fragment

- ideas.md の「再計算 API はメッセージしか直さない (Chronicle・Memopedia の古いベクトルは残る)」という記述 (docs/overview/ideas.md:64) は、コードと一致した。Fragment も同じく残る。
- 3 系統を埋める唯一の機構 (`embed_chronicle_entries` / `embed_memopedia_pages` / `embed_memopedia_fragments`、§1.3) は「埋め込み行が無いもの」だけを対象にする (unified_recall.py:126-127, 201-202, 287-289)。**長さが違う行を作り直す機構は無い**。
- 3 系統を作り直すには、行を消してから `ensure_recall_embeddings` (次の Metabolism)・`POST /debug/generate-embeddings`・`scripts/embed_recall_sources.py` のどれかを走らせる必要がある。行を消す口は既存に無い (Chronicle の本文変更・Fragment 編集など個別の道連れ削除だけ、§1.4)。
- Chronicle は level=1 しか埋めないので、level 2 以上は移行の対象外 (そもそも埋め込みが無い)。

---

## 6. モデルファイルの配り方と GPU

### 6.1 sbert/ ディレクトリ

- .gitignore:121 — `sbert` はリポジトリに入れない。
- setup.bat:290-298 / setup.sh:148-154 — セットアップの第 12 段で `huggingface_hub.snapshot_download('intfloat/multilingual-e5-small', local_dir='sbert/multilingual-e5-small', ignore_patterns=['*.bin','*.safetensors','*.h5','openvino/*','*.ot'])` を実行する。失敗したら「初回起動時に再試行」と WARN を出して続行する。setup.sh は失敗時にも直後の `[OK] ... 完了` を表示する (154 行が `||` の外にある)。
- update.bat / update.sh / scripts/self_update.py には埋め込みモデルのダウンロード処理が無い (Grep で 0 件)。既存利用者に新しいモデルを届ける経路は、今は「起動時の自動ダウンロード」しか無い。
- sai_memory/config.py:111-116 — 探索先はリポジトリ直下の `sbert/<モデル名の / の後ろ>`。ONNX ファイル (`onnx/model.onnx`、`onnx/model_optimized.onnx`、`model.onnx`、`model_optimized.onnx`、22-32) があればローカルパスとして使う。
- sai_memory/config.py:117-130 — ディレクトリがあっても ONNX が無い場合と、`SAIMEMORY_EMBED_MODEL` を明示したのにローカルが無い場合は WARNING。
- sai_memory/memory/recall.py:91-121 — fastembed が知らないモデル名でローカルパスが無いと、`ValueError ... not supported` を拾って `_auto_download_model` で `sbert/<名前>` へ HuggingFace から落とし (157-196、同じ ignore_patterns)、`add_custom_model` で登録する (199-242)。
- この開発機の .venv の fastembed 0.7.4 は `intfloat/multilingual-e5-small` を組み込みで知らない (組み込みの多言語 e5 は large だけ。.venv/Lib/site-packages/fastembed/text/pooled_embedding.py:74, text_embedding.py:101)。したがって e5-small は常に「ローカルスナップショット + add_custom_model」の経路で読まれている。
- この開発機の `sbert/` には multilingual-e5-small / -base / -large の 3 つがあり、e5-small の `onnx/` には model.onnx・model_O4.onnx・model_qint8_avx512_vnni.onnx がある (使われるのは model.onnx、recall.py:245-255 の順)。
- recall.py:204-242 — fastembed の custom model 経路は pooling (`1_Pooling/config.json`、既定 MEAN)・正規化 (`modules.json`、既定 True)・追加ファイル (tokenizer.json 等 8 種) をディレクトリから推定する。onnxruntime 直叩きに替えると、トークナイズ・pooling・正規化・プロンプト文字列の付与はすべて新しい実行体の責任になる。
- saiverse/addon_external_loader.py:84-86 — アドオン読み込みの探索から `sbert` を除外している (ディレクトリ名に依存)。
- docs/developer-guide/project-structure.md:39 — 「sbert/ 埋め込みモデル (存在すれば利用、なければ自動DL)」。

### 6.2 SAIMEMORY_EMBED_CUDA と onnxruntime-gpu

- sai_memory/memory/recall.py:57-63 — `SAIMEMORY_EMBED_CUDA` が設定されていればその真偽、未設定なら `onnxruntime.get_available_providers()` に `CUDAExecutionProvider` があるか (29-35) で GPU を使うか決める。
- recall.py:87-89 — GPU を使うときは fastembed に `cuda=True` を渡す。
- recall.py:93-102 — `TextEmbedding` の生成が **ValueError で**失敗したときだけ CPU へ落とす。他の例外型はそのまま上がり、adapter 側で embedder=None (埋め込み全停止) になる (adapter.py:274-281)。
- recall.py:65 — モデルのキャッシュキーに GPU 使用の真偽が入る。プロセス内で 1 モデル 1 インスタンスを全ペルソナで共有する (25-26, 67-124)。
- requirements.txt:51-58 — `fastembed>=0.7.3`、`onnxruntime>=1.17,<1.24` (Intel Mac の wheel 問題で上限必須、docs/intent/dependency_management.md:100)。requirements.lock:95, 230 — fastembed 0.7.4、onnxruntime 1.23.2。
- requirements-gpu.txt:13-16 — GPU 版は手動で `onnxruntime` を外して `onnxruntime-gpu>=1.21.0` を入れる手順。setup / update は GPU 版を入れない。
- docs/getting-started/gpu-setup.md:3-257 — CUDA 12.x / cuDNN 9 / onnxruntime-gpu の手順と、`SAIMEMORY_EMBED_CUDA` の意味、ログ文言。
- この開発機の .venv では `onnxruntime` 1.23.2 が import され、providers は `['AzureExecutionProvider', 'CPUExecutionProvider']` だった (CUDA なし)。同じ site-packages に `onnxruntime_gpu-1.24.4.dist-info` も残っている。未確認: 両方の dist-info が同じ `onnxruntime/` を上書きし合っている状態で、どちらの実体が入っているかの経緯。
- saiverse/windows_runtime_check.py:3-86 — Windows で onnxruntime が VC++ ランタイム不足で読めないときに案内を出して止める起動時チェック (onnxruntime 依存はここにもある)。

---

## 7. メディア想起の現状 (入口と出口だけ)

- 現行の形: 画像・音声・動画は**ベクトルにしない**。送信時に LLM で文字の要約を作り、その文字を**クエリ側にだけ**足す。保存側 (message_embeddings) に入るのはメッセージ本文の文字だけで、添付の要約は埋め込まれない (adapter.py:3015 は `content` だけを chunk する)。

### 入口 (要約を作る)

- api/routes/chat.py:1017-1038 — `manager.state.media_recall_enabled` (manager/state.py:29 で `SAIVERSE_MEDIA_RECALL_ENABLED` を読む) が ON のとき、添付の保存に `sync_summary=True` を渡す。
- api/routes/chat.py:538-549 — 画像: `generate_contextual_image_description` を同期で呼び (OFF 時は 550-567 で背景スレッド)、結果を `summary` に入れる。
- api/routes/chat.py:705-711 (音声 `ensure_audio_summary`)、781 / 840 (動画 `ensure_video_summary`) — ON のときだけ同期生成。
- api/routes/chat.py:1051-1052, 1072-1073 — 要約をメッセージ metadata の `images[]` / `media[]` 各エントリの `summary` キーに載せる。
- saiverse/media_summary.py:23-47, 55-98 — 要約は LLM クライアントで作る (モデルは `SAIVERSE_IMAGE/AUDIO/VIDEO_SUMMARY_MODEL`、空なら組み込みの軽量モデル)。saiverse/media_summary.py:112-155 の `ensure_*_summary` は保存済みの要約 (saiverse/media_utils.py:470, 481) があれば再利用する。
- api/routes/config.py:826-843 — `GET/POST /api/config/media-recall` で切り替え、`.env` に書く。UI は frontend/src/components/GlobalSettingsModal.tsx:341-354。

### 出口 (要約を使う)

- sea/auto_recall.py:379-403 — `_current_attachment_summaries`: 最新のユーザーメッセージ 1 件の metadata から `summary` を集める。
- sea/auto_recall.py:482-490 — `build_query`: ON のとき、その要約をクエリ文字列の末尾に足す (→ unified_recall の `is_query=True` 埋め込みへ)。
- sea/auto_recall.py:1178-1181 — 強化 ON 時、反射判断に渡す state にも同じ要約を `attachments` として入れる。
- 将来の置き換え候補 (推測): 入口は chat.py の `sync_summary` 分岐、出口は `build_query` の文字列連結。画像・音声を直接埋め込むなら、クエリベクトルを「文字の連結」ではなく「ベクトルの組」で作る形になり、unified_recall の `embedder.embed([query], is_query=True)` (unified_recall.py:1050) の 1 本のクエリという前提が変わる。未確認: 保存側 (過去の添付画像) もベクトル化するなら、どのテーブルに載せるかの置き場は今のスキーマに無い。

---

## 8. 埋め込みモデル名・次元が UI / ドキュメントに現れる箇所

### UI

- UI にモデル名・次元を表示する箇所は無い (frontend/src/i18n/messages.json に `multilingual` / `e5-small` / 次元の語は無い)。
- 埋め込みに関する UI 文言と画面: frontend/src/app/page.tsx:1267-1275, 1683-1726, 3763-3780 (モデル変更バナー。文言は manager/persona.py:163-165 のサーバー側日本語固定文)、messages.json `app.page.text005` (「設定 > メモリ管理 > エンベディング管理から再実行できます」)、`components.memory.MemoryImport.text002-005` (エンベディング管理・未処理を埋める・全て再作成)、`components.memory.MemoryImportForm.text006-008` (取り込み時に作るか)、`components.DebugPanel.text004-005` と MemoryRecall の一括生成ボタン (messages.json:2055-2060, 5195-5204)、エラー文言 (10219, 10243, 10651, 10699)。
- メディア想起のトグル: GlobalSettingsModal.tsx:341-354。

### ドキュメント・設定

- docs/reference/environment-vars.md:42-44 — `SAIMEMORY_EMBED_MODEL` (既定 `intfloat/multilingual-e5-small`)・`_PATH`・`_CUDA`。`SAIMEMORY_EMBED_MODEL_DIM` は載っていない。64 — `SAIVERSE_MEDIA_RECALL_ENABLED`。78 — 反射判断の定数 (0.78 / 0.5)。
- docs/getting-started/configuration.md:25-26 — `SAIMEMORY_EMBED_MODEL` / `_PATH`。
- docs/getting-started/gpu-setup.md — GPU 手順一式。
- .env.example:39-51 — `SAIMEMORY_EMBED_CUDA` (コメント)、`SAIMEMORY_EMBED_MODEL=intfloat/multilingual-e5-small` (コメント)、チャンク字数 120 / 480。107 — `SAIVERSE_MEDIA_RECALL_ENABLED=false`。
- docs/concepts/legacy/saimemory.md:90-105 — 「e5-small、384 次元」と sbert/ の置き方。
- docs/developer-guide/project-structure.md:39 — sbert/。
- docs/third_party_licenses.md:38, 84-85 — fastembed (Apache-2.0)、e5-small / e5-base (MIT)。
- sai_memory/README.md:86 — 「埋め込みは fastembed」。
- requirements.txt:51-58、requirements-gpu.txt:13-16、requirements.lock:95, 230、docs/intent/dependency_management.md:100, 121。
- docs/issues/python_314_support_verification.md:6-30 — fastembed / onnxruntime の Python 3.14 検証。
- docs/intent/memory_architecture_v2.md:31, 88, 276, 290 — e5-small 前提のコストと 0.86 の根拠、埋め込み整備ステップ。
- docs/intent/ultramemory.md:27 — e5 の cosine 圧縮を想起精度の天井として記述。
- docs/intent/saiverse_lite.md:70-71 — Lite の意味想起は「transformers.js + multilingual-e5-small = 本体 SAIMemory と同モデル」と書かれている。未確認: Lite が本体とベクトルを交換するか (同モデルであることが契約になっているか)。
- docs/intent/metis_memory_bridge.md:150 — 「e5-small スタック共通」の記述。
- CLAUDE.md:79, 288 — `SAIMEMORY_EMBED_CUDA`、`SAIMEMORY_EMBED_MODEL`。
- docs/overview/ideas.md:64 — 今回の移行の発案の行。

---

## 9. 実行体の差し替えで触ることになる接点 (補足)

- `Embedder` の外から使われている面は 3 つだけだった: `embed(texts, *, is_query)` (全経路)、`model.embedding_size` (reembed.py:41、reembed_memory.py:54)、`model_name` (embed_recall_sources.py:81)。
- sai_memory/memory/recall.py:134-135 — 現行のプレフィックスは「モデル名に `e5` を含むなら `query: ` / `passage: `」の 1 本だけ。`google/embeddinggemma-2` は名前に `e5` を含まない。
- tests/ の約 45 か所が `patch("saiverse_memory.adapter.Embedder", DummyEmbedder)` で差し替えている (例: tests/test_saiverse_memory_adapter.py:76)。adapter が import する名前 `Embedder` を変えるとこれらが全部外れる。実モデルを読むテストは見つからなかった。
- sai_memory/unified_recall.py:499-521 — メッセージ抜粋の復元は書き込み時と同じチャンク字数 (env の 120 / 480) で再分割して chunk_index で引く。チャンクの切り方を変えると、旧チャンク数との不一致でフォールバックする整合性ガード (581 付近) に入る。
- 未確認: 全経路が毎回テーブル全件の JSON を `json.loads` して Python で cosine している (storage.py:978-991、unified_recall.py の各 get_*)。768 次元では JSON の文字列長がおよそ倍になり、検索ごとのパースと計算も増えるが、本調査では計測していない。

---

## 10. 次元替わり・モデル替わりで壊れる箇所の一覧表

| 場所 | 壊れ方 | 検出のされ方 |
|---|---|---|
| sai_memory/memory/recall.py:371-378, 449-456 (semantic_recall / _groups → memory_recall ツール、recall_snippet / recall_hybrid、memory_search_brief、recall_walk、Memopedia 生成、/recall, /recall-debug) | 旧次元のメッセージが全部スキップされ、再計算までは結果 0 件または新しいメッセージだけになる | **行ごとに** WARNING `embedding dim mismatch`。全件旧次元なら 1 検索でチャンク行数ぶん出る |
| sai_memory/unified_recall.py:1058, 1085, 1109, 1138 (自動想起・記憶想起スペル・横断検索 API・会話検索のフォールバック) | 旧次元の Chronicle / ページ / Fragment / メッセージが黙って検索から消える。キーワード一致のヒットだけ残る | **ログ無し**。結果が減るだけ |
| sea/auto_recall.py:1536-1541 (自動想起の採否) | 旧次元の記憶はキーワードヒットになり `embed_score=None` で不採用。自動想起がほぼ何も浮かべなくなる | DEBUG ログ `skip keyword-only hit` だけ |
| sea/auto_recall.py:760-772, 818-936 (鮮明想起) | 長さ不一致を cosine 0.0 として候補に残す。新旧が混ざると抜粋の選び方が狂う | ログ無し |
| arasuji_embeddings / memopedia_embeddings / memopedia_fragment_embeddings (§5.3) | 既存の再計算 (API・CLI・バナー) の対象外で、`ensure_recall_embeddings` も「行が無いもの」しか埋めないため、**旧次元の行が恒久的に残る**。新しく作られた分だけ新次元になる | 起動時警告は message_embeddings とモデル名しか見ないので、メッセージを再計算した後は**何も警告されない** |
| saiverse_memory/adapter.py:300-331 (起動時判定) | モデル名の比較だけ。名前が同じで中身 (量子化・ファイル) が変わった場合や次元だけ変わった場合は検出しない。新モデルの読み込みに失敗すると判定自体が走らない | 前者は検出なし。後者は adapter.py:275-280 の WARNING と、想起が全部止まること |
| api/routes/people/reembed.py:105-108 + api/routes/config.py:1075-1086 (バナー) | 再計算しても `startup_warnings` が残り、再読み込みで再起動まで同じバナーが出る (コード上の推論、実機未確認) | 画面でバナーが再表示される |
| scripts/reembed_memory.py (CLI) | `embed_metadata.embed_model` を更新しないので、CLI で直しても起動時警告が出続ける。768→384 へ戻すときは `--force` 無しだと拒否される | 起動時バナー / CLI の `[error] Refusing to re-embed to a smaller dimension` |
| api/routes/people/reembed.py:41、scripts/reembed_memory.py:54 | 期待次元を fastembed の `embedder.model.embedding_size` から取っている。fastembed をやめた実行体に同じ属性が無いと AttributeError で再計算が止まる | API は status に `Error: ...`、CLI は例外 |
| sai_memory/memory/recall.py:134-135 (プレフィックス) | e5 用の `query:` / `passage:` 判定はモデル名で分岐する。gemma のプロンプト文字列を付ける分岐は無い (プロンプト無しで埋め込むと品質が落ちる、というのは別資料の計測側の話) | ログ無し |
| sea/auto_recall.py:78-79, 119-120, 190 (しきい値) | 0.86 / 0.88 / 0.78 は e5-small の cosine 分布で決めた値。gemma で分布が変わると、注入が毎ターン全件か、ゼロ件のどちらかに寄る可能性がある (未確認) | DEBUG ログ `[auto_recall] hit ... embed=... threshold=...` で値を見るしかない |
| sea/auto_recall.py:1238-1245 | しきい値を env で 0.78 未満にすると `_REFLEX_FLOOR` (定数 0.78) との逆転警告が毎ターン出て、その帯が強化経路で落ちる | WARNING `the candidate floor ... is above the cosine acceptance threshold` |
| tests/test_auto_recall_jev.py:482 ほか (§4) | 既定値を変えるとテストが落ちる | pytest |
| tests/ の `patch("saiverse_memory.adapter.Embedder", ...)` 約 45 か所 | adapter が import する名前を変えると差し替えが効かなくなり、実モデルを読みに行く | pytest (モデル読み込み失敗または遅延) |
| setup.bat:290-298 / setup.sh:148-154 / 自動ダウンロード recall.py:157-196 | セットアップは e5-small を固定で落とす。更新スクリプトにはダウンロードが無いので、既存利用者には起動時の自動ダウンロード (ONNX と tokenizer 系だけを落とす ignore_patterns) しか経路が無い | ダウンロード失敗時は WARNING と「埋め込み無しで起動」 |
| sai_memory/config.py:132-142 | `SAIMEMORY_EMBED_MODEL` 未設定時の自動選択は e5-small → e5-base しか知らない。gemma を既定にするにはここの既定値と探索リストを変える必要がある | — |
| docs/intent/saiverse_lite.md:70-71 | 「本体と同モデル」と書かれている Lite の想起とずれる (ベクトル交換の有無は未確認) | — |
