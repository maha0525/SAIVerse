# W4 (実行台帳 Phase 4 — Metabolism 残片 = 体験の構造 工程(2)) セッション走行メモ (2026-07-21)

**用途**: このセッション (Fable/メティス) の確定設計と実装・検収の再開点。工程の真実は [完了計画書](../overview/audit_remediation_plan.md)、finding の真実はレビュー台帳 (記憶監査 Metabolism 系 = M2 残片) と [体験の構造](../intent/experience_structure.md) (§4 圧縮七原則 / §11-8 束ねアルゴリズム確定)。

**スコープ** (計画書 W4): M2 (Chronicle 生成の残る原子性課題) を、旧バッチ生成経路に hardening を入れず**新設の episode 整列経路**で消化する (2026-07-19 統合裁定)。完了条件 = Metabolism 系 finding 全消し込み + 圧縮七原則が生成経路の回帰で固定。

**進行**: 調査 (済み) → 設計 (本書 = 確定) → 実装 (Chunk A/B/C) → 検収 → コミット。

---

## 調査で確定した現状 (行番号は 2026-07-21 時点)

### 患部の構造

- **入口の合流点**: `sea/session_lifecycle.py: generate_chronicle` (:975)。全 5 入口 (①応答後 Metabolism ②会話前 anchor 失効 ③手動 organize-memory ④session close ⑤gold_panning 内) がここに合流。**M1 (冪等 claim `metabolism.run`, key=`{persona}:{window_end_id}`) と S2 (status ゲート: ok/disabled のみ anchor 前進) は §6-5 で結線済み**。
- **生成本体**: `sai_memory/arasuji/generator.py: ArasujiGenerator`。`generate_unprocessed` (:1158) が Lv1 source_ids から processed_ids を引き連続 run 抽出 → `generate_from_messages` (:962) が **20 件固定バッチ**で `generate_level1_arasuji` (LLM 1 コール→`create_entry` 即 commit) → gap-fill/dismantle 判定 → `maybe_consolidate` (:564, **10 個固定**で Lv+1、再帰)。
- **格納**: P3b (2026-07-11) で memopedia_pages (category='chronicle') に移行済み。`arasuji_entries` は互換 VIEW。level/source_ids/start_time/end_time/is_consolidated は metadata JSON 内。全 Chronicle ページは ROOT_CHRONICLE 直下 + `mark_consolidated` が親子を `parent_id` で張る。
- **読み込み**: `sai_memory/arasuji/context.py: get_episode_context` — level ベース逆行昇格 + 文字数予算 (既定 2 万字) + 昇格梯子 (10→5→3→1)。**level 列は読み込み互換の必須面** (体験の構造 §3.2 で「導出キャッシュに格下げ」— 書き続けるが真実は木)。
- **episode の器**: world DB `Episode` テーブル (`saiverse/episodes.py` — open/close/digest_ref/occurrence_id、会話 episode は `user_conversation_handler` が開閉)。**messages.origin_episode 層0タグは全保存経路で自動継承** (`sea/runtime.py:1583` — 開いている episode があれば必ず付く)。episode digest = post_session が書く `session_digest` タグの message (main_line/committed) + `episodes.digest_ref='message:{id}'` (W1 D9-5)。
- **anchor**: world DB `session_anchor` (persona, model)。Chronicle は persona の memory.db — **DB 跨ぎで単一 tx は原理的に不可** → 順序保証 (commit 後にのみ前進) + 冪等が正解 (現行 S2 ゲートの形は正しい)。

### M2 = 残る原子性・整合性の欠陥 (今回消化する全量)

| # | 欠陥 | 場所 | 実害 |
|---|---|---|---|
| M2-a | **consolidation の親子更新が別 commit** — 親 create (commit) → mark_consolidated (別 commit、失敗許容で続行) | generator.py:500-558 (else 節が「entry created but children not marked」と自認) | 間で死ぬと子が unconsolidated のまま → 次回 maybe_consolidate が**同じ子から別の親を二重生成** (同一範囲の二重語り) |
| M2-b | **窓内 (まだ提示中) のメッセージまで編纂** — 編纂対象が「全未処理」で anchor 位置と無関係 | generate_chronicle (get_messages_for_chronicle 全量) | 体験の構造 §4-1 違反 (「アンカーで生のまま残っている間は圧縮しない」)。窓内が Lv1 化され weave と Session 窓に二重提示 |
| M2-c | **session_digest 行が Chronicle 材料に混入** — CHRONICLE_EXCLUDED_TAGS = (handy_tool, spell, event_message) に W1 の DIGEST_TAG が無い | storage.py:1806 | §4-4 違反 (digest の再圧縮)。episode digest がバッチあらすじに二重圧縮される — 病巣診断 §1-1 そのもの |
| M2-d | **source 集合の重複を DB が止めない** — UNIQUE 制約なし (metadata JSON 内のため物理制約は張れない) | memopedia_pages | M1 claim が唯一の守り。claim degrade (台帳なし環境) で二重編纂が素通り |
| M2-e | **dismantle/gap-fill の複合更新に tx なし** — L2 削除 + L1 復帰 + 再統合 LLM が逐次 commit | generator.py:806-914, storage.py:922 | 途中失敗で「L2 は消えたが L1 が復帰していない」等の中間状態 |

### 前提となる裁定 (すべて確定済み・再議論しない)

- 束ねアルゴリズム = §11-8 モック検証済み確定: **U=1 万字・B=10**。既存 Lv 中央値 (9,708 / 11.4 万 / 136 万字) が**級 k の標準被覆 = U×B^(k-1)** を実測支持
- 旧バッチ経路に hardening を入れない (捨てる経路を固めない) / 移行 = 再生成なし帰化 (§11-3) / Track Chronicle = 廃止方向・早めでよい (§11-10) / level 列 = 導出キャッシュとして維持 / digest の正準 = 咀嚼層の器 (§3.2)
- 新実装で旧 path を残さない・env flag 切替禁止 (feedback_no_dead_code_via_flags)

---

## 確定設計 (私 = Fable の裁定)

### 全体方針

生成経路を **整列 (純関数) → 実行 (LLM+保存) → 束ね (帯あふれ)** の三段に分解して世代交代する。M1 claim / S2 ゲート / 台帳結線の外殻は現行のまま、`generate_chronicle` の内側と `ArasujiGenerator` の呼び出し面を差し替える。

```
generate_chronicle (外殻: claim / 確認ゲート / status — 現行維持)
  └─ plan_alignment(messages, episodes, existing_entries)   ← Chunk A (純関数)
       → AlignmentPlan { chunks: [identity | episode_digest | llm_batch] }
  └─ execute_plan(plan, client, conn)                        ← Chunk B
       → チャンクごとに 単一 tx (create + 由来メタ) で確定、進捗を台帳 RESULT_JSON へ
  └─ run_band_overflow(conn, client)                         ← Chunk C
       → 帯 k の被覆合計 > U×B^k なら古い端から連続列を束ねて級 k+1 (親子単一 tx)
```

### D1. 編纂範囲の再定義 (M2-b の解、§4-1)

- **自動経路 (Metabolism/anchor 失効/session close)**: 編纂対象 = **「この Metabolism で退役する新 anchor 位置より古い」全未編纂メッセージ**。窓に残る範囲は圧縮しない。過去の取りこぼし (未編纂の穴) は「新 anchor より古い」に含まれるので毎回自然に拾われる。
- **force (手動 organize-memory)**: 現行どおり全量 (ユーザー明示の全整理)。
- 実装: `generate_chronicle(..., evict_boundary_id=None)` を追加。呼び出し元 `_run_metabolism_locked` が新 anchor 候補 id を渡す。boundary 指定時は `get_messages_for_chronicle` の結果を boundary の created_at 以前に切る。

### D2. 退役境界の episode スナップ (§4-1 × §4-2 の合流)

- **open episode に属するメッセージは退役させない**: `_run_metabolism_locked` の evict_count を「新 anchor が open episode の内部に落ちない最大値」へ切り詰める。episode は退役でも原子 — 「窓から出たのに閉じ判断がまだ」の中間状態を作らない。
- open episode が窓全体を占めるときは evict 0 → Metabolism 見送り (WARN。マラソン会話の窓肥大は §6 の pulse 関節細分 = 後続工程まで引き受ける)。
- closed episode / origin_episode NULL (旧データ・episode 外) は制約なし — どこでも切れる。

### D3. 整列器 `sai_memory/arasuji/alignment.py` (Chunk A の核、新規)

純関数 `plan_alignment(messages, episode_digest_index, min_llm_chars, target_chars) -> AlignmentPlan`。LLM もDB 書き込みもしない — 七原則の回帰テストの主戦場。

1. **セグメント化**: 編纂対象 (D1 で切った未編纂列、created_at 昇順) を `origin_episode` の値で走査し、**同一 episode 連続域**をセグメントにする (NULL は「無帰属」セグメント。時系列で episode が交互に出る場合はその都度切る = 連続束ねのみ §4-5)。
2. **episode_digest チャンク**: digest_ref 確定済み episode のセグメント → 恒等転写チャンク (LLM なし)。**単独で 1 ノード** — 前後の束ねに巻き込まない (§4-4 digest 再圧縮禁止の生成側)。
3. **llm_batch チャンク**: digest の無い連続セグメント列 (会話 episode・無帰属・旧データ) を貪欲に束ねる — **被覆合計が target_chars (= U = 1 万字) に達したら閉じる**。episode 境界は跨いで束ねてよい (§4-2 結合可) が、**episode の途中では閉じない** (分割禁止)。1 episode 単独で target 超過なら単独 1 チャンク (分割せずそのまま — §6 の内部細分は後続)。
4. **identity チャンク (恒等圧縮 §4-3)**: 束ねの結果 min_llm_chars (= 1,000 字) 未満で、かつ隣接に束ねる相手がいない残片 (digest 済みに挟まれた豆粒 / 列の端数で前後に未編纂が続かないもの) → 生のまま級 1 ノード化 (LLM なし)。端数でも min_llm_chars 以上なら llm_batch として圧縮する (地図の無い土地を残さない — 現行の「20 件未満スキップ」は廃止)。
5. 出力チャンクは `kind` / `message_ids` / `episode_refs` / `coverage_chars` / (episode_digest なら `digest_text` の参照) を持つ。

### D4. 実行器とチャンク単位の原子性 (Chunk B、M2-a/M2-d の生成側の解)

- チャンク 1 個 = **単一 tx**: LLM 応答取得 (tx 外) → `create_entry` 相当の INSERT + 由来メタ (下記) を BEGIN〜COMMIT で確定。チャンク間は独立 (途中失敗 = そこまでのチャンクは確定済み、source_ids 冪等で再試行時スキップ — 現行の安全網を維持)。
- **由来メタ (metadata 追加)**: `digest_origin`: `"episode"` (恒等転写) / `"batch"` (LLM 束ね) / `"identity"` (恒等圧縮) — §3.2 の出自区別。`coverage_chars`: 被覆生ログ文字数 (D6 の束ね判定の一次データ)。`episode_refs`: 被覆する episode_ref 列 (unfold 読み口の将来材料)。
- **episode_digest 転写**: digest_ref が指す message の本文をそのまま content に (切り詰めない — feedback_no_truncation)。source_ids = セグメントの message ids。正準は咀嚼層側に移る第一歩 (§3.2)、messages 側 digest 行と digest_ref は不変 (episode_read 読み口を壊さない)。
- **tx 内の重複再検査 (M2-d の代替)**: INSERT 直前に同 tx で「source_ids の先頭 id が既に Lv1 の source に含まれていないか」を検査し、含まれていれば чанк を skip (WARN)。物理 UNIQUE が張れない構造への代替。M1 claim (プロセス間) + adapter._db_lock (プロセス内) + この再検査 (degrade 環境) の三段。
- **失敗の語彙**: チャンク失敗 = run 全体を "failed" で返し anchor 据え置き (現行 S2 と同じ)。確定済みチャンクはそのまま (再試行で二重にならない)。

### D5. session_digest の材料除外 (M2-c の解、1 行 + 回帰)

- `CHRONICLE_EXCLUDED_TAGS` に `'session_digest'` を追加 (`sai_memory/memory/storage.py:1806`)。digest 行は「digest をこう記述したというメタ事実」として messages に残る (§3.2) が、圧縮材料には二度と入らない。
- 会話窓 (Session head/tail) への提示は不変 — 材料除外は get_messages_for_chronicle 系だけに効く。

### D6. サイズ級と帯あふれ束ね (Chunk C の核、§4-6/§4-7)

- **級の定義**: 級 k ノードの標準被覆 = `U × B^(k-1)` 字 (U=10,000 / B=10 → 級 1≒1 万・級 2≒10 万・級 3≒100 万)。level 列 = 級 (導出キャッシュ、読み込み互換)。判定は全て **coverage_chars (被覆生ログ文字数)** — digest 自体の長さではない (LLM 出力のブレに依存しない決定論)。
- **あふれ判定**: 編纂後に帯 k (= level k の unconsolidated ノード列、時系列順) の被覆合計 > `U × B^k` なら、**古い端から**連続ノード列を被覆合計が `U × B^k` (= 級 k+1 の標準被覆) に達するまで取り、LLM で束ねて級 k+1 の親を作る。帯 k+1 も再帰的に判定。
- **壁 (§4-6)**: 束ね中に「level > k のノード」が時系列上に挟まっていたらそこで列を打ち切る (壁は巻き込まない = 旧 Lv3 は再要約されない — 移行の約束の構造的保証)。壁の手前の端数は次回のあふれまで持ち越し。
- **親子の単一 tx (M2-a の解)**: 親 INSERT + 全子の `is_consolidated=1` + `parent_id` UPDATE を **BEGIN〜COMMIT 一つ**で。現行 `generate_consolidated_arasuji` の「親だけ commit 済み」中間状態を構造的に廃絶。
- **§4-7 (最古が黙って落ちない)**: 束ねは常に「古い端から」。生成側は情報を消さない (束ねられた子も DB に残る)。提示側の全期間カバーは読み込み (context.py) の既存予算制がそのまま担う。
- **10 個固定の maybe_consolidate / gap-fill / dismantle は新経路に呼び出しを残さない**: 穴 (過去の未編纂範囲) は D1 で毎回自然に拾われ、独立ノードとして埋まる。時間重複する既存上位ノードとは束ねない (壁) — dismantle の「L2 代表性喪失」問題は両方提示 (情報非重複) で引き受け、複合更新の原子性課題 (M2-e) は経路ごと消滅。

### D7. 帰化バックフィル (§11-3、Chunk C)

- 既存 entries に `coverage_chars` が無ければ埋める: Lv1 = source_ids → messages.content 長合計 (欠損 source は引けた分のみ。全滅時は content 長 × 10 の圧縮率近似で埋め、`coverage_estimated: true` を刻む)。Lv2+ = 子の coverage 合計 (再帰、子欠損は同近似)。
- 実行点: 新経路の入口 (generate_chronicle 内、claim 後) で「coverage_chars 欠落 entry があれば一括計算」— 一回きり・冪等・LLM なし。migration スクリプトは使わない (memory.db は persona ごとに散在、world migrate の管轄外)。
- ※ intent §11-3 の「arasuji_progress から復元」は実査の結果不成立 (arasuji_progress は last_processed 一点のみで範囲を持たない)。source_ids 実測 + 圧縮率近似に置き換える — intent へ実装時確定として反映する。

### D8. Track Chronicle 生成の廃止 (§11-10)

- `generate_track_chronicle` (session_lifecycle.py:1363) と `_run_metabolism_locked` からの呼び出し (2.5 節) を削除。ArasujiGenerator の track 系機能はこの唯一の呼び出し元を失う。
- 既存 Track Chronicle データと読み込み側 (get_track_entries / Track dump) は**残す** — 過去に生成された entries の提示は壊さない。生成が止まるだけ。読み込み側の完全撤去と再訪問題は [track_episode_continuity issue](../issues/track_episode_continuity.md) の管轄。
- 独立コミットに分ける (削除系は差分を混ぜない)。

### D9. 消費者の載せ替え (Chunk B/C に同梱)

- **API 生成ジョブ** (`api/routes/people/arasuji.py` background job): generate_unprocessed 呼び出しを新経路 (plan→execute→band) へ。
- **コスト見積もり** (`sai_memory/arasuji/estimate.py` + generate_chronicle 内の事前計算): 「20 件バッチ数」→「plan_alignment の llm_batch チャンク数 (+ あふれ束ね見込み)」。整列器を dry で呼ぶ一点管理 (現行の二重実装 — generate_chronicle 内のインライン run 計算と estimate — を整列器に統一)。
- **CLI** (`scripts/arasuji/build_arasuji_core.py`): 同上の載せ替え。
- 読み込み側 (context.py / head の chronicle_index / Atlas / recall) は **無変更** — level 列と source_ids の互換を D4/D6 が保っているため。

### D10. 台帳結線 (外殻、現行維持 + 観測強化)

- M1 claim (`metabolism.run`, key=`{persona}:{window_end_id}`) は現行のまま。窓が伸びれば key が変わり失敗窓の再試行が成立する既存設計を維持。
- RESULT_JSON に plan の要約 (`chunks_total` / `chunks_llm` / `chunks_identity` / `chunks_episode` / `bands_consolidated`) を記録 — 「何がどう編纂されたか」の観測点 (W3 教訓①: 回復を当てにする分岐は回復側が観測できなければ嘘)。
- anchor 前進は現行 S2 ゲート (ok/disabled のみ) — **Chronicle commit 後にのみ前進**の順序が M2 本体の解 (DB 跨ぎのため順序+冪等で閉じる。単一 tx は不可能と明記)。

### パラメータ (env、モック検証値を既定に)

| 名前 | 既定 | 意味 |
|---|---|---|
| `SAIVERSE_CHRONICLE_BAND_BUDGET` (U) | 10000 | 級 1 の標準被覆字数 = 帯あふれの基準単位 |
| `SAIVERSE_CHRONICLE_BAND_BASE` (B) | 10 | 級の底 (幾何級数) |
| `SAIVERSE_CHRONICLE_MIN_DIGEST_CHARS` | 1000 | LLM 圧縮する最小被覆 (未満は恒等圧縮) — Track Chronicle 1000 字スキップの一般化 |

---

## 実装チャンク (順序)

- **Chunk A — 整列器** (`sai_memory/arasuji/alignment.py` 新規 + 回帰): plan_alignment 純関数。七原則のうち §4-1(範囲)/§4-2(原子・結合)/§4-3(恒等)/§4-4(digest 単独)/§4-5(連続のみ) をここの純関数テストで固定。D2 の evict スナップ関数もここ (episodes 依存は引数注入)。
- **Chunk B — 実行器と載せ替え** (generate_chronicle 内側 + D4/D5/D9): execute_plan、チャンク単一 tx、由来メタ、digest 除外タグ、evict_boundary、API/CLI/estimate 載せ替え、旧 generate_from_messages/generate_unprocessed の呼び出し元排除。
- **Chunk C — 帯あふれ束ねと帰化** (D6/D7): run_band_overflow、親子単一 tx、壁、バックフィル。§4-6/§4-7 の回帰。
- **Chunk D — Track Chronicle 生成廃止** (D8、独立コミット)。
- 旧経路の削除 (generate_from_messages ほか呼び出し元が消えた関数群) は Chunk B/D の後に dead code として落とす (feedback_no_dead_code_via_flags — flag 切替は置かない)。

## 実装中の裁定・挙動変更 (設計からの差分、レビュー時の注視点)

1. **D8 (Track Chronicle 生成廃止) は本体コミットに統合** — 「新経路があるのに Track だけ旧 ArasujiGenerator を使い続ける」中間状態の方が不健全なため、分離コミット方針を撤回。
2. **session_digest 除外 (D5) は共用フィルタ経由で波及**: `CHRONICLE_EXCLUDED_TAGS` はコア記憶 scene (`_conversation_exclusion`) と自動想起の実会話フィルタ (`real_conversation_filter`) も共用する。digest 行 (システム生成の要約) が「本人の実会話」から外れるのは正しい方向として引き受け (想起は Chronicle 転写後に chronicle 経路で担われる)。
3. **with_memopedia の漸進反映は喪失**: 旧 API ジョブは「バッチごとに Memopedia が育ち次バッチのプロンプトに反映」だったが、新 executor は memopedia_context を実行中固定で受ける。extract_knowledge 自体は batch_callback で従来どおり走る (非推奨・developer mode 限定機能のため許容)。
4. **CLI `--dry-run` の意味変更**: 旧「LLM を呼んで保存だけ抑止 (費用が発生する矛盾)」→ 新「整列計画の表示のみ (LLM なし)」。
5. **API/CLI の batch_size / consolidation_size は受理して無視** (deprecated、422 回避)。frontend の入力フォームは撤去済み (ArasujiViewer)。cost-estimate レスポンスの batch_size は 0 固定 (型互換)。
6. **API 生成ジョブに M1 claim を結線** — 旧実装は claim を通らない別コネクション入口 (監査「全入口は直列化されない」の穴)。Metabolism と同じ kind=`metabolism.run`・同じキー形で claim し、競合時は `window_claimed` エラーでジョブ失敗 (LLM 二重コストの収束)。
7. **LLM ゼロの実行 (identity / episode 転写のみ) は確認ダイアログをスキップ** — 確認は LLM コストへの同意のため。
8. **旧経路の削除**: `ArasujiGenerator` / `generate_from_messages` / `generate_unprocessed` / `maybe_consolidate` / `generate_consolidated_arasuji` / `integrate_gap_fill` / `regenerate_consolidated_content` を撤去 (generator.py は 1314→約 360 行)。`generate_level1_arasuji` + `_format_*` / `_record_llm_usage` は残存 (単一エントリ再生成 = `regenerate_entry_from_messages` と executor/bands/note 系が共用)。storage 側の `dismantle_entry` / `find_covering_entry` 等は API 面として残置 (棚卸しは W12)。
9. **intent §11-3 の「arasuji_progress から被覆復元」は実査で不成立** (arasuji_progress は last_processed 一点のみ) — coverage backfill は source_ids 実測 + 圧縮率 10 倍近似 (`coverage_estimated` マーカー付き) に置き換え。intent へ反映済みであること (記帳タスク)。
10. **env**: `SAIVERSE_CHRONICLE_BAND_BUDGET` / `_BAND_BASE` / `_MIN_DIGEST_CHARS` / `_MAX_BAND_CONSOLIDATIONS_PER_RUN` 新設、`MEMORY_WEAVE_BATCH_SIZE` / `_CONSOLIDATION_SIZE` 廃止 (environment-vars.md 更新済み)。
11. **intent §11-9 (presence 極小 episode の窓占有) は生成側では対応不要と判断**: presence は digest_ref を持たず通常材料 → 豆粒として隣接束ね or 恒等圧縮に自然に吸収される (coverage は実測字数)。「窓予算に数えるか」は読み込み帯レンダラの論点として W14 へ。
12. **issue 消し込み**: `general_chronicle_metabolism_trigger` (押し出し対象だけ編纂 = D1 がそのもの) を解決で archive へ / `chronicle_generation_dual_pipeline` (2026-05-28 解決済み・移動漏れ) を archive へ。`chronicle_cross_thread_mixing` は継承 DAG (W13) 管轄のため現状維持。

## Codex (Sol) レビュー消し込み (2026-07-21、第一巡 10 件 — 全件受諾)

| # | 重大度 | 指摘 | 対応 |
|---|---|---|---|
| 1 | P1 | executor の重複再検査が tx 外で LLM を挟む + API/Metabolism は範囲が違うと別 claim key で並走可 → 同じ先頭 source の Lv1 を二重 INSERT | INSERT 直前 (LLM 後・同一 tx 内) に再検査を追加。事前検査は LLM コスト節約として残置 |
| 2 | P1 | 帯束ねに子の未統合再確認が無く、並走ジョブが同じ子列を選ぶと親が二重生成・parent_id 後勝ち | `_all_children_unconsolidated` を束ね tx 内に追加 — 1 子でも統合済みなら rollback + 放棄 |
| 3 | P1 | plan.chunks 空だと backfill/帯束ねへ到達せず、帯 LLM だけ失敗すると永久に再試行されない (W3 教訓①の再演) | `plan_band_overflow` (dry) が backlog を検出し、plan 空でも帯統合のみの実行に進む。空メッセージ早期 return も統一フローに吸収 |
| 4 | P1 | `llm_calls==0` の確認スキップが帯束ね LLM を数えず、無料チャンクだけでも確認なしで統合 LLM が走る | dry 予測を `estimated_llm_calls` に加算してから確認ゲートを評価 |
| 5 | P1 | 壁で打ち切られた目標未達の列 (`len>=2`) を束ね、小粒の上位ノードを量産 — 設計文書 (持ち越し) と実装の乖離 | `_select_bundle_run` = 目標到達列のみ返す。壁打ち切りは破棄して次の位置から集め直し |
| 6 | P1 | digest 転写の重複防止 `seen` が run 毎リセット — processed 挟みの分裂 episode が二重転写 | `transcribed` 集合を plan_alignment スコープへ持ち上げ全 run で共有 |
| 7 | P1 | CLI が整列前に `--limit/--offset` で生メッセージを切り、episode を分断 (§4-2 違反) | 全量から計画して `truncate_plan(limit)` で切る。`--offset` は deprecated 受理無視。update_progress は計画に載った末尾のみ |
| 8 | P1 | API のキャンセル済み実行を applied/completed で封印 — 最初のチャンク前のキャンセルでも同じ窓が window_claimed で再実行不能 (W3 教訓③違反) | 両入口とも cancelled → `mark_failed("cancelled by user")`。Metabolism は "deferred" (anchor 据え置き)。API はキャンセル後の帯統合もスキップ |
| 9 | P1 | estimate の統合コールが既存の未統合帯と実 coverage を無視 (新規数 ÷ B の近似) | `plan_band_overflow` (実行と同じ選定ロジックの dry) に置換、extra_leaves で新チャンクを加算 |
| 10 | P2 | Lv2+ backfill の部分欠損が過小評価のままマーカーも付かない | 欠損カウントを取り、一部でも欠損があれば `coverage_estimated` を付与 |

消し込み回帰 = alignment +1 / executor +1 / bands +5 / metabolism +2 (計 66 件)。band-only 実行は claim を持たない (並走防御は tx 内再検査、並走時の +1 LLM コールは許容) — 設計判断として記録。

## Codex (Sol) レビュー消し込み (第二巡 6 件 — 全件受諾。第一巡修正の詰め残し)

| # | 重大度 | 指摘 | 対応 |
|---|---|---|---|
| 1 | P1 | 「tx 内再検査」の前に BEGIN が無い — sqlite3 の SELECT は暗黙 BEGIN を張らず、検査は実は tx 外 (check-then-insert が非原子のまま) | `BEGIN IMMEDIATE` (write ロック先取り) を再検査の前に。既に tx 内なら OperationalError を握って参加 |
| 2 | P1 | 束ね側の子検査も同様に非原子 + band-only は claim も無い → 二接続が同時に「全子未統合」を読める | 同じく `BEGIN IMMEDIATE` を子検査の前に — band-only の並走防御がこの原子化で実効化 |
| 3 | P1 | dry 予測が backfill より先に走り、未帰化 DB (coverage 全 NULL) では 0 予測 → 確認スキップ → backfill 後の実 band で未承認 LLM | `_coverage_index` に content 長 ×10 の読み取りフォールバック — dry は近似・実行は backfill 済み実測 |
| 4 | P1 | 安全弁が「成功親数」カウントで、LLM 失敗が続くと予測 (dry) を超えて試行し続ける | attempts (run 選定 = LLM 試行) でカウントする形へ |
| 5 | P1 | API 入口に `if not all_messages` 早期 return が残存 — メッセージ全削除済み DB で band backlog が永久に統合されない | 早期 return 除去 (plan は空 chunks で統一フローに乗る) |
| 6 | P1 | cancelled → failed にしても begin_execution は状態を問わずブロック — 同窓の即時再実行が deferred/window_claimed のまま (第一巡 #8 の裁定「窓が伸びるまで待つ」を Codex が再指摘、裁定を改める) | 両入口を `claim_execution` (failed はキー退避して新規 prepared = 即時再試行可) + `try_mark_running` (prepared 再利用の二重 claim の席取り) へ乗り換え |

第二巡の教訓: **「同一 tx 内」と書く前に、その DB ドライバがどこで tx を開くかを確認する** — sqlite3 (isolation_level 既定) は DML で暗黙 BEGIN、SELECT では張らない。check-then-write の原子化は BEGIN IMMEDIATE の明示が必要。

## Codex (Sol) レビュー消し込み (第三巡 3 件 — 全件受諾。第二巡修正の詰め残し)

| # | 重大度 | 指摘 | 対応 |
|---|---|---|---|
| 1-2 | P1 | `except OperationalError: pass` が「既に tx 内」だけでなく **database is locked (busy_timeout 超過) も飲む** — ロック無しで検査に進み原子化が無効 | `if not conn.in_transaction: conn.execute("BEGIN IMMEDIATE")` へ。tx 内なら参加、locked は raise (executor はチャンク失敗として propagate = 再試行安全 / bands は外側 except で rollback + この束ね見送り) |
| 3 | P1 | dry 予測の content×10 近似と実測 backfill が食い違い、20 倍圧縮の旧 entry では「予測 0 → 早期 return → backfill へ永久に到達しない」 | **backfill を計画・dry 予測より前へ移動** (両入口、テーブル init 直後)。帰化はメタデータ補完で確認ゲート対象外。読み取り近似は防御 + read-only の estimate 用に残置。順序の回帰テスト追加 (backfill → band) |

第三巡の教訓: **例外を「予期した一形態」のために握るときは、その例外型が運ぶ他の形態を列挙してから** — OperationalError は「tx 内」と「locked」を同じ型で運ぶ。状態は例外でなくプロパティ (in_transaction) で判別する。

## Codex (Sol) レビュー消し込み (第四巡 1 件 — 受諾。第三巡修正の詰め残し)

| # | 重大度 | 指摘 | 対応 |
|---|---|---|---|
| 1 | P1 | claim 前へ移した backfill が metadata JSON 全体を read-modify-write — 並走する束ねの `is_consolidated=1` 更新を古い JSON の書き戻しで消す (lost update、W2 第五陣と同型) | backfill 全体を BEGIN IMMEDIATE の単一 tx 化 (`_backfill_coverage_locked` 抽出、成功時は 0 件でも commit で tx を閉じる、失敗時 rollback+raise) — 読みから書きまで write ロック保持 |

巡数の推移: 10 件 (差分全体) → 6 件 (修正差分) → 3 件 (修正の修正) → 1 件 (回収 1 関数) → **0 件 (第五巡 = クローズ)** — 対象の単調縮小で収束。計 5 巡 20 件、受諾 20 / 却下 0。最終回帰 = alignment 20 / executor 10 / bands 18 / metabolism 18 (計 66 件)。

第四巡の教訓: **read-modify-write を「安い掃除」として claim の外に置くときも、それが書き戻す列は並走の書き手と共有されている** — metadata JSON のような複合列は部分更新のつもりの全体書き戻しで他者の commit を消す (W2 第五〜七陣で三度学んだ型の四度目)。SQLite では BEGIN IMMEDIATE の単一 tx が最小の解。

## 検収チェックリスト (レビュー前) — メイン検収済み

- [x] 七原則の named 回帰 (§4-1=test_evict_boundary_limits_compile_range+EvictEpisodeSnapTest / §4-2=TestBundling / §4-3=TestIdentityCompression / §4-4=TestEpisodeDigestChunks+D5 / §4-5=TestRunSplitting+TestWalls.gap / §4-6=TestOverflowTrigger+TestRecursiveBands / §4-7=oldest-first 固定)
- [x] チャンク tx / 親子 tx の失敗注入 (TestPartialFailureIdempotency / TestAtomicity.test_llm_failure_changes_nothing)
- [x] open episode スナップ: evict 0 見送り + 境界切り詰め + closed 無制約 (EvictEpisodeSnapTest 3 本)
- [x] 検算退化: 均一 coverage で級構造に収束 (TestRecursiveBands — B 個相当で 1 親の再帰)
- [x] 壁: 既存上位ノード content 不変 (TestWalls.test_wall_content_never_rewritten)
- [x] W2/W3 教訓: evict boundary は epoch 一貫 (id 変換なし・追加メッセージは boundary より新しく安全側) / 「次回 maybe_run が再試行」は watermark 超過継続で実在・「帯束ね失敗は次回」は毎 Metabolism 走行で実在 / チャンク成功=commit のみが source_ids を封印 (試行は封印しない)
- [x] 既存スイート全緑 (途中版 2884 passed、最終版は下記) + ruff clean (変更ファイル 0 件、残 5 件は変更外の既存 F841)
- [x] 会話前 anchor 失効経路 (runtime_context) と session close 経路 (gold_panning) の Track 呼び出し残骸を除去 — memory の「実行点は 2 つ」警告どおり grep で発見
