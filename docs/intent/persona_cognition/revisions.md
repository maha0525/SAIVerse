# 改訂履歴

**親**: [README.md](README.md)

旧 `persona_cognitive_model.md` (Intent A) と `persona_action_tracks.md` (Intent B) の v0.1〜v0.14 改訂差分を集約する。確定文書 (`01_concepts.md` / `02_mechanics.md` / `03_data_model.md` / `04_handlers.md`) からは「v0.X で確定」「v0.Y で改訂」等の差分情報を取り除き、ここに集約する。

設計判断の経緯 (なぜそう変えたか) を追跡する目的。

---

## Intent A: persona_cognitive_model.md の改訂

### v0.20 (2026-05-01) — line と memorize タグの責務分離 + 入れ子サブライン Spell の Intent 起草

**確定事項**:

- `line_role` / `line_id` / `scope` カラム (Phase 1 実装済) と `metadata.tags` の責務を明確に分離
  - **Line**: メッセージの階層属性と永続性 (= context 構築の主軸)
  - **タグ**: 意味分類のみ (= 検索・recall・連携用、context 構築には関与しない)
- 二重制御 5 件を特定し、移行プラン (段階 4-A〜4-D) を策定
- `/run_playbook` Spell 機構の Intent を起草 (`nested_subline_spell.md` v0.1)
- 揮発設計を line ベースに乗せ直し (旧 `internal` タグでの揮発表現を廃止前提)
- Phase 3 残作業の依存グラフを確定:
  ```
  [line vs タグ整理] → [migrate_playbooks_to_lines.py] → [/run_playbook 実装]
  → [track_user_conversation 書き換え] → [meta_user 廃止] → [実機検証]
  ```

**追加 Intent doc**:

- `nested_subline_spell.md` v0.1 — `/run_playbook` Spell 機構の設計
- `line_tag_responsibility.md` v0.1 — line と memorize タグの責務分離

**改訂理由**:

入れ子サブライン Spell (`/run_playbook`) を実装する前に、まはー指摘で「`line` と `memorize` タグの両方が context 制御に関与している二重制御の問題」が判明。Phase 1 で line_role / scope カラムを追加した時点で「タグ参照を捨てて line 制御に統一する」つもりだったが、移行が中途半端で残っていた。

このまま入れ子サブライン Spell を実装すると「二重制御の上に新機構を積む」ことになり、設計上の負債が増える。先に整理を済ませる判断。

工数見積:
- 完全 line ベース統一案 (タグ全廃): 4 ファイル + 5+ Playbook、2000+ LOC、Phase 3 全翻訳と同規模 → 重すぎる
- 責務分離案 (採用): タグは search / recall 用に残す、context 構築だけ line ベースに統一 → Phase 3 翻訳と一体化で 2〜3 セッション

不変条件 2 (単一主体の記憶), 7 (キャッシュヒット継続), 11 (メタ判断はペルソナ自身の思考) の保証がより厳密になる副作用あり。

### v0.19 (2026-05-01) — Phase 3 翻訳前段の Playbook 整理

**確定事項**:

- 旧自律稼働プロトタイプ用 Playbook 群を一括削除 (`meta_auto`, `meta_auto_full`, `sub_router_auto`, `sub_perceive`, `sub_reaction`, `sub_finalize_auto`, `sub_execute_phase`, `sub_detect_situation_change`, `sub_generate_want`, `wait`)
- テスト用 / 残骸 Playbook を削除 (`meta_websearch_demo`, `detail_recall_playbook`, `meta_agentic`, `agentic_chat_playbook`)
- Spell 階層に置き換え可能な Playbook を削除し、対応するツールに `spell=True` を付与:
  - `memory_recall_playbook` → `memory_recall_unified` Spell (既存)
  - `web_search_step` → `source_web` Playbook (依存していた `deep_research` は `source_web` 呼び出しに切り替え)
  - `uri_view` → `resolve_uri` Spell (新規 Spell 化)
  - `send_email_to_user_playbook` → `send_email_to_user` Spell (新規 Spell 化)
- `web_search_sub` (Phase C-2b 動作確認サンプル) は `phases/sub_line_playbook_sample.md` に内容を保存して本体 Playbook 削除
- `run_meta_auto` 関数 (sea/runtime.py) と関連分岐 (sea/pulse_controller.py の auto-without-meta_playbook 分岐、`_choose_playbook` の `meta_auto` fallback) を削除。auto pulse は `meta_playbook` 必須化
- `ConversationManager` (saiverse/conversation_manager.py) を no-op 化。Building 内 AI 自律会話は PulseScheduler + `track_autonomous` 経由に統一済みのため、旧プロトタイプの周回駆動は不要
- 削除を反映してテスト類を整理 (`tests/sea/test_runtime_regression.py` の `run_meta_auto` テスト、`tests/test_subplay_line.py` の `web_search_sub` テスト、`test_fixtures/test_api.py` の `EXPECTED_PLAYBOOKS`)
- `builtin_data/tools/detail_recall.py` を削除 (`detail_recall_playbook` 専用ツールだったため)

**改訂理由**:

Phase 3 残件「既存 Playbook の `context_profile` / `model_type` → `line: "main"|"sub"` 翻訳」に着手する前に、翻訳対象の総数を減らして作業を圧縮するため。Spell 階層 (`memory_recall_unified` / `resolve_uri` / `searxng_search` / `read_url_*` 等) が充実してきており、旧 Playbook で表現していたパターンの大半は Spell 単発呼び出しで賄えるようになっていた。

加えて、新認知モデル (Track + メタ判断 Playbook) への完全移行に伴い、旧自律稼働プロトタイプ (`meta_auto` 経路 + `ConversationManager` の周回駆動) は呼ばれなくなっていた。コード側で残骸を抱え続けると Phase 3 翻訳作業時に「これは現役か旧版か」の判定が増えてミスが起きやすくなるため、翻訳前に旧経路を完全に断つ判断。

DB 上の Playbook は 67 → 48 件。翻訳対象 (`context_profile` / `model_type` を使う Playbook) も同時に減る (旧プロトタイプ系が消えたため)。

不変条件としての変更はなし。あくまで Phase 3 翻訳前のクリーンアップ。

**追補 (同日)**:

整理直後の動作確認で「Disk から消した Playbook が DB に残り、`router_callable=1` のものがシステムプロンプトに乗ってペルソナが Spell として呼ぼうとして警告 (`Unknown spell 'read_url_content'` 等) が出る」事象が発覚。

原因は起動時の `sync_playbooks_from_files` (`saiverse/playbook_sync.py`) が import 専用で orphan prune を行わない設計だったため。`scripts/import_all_playbooks.py` 側には `prune_orphan_playbooks` が実装済みだったが、これは手動実行 / バージョンアップフロー経由でしか走らない。

修正:
- `sync_playbooks_from_files` 内に `_prune_orphan_playbooks` を実装し、毎起動時に Disk と DB の整合性を取る
- 対象は scope='public' AND source_file IS NOT NULL かつソースファイルが disk に無い Playbook
- `save_playbook` ツール経由 (source_file IS NULL) は保護される
- addon 関連は expansion ファイルが存在する限り保護され、addon を一時的に外すと対応 Playbook も削除されるが、addon 再追加で復元される
- DB 残骸の即時クリーンアップとして `read_url_content` / `searxng_search` / `x_reply` (旧 builtin → expansion 移行残骸) は新 prune で削除、`sub_speak_meta` / `sub_speak_simple` (source_file IS NULL の旧 meta layer 残骸) は手動削除

DB 上の Playbook は 48 → 43 件。

### v0.18 (2026-05-01) — 自律先制と外部 alert のレース解消 (Phase 2.6)

**確定事項**:

- `set_alert` の状態遷移と observer 通知を分離。既 running の Track への set_alert は状態 no-op のまま、observer には `target_already_running=True` フラグ付きで通知する
- context に `target_track_title` / `target_track_type` も常に乗せて、メタ判断者が UUID でなく自然言語で対象を識別できるようにする
- `meta_judgment.json` judge prompt に「target_already_running=true は自律先制と外部イベントの衝突 → 通常は継続判断で OK」のガイダンスと独白例を追加
- `MetaLayer._build_state_message` (legacy path) もフラグを自然言語化

**改訂理由**:

実機検証 (2026-05-01) で Pulse A (自律メタ判断で対 user1 を pending→running に先制起動) の直後に Pulse B (ユーザー発話起因のメタ判断) が起動したが、context に alert 情報が乗っていないことを発見。Pulse A のエアは「自分が pending→running にした」と認識し、Pulse B のエアは「特に理由なくメタ判断が走った」と認識する不整合が起きていた。

原因は `set_alert` が既 running の Track に対して状態遷移と observer 通知を**両方** no-op にしていたため。仕様としては状態遷移 no-op は正しいが、外部イベント (ユーザー発話) の事実そのものはメタ判断者に届けるべきだった。状態遷移と通知の責務を分離することで、ペルソナの自律先制と外部イベントが時間的に衝突しても、メタ判断者がきちんと認識できるようになる。

不変条件 11 ("メタ判断 = ペルソナ自身の思考の流れ") の延長として、「思考の連続性」が外部イベントとの衝突で断絶しないための基盤整備。

### v0.17 (2026-05-01) — SAIMemory `messages.pulse_id` カラム化 (Phase 2.5)

**確定事項**:

- per-persona memory.db の messages テーブルに `pulse_id TEXT` 専用カラム + INDEX (`idx_messages_pulse_id`) を追加
- `_store_memory` (sea/runtime.py) は当面、列とタグ (`metadata.tags` の `"pulse:{uuid}"`) の両方に書き込む (互換維持)。読み出し経路が全部カラム参照に移行したらタグ書き込みは廃止予定
- `add_message` (sai_memory/memory/storage.py) と `_append_message` (saiverse_memory/adapter.py) に `pulse_id` 引数を追加
- `_promote_meta_judgment_in_pulse` の SQL を `pulse_id = ?` の INDEX 付き直接 WHERE に書き換え (旧 json_each 線形スキャンから O(log N) へ)
- `_backfill_messages_pulse_id` で既存行の pulse_id をタグから抽出して埋める (起動時 1 回、べき等)

**改訂理由**:

Phase 2 実装直後の実機検証で `_promote_meta_judgment_in_pulse` が `OperationalError: no such column: pulse_id` で落ちることが発覚。前セッション (0cfe61c) の SQL 設計が SAIMemory の保存実装 (タグ経由) と整合していなかった。

応急処置として一旦 json_each(metadata, '$.tags') 経由に書き換えたが、本質的にはタグ照会は (1) INDEX が効かず将来スケールに対応できない (2) `pulse:` プレフィックス命名規則が暗黙の前提で脆い (3) `pulse_logs` テーブルとの JOIN や Pulse 単位集計を素直に書けない、という 3 つの不満があった。Phase 2 で pulse_id ベースの参照経路を入れたばかりで関連箇所の記憶も新しいうちに、専用カラム化を済ませた。

メタ判断ログ機構 (Phase 2) が動き始める前に基盤を固める判断。`pulse_logs` テーブルが既に pulse_id カラムを持っているため、整合性も同時に取れた。

### v0.16 (2026-04-30) — メタ判断 Pulse の per-persona 直列化 + メタ判断ログ機構の運用

**確定事項 (Part 1: 直列化)**:

- 同一ペルソナのメタ判断 Pulse は同時 1 本に制限する。`MetaLayer` が persona_id ごとの `threading.Lock` を保持し、`on_track_alert` / `on_periodic_tick` の両入口で取得待ちする
- 競合時は **wait** で確定 (skip しない)。理由: alert を skip すると即応イベントを取りこぼし、定期 tick を skip するとメインキャッシュ TTL 切れを誘発する
- 別ペルソナ同士は Lock が独立しているため並列実行可能 (per-persona 粒度)
- chat thread のブロックは一時的に許容。将来「安全な中断機構」を作る意思は持つ
- `02_mechanics.md` §"メタ判断 Pulse は同時 1 本 (per-persona 直列化)" を追加

**確定事項 (Part 2: ログ機構の運用)**:

- `meta_judgment_log` スキーマを v0.15 (独白 + /spell 方式) に整合化。旧 4 値 enum (`judgment_action`) と関連カラム (`switch_to_track_id` / `new_track_spec` / `notify_to_track` / `raw_response`) を廃止し、`spells_emitted` (JSON 配列) を新設
- 書き込み機構を実装:
  - **Playbook path**: `_run_spell_loop` が `pulse_type == 'meta_judgment'` のとき `PulseContext.meta_judgment_buffer` に独白 + spell + 結果を蓄積。Pulse 完了時 (`runtime_graph.py`) に `MetaLayer._record_judgment_log` を呼んで永続化
  - **legacy path**: `MetaLayer._run_judgment` が判断ループ中に直接バッファし、`finally` で `_record_judgment_log` を呼ぶ
- 動的注入: `MetaLayer._build_recent_judgments_block(persona_id, n=5)` で過去 5 件を箇条書きにし、Playbook path は `meta_judgment.json` の `recent_judgments` 入力経由で `{recent_judgments}` 展開、legacy path は状態メッセージ末尾に追記
- これで: (a) 過去のメタ判断を踏まえた連続性 (Intent A v0.14 メタ判断ログ領域の本来意図)、(b) 古い snapshot 問題への対処 (前回操作の結果が今回の判断材料になる) を達成

**改訂理由**:

Part 1: Phase C-2 のテスト中に「pending と思って pause したら裏で alert になっていた」現象を観測。原因: alert observer (chat thread 経由) と AutonomyManager 定期 tick (background thread 経由) が別 thread で同じ persona に対するメタ判断 Playbook を起動し、それぞれが独立した snapshot を見て Track 操作を発動していた。不変条件 11 ("メタ判断 = ペルソナ自身の思考の流れ 1 本") を構造で守るには、入口での直列化が必要だった。

Part 2: `meta_judgment_log` テーブルは Phase 1 で新設したが、書き込み・読み込み・動的注入は Phase 2 以降で運用予定 (`phase_1_base.md`) と明記されていた。Part 1 で並列実行を抑止しても、過去の判断結果を参照できないと: (1) 別 Track からアラートが連続して来た時に毎回独立判断になり判断が劣化、(2) 古い snapshot 問題 (前回 pause したのに「pending と思って pause」をまた書く等) が残る。Part 2 でログ機構を実運用に乗せることで両者を解消。

**実機検証で発覚した追加修正 (2026-05-01)**:

Phase 2 + Phase 2.5 の動作確認中に SQL レベルで以下の不整合が見つかり、同セッション内で順次修正した:

1. **scope 昇格 SQL のカラム不在 (cfb68b4 で応急処置 → 1728dbf で本修正)**: `_promote_meta_judgment_in_pulse` が `messages.pulse_id` カラムを直接叩いていたが、SAIMemory の messages テーブルには pulse_id 専用カラムが無く `metadata.tags` の `"pulse:{uuid}"` で保存されていた。応急処置として json_each 経由のタグ照会に書き換え、その後 Phase 2.5 で専用カラム化して INDEX 利用に戻した。

2. **fuzzy spell parser のノイズ WARNING (cfb68b4)**: `_parse_fuzzy_spell_args` が strict parser をフォールバック前置で呼ぶため、fuzzy 形式 (`/spell name key='value'`) でも常に WARNING が出ていた。silent モードを追加して fuzzy 経由は DEBUG に降格。canonical 形式での失敗のみ WARNING を維持。

3. **`committed_to_main_cache` が常に False (b2c0c81)**: `runtime_graph.py` の Pulse 完了処理で、`_apply_deferred_track_ops` が `deferred_track_ops` を clear した**後**で `any(op.op_type == 'activate')` を評価していたため、Track 切替が発動した判断ターンでも `committed_to_main_cache=False` と記録されていた。判定を apply の**前**に移動。CRITICAL ORDER のコメントで再発防止。
   - 実機影響: messages 本体は scope='committed' で正しく残っていたため Track 切替動作自体は問題なし。次回メタ判断時の judge prompt 注入で `[switch]` マーカーが付かず、過去の重要決断を全て「継続」として読んでしまう不整合があった (実害は Phase 2 の効果が半減する程度)。

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
