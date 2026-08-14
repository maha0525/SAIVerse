# v0.3.0 リリースに向けた作業認識（ワークリスト）

> **作成**: 2026-06-27
> **目的**: いま v0.3.0 に向けて「やっているはずの作業」を一箇所に固定し、
> 思いつきで別実装に逸れても**作業スレッドが行方不明にならない**ようにする。
> [`roadmap_status.md`](roadmap_status.md) が俯瞰マップ（粒度が粗い）なのに対し、
> こちらは**今まさに手をつける残タスクと、その判断の経緯**を保持する作業記録。
> **ステータス記法**: ✅ 完了 / 🟡 進行中 / 🔵 起草中 / 🔲 着手前

---

## 現在地

- バージョン: **`0.3.0.dev2`**（リリース前）
- 中心軸: **自律稼働**（AI が連続して動き続ける）
- 骨格は立っている: `autonomy_manager` / `event_scheduler` / `meta_layer` /
  `pulse_dispatcher` / `pulse_scheduler` / ライフビュー UI まで実装済み。
  「ティック → メタ検収 → 軽量実行 → レポート」のループは実機で回る状態。

このワークリストは「中心軸＝自律稼働を、実際に**止まらず回り続ける**状態にする」
という観点で残タスクを並べている。

---

## スコープ外（リリースブロッカーではない）

混同しないように、最初に外すものを明記する。

- **Region RPG / Observer / Fixture** — いずれも**仮実装でユーザー非公開**。
  API を直接叩かないと動かないため、放置しても誤発火しない。v0.3.0 のブロッカーではない。
- **ペルソナ間会話（Social Track 入口）** — ロードマップ上 **Phase 5** 扱い。
  ここを待つとリリースが大きく後ろにずれるため、v0.3.0 の射程外とする。

---

## 残タスク

### ① メタ判断パルスの失敗時リカバリ 🪦 — 対象機構ごと退役（2026-08-14）

> v1 メタ判断は Track 撤廃の順序①（[track_retirement.md](../intent/track_retirement.md) §7.4）で退役し、本項のリカバリ機構（リトライ・連続失敗降格）も同時に削除された。issue は [archive/phase4_meta_judgment_recovery.md](../issues/archive/phase4_meta_judgment_recovery.md) へ。以下は当時の記録。

- **問題**: `meta_judgment` Playbook 実行時の LLM エラー / パースエラー / Lock 解放が
  未ハンドル。一時的な API 失敗で**自律稼働が無音で止まる**。
- **詳細 issue**: [`phase4_meta_judgment_recovery.md`](../issues/phase4_meta_judgment_recovery.md)（案 A+B+C）
- **実装状況（調査して判明）**:
  - **A（全例外 catch + Lock 解放）= 既存**。`on_track_alert` / `on_periodic_tick` が
    `with lock:`（例外時も解放）+ `try/except logging.exception` で実装済み（`saiverse/meta_layer.py`）。
  - **B（リトライ + backoff）= 既存**。`_run_judgment_via_playbook` が `max_retries` /
    `retry_backoff_seconds`（`META_JUDGMENT_CONFIG`）駆動でリトライ済み。
  - **C（連続失敗で Activity 降格 + 通知）= 今回実装**。per-persona 連続失敗カウンタ +
    `max_consecutive_failures`（既定3）。閾値到達で `_handle_persistent_failure` が
    ACTIVITY_STATE を Idle に降格（無音スピンを断つ）+ `add_building_event` で host 通知
    （event_message タグでペルソナ/UI に届く）。成功でカウンタリセット。
  - テスト: `tests/test_meta_judgment_recovery.py`（4件）+ 既存 meta/autonomy テスト（50件）pass。
  - **残（← 現在の検証対象, 2026-07-02）**: 実機で「メタ判断を連続失敗させたとき Idle に落ちて通知が出る」確認。検証ブロッカーだった**失敗注入デバッグ機能は実装済み**（トグルで強制失敗させられる）→ 再起動して連続失敗→Idle降格→host 通知の一巡を確認するだけ。まはーが検証中。
- **依存**: ②③とは独立。

### ② トラック操作スペルの権限制御 ✅ — 実装完了・実機検証済（2026-06-28）

- **問題**: `AUTONOMOUS` アスペクトが `track_complete` を実行できてしまう
  → Track が簡単に完了 → 自律行動が即終了 → 次のメタ判断まで何も起きない、という空転。
- **設計意図**:
  - **`META` と `CONVERSATION` アスペクトのみ** トラック操作スペル（`track_complete` /
    `track_abort` / `track_pause` 等）を使える。
  - **`AUTONOMOUS` / `WORKER`（サブ）は使えない**。代わりにタスク操作スペル等でやりくりする。
- **現状の事実（2026-06-27 調査）**:
  - **専用のインテントドキュメントは存在しない**。近い概念は
    [`02_mechanics.md`](../intent/persona_cognition/02_mechanics.md) §336
    「使用可能 Playbook 候補（Track 種別で許可される Playbook 群）」だが、これは
    *Playbook 候補をコンテキストに文字提示*する話で、*スペル実行をアスペクトで強制ガード*する話ではない（別物）。
  - スペル一覧を組む [`spell_list.py`](../../sea/head_pipeline/sections/spell_list.py)
    のフィルタ軸は **`SPELL_ENABLED`（ペルソナ単位）/ MCP の Building 可視性 /
    `availability_check` / `spell_visible`** の 4 つだけ。
    **アスペクト（CONVERSATION/META/AUTONOMOUS/WORKER）の次元は存在しない**。
  - 結論: この設計は**ドキュメントも実装も未着手**。口頭で固めかけて止まっていた。
- **やること**: 設計は下記「最新の到達点」＋「命名確定」でほぼ固まった。具体的な残作業は
  この §② 末尾の「**②の残作業（命名確定後）**」を正とする（旧 (a)(b) はそこに統合）。
  - 注: アスペクト（モード）次元の権限判定は**必要**。ただし置き場所は**表示側ではなく実行側**。
    - **表示リスト（spell_list.py）にアスペクトフィルタを足す案は棄却** — 全スペル＋ルールを常に system prompt に出す方針（リストから消さない）。
    - **実行時ゲートは aspect を参照する** — active frame の `LineFrame.aspect`（[pulse_context.py:152](../../sea/pulse_context.py:152)）を引き、「スペル→許可モード」の中央マップと照合。不許可なら実行せずゲット文を返す。リスト描画は不変＝キャッシュ無傷。
- **①との関係**: Track が空転しなくなって初めて、①のリカバリが
  「実際に判断が走り続ける」前提として活きる。
- **過去の到達点（2026-06-04〜05, セッション `d761f935`）**:
  - **問題提起**: 自律 Pulse が一回で Track を complete → メタ判断で対ユーザー会話に戻る
    → 完全自動モードが数十秒で終わる、という空転をまはーが「一番怖い」と指摘。
  - **2 つの方向性で合意**: (B1) Track 操作スペルを自律 Pulse で使わせず CONVERSATION/META に限定（守り）
    ／ (B2) Track に抽象的で継続可能な目的を込め、その下に短期目標（Task）を動的に作る・完了する層を置く（攻め、Task 機構の再利用 or 参考）。両方必要との認識。
  - **🔑 本格設計は意図的に保留**: 「スペル機能全体について再考しないといけないから今触るべきじゃない」（まはー）。
  - **着地した暫定対応**: `builtin_data/playbooks/public/track_autonomous.json` の action テンプレートから
    Track 操作スペルのリマインドを削除し、タスク管理スペル（`track_task_add` / `track_task_done`）のみに差し替え。
    **今も生きている**（`track_complete`/`track_pause` は `description` フィールドに例として残るだけで LLM には見せない）。
    ただし**プロンプトのリマインドを消しただけで実行ガードは無い**（= spell_list.py にアスペクト次元なし、と一致）。
  - **未着手**: アスペクト別の強制ガード本体、B2 の Track 目的 ↔ Task 短期層の分離、インテントドキュメント。
- **最新の到達点（2026-06-15〜21, セッション `e2e1c73c`）**: 設計の骨格がここで固まった。
  - **ゲートの置き場所（技術的に確定）**: 既存の `availability_check`（[spell_list.py](../../sea/head_pipeline/sections/spell_list.py)）は使えない
    （aspect を知らない＆フィルタするとスペルが一覧から消える＝「リストには常に残す」方針と逆）。
    ゲートは**スペル実行時**（`_run_spell_loop` / `_execute_pre_spells`、アクティブな `LineFrame.aspect` が取れる場所）に置く新機構。
    **リスト描画は一切いじらない＝キャッシュ無傷**。「ルールは全部システムプロンプトに示し、今どのモードかだけ最新プロンプトに入れる」発想。
  - **能力レイヤーモデル（確定）**: META=大目的を決める ／ AUTONOMOUS=小目標を決めて実行 ／ SUB=実行補助 ／
    CONVERSATION=ペルソナ本体・ユーザー接点・すべてを制御しうる存在（META+AUTONOMOUS 両方を実行できる）。
  - **権限マトリクス（厳密版・確定）**: 6/5 の暫定から META の Task操作が ✅→❌ に変わった（小目標＝Task＝AUTONOMOUS の層だから META は Task に触らない）。

    | aspect | Track操作 | Task操作 |
    |---|---|---|
    | CONVERSATION | ✅ | ✅ |
    | META | ✅ | ❌ |
    | AUTONOMOUS | ❌ | ✅ |
    | SUB | ❌ | ❌ |

  - **不変条件3の再解釈**: 「メタレイヤーが切り替えを独占」→「AUTONOMOUS は自分で Track を切り替え/完了できない」という具体ルールとして、今回のゲートが enforcement を肩代わりする。CONVERSATION の Track操作はユーザー駆動ライフサイクルとして別物。
  - **途切れた場所**: aspect の呼び名のたたき台（統括/遂行/補助/対話）を提示した直後で会話が途切れていた。→ 下の「命名確定」で決着。

- **✅ 命名と解説セクションの確定（2026-06-27, まはー決定）**:
  - **Aspect そのものを対ペルソナ用語化して「モード」と呼ぶ**。上2つ（自律制御/自律作業）は自律行動を ON にしないと発生しない、という性質が命名で明確になる。

    | 内部 aspect | ペルソナ向けモード名 |
    |---|---|
    | CONVERSATION | **メインモード** |
    | META | **自律制御モード** |
    | AUTONOMOUS | **自律作業モード** |
    | SUB | **分身モード** |

  - **システムプロンプトに自律行動/Track/モードの体系的解説セクションを新設する**（現状この解説が無いのが課題）。確定した本文ドラフト（まはー記述、実装時はこれを正とする）:

    > **【自律行動について】**
    > ユーザーのUI操作によって自律行動が開始されます。
    > 自律行動のためには、まず長期的な目的を定めることが必要です。目的を持つTrackと呼ばれる脳内の「走路」を作成することで、その目的に向けて日常的に行動し続けることができます。
    > 自律行動中にユーザーや他ペルソナの発話を検知した場合はすぐに中断して反応できるので、ユーザーを待つために行動を遠慮する必要はありません。
    > Trackの作成や切り替え等の制御はスペルによって行われます。
    >
    > **【Trackについて】**
    > Trackはあなたが長期的・あるいは永続的に取り組む大目的を持ちます。
    > 読書やネットサーフィンといった「趣味」、ユーザーからの依頼やCityのための「仕事」、食事や入浴といった「生活」、長編小説の執筆や「プロジェクト」
    >
    > 対ユーザー会話Trackについて
    > ユーザーと会話するTrackです。対ユーザー会話Trackに切り替えることでメインモードに戻り自律行動を終了できます（この切り替え直後、メインモードでの発言機会を一回得ます）。
    >
    > **【モードについて】**
    > SAIVerseにおけるあなたの活動は以下の4モードに分けられます。モードによって使われるモデルや発言・記憶の扱い、使えるスペルが異なります。
    >
    > **メインモード：**
    > あなたがユーザーや他ペルソナと会話する際に用いられる、基本のモードです。標準モデルが使用されます。
    > メインモードでの発言はBuilding内に発声され、ユーザーの見るUIに表示されます。また、同一Buildingにいる他のペルソナにも発言内容が知覚されます。
    > メインモードでは特に種類の制限なくスペルの使用が可能です。記憶想起系の機能を使った際のあなたのプライバシーを保護するため、使用したスペルの返り値は他のペルソナには見えません。
    >
    > **分身モード：**
    > あなたと同一の記憶・自己認識を持つ分身体が作業する際のモードです。軽量モデルが使用されます。
    > あなたのコンテキストが作業内容で埋まるのを防ぐこと、複数ターンの作業をより軽量なモデルで行い効率化することを目的としています。
    > run_playbookスペルでPlaybookを使用した時に用いられます。分身体はPlaybookに定められたワークフロー通りに稼動し、結果の概要を返します。
    > 分身モードでの発言は外からも、他モードの自分からも見えません。
    > 分身モードではTrack制御系・Task制御系のスペルを使うことはできません。
    >
    > **自律制御モード:**
    > 自律行動の際に自分自身の行動方針を制御するためのモードです。標準モデルが使用されます。
    > 自律行動中、一定の間隔で自動的に起動し、後述する自律作業モードを俯瞰的に管理します。
    > 主に走るTrackを切り替えたり、目的達成時にTrackを完了状態にすることが責務です。
    > 自律制御モードでの発言は外からは見えません。
    > 自律制御モードではTrack制御系のスペルのみ使用できます。
    >
    > **自律作業モード：**
    > 自律行動の際の実作業モードです。軽量モデルが使用されます。
    > 自律行動中、短い間隔で自動的に起動します。Trackの目的に合わせた作業を行うことが責務です。
    > 自律作業モードでの発言は外からは見えません。
    > 自律作業モードではTrack制御系のスペルを使うことはできません。Task制御系のスペルで小目標の管理を行ってください。

  - **このドラフトから読み取れる追加要件（実装時に拾う）**:
    - メインモードのスペル返り値は他ペルソナから不可視（プライバシー保護）。現状の実装有無は要確認。
    - 「現在のモード」名だけを最新プロンプト末尾に差し込む供給方式（キャッシュ無傷）。Track切替通知・メタ判断プロンプトがモード名を名指しする。

- **②の実装（2026-06-27 完了・実機検証待ち）**:
  1. ✅ インテントドキュメント確定 → [`mode_spell_permissions.md`](../intent/persona_cognition/mode_spell_permissions.md) v1.0。
  2. ✅ 中央マップ: `sea/mode_spell_permissions.py`（権限マップ + ゲット文）+ `Aspect.mode_display_name`（`sea/pulse_context.py`、aspect→モード名表示）。
  3. ✅ 実行時ゲート: `_run_spell_loop` / `_execute_pre_spells`（`sea/runtime_llm.py`）に挿入。active frame の aspect を引き、不許可スペルは実行せずゲット文を×ブロックで返す。
  4. ✅ システムプロンプト解説セクション: `AutonomyModesSection`（`sea/head_pipeline/sections/autonomy_modes.py`, order 550, spell_list の直前）。
  5. ✅ 「今どのモード」供給: `track_autonomous.json`（自律作業モード）/ `meta_judgment.json`（自律制御モード）のプロンプトに明示 + DB 再 import 済み。
  6. ✅ 不変条件3 再解釈を `01_concepts.md` に反映。
  - 検証: ruff pass / 新テスト `tests/test_mode_spell_permissions.py`（8件）+ 既存 aspect・head pipeline・spell テスト（73件）pass / DB にモード名反映を確認。**✅ 実機検証済（2026-06-28）**: ③-0 の Task スペル実機検証中、自律作業モード内で `track_complete` が実行され**ゲートにブロックされた**ことを確認（空転しない）。
  - 既実装のため対応不要だった点: メインモードのスペル返り値プライバシー（全スペル共通で既に他ペルソナ不可視）。

### ③-0 前段: Task モデルの一本化 ✅ — 完了・実機検証済（2026-06-28）

- **問題**: Task が2系統に分裂（track_task = `Track.tasks_json` の軽量チェックリスト / standalone Task = tasks.db のリッチ機能）。③の「候補=Task」が二重土台の上に乗ってしまう。
- **intent doc**: [`unified_task_model.md`](../intent/persona_cognition/unified_task_model.md)（v0.1 設計 → ✅ 実装完了・実機検証済み 2026-06-28）。
- **目標（達成）**: **1枚の `persona_task` テーブル（main DB）+ 親の任意バインド**（`note_id`=候補 / `track_id`=Track内小目標 / なし）。standalone のリッチフィールドを正に。**昇格 = 親を note_id→track_id に張り替え**（`promote_to_track`）。
- **完了サマリ**: モデル `PersonaTask`/`Step`/`History` + `PersonaTaskManager`（昇格・track_task 互換層）+ 移行3本（migrate.py フック / 順方向 / standalone, 全冪等・dry-run）+ 切替（track_manager 委譲 / アダプタ `PersonaTaskStore`）。タスクは `short_id`（`task:N`, 所属横断）で指す。**不変条件: 行は物理削除しない → 番号再利用なし**。スペル整理 = 温存 {`task_add`, `task_done`, `task_update_step`} + 新規 `task_decompose`（純決定論スペル, canonical 形式 `/spell name= args=` 必須）、撤去 {task_change_active, task_close, task_request_creation + creation.py + process_task_requests.py}。
- **実 DB 適用済（まはー環境, 2026-06-28）**: `Track.tasks_json` ドロップ → migrate.py 全書換でフックが persona_task に保全（short_id 採番込）。全テーブル行数完全一致（無損失）・2 tracks のタスク移行・standalone tasks.db 全ペルソナ空。**4スペル全て自律 Track 上で正常作動確認**。
- **残派生**: UI 表示の出し分け（`api/routes/people/tasks.py` candidate / track-task）→ ③で候補概念が立ってから一緒にやるのが自然。

### ③ 自律の源泉（欲求エンジン + やりたいこと候補）🟡 — 実装完了、実機検証でバグ1件発見→修正（再検証待ち, 2026-07-02）

**実装サマリ（2026-06-28）**: autonomous_desire.md §8 タスク 1〜7 完了・テスト緑。
- `desire` note_type（`NoteManager.ensure_desire_note` singleton、user 作成不可）。
- `open_notes` head セクション（desire 候補プール ＋ 現 running Track の attach ノート）。cache 手法は visual_context 準拠（capture 時点を凍結→`get_running` で active track 解決→変動は末尾通知→Metabolism 再capture）。**当初「track_id 無しで cache 破壊」と誤認し desire のみに絞ろうとしたが、まはー指摘で既存パターンに気付き訂正**。
- `desire_add` スペル（AUTONOMOUS が候補追加、`TASK_CONTROL_SPELLS`）。
- `LIFE_PURPOSE` カラム（追加系 migration・安全）＋ヘルパ＋`LifePurposeSection`（① 駆動文＋② 目的、order 560）＋`life_purpose_set` スペル。**初回目的設定は促し駆動**（`LifePurposeSection` が未設定時に促し render→ペルソナが自分で決めて保存）。**当初「メインモード切替＋ユーザー確認＋自律停止」の重い対話フローを設計したが、まはーに却下され全廃**。
- `track_create(from_candidate='task:N')` で候補昇格（`promote_to_track`、候補プールから自動消去、META/CONVERSATION のみ）。
- テスト: `test_open_notes.py`（11）/ `test_note_manager.py`（desire 3）/ `test_life_purpose.py`（10）/ `test_mode_spell_permissions.py`（desire_add）。
- **実機検証（2026-07-02）**: 候補→head→昇格→消去の一巡、目的設定→ライフビュー表示まで確認。`LIFE_PURPOSE` の実 DB migration 適用済。
- **🐛 バグ発見→修正（2026-07-02, quon_city_a）**: 生きる目的設定のメタ判断が走った後、本人が**自分で定めた目的を認識しない**応答をした。
  - **真因**: committed のメタ判断が `line_role` フィルタで main_line コンテキストから除外されていた。`_payload_passes_context_filter`（`saiverse_memory/adapter.py`）が `required_line_roles=['main_line']` のとき `line_role='meta_judgment'` を scope 問わず弾いていた。設計意図（`committed_to_main_cache=TRUE` = 既にメインキャッシュに乗っている, `03_data_model.md §176`）と食い違う**実装バグ**。life_purpose メタ判断は `life_purpose_set` スペル発火で既に `scope='committed'` だったが、line_role で消えていた。
  - **修正**: `_payload_passes_context_filter` を「main_line 要求時、committed なメタ判断も通す」に一元修正。会話コンテキスト＋Metabolism カウント両経路に効く。discardable のメタ判断は従来通り除外（`_build_recent_judgments_block` が judge プロンプトへ別注入、二重なし）。Track 切替の確定独白も同様に会話に載るようになる（これまで載っていなかったのがバグ）。
  - テスト: `test_payload_context_filter.py` に committed メタ判断の包含 / discardable 除外 / sub_line 非昇格の3件追加、既存 meta/life_purpose/history 系緑。**既存データで効く**（quon の判断は既に committed なので再設定不要、次会話で認識するはず）。**残**: まはーが quon で再検証（会話して目的を認識するか）。
- **残派生**: ③-0 の UI 表示出し分け（`api/routes/people/tasks.py` candidate / track-task）はまだ。候補概念が立った今、単独で片付けられる。

---

### ③（旧・設計メモ、実装済み）— 設計確定・実装前

- **問題（再定義）**: 当初「やることを決める恒久 Track のプリセット」と捉えていたが、
  本質は**ペルソナが自分のやりたいことをどう内発的に生成するか**。直近コンテキストから
  拾うだけでは「依頼を先回りするAI」止まり。終わりがなく単独で充足可能な**欲求エンジン**が要る。
- **intent doc**: [`autonomous_desire.md`](../intent/persona_cognition/autonomous_desire.md) **v0.2 設計確定**（2026-06-27）。実装前。
- **確定した骨格（4層）**: ①欲求エンジン / ②生きる目的 / ③やりたいこと候補 / ④Track。
  - **①**: 共通の薄い駆動（成長欲求・知的好奇心 等）。**初期セットは作らず運用で増やす**。パラメータ化B案は不採用。
  - **②**: 初回聞き取り = **C案**（ペルソナが人格定義＋直近記憶から目的/趣味/仕事をドラフト → メインモードでユーザー確認）。
    自律初回ON時に一度起動。保存先は AI 新カラム **`LIFE_PURPOSE`**（JSON、Note でなく persona 属性）。プロンプト常駐でリマインド。
  - **③ 候補 = Task**（既存 standalone Task が persona_id キー・Track 非依存＝候補プールに最適。粒度・消化マーキングが Task の単位・status で自動解決）。
    Task 単独は head 機構が無いので**desire ノートに組み込む**（新 note_type `desire`、ペルソナ1枚恒久）。AUTONOMOUS が候補 Task を追加。
  - **④**: META が desire ノート内 Task を読んで Track 化 + 当該 Task を「昇格済み」マーキング。
  - **desire ノート = 全 Track 自動 Open 対象**。head 注入は `visual_context`/`memory_weave` 同様のキャッシュ制御。
- **判明した配線ギャップ（③で完成させる）**:
  - **Note↔Task 紐付けが無い**（`NoteTask` 表も tasks の `note_id` 列も無し）→ 新設。
  - **open ノートが context に未注入**（`list_open_notes` がどこからも読まれない）→ `open_notes` head セクション新設。
- **残課題（intent §9・実装中に詰める）**: 候補 Task(standalone) ↔ Track 内小目標(track_task) の棲み分け / Memopedia 薄ページ検出（任意）/ 候補腐敗の掃除ポリシー。
  - **解決済**: ①初期セット（置かない）/ 候補粒度・消化マーキング（Task に吸収）。
- **依存**: ②（モード権限・実装済み）に構造的に依存（AUTONOMOUS は Track 作れない前提）。

### ④ オートノミー系 ↔ Track 機構の関係整理 ✅ — **結論確定（2026-07-10 まはー裁定）**

- **結論**: **時間割（自律行動v2）への完全移行**。数分刻みの自律 Pulse は意味のある行動を
  生まない（v1 失敗診断の言い直し）。v1 が担った機能は全て v2 に座席がある —
  連続実行→コマ内作業セッション（予算） / 自発性→無意味の予算コマ / 割り込み→呼びかけ即応 /
  途絶検知→watchdog。
- **v1 系 Playbook の処遇**（Codex 監査 `docs/handoff/2026-07-10_memory_atlas_p2c_consumer_audit.md` §3 に従い実装は概念再編⑥ P2c-3 で実施）:
  - `track_autonomous` / `meta_autonomy_decision` — **退役**（public JSON 除去 + DB prune +
    `SELECTED_META_PLAYBOOK` 巻き取り + docs 追従）
  - `autonomy_creation` / `autonomy_web_research` — **archive**（復活時は memory_* 語彙で再設計）
  - `autonomy_memory_organization` / `fragment_organize` — **P4 庭仕事のワーカーへ転生**（保留）
- `self_reflection` 専用プレイブック欠けの件: 専用 playbook は作らない — v2 では
  就寝判断（day_close のふりかえり）と無意味の予算コマが内省の座席。
- 必要になった能力は**新しい種のコマ**として作る（新種コマの作成手順は未整備 →
  アイディア帳に記録、本作業の外）。

---

## 推奨着手順（2026-07-02 更新）

1. ~~② 権限ゲート~~ ✅ 完了・実機検証済。
2. ~~③-0 Task モデル一本化~~ ✅ 完了・実機検証済。
3. ③ 自律の源泉（欲求エンジン＋候補）🟡 実装完了。実機検証で目的認識バグ発見→修正済（committed メタ判断が main_line 未載）。まはー再検証待ち。
4. **① メタ判断リカバリの実機検証** ← **いま検証中**。失敗注入トグル実装済み → 再起動して連続失敗→Idle降格→通知を確認。
5. ~~④ オートノミー系 ↔ Track 整理~~ ✅ **結論確定（2026-07-10、時間割へ完全移行）**。実装（Playbook 退役）は概念再編⑥ P2c-3 に合流。
6. ③-0 残派生: UI 表示出し分け（candidate / track-task）。単独で挿せる小タスク。

---

## 未決事項（次セッション持ち越し用）

- ~~② の権限ガードを「表示から落とす」か「実行時に拒否」か~~ → **決着**: リスト不変・実行時ゲート。
- ~~② のモード命名~~ → **決着**: メインモード / 自律制御モード / 自律作業モード / 分身モード（2026-06-27）。
- ~~② メインモードのスペル返り値プライバシー（他ペルソナ不可視）の実装状況~~ → **既実装で確認済**（全スペル共通で他ペルソナ不可視）。
- ~~① の失敗注入デバッグ機能をどう作るか~~ → **決着**: トグル実装済み。あとは実機検証（2026-07-02, まはー検証中）。
- ~~③ の `open_notes` head セクションのキャッシュ制御~~ → **決着**: `visual_context`/`memory_weave` 同方式（capture 凍結→末尾通知→Metabolism 再 capture）で実装・検証済。
- ~~③ の desire ノートの "消えない" をどう担保するか~~ → 実装済み（singleton `ensure_desire_note`）。実機一巡で消えないこと確認済。
- ④ で `self_reflection` を足すか否か、`autonomy_*` を Track 機構へ寄せるか廃すか。 ← **残る唯一の設計未決**。

## 経緯: v0.3.0 ④ オートノミー整理 (2026-08-04 in_flight 台帳より移送)

> 台帳の器の再設計 (次アクション欄=前向きのみ) に伴い、それまで台帳セルに積もっていた経緯の全文をここへ移した。時系列の生の堆積であり、整理はしていない。

**実装済(2026-07-10、⑥P2c-3)**: 退役2/archive 4・upgrade handler dev4(PersonaSchedule+SELECTED_META_PLAYBOOK 巻き取り)・VERSION dev4・docs 追従(CLAUDE.md/autonomous-mode 等)。
dry-run で prune 6件確認。
残: **実機再起動で巻き取り+prune の本走行確認**(まはー)
