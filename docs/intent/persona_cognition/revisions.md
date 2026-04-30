# 改訂履歴

**親**: [README.md](README.md)

旧 `persona_cognitive_model.md` (Intent A) と `persona_action_tracks.md` (Intent B) の v0.1〜v0.14 改訂差分を集約する。確定文書 (`01_concepts.md` / `02_mechanics.md` / `03_data_model.md` / `04_handlers.md`) からは「v0.X で確定」「v0.Y で改訂」等の差分情報を取り除き、ここに集約する。

設計判断の経緯 (なぜそう変えたか) を追跡する目的。

---

## Intent A: persona_cognitive_model.md の改訂

### v0.16 (2026-04-30) — メタ判断 Pulse の per-persona 直列化

**確定事項**:

- 同一ペルソナのメタ判断 Pulse は同時 1 本に制限する。`MetaLayer` が persona_id ごとの `threading.Lock` を保持し、`on_track_alert` / `on_periodic_tick` の両入口で取得待ちする
- 競合時は **wait** で確定 (skip しない)。理由: alert を skip すると即応イベントを取りこぼし、定期 tick を skip するとメインキャッシュ TTL 切れを誘発する
- 別ペルソナ同士は Lock が独立しているため並列実行可能 (per-persona 粒度)
- chat thread のブロックは一時的に許容。将来「安全な中断機構」を作る意思は持つ
- `02_mechanics.md` §"メタ判断 Pulse は同時 1 本 (per-persona 直列化)" を追加

**改訂理由**:

Phase C-2 のテスト中に「pending と思って pause したら裏で alert になっていた」現象を観測。原因: alert observer (chat thread 経由) と AutonomyManager 定期 tick (background thread 経由) が別 thread で同じ persona に対するメタ判断 Playbook を起動し、それぞれが独立した snapshot を見て Track 操作を発動していた。

不変条件 11 ("メタ判断 = ペルソナ自身の思考の流れ 1 本") を構造で守るには、入口での直列化が必要だった。

### v0.15 (2026-04-30) — メタ判断を独白 + /spell 方式に回帰

**確定事項**:

- メタ判断 LLM は **構造化出力 (response_schema) を使わない**。自然言語の独白の中に `/spell <name> ...` を埋め込んで Track 操作を発動する形式に統一
- 旧 4 値 enum (`continue`/`switch`/`wait`/`close`) と `meta_judgment_dispatch` ツールを廃止
- scope 昇格 (`'discardable'` → `'committed'`) は Track 切替系スペル発動時に `_track_common._maybe_promote_meta_judgment` が実施。ツール経由ではなく「Track 切替スペル発動 = 判断確定」と解釈
- 「アクティブ Track なし状態に遷移」は `/spell track_pause` のみ発動 (新規 activate しない) で表現
- `02_mechanics.md` から response_schema セクションを削除し、独白 + スペル方式の応答形式 + scope 昇格機構の説明に差し替え

**改訂理由**:

v0.12〜v0.14 で構造化出力ベースに走った結果、以下 3 つの問題が顕在化:

1. **JSON 混入によるメインキャッシュ汚染** (不変条件 11 違反): メタ判断ターンは [B] 移動時にメインキャッシュに乗る。一度 JSON が混入したキャッシュ末尾を持つ会話は、以降のメインライン発話も JSON 化する副作用が出る。intent A の「メタ判断 = ペルソナ自身の思考の流れ」と矛盾していた
2. **マルチプロバイダ互換性の制約**: Gemini SDK は `any_of` のみ、OpenAI strict は anyOf 非対応、Anthropic は anyOf 16 個制限と、プロバイダ毎の差が大きい。構造化出力依存だとペルソナのモデル選択が制約される
3. **wait/close enum の冗長**: 4 値構造は wait/close が switch のサブセットでしかなく、「アクティブなし状態へ遷移」の選択肢が enum で表現されていなかった

intent docs 自身に矛盾が含まれていた (本文は「独白 + スペル」原則、response_schema セクションは構造化出力) ことを 2026-04-30 のデバッグセッションで発見し、本来の設計に戻すと同時に response_schema 関連の記述を撤去した。実装上、SEA runtime のスペルループ機構 (`_run_spell_loop`) が既に Playbook の自然言語 LLM ノードに対して動くため、Playbook 側の変更だけで切り替え可能だった。

### v0.14 (2026-04-29) — ライン 3 軸独立化 + 7 層ストレージモデル

**確定事項**:

- **ライン (Line) の 3 軸独立化**: モデル/キャッシュ種別 (メイン/サブ) × 呼び出し関係 (親/子) × Pulse 階層位置 (起点/入れ子) の 3 軸に整理。旧 v0.8〜v0.13 で混在していた語義 (「メインライン = 重量級 + Track 横断」等) を分離
- **7 層ストレージモデル**: メッセージ・思考・ログを 7 つの層 (メタ判断ログ / メインキャッシュ / Track 内サブキャッシュ群 / 入れ子一時 / Track ローカルログ / SAIMemory / アーカイブ) で整理。タグベース管理の限界を解消
- **「呼んだラインの記録レイヤーに従う」原則**: Spell loop 等の記録は呼び出し元のラインに従う (固定タグ排除)
- **メタ判断フロー再定義**: メタ判断は Track 内メインラインからの一瞬の分岐として動く。継続時は分岐ターン破棄 + メタ判断ログ領域に保存、移動時は分岐ターンが新 Track の冒頭来歴に。メインキャッシュは Track 横断 1 本を維持
- **メタ判断ログ独立領域**: 全メタ判断結果を独立保存し、次のメタ判断時に参考情報として動的注入。判断の連続性を確保
- **`report_to_main` → `report_to_parent` 改名**: 親が必ずメインラインとは限らないため
- **不変条件 12 新規**: 親-子ラインの寿命関係 (子は親の中で完結)

**改訂理由**:

旧 v0.13 までの「メインライン = 重量級モデル + Track 横断混合キャッシュ」「サブライン = 軽量モデル + Track 内連続キャッシュ」は**役割と一体**で語っていた。3 つの軸が混ざっていたため、「親サブから子メインを呼ぶ」「親サブから子サブを呼ぶ」のような組み合わせを論理的に表現できなかった。v0.14 で 3 軸を分離。

旧 v0.12〜v0.13 の「軽量で要約 → 重量級で独立判断」の 2 段階フローはコスト効率が悪かった。Track ごとに重量級キャッシュを別建てするとコスト破産する。v0.14 で「メインキャッシュ Track 横断 1 本 + メタ判断ログ独立領域 + commit/discard 機構」に再設計。

### v0.10〜v0.13 (2026-04-28) — Pulse 階層と Track 特性の整備

**確定事項**:

- **v0.10**: Track 特性 / Track パラメータ / 内部 alert / スケジュール統合方針の導入
- **v0.11〜v0.13**: メインラインの Pulse 開始プロンプト構成 (固定/動的分離) / Pulse 階層 (メインライン Pulse / サブライン Pulse) / 7 制御点

**改訂理由**:

「Pulse」が単一概念で扱われていたが、実際にはモデル種別とキャッシュ管理の単位が違うため 2 階層に分離する必要があった。これにより「Claude メイン + ローカルサブ」のような環境別最適化が可能に。

### v0.9 (2026-04-28) — 永続 Track / alert 状態 / ACTIVITY_STATE / ライン分岐仕様

**確定事項**:

- **永続 Track の導入**: `is_persistent=true` で完了/中止しない Track。対ユーザー会話 Track (ユーザーごと 1 個) + 交流 Track (ペルソナにつき 1 個)
- **alert 状態の導入**: pending と running/waiting の中間、可及的速やかに対応が必要
- **ACTIVITY_STATE 4 段階**: Stop / Sleep / Idle / Active で旧 INTERACTION_MODE を置き換え
- **`SLEEP_ON_CACHE_EXPIRE` フラグ**: API 料金保護
- **`SubPlayNodeDef.line` フィールド**: "main"|"sub"
- **サブライン分岐 = 親 messages のコピー** (完全独立ではない)
- **`output_schema` の `report_to_main` 必須化** (`can_run_in_sub_line=true` の Playbook で)

**改訂理由**:

ユーザーとの長期的関係性を「永続 Track」として明示することで、再会時の文脈復元が自然になる。

`alert` 状態の導入により、メタレイヤーが「すぐ対応すべき / 後回しでいい」を判断できる粒度になった。旧モデルでは pending と running の二択しかなく、ユーザー発話のような即応すべきイベントの優先度を表現しづらかった。

### v0.8 (2026-04-28) — Note / 行動 Track / Line / ペルソナ認知モデル基盤

**確定事項**:

- **Note 概念の導入**: 関心の固まりを表す単位。Memopedia ページ + メッセージ群を束ねる
- **Note の type は 3 種類のみ**: person / project / vocation
- **行動 Track と SAIMemory thread の分離**: 3 人会話問題の解決
- **Line (ライン) の導入**: メインライン / サブライン / モニタリングライン
- **メタレイヤーは Playbook 内 LLM ノードで実装** (Phase C-1 別系統メッセージは廃止)
- **Track 種別ごとに専用 Playbook を新規作成** ((a) 路線)

**改訂理由**:

3 人会話で「対 A」「対 B」両方の Track に同じメッセージを書き込む必要があるという問題から、初期案の「track_id = thread_id」を撤回。Note 概念で多対多を実現。

### v0.6〜v0.7 (2026-04-28) — Track 種別整理と Handler パターン

**確定事項**:

- Track 種別を `track_type` で表現
- Handler パターンを Track 種別ごとに繰り返し適用
- 種別ごとの追加情報は `action_tracks.metadata` JSON に格納 (早すぎる正規化を避ける)
- Track 特性レイヤーは TrackManager 変更なしで実装可能に

**改訂理由**:

新しい Track 種別を追加するたびに TrackManager に手を入れるのは責務肥大化を招く。Phase C-1 で確立した Handler パターン (UserConversation / Social) を繰り返し適用する形で拡張可能にした。

### v0.5 (2026-04-25) — メインサイクルと Track 内動作パターン

**確定事項**:

- メインライン = メタレイヤー + Track 内重量級判断 (同じキャッシュ連続)
- サブライン = アクティブ Track の Playbook 実行
- Track 内動作 3 パターン: 他者会話 / タスク遂行ループ / 待機
- モニタリングラインは Track ではない、独立した並列ラインとして将来追加

### v0.2〜v0.4 (2026-04-25) — メタレイヤー / 状態モデル / 応答待ち統合

**確定事項**:

- AutonomyManager は「責務再配置と拡張」 (取り壊しではない)
- メタレイヤーから ExecutionRequest 投下で線切り替えが成立
- 状態モデル: running / pending / waiting / unstarted / completed / aborted の 6 状態 + `is_forgotten` 直交フラグ (v0.4)
- 「実行中は 1 本」は `track_activate` の実装で自動保証
- 応答待ちは SAIVerse 側自動ポーリング → イベント通知でメタレイヤー判断
- 多重応答時は新しい Track 優先

---

## Intent B: persona_action_tracks.md の改訂

### v0.11 (2026-04-29) — 7 層ストレージのテーブル化 + handoff 解消

**確定事項**:

- **7 層ストレージのテーブル対応**: 各層を本ドキュメントのテーブル設計にマッピング
- **`meta_judgment_log` テーブル新設**: メタ判断の全履歴を独立保存
- **`track_local_logs` テーブル新設**: Track 内のイベント・モニタログ・起点サブの中間ステップトレース
- **`messages` メタデータ拡張**: `line_role` / `line_id` / `scope` / `paired_action_text` カラム追加
- **`report_to_main` → `report_to_parent` 改名**: 親が必ずメインラインとは限らないため
- **ライン階層管理機構の最小実装**: `PulseContext._line_stack` で親子関係を追跡
- **Spell loop 保存方針**: 「呼んだラインの記録レイヤーに従う」原則。`tags=["conversation"]` 固定を廃止
- **action 文ペア保存方針**: action 文を user role 単独保存せず、応答メッセージの `paired_action_text` に紐付け
- **Pulse Logs の役割縮退**: 実行トレース専用へ
- **handoff 3 経路問題の解決**: 経路 A (Spell loop) / B (`_emit_say` で `speak: false` を skip) / C (action 文ペア保存) を Phase 0 タスクとして明文化

**改訂理由**:

handoff 観察記録 (`handoff_track_context_management.md`) で報告された多重記録問題が、Spell loop / `_emit_say` / action 文の保存先がバラバラだったことに起因していた。Intent A v0.14 の 7 層ストレージモデルを実装側に展開する形で、テーブル設計と保存方針を整理。

### v0.10 (2026-04-28) — Pulse スケジューラの責務分離

**確定事項**:

- Pulse スケジューラを 2 系統 (MainLineScheduler / SubLineScheduler) に分離
- Handler に v0.10 拡張属性追加 (`default_pulse_interval` / `default_max_consecutive_pulses` / `default_subline_pulse_interval`)
- 7 制御点の実装場所明確化 (action_tracks.metadata + 環境変数 + Handler 属性 + モデル設定)
- AutonomyManager は MainLineScheduler に再配置
- 環境別デフォルト値 (Pattern A/B/C) を明示
- Phase C-3 を C-3a/b/c/d に分割

**改訂理由**:

メインライン Pulse とサブライン Pulse の頻度制御を 1 つのスケジューラで管理するのは無理があった。責務を分離して各 Scheduler を独立実装することで、環境差 (Claude / ローカル / 混在) を仕様変更なしで吸収できる構造に。

### v0.9 (2026-04-28) — Playbook ノードの line フィールド + 段階廃止計画

**確定事項**:

- `SubPlayNodeDef.line: "main"|"sub"` フィールド追加 (デフォルト "main")
- 最初に呼ばれる Playbook はメインライン強制
- サブライン分岐 = 親 messages のコピー、軽量モデル実行
- サブライン完了時に `report_to_main` がメインラインに system タグ付き user メッセージとして append
- `output_schema` の `report_to_main` を `can_run_in_sub_line=true` の Playbook で必須化
- 旧 `context_profile` / `model_type` / `exclude_pulse_id` を段階的に廃止 (C-2a → C-2b → C-2c)
- Phase C-1 MetaLayer は alert ディスパッチ役へ縮退、判断ロジックは Playbook へ移植
- 完全独立コンテキスト (worker 系) は本ライン仕様の上で将来別途実装

### v0.7〜v0.8 (2026-04-28) — Track 特性レイヤー + Pulse プロンプト構造

**確定事項**:

- Track 特性レイヤーは Handler パターンの繰り返し適用で実装する (TrackManager は変更しない)
- 種別ごとの追加情報は `action_tracks.metadata` JSON に格納 (早すぎる正規化を避ける)
- Track パラメータは `metadata.parameters` に連続値として持つ
- 内部 alert は Handler の `tick()` メソッド内で判定 + 既存 `set_alert` 発火
- Handler tick は SAIVerseManager の background loop に統合 (`SAIVERSE_HANDLER_TICK_INTERVAL_SECONDS`)
- メタレイヤーには `on_periodic_tick` 入口を追加、`on_track_alert` と同じ判断ループを共有
- 「Pulse 完了直後にメタレイヤー起動」は **採用しない** (ユーザー応答待ち優先)
- ScheduleManager は段階的に Track 特性に吸収、v0.4.0 で完全移行
- Track 種別ごとに専用 Playbook を新規作成する方針 ((a) 路線)
- Handler に `pulse_completion_notice` 文字列 + `post_complete_behavior` 列挙
- Pulse プロンプト = 固定情報 (初回のみ先頭) + 動的情報 (毎 Pulse 末尾)

### v0.4〜v0.6 (2026-04-25〜2026-04-28) — 状態モデル / 永続 Track / 多者会話

**確定事項**:

- 状態モデル: `running` / `pending` / `waiting` / `unstarted` / `completed` / `aborted` の 6 状態 + `is_forgotten` 直交フラグ (v0.4)
- メタレイヤーのトラック管理は 10 個のツール群 (`track_*`) (v0.4)
- 応答待ちは SAIVerse 側自動ポーリング → イベント通知でメタレイヤー判断 (v0.4)
- 多重応答時は新しい Track 優先 (v0.4)
- 永続 Track (`is_persistent=true`) の導入: 対ユーザー会話 + 交流 Track (v0.6)
- 状態モデルに `alert` 追加 (v0.6)
- `output_target` フィールド追加 (v0.6)
- 「対ペルソナ会話 Track」は持たない (Person Note + 交流 Track の組み合わせ) (v0.6)
- `ACTIVITY_STATE` 4 段階 (v0.6)
- 多者会話のループ防止: audience 厳格 + メタレイヤー判断 + 環境変数によるヒント (v0.6)

### v0.3 (2026-04-25) — Track と thread の分離 + Note 概念の導入

**確定事項**:

- track_id は独立した UUID
- Track と thread は別概念、メッセージは thread に物理保存
- Note を介してメッセージのメンバーシップを多対多管理
- Note の type は person / project / vocation の 3 種類のみ
- audience による自動 Note メンバーシップ生成
- メンバーシップ付与は Metabolism 時に後付け
- 再開時は起源 Track の認識回復が主、他 Track 由来の情報は Note 差分として event entry で挿入

**改訂理由**:

3 人会話で「対 A」「対 B」両方の Track に同じメッセージを書き込む必要があるという問題から、v0.2 で確定した「track_id = thread_id」を撤回。Note 概念導入で多対多を解決。

### v0.2 (2026-04-25) — 既存資産の責務再配置

**確定事項**:

- AutonomyManager は「責務再配置と拡張」
- メタレイヤーから ExecutionRequest 投下で線切り替えが成立
- 既存 thread metadata と range_before/range_after は参照可能な仕組みとして残る

---

## Phase 番号の変遷

旧ドキュメントには複数系統の Phase 番号が混在していた。新ディレクトリでは Phase 1〜6 の線的順序に集約。

### 旧 Intent B 由来: Phase 0 / C-1 / C-2 / C-3

`persona_action_tracks.md` で使われていた Phase 番号。C は Cognitive の C と推測されるが、明示的な定義はなかった。

| 旧称 | 内容 | 新 Phase |
|------|------|---------|
| Phase 0 (P0-1〜P0-7) | handoff 3 経路問題の解消 | Phase 1 |
| Phase C-1 | MetaLayer / Track 基盤 | Phase 2 |
| Phase B-X | social_track_handler 雛形 | Phase 2 |
| Phase C-2a | line / context_profile DEPRECATED 仕様の追加 | Phase 3 |
| Phase C-2b | 既存 Playbook の改修 | Phase 3 残件 |
| Phase C-2c | 旧仕様の削除 | Phase 3 残件 |
| Phase C-3a | Handler v0.10 拡張属性追加 | Phase 4 |
| Phase C-3b | SubLineScheduler 新設 | Phase 4 |
| Phase C-3c | AutonomyManager → MainLineScheduler 再配置 | Phase 4 残件 |
| Phase C-3d | ConversationManager 関係整理 | Phase 4 残件 |

### 旧 unified_memory_architecture.md 由来: Phase 1〜4

`unified_memory_architecture.md` v3 で使われていた Phase 番号。認知モデルとは別系統だが命名衝突していた。

| 旧称 | 内容 | 新 Phase での扱い |
|------|------|----------------|
| Phase 1 (実装済み) | pulse_logs / Important フラグ / 自動タグ付け / サブエージェント隔離 | unified_memory_architecture 側で管理 |
| Phase 2 (次) | 統一記憶探索 + 記憶基盤強化 | unified_memory_architecture 側で管理 |
| Phase 3 (自律稼働バイオリズム) | 1 時間サイクル | unified_memory_architecture 側で管理、認知モデル Phase 4 と連携 |
| Phase 4 (構想) | 恒常入力処理サブモジュール (カメラ、X 等) | 認知モデル Phase 6「モニタリングライン」に吸収 |

### 直近 (2026-04-29 マージ): Phase 1.1〜1.4

`f6d555b` コミットで導入された Phase 番号 (「認知モデル Phase 1.1〜1.4」)。

| 旧称 | 内容 | 新 Phase |
|------|------|---------|
| Phase 1.1 | Pulse-root context 構築機構 + Handler.track_specific_guidance | Phase 2 |
| Phase 1.2 | meta_judgment.json + meta_judgment_dispatch.py、Playbook 経由パス | Phase 3 |
| Phase 1.3 | scope='discardable'/'committed' 機構、scope 昇格 SQL UPDATE | Phase 1 |
| Phase 1.4 | context_profile / model_type DEPRECATED 宣言 | Phase 3 |

---

## 命名衝突の経緯

旧 Phase C-1〜C-3 と Phase 1.1〜1.4 が並存した時期 (2026-04 後半) があり、ドキュメント参照が複雑化した。本再構造化 (2026-04-30) で Phase 1〜6 に統一し、すべての旧称を「旧称マッピング」で参照可能にした。

---

## 関連ドキュメント

- [README.md](README.md) — 全体俯瞰 (旧称マッピング含む)
- (旧) `persona_cognitive_model.md` v0.14 — 整理完了まで残置
- (旧) `persona_action_tracks.md` v0.11 — 整理完了まで残置
- `handoff_track_context_management.md` — Phase 1 (旧 Phase 0) のもとになった観察記録
