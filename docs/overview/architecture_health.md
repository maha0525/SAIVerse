# SAIVerse アーキテクチャ健診レポート

> **ステータス**: v1.0 (2026-07-06 初版)
> **対象読者**: まはー本人・エア（将来のセッション含む）
> **目的**: リポジトリ全体の構造リスクを一度棚卸しし、「ついで直し」「触る前の準備リファクタ」（CLAUDE.md の Continuous Refactoring 節）の判断の下敷きにする
> **使い方**: サブシステムに大きい変更を入れる前に、該当する所見（§3）を確認する。全面リファクタの計画書では**ない**（§5 参照）
> **更新**: 所見が解消されたら該当項目に取り消し線 + 解消日を記す。構造が大きく変わったら再健診

---

## 1. 総合評価

**結論: 健全。負債は「拡散」ではなく「集中」しており、場所が特定できている。**

このコードベース（Python 614 ファイル / 約 17.8 万行、frontend TS/TSX 約 3.1 万行）は、
個人開発の増改築としては例外的に管理が行き届いている:

| 観点 | 測定値 | 評価 |
|---|---|---|
| lint (ruff) 指摘 | 全体で 10 件 | ◎ ほぼゼロ |
| TODO / FIXME / HACK コメント | 全体で **1 件** | ◎ 「後で直す」の押し込みがない |
| テスト | `tests/` に 129 ファイル | ◎ 主要サブシステムを広くカバー |
| 既知負債の文書化 | `docs/issues/` 66 件 + landscape §9 死んだ概念 | ◎ 負債が「知られている」 |
| 概念⇄実装の対応文書 | landscape / concepts / intent 三層 | ◎ 大掃除が必要になる典型原因（Why の喪失）を既に回避 |

一方で、負債は数カ所に**集中**している。最大のものは
(1) `sea/runtime_llm.py` の巨大閉包、(2) SEARuntime の二重責務、
(3) 横断ユーティリティの配置に起因する全パッケージ循環、(4) frontend `page.tsx` の神ページ化。
いずれも「今すぐ壊れる」類ではなく、**該当区画を次に触るときの事故率と作業コストを引き上げる**類のリスク。

---

## 2. 機械測定サマリ

計測方法: git 管理下の .py を AST 解析（import グラフ / 関数・クラス長）。2026-07-06 時点、ブランチ `feature/autonomous-behavior-v2`。

### 規模ホットスポット（ファイル）

| ファイル | 行数 | 中身 |
|---|---|---|
| `sea/runtime_llm.py` | 3,781 | LLM ノード実行 + Spell loop + streaming。**§3.1** |
| `sea/runtime.py` | 2,888 | SEARuntime クラス（90 メソッド）。**§3.2** |
| `saiverse/saiverse_manager.py` | 2,455 | SAIVerseManager（115 メソッド + 10 mixin）。**§3.5** |
| `llm_clients/gemini.py` | 2,116 | GeminiClient（`generate` 538 行 / `generate_stream` 329 行） |
| `saiverse_memory/adapter.py` | 2,087 | SAIMemoryAdapter（63 メソッド） |
| `tools/mcp_client.py` | 2,063 | MCPClientManager |
| `manager/items.py` | 2,050 | ItemService（52 メソッド） |
| `frontend/src/app/page.tsx` | 3,092 | フロントの神ページ。**§3.4** |

### 規模ホットスポット（関数）

120 行超の関数は全体で 111 個。上位は:

| 行数 | 場所 | 関数 |
|---|---|---|
| 1,616 | `sea/runtime_llm.py:2142` | `lg_llm_node` 内の閉包 `node` |
| 602 | `tools/utilities/memory_settings_ui.py:1054` | `create_memory_settings_ui` |
| 538 | `llm_clients/gemini.py:1100` | `GeminiClient.generate` |
| 529 | `sea/runtime_llm.py:1185` | `_run_spell_loop` |
| 398 | `sea/runtime_context.py:46` | `prepare_context` |
| 391 | `saiverse/saiverse_manager.py:101` | `SAIVerseManager.__init__` |

### パッケージ間依存

パッケージ単位の強連結成分（SCC）検出の結果、**主要 13 パッケージが 1 つの循環に全部入り**:

```
api <-> builtin_data <-> database <-> llm_clients <-> manager <-> persona
    <-> phenomena <-> sai_memory <-> saiverse <-> saiverse_memory
    <-> scripts <-> sea <-> tools
```

fan-in（被 import 数）上位: `saiverse` 480 / `tools` 344 / `database` 319 / `sai_memory` 251。
モジュール単位の最頻 import 先: `database.models` (231) / `tools.context` (133) / `tools.core` (110)。
循環の実体と処方は **§3.3**。

---

## 3. 所見（優先度順）

優先度の意味: P1 = 該当区画を次に触る前に準備リファクタを検討すべき / P2 = 触るタイミングで段階的に解消 / P3 = 認識しておけばよい・谷間タスク向き。

### 3.1 [P1] `sea/runtime_llm.py` — 1,616 行の閉包 `node`

**症状**: `lg_llm_node`（`runtime_llm.py:2141`）はノード実行関数のファクトリで、
返される閉包 `node` が 1,616 行ある。中に LLM 呼び出し・streaming 分岐・
Spell loop 接続・cache 処理・emit（発話イベント）・usage 記録・エラー処理が同居する。
同ファイルの `_run_spell_loop`（529 行）も同種。

**なぜ危ないか**:
- ここは **Beat 生成の心臓部**（landscape §4）。自律行動 v2 でも確実に触る場所
- 閉包内ローカル変数が数十個あり、部分テストが書けない（テストは外側からの結合テストのみ）
- 「1 行直すのに 1,600 行分の文脈を頭に載せる」状態で、修正事故率が構造的に高い
- Beat が型を持たない問題（`docs/issues/beat_concept_not_typed_in_implementation.md`）の根本原因がここ。
  Beat の実体（`full_merged_text` / `final_continuation`）がこの閉包のローカル変数として埋まっているため、型を与える場所がない

**処方**（次に SEA 区画を触る前の準備リファクタとして）:
1. 閉包が抱える状態を dataclass（例: `BeatExecution` — Beat 型導入と兼ねる）に括り出す
2. 段階分割: ①メッセージ構築 → ②LLM 呼び出し + streaming → ③Spell loop → ④emit / 記録、を
   `BeatExecution` を受け渡すメソッド群に切る（挙動は変えない・機械的分割に徹する）
3. 分割後に各段の単体テストを足す
- 一括でなく「今回触る段だけ括り出す」の反復でよい。①〜④の縫い目は現コードの節理に沿っており、途中で止めても壊れない

**トリガー**: 自律行動 v2 実装で Spell loop / Beat 周りに入るとき。それより先に軽微修正でここに入る場合も、触る段だけの括り出しを検討する価値がある。

**→ 分割設計書あり**: [`docs/issues/runtime_llm_node_split_design.md`](../issues/runtime_llm_node_split_design.md)（重複インベントリ・段階手順・不変条件11項まで確定済み。実装はこれに従う）

### 3.2 [P1] SEARuntime の二重責務 — Playbook 実行エンジンと記憶ライフサイクルの同居

**症状**: `SEARuntime`（`sea/runtime.py`、2,829 行 / 90 メソッド）に、
性質の異なる 2 系統が同居している:

- **Playbook 実行エンジン**: `_lg_*_node` 群、LangGraph コンパイル、structured output 処理、LLM クライアント選択
- **記憶ライフサイクル**: Anchor 管理（`_load_anchors` / `_touch_anchor_after_llm_call` 等 8 メソッド）、
  Metabolism（`_maybe_run_metabolism` / `_run_metabolism`）、Chronicle 生成（`_generate_chronicle` 262 行、
  `_generate_track_chronicle`）、context 構築（`_prepare_context`）

**なぜ危ないか**:
- landscape ではこの 2 つは**別の概念層**（§4 行動 と §6 短期記憶と節目）。概念地図と実装の対応が
  ここでだけ崩れており、「Metabolism を直したい」人が Playbook 実行エンジンの中を探すことになる
- **Session 概念（`session.md` 起草中）の実装先が無い**。「Session という統一制御単位はコードに存在しない」
  （landscape §6）のは、置くべき場所が SEARuntime の中に埋まっているから。このままだと Session 実装が
  SEARuntime をさらに肥大させる

**処方**: Anchor / Metabolism / Chronicle / context 構築系を `SessionLifecycle`（仮称）として
`sea/` 内の別モジュールに抽出する。SEARuntime はそれを保持して委譲。
これは単なる掃除ではなく **session.md 実装の受け皿づくり**になる — 抽出したクラスが
そのまま「Session 統一制御単位」に育つ。

**トリガー**: session.md（v0.1 起草中）を実装に移すとき、その第一歩として。記憶アーキテクチャ v2 の Phase 0（自律 Pulse の Chronicle 欠落修正）で Chronicle 系に入るなら、そのときでも良い。

**→ 分割設計書あり**: [`docs/issues/session_lifecycle_extraction_design.md`](../issues/session_lifecycle_extraction_design.md)(移動対象一覧・外部呼び出し元インベントリ・委譲シム方式まで確定済み。実装はこれに従う)

### 3.3 [P2] 全パッケージ循環 — 横断ユーティリティが `saiverse` パッケージに居る

**症状**: 主要 13 パッケージが 1 つの import 循環に入っている（§2）。実行時は関数内 lazy import で
回避されており、現状動いている。循環を作っている「下層 → 上層」の逆流 import の実体:

| 逆流 | 実体 | 引かれているもの |
|---|---|---|
| `llm_clients` → `saiverse` | 17 箇所（`gemini.py` はモジュールトップで） | `media_utils` / `media_summary` / `llm_router` / `logging_config` / `model_configs` |
| `llm_clients` → `tools` | 7 箇所 | `OPENAI_TOOLS_SPEC` / `GEMINI_TOOLS_SPEC` / `tools.context` |
| `sai_memory` → `saiverse` | 4 箇所 | `usage_tracker` / `model_configs` / `references` |
| `sai_memory` → `scripts` | **`arasuji/storage.py:797` → `scripts.arasuji.build_arasuji_core`** | 再生成ロジックがライブラリでなくスクリプト側に居る |
| `manager` → `api` | `runtime.py:13` → `api.deps.avatar_path_to_url` | URL 変換ヘルパ |
| `manager` → `scripts` | `admin.py:28` → `scripts.import_playbook.infer_scope_from_path` | パス→scope 推論 |
| `database` → `saiverse` | 3 箇所 | `__version__` / `data_paths` / `model_configs` |

**なぜ危ないか**:
- **根本パターンは 1 つ**: `model_configs` / `logging_config` / `media_utils` / `usage_tracker` /
  `data_paths` / `references` という**横断ユーティリティが、アプリ本体パッケージ `saiverse/` の中に
  住んでいる**。下の層がこれらを使うたびに上向きの辺が生える
- 実害は今は小さい（lazy import で動く）が: ①import 順序に敏感で、モジュールトップに import を 1 つ
  足しただけで循環 import エラーが顕在化しうる ②`sai_memory` を独立ライブラリとして切り出す・
  `llm_clients` を他プロジェクトで再利用する、が構造的に不可能 ③`scripts/`（本来 leaf）が
  ライブラリから import される 2 件は、スクリプト整理・移動が本体を壊す地雷になっている

**処方**（段階的・触ったついでに 1 辺ずつ）:
1. **逆流の悪質な 2 件を先に**: `scripts.arasuji.build_arasuji_core` の再生成ロジックを
   `sai_memory/arasuji/` 側へ移す（scripts はそれを呼ぶだけにする）。`manager → api.deps` /
   `manager → scripts.import_playbook` も関数の移動だけで切れる
2. **横断ユーティリティの leaf パッケージ化**: `saiverse/` から `model_configs` / `logging_config` /
   `media_utils` / `media_summary` / `usage_tracker` / `data_paths` / `references` を
   「どこからも import してよい」leaf パッケージ（例: `saicore/`）へ移す。旧パスに
   re-export シムを置けば一括変更は不要で、利用側は触ったついでに書き換える
3. `llm_clients → tools`（TOOLS_SPEC）は「ツール仕様の定義場所」の問題。仕様定義を leaf に置き、
   `tools/` と `llm_clients/` の双方がそれを見る形にする

**トリガー**: 1 は谷間タスクで即可能（小さく独立）。2 は新しい横断ユーティリティを足したくなった時・循環 import エラーを踏んだ時。一括移行はしない。

### 3.4 [P2] frontend `page.tsx` — 3,092 行の神ページ

**症状**: `frontend/src/app/page.tsx` が 3,092 行。チャット UI・サイドバー・モーダル群の
オーケストレーション・WebSocket / API 呼び出し・状態管理が単一コンポーネントに同居（典型的な
Next.js 神ページ）。次点はモーダル群（`MemopediaViewer` 1,388 / `SettingsModal` 1,280 /
`GlobalSettingsModal` 1,152）だが、これらは独立コンポーネントに割れており性質が良い。

**なぜ危ないか**: UI 変更の大半がこのファイルを通る。状態とハンドラの依存関係が追いにくく、
「関係ない機能の state を壊す」型の事故が起きやすい。ダークモード対応漏れ・モーダル ID 整合性
（過去の実事故）もレビュー範囲が広すぎることが遠因。

**処方**: 機能単位の custom hook 抽出（`useChatSession` / `useBuildingState` など）から始め、
表示部は触る機能のものからコンポーネントに切る。一括分割はしない。

**トリガー**: 次にチャット UI 本体へ機能を足すとき、その機能が触る state 群を hook に括り出してから実装する。

### 3.5 [P3] SAIVerseManager — 神オブジェクト（ただし解体は進行中）

**症状**: `SAIVerseManager` は 10 mixin + 本体 115 メソッド + 391 行 `__init__`。
fan-in 480 で全域から参照される。

**評価**: これは「放置された神オブジェクト」ではない。mixin 分割（`manager/*.py`）と
サービスオブジェクト化（`ItemService` / `AdminService` / `RuntimeService`）が既に走っており、
解体の路線は敷かれている。mixin は名前空間を割るだけで結合は残るので、最終形はサービス
オブジェクトへの委譲（composition）に寄せるのが良い。

**処方**: 新規機能をマネージャに足すときは mixin でなくサービスオブジェクト側に置く。
`__init__` 391 行は初期化フェーズごと（DB / ペルソナ登録 / スケジューラ起動 / ゲートウェイ）の
プライベートメソッドに割るだけで読みやすさが大きく変わる（挙動不変・低リスク）。

### 3.6 [P3] `tools/context.py` の contextvars — 暗黙の大域チャネル（設計特性として認識）

**症状**: fan-in 133 で `database.models` に次ぐ被参照。ContextVar 10 本
（persona_id / manager 参照 / pulse_context / LLM messages 等）がツール実行時の暗黙 DI として機能。

**評価**: これは欠陥ではなく**意図した設計**（ツール関数のシグネチャを汚さずに実行文脈を渡す）。
ただし ①テストで patch 必須（`reference_test_infrastructure` に記録済み）②「誰が set して誰が
read するか」がコードから追えない、という特性がある。ContextVar を増やす前に「本当に暗黙で
渡すべきものか」を一度考える、set/read の対応表を `docs/concepts/` のツール解説に持つ、程度で十分。

### 3.7 [P3] 死んだ概念の残骸 — 既知・リスト化済み

landscape §9 の 8 件（ConversationManager / action_handler / BuildingToolLink / working_memory /
note_extractor / task / Emotion / Blueprint）+ `docs/issues/` の dead code 系
（`legacy_action_handler_cleanup` / `phase3_4d_dead_code_removal` / `quarantine_path_dead_code_removal`）。
すべて把握済みで新規発見なし。**谷間タスクの弾倉**として機能している。掃除の際は
`feedback_no_dead_code_via_flags`（env flag で残さない・消すなら消す）に従う。

### 3.8 [P4] 小物（ついで直しで消えるもの)

- **UTF-8 BOM 付きソースが 8 ファイル**: `main.py` / `discord_gateway/permissions.py` /
  `discord_gateway/saiverse_adapter.py` / `builtin_data/tools/__init__.py` /
  `builtin_data/tools/calculator.py` / `scripts/chatlog_fix.py` / `tools/adapters/__init__.py` /
  `tools/adapters/openai.py`。実害は薄いが、AST 解析系ツール（今回の健診スクリプト含む）が
  素朴に読むと落ちる。触ったついでに BOM を外す
- **ruff 指摘 10 件**: F841（未使用変数）8 件ほか。30 分未満で全消しできる規模

---

## 4. 運用への接続 — 「触る前チェック」対応表

CLAUDE.md の Continuous Refactoring 節が定める「着手前の健康診断」で参照する早見表:

| これから触る区画 | 先に見る所見 | 準備リファクタの目安 |
|---|---|---|
| SEA / Spell / Beat / 自律行動 v2 | §3.1, §3.2 | `node` 閉包の該当段の括り出し。Metabolism 系に入るなら SessionLifecycle 抽出 |
| 記憶（Chronicle / Metabolism / Session 実装） | §3.2 | SessionLifecycle 抽出を実装の第一歩にする |
| LLM クライアント / 新プロバイダ | §3.3 | 触るクライアントの逆流 import を leaf 化の作法で書く |
| sai_memory の独立性が絡む作業 | §3.3-1 | `scripts.arasuji` 逆流の解消を先に |
| チャット UI 本体 | §3.4 | 触る state 群の hook 抽出を先に |
| マネージャへの機能追加 | §3.5 | mixin でなくサービスオブジェクトに置く |

---

## 5. この健診がやらないと決めたこと

- **全面リファクタの推奨はしない**。触る予定のない場所の改修はリスクだけ払ってリターンがない。
  すべての処方に「トリガー」（いつやるか）を付けたのはそのため
- **アーキテクチャの作り直し提案はしない**。概念設計（landscape）と実装の対応は §3.2 の一点を除き
  健全で、作り直す理由がない
- 検証コスト（まはーとの共同テスト）を増やす提案を避け、挙動不変の機械的分割・関数移動を基本にした
