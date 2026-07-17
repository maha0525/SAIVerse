# 監査対応wave セッション2の引き継ぎ (2026-07-17)

**用途**: このセッション (Fable/メティス、2日目) が5時間制限で切れた場合の再開点。全体の状態は in_flight 台帳と各 intent が正。**走行中サブエージェントの検収手順**と **§6-3b の確定済み設計**はここにしか書いていない。前セッションの引き継ぎは `2026-07-16_audit_wave_session_handoff.md` (決着済み、履歴)。

## 1. このセッションでコミット済みのもの (検収済み・安全)

| コミット | 内容 |
|---|---|
| `a96bf13` | **実行台帳の覚醒 (§6-2前半)**: manager結線 + 起動時回復 + 60秒掃除tick + 実ハンドラ2種 (saimemory.append / perception.push)。回帰15件 |
| `b25cf0d` | **柱4: native import 復元/移植分離** (M4/M5/M6 消し込み)。回帰10件。監査文書追記+レビュー台帳更新同梱 |
| `fcb1707` | Codex の wait_response 再装填修正 (検収して取り込み。実機はこのコードで稼働中とまはー確認済み) |
| `54f3bac` | UI の7層ストレージ/Tracksタブ退役 (並走セッション成果の検収。tsc/残参照ゼロ確認済み) |
| `7475f77` | 台帳同期 + ideas.md (日本語会話基盤モデル=まはー雑談由来、出所確認済み) |
| `4c64bbe` | **§6-2後半: Beatロック+関所+main/META並行解体** — sea/beat_gate.py + 取得点4系統 + spellループ周境界 + 回帰17件。**実行台帳 Phase 0 完了**。スレッドモデル実測は intent §3.4 末尾 |

全体スイート基準数: **2517 passed** (`--ignore=tests/test_avatar_pipeline.py`)。avatar 118件の失敗は外部 stackchan addon の checkout 版ずれ (既知、台帳の第二陣行に記録済み)。

## 2. §6-3a (anchor行分離 + 実model記帳) — 検収済み・コミット済み

サブエージェント (レート制限死) の成果を 2026-07-17 のセッション3で検収し、残骸3件を潰してコミットした。

- **本体 (サブエージェント実装、検収で挙動不変を確認)**: 新テーブル `session_anchor` / 冪等 backfill / 行 upsert 化 (TTL 延命規則は upsert_anchor_entry へ挙動不変で移植) / usage.model 記帳 / watchdog 予約キー `ttl:{persona}:{model}` 化 / tests/test_session_anchor_rows.py (13件)
- **検収で拾った残骸 (サブエージェントが「別 wave」送り or 結線忘れしたもの、同コミットで修正)**:
  1. `day_plan._cancel_keepalive_reservation` が旧 key `ttl:{persona}` を cancel し続けていた → `EventScheduler.cancel_prefix` を新設して prefix 一括 cancel に (発火時ゲート is_keepalive_allowed は安全網として健在だった)
  2. organize-memory API (`api/routes/people/config.py`) が旧列 NULL 化のみで session_anchor 行を消さない機能退行 → `clear_anchor_entries` を結線
  3. `scripts/clone_persona_to_test_env.py` が anchor を複製しなくなっていた → `_clone_session_anchor_rows` を追加 (AI 行・memory.db と対で複製する規則の維持)
- 実装レベルの設計裁定2つは intent §3.1 に記録済み: 新テーブル+backfill 方式 (全書換 migration 不使用) / 記帳先の正= usage.model
- 旧 `AI.METABOLISM_ANCHORS` 列は backfill の変換元としてのみ残存 (常に NULL)。列 DROP は後続の掃除 wave

## 3. §6-3b の確定済み設計 (次タスク。この設計で委譲してよい)

head snapshot + last_notified の (persona, model) キー化。調査済みの現状 (§6-3 調査エージェント報告、要点):

- 現物理キーは `(PERSONA_ID, LINE_ID)` で LINE_ID は実質常に "main" (`database/models.py:547-556` line_head_snapshot / `sea/head_pipeline/pipeline.py:71` in-memory dict / `store.py` 全経路)。MODEL_KEY は列にあるが非キー、値は `persona.default_model` の推測 (integration.py:78-83)
- last_notified は同じ行の LAST_NOTIFIED_JSON に相乗り → **head のキー化と一体で model 分離される**

設計 (私=Fable の裁定):
1. 新テーブル `session_head_snapshot` PK=(PERSONA_ID, MODEL_KEY)、列は line_head_snapshot から LINE_ID を除いた形 (SECTIONS_JSON / LAST_NOTIFIED_JSON / UPDATED_AT 等は現物に合わせる)
2. in-memory キーも (persona_id, model_key) へ (`pipeline.py` の `_states` dict)
3. model_key の供給: ExecutionContext が届く経路 (LLM node / work_session / gold_panning / keepalive、state["_execution_context"]) からは ctx.model_key。届かない経路 (起動時 capture_all 等) は persona.default_model フォールバック
4. 冪等 backfill: line_head_snapshot → 新テーブル。同一 (persona, model) への集約衝突は **line="main" の行を優先、無ければ UPDATED_AT 最新**。既に新テーブルに行がある組は上書きしない。旧テーブルは読み口を廃止し、DROP は後続の掃除 wave (drop_empty_legacy_note_tables の先例)
5. 壊れる候補テスト: tests/test_head_pipeline.py (直撃)、test_head_pipeline_anchor_ttl.py。恒久検査 test_head_section_wiring.py はキー構造非依存 (緑維持のみ)
6. 意味変化として引き受けるもの: 「line ごとの diff 既読」→「model ごと」(サブラインも同 model なら head 共有 — intent §3.1 まはー裁定と整合)

## 4. 次の走路 (§6-3 完了後)

1. §6-4: head操作の内容型通知 (issue head_mutation_notification_gap 解消。経路= outbox→知覚バッファ→tail、§6-2 の台帳ハンドラ2種が土台として結線済み)
2. §6-5: Metabolism 二層分離 (S2/M1。台帳の冪等 claim `(metabolism.run, persona:窓)` を使う)
3. §6-6: thread の ExecutionContext 化 (S4、Stelis push/pop)。**Beat ロックのスレッド束縛→実行トークン化もここ** (intent §3.4 末尾の実測帰結参照 — running-loop レガシー分岐の boundary no-op の恒久解)
4. §6-7: session.md / dynamic_state_sync.md の正典改訂
5. 台帳 Phase 1 (判断点 A2/A7/A8/A9/A11) → Phase 2〜5
6. 柱5〜8

## 5. 運用メモ (このセッションで確立・確認したこと)

- **委譲→検収の型で回す** (まはー指示の再確認 2026-07-17。私が A' を直接実装して「一人で動きすぎ」と止められた)。調査(Explore)→設計(メイン)→実装(general-purpose)→検収(メイン)。委譲プロンプトには毎回: メインツリー直接 / worktree・再委譲禁止 / git 操作禁止 / 触ってよいファイル明示 / venv python / pytest パイプ禁止
- **検収は設計検証の第二パス** (memory 更新済み: feedback_delegate_impl_to_subagents)。実装の前提が本番経路で成立するかを疑う — 今回はスレッドモデルをプローブ2本で実測した
- 並走セッションに注意: このリポジトリの作業ツリーを Codex/別セッションが同時に触ることがある。コミットは**ファイル明示ステージ**で自分の分だけ。in_flight.md は共有台帳なので取り込み前に必ず現物確認
- 実機検証待ち (まはー): 第二陣導線 / Memory Atlas / multi-city封鎖 / §6-2 Beatロック (実機で会話+自律の併走確認は未)
- 全部終わったら `gen_reference_docs.bat` 一括再実行 (§6-3a で models.py にテーブルが増えるため database-schema.md に差分が出る)
