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

## 3. §6-3b — 実装・検収済み・コミット済み (2026-07-17 セッション3)

下記の確定設計どおり委譲→検収で完了。設計どおりの実装に加えて、検収で確認した逸脱3点 (いずれも妥当と裁定):

1. **結線はスコープ外5ファイルに波及** (runtime_context / runtime / work_session / gold_panning / runtime_runner) — head render が ExecutionContext 解決より前に走るため、`prepare_context` に `model_key` kwarg を通し、呼び出し側が解決を前倒しした。work_session の早期解決は WORKER フレーム push 済みのため挙動不変。runtime_runner の probe (legacy state 経由) は pulse_type 3 分類すべてで root aspect の tier と一致することを検収で照合済み
2. **フォールバックの正は `persona.model`** — 設計記載の `persona.default_model` は存在しない属性で、旧実装は全行 MODEL_KEY='default' に落ちていた実バグ (実 DB で確認)。anchor-TTL 失効時の snapshot 再 capture がこの修正で初めて実ペルソナで機能する
3. **backfill は 'default' → ai.DEFAULT_MODEL 解決を追加** (上記バグの帰結。解決不能行はスキップ = head は損失許容)

留意 (次 wave への引き継ぎ): diff flush 経路の Session 窓ごと配送は §6-4、Metabolism dispatch の lightweight 側節目は §6-5 の領分として据え置き。

### 確定済み設計 (記録)

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

## 4. §6-4 — 実装・検収済み・コミット済み (2026-07-17 セッション3)

下記の確定設計どおり委譲→検収で完了。逸脱なし。実装の要点: `sea/head_pipeline/notify.py` (決して raise しない2層ヘルパー + tools/context contextvars 解決の便宜口) / `HeadPipeline.advance_last_notified` (全 model 行の B 前進 + dirty 除去 + persist) / `flush_diffs(advance=False)` が `(labels, {section: new_snapshot})` を返す分離形 (配送失敗時は dirty 据え置き = 次回再検出) / ツール結線は Error 文字列ガード + `memory_atlas._parse_ref` での宛先 section 振り分け + DB ロック外での発火。issue `head_mutation_notification_gap` は実装済み・実機検証待ちに更新済み。

### 確定設計 (記録)

head操作の内容型通知 (issue head_mutation_notification_gap 解消)。調査で確定した構造事実: **`section.render(snapshot)` は snapshot のみに依存** (DB 非参照) — `section.capture(ctx)` → `render` の2ステップで head と寸分たがわぬ断片が得られ、head 本体の凍結は維持できる。

設計 (私=Fable の裁定、intent §3.3 の確定裁定からの演繹):

1. **操作起点 push の対象 = tool 書き込み経路がある 4 section**: core_memory (memory_write/delete/clip) / desk (memory_open/close) / life_purpose (life_purpose_set — META finalize は TOOL_REGISTRY 経由で同ツールを叩くため自動カバー、issue の中核解消) / memopedia_index (memory_write/delete、opt-in OFF なら push しない)。**memory_weave / chronicle_index は書き手がシステム編纂のみなので §6-5 (Metabolism 節目の可視化) の領分に送る** (intent の「未整理も本工事で潰す」はこの2つの diff 整理を指す — §6-5 で扱う旨を intent に記録済み)
2. **通知本文 = capture→render の text そのもの** + 操作ラベル (何をしたか1行)。ヘルパー `notify_head_mutation(persona, manager, building_id, section_name)` を head_pipeline に新設し、各 tool の成功点から呼ぶ。model_key は resolve_default_model_key フォールバックでよい (capture/render は model 非依存、ctx の anchor-TTL メタは通知生成に効かない)
3. **配送 = outbox 経由** (§6-2 の台帳ハンドラが土台): begin_execution(kind="head.mutation_notify") → mark_applied(outbox_items=[{target: "perception.push", payload: {kind: "head_mutation", content, reduce_key: f"head_mutation:{section}"}}], deliver=True)。台帳が無い環境 (旧テスト) は直接 push_perception に degrade + warning
4. **二重通知の防ぎ方 = push 成功後に B (last_notified) を該当 section だけ全 Session 行で前進** (diff は消さない — UI 編集など tool 外の変化は backstop diff が拾い続ける)。根拠: 知覚バッファ→SAIMemory は persona 共有の履歴ストリームで、push は全 Session の窓に届く。pipeline に `advance_last_notified(persona_id, section_name, new_section_snapshot)` (全 model 行) を新設
5. **S5/S3 も同時に閉じる**: inject_diff_notifications の直接 `push_perception("world_state", ...)` (integration.py:213 付近) を outbox 経由に統一し、flush_diffs の B 前進を outbox mark_applied (durable 確定) 後に移す
6. **life_purpose の diff ラベル (内容なし) は render 断片同梱に改める** (§3.3 不変条件「操作ラベルでなく render 同一断片」への追従)

## 5. §6-5 — 実装・検収済み・コミット済み (2026-07-17 セッション3)

下記の確定設計どおり委譲→検収で完了 (実装エージェントはレート制限で落ちたが実装は完遂しており、検収で全項目・全テスト緑を確認)。**逸脱1点 (妥当と裁定)**: 非対話の確認スキップ (自律 Pulse で AUTONOMOUS_CHRONICLE_ENABLED=False) は設計の "deferred" でなく **"disabled"** (= 退役許可) に分類 — deferred にすると自律 Pulse しか走らない persona の anchor が永久据え置きになるため。押し出された生ログは SAIMemory に残り、後続の user Pulse / session close (force=True) で編纂される。

検収メモ: call-local anchor は `prepare_context(context_meta=)` out-param → `state["_prefix_anchor_id"]` → `touch_anchor_after_llm_call(anchor_id=)` の3点で運ぶ。runtime_llm の touch 4箇所 + keepalive (見張り対象行の値) + gold_panning + work_session (history_depth=0 → None = touch なし、旧「WORKER 実行中に main-line anchor を誤 touch」も同時に消滅) 全結線を確認。claim は確認ゲート通過後・LLM 前 (mark_failed で失敗表明、対象なしは claim せず ok)。

### 確定設計 (記録)

Metabolism 二層分離 (intent §3.2)。調査で確定した現状: 編纂と退役は同一経路に混在、入口は5つ (①応答後 maybe_run_metabolism ②会話前 Case 3 = runtime_context.py:165-211 ③手動 organize-memory ④gold_panning session close ⑤①内の gold_panning)、全て `session_lifecycle.generate_chronicle` に合流。**S2 実在**: `_run_metabolism_locked` は編纂例外を warning で握り潰し anchor を無条件前進 (tests/test_gold_panning.py:420 がこの挙動を固定→期待値逆転が必要)。**M1**: 編纂に冪等 claim なし (arasuji の source_ids スキップは事後冪等のみ)、Beat ロックが唯一の防壁。

設計 (私=Fable の裁定):

1. **編纂の冪等 claim (M1)**: `generate_chronicle` 内部を実行台帳で包む (全入口が合流する一点)。kind="metabolism.run"、idempotency_key = f"{persona_id}:{窓末尾ID}" (窓末尾ID = 編纂対象 qualifying run の最大メッセージ ID。対象なしなら claim せず no-op)。begin_execution が created=False を返したら編纂スキップ (別入口が同じ窓を claim 済み)。generate_track_chronicle も同様 (kind="metabolism.run_track")
2. **S2 ガード**: generate_chronicle が status を返す — "ok" (成功 or 対象なし) / "disabled" (トグル OFF) / "failed" (例外) / "deferred" (確認待ち・拒否)。anchor 前進は ok / disabled のときだけ。failed / deferred は据え置き = watermark 超過が残るので次の maybe_run で自然再試行。**disabled で前進するのは設計判断** (Chronicle を切った persona は「編纂なしで忘れる」を選んでいる — 前進を止めると metabolism が永久デッドロック)
3. **`history_manager.metabolism_anchor_message_id` 廃止 (全8箇所)**: 正 = session_anchor 行 (persona, model)。(a) 退役書き session_lifecycle.py:838 → ExecutionContext.model_key の行へ、in-memory 属性は削除 (b) Case 1/2 の解決値 (runtime_context.py:155/319) → **call-local 化**: 解決した anchor_id を state["_prefix_anchor_id"] に載せ、touch_anchor_after_llm_call が引数で受ける (persona 属性経由をやめる = 記憶監査第4片「TTL 失効後に旧 anchor を touch」の根治) (c) 読み3点 (発火判定 :644 / gold_panning:674 / memory_weave.py:88) → load_anchor_entry(persona_id, model_key) (d) config.py:207 の in-memory clear は属性ごと消滅
4. **閾値と退役の model 化**: get_high_watermark / get_low_watermark / maybe_run_metabolism / run_metabolism に ExecutionContext 由来の model_key を通す (呼び出し元 runtime.py:261 は Pulse の実行 model を渡す)。退役は「その model の窓」だけ進める
5. **可視化 = model の節目**: on_metabolism の head 再 capture (session_lifecycle.py:846-849 / config.py:245) を「anchor を進めた model の (persona, model) だけ」に絞る (§6-3b で head は model 別キー済み)
6. **weave/chronicle diff 整理** (§6-4 からの宿題): chronicle_index の件数ラベル diff (sections/chronicle_index.py:77-97) を退役 ([] に) — 可視化は節目の構造交換 (退役の瞬間に Chronicle が生ログと入れ替わりで見える、intent §3.2) が担保するのでラベルは重複ノイズ。memory_weave diff は [] のまま。これが intent §3.3 の「未整理も本工事で潰す」の実装形
7. **Case 3 (会話前) は claim だけ足して挙動維持**: anchor 前進が絡まないので S2 の対象外。編纂失敗しても生ログは SAIMemory に残り次回編纂で自然回復

## 6. §6-6a — 実装・検収済み・コミット済み / §6-6b — 分離して後続へ

§6-6a は下記設計どおり委譲→検収で完了。逸脱2点 (いずれも妥当と裁定): (1) 孤児復旧は adapter 初期化全般でなく**ペルソナ登録経路の opt-in** (`recover_orphaned_thread=True` は persona/bootstrap.py のみ — 使い捨て adapter が走行中 Stelis を誤って巻き戻さない門、startup_backup と同じ規律) (2) マーカー書き込みは set_active_thread 拡張でなく**別メソッド** (恒久切替がマーカーを自然に消す意味論がデフォルト成立 — クラッシュ復旧が恒久選択を打ち消さない)。`stelis_parent_thread_id` state は読み手が複数いるため維持 (復元の正は pulse_ctx スタック)。§6-7 (正典改訂) も同コミット: session.md ×2 / dynamic_state_sync.md 改訂注記+3箇所 / concepts/metabolism.md。

### 元の設計 (記録) — a/b 分割裁定

調査で確定した現状: thread の正は `active_state.json` (persona 共有可変ファイル、プロセス内キャッシュなし)。Stelis (sea/runtime_nodes.py:329-405) は start で子へ切替・end で親復元するが、**復元は end ノードにしか無く**、例外/cancel で end 不達だと以後の全 Pulse が子 thread へ保存され続ける (S4、監査原文 = docs/handoff/2026-07-15_sea_runtime_session_head_tail_audit.md:108-126)。`PulseContext.thread_id` は生成時固定の死に値 (読み手2箇所のみ)。`ExecutionContext.thread_id` は解決済みだが**消費者ゼロの飾り**。beat_gate は RLock + threading.local 深度でスレッド束縛 (boundary は非所有スレッドで no-op — runtime_graph.py:225-230 の running-loop executor 分岐で劣化)。

**分割裁定 (私=Fable)**: §6-6a = S4 根治 (今 wave)。§6-6b = Beat ロックの実行トークン化は**分離して後続へ** — 理由: 現状の劣化は「レガシー分岐で META の待ちが最大1 Pulse」という軽微なもの (直列性・関所は無傷) に対し、トークン化はトークン伝播漏れ = デッドロック新設のリスクを持つロック層の作り直しで、半端な状態を残せない。§6-2 の Beat 直列化により S4 の「並列 Pulse が汚染値を読む」経路は既に消えており、残る実害 (例外時復元漏れ / クラッシュ孤児) は 6-6a で閉じる。

### §6-6a の設計 (S4 根治)

前提裁定: **`active_state.json` は「直列に一人ずつ触る台帳」として生かす** (Beat 直列化下では、保証された復元があれば「thread は実行の属性」の意味論が成立する)。adapter 読み口全部の contextvar 化は追わない — 高リスク低便益。

1. **PulseContext に thread スタック**: `push_thread(child_thread_id)` (親 = 現在のファイル値を記録して `set_active_thread(child)`) / `pop_thread()` (親へ復元) / `unwind_threads(depth)` (指定深さまで一括復元)。Stelis start/end ノードと subagent 経路 (runtime.py:1157/1206) をこれ経由に置換
2. **保証された復元**: run_playbook (または graph 実行の finally — 現物で最適点を確認) が入口で thread スタック深さを記録し、finally で `unwind_threads(その深さ)` — 例外/cancel/正常終了すべてで親へ戻る
3. **クラッシュ孤児の復旧**: push 時に active_state.json へ `pulse_scoped_parent` を書き、pop/unwind で消す。adapter 初期化 (起動時) に `pulse_scoped_parent` が残っていたら親へ復元して WARN (プロセス死で孤児化した Stelis の自然回復)
4. **死に値の廃止**: `PulseContext.thread_id` フィールドを削除。読み手2箇所の置換 — runtime.py:1378 (_flush_pulse_logs) は flush 時点の adapter 現在値、pulse_context.py:302 (resolve_execution_context のフォールバック) は削除
5. **ExecutionContext.thread_id が実消費者を得る**: Beat 開始時点の解決値として維持 (今回は消費者追加なし — _store_memory の live 読みは stack-mirror されたファイルが正しく反映するため現状維持が正)

### §6-6b の材料 (後続 wave 用の記録)

- depths (threading.local、beat_gate.py:85) → persona→保持トークンスタックへ。トークンは Beat 単位の新規発番 id (execution_id は現状常に None で使えない)
- 再入判定は「同一スレッド」→「親トークン継承」に変わる。子 Beat への伝播は state 経由 (`_pulse_context` 継承と同型、runtime_graph.py:176)。**伝播漏れ = デッドロック** (現 RLock は同一スレッド再入を自動許容するが、トークンは明示継承が必須) — 全子 acquire 経路 (metabolism 再入 / work_session / spell executor) の伝播マップを作ってから着手
- ロック順序不変条件 (MetaLayer Lock → Beat ロック の一方向) は維持

## 7. 次の走路 (§6-6a 完了後)

1. §6-7: session.md / dynamic_state_sync.md の正典改訂 — 対象確定済み: session.md :48 (WORKER の「別 line_id で独立 head」→ (persona,model) 共有へ) / :113,115 (同)。dynamic_state_sync.md :56-60 (操作ラベル型の例 → 内容型) / :162-163 (差分検出の不変条件 → 操作起点 push + backstop) / :133-140 (Metabolism 統合フックの差分駆動前提)
2. §6-6b: Beat ロックの実行トークン化 (上記材料)
3. 台帳 Phase 1 (判断点 A2/A7/A8/A9/A11) → Phase 2〜5
4. 柱5〜8
2. §6-7: session.md / dynamic_state_sync.md の正典改訂
3. 台帳 Phase 1 (判断点 A2/A7/A8/A9/A11) → Phase 2〜5
4. 柱5〜8

## 8. 運用メモ (このセッションで確立・確認したこと)

- **委譲→検収の型で回す** (まはー指示の再確認 2026-07-17。私が A' を直接実装して「一人で動きすぎ」と止められた)。調査(Explore)→設計(メイン)→実装(general-purpose)→検収(メイン)。委譲プロンプトには毎回: メインツリー直接 / worktree・再委譲禁止 / git 操作禁止 / 触ってよいファイル明示 / venv python / pytest パイプ禁止
- **検収は設計検証の第二パス** (memory 更新済み: feedback_delegate_impl_to_subagents)。実装の前提が本番経路で成立するかを疑う — 今回はスレッドモデルをプローブ2本で実測した
- 並走セッションに注意: このリポジトリの作業ツリーを Codex/別セッションが同時に触ることがある。コミットは**ファイル明示ステージ**で自分の分だけ。in_flight.md は共有台帳なので取り込み前に必ず現物確認
- 実機検証待ち (まはー): 第二陣導線 / Memory Atlas / multi-city封鎖 / §6-2 Beatロック (実機で会話+自律の併走確認は未)
- 全部終わったら `gen_reference_docs.bat` 一括再実行 (§6-3a で models.py にテーブルが増えるため database-schema.md に差分が出る)
