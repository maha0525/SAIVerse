# Intent: Task モデルの一本化（standalone Task ↔ track_task 統合）

> **ステータス**: ✅ 実装完了・実機検証済み（2026-06-28）
> **親**: [`autonomous_desire.md`](autonomous_desire.md)（③の前段。候補=Task の土台）
> **関連**: [`mode_spell_permissions.md`](mode_spell_permissions.md)（Task操作スペルの権限）/ [`01_concepts.md`](01_concepts.md)
> **実装状況**: 全 step（1-6 + スペル改名）完了。まはー実 DB に適用済み。
> **実機検証 2026-06-28**: 4 スペル（`task_add` / `task_done` / `task_update_step` / `task_decompose`）
> すべて自律 Track 上で正常作動を確認。③（自律の源泉）の地面が完成した。

---

## 1. 背景・問題

現在 Task が**2系統**に分裂しており、③（候補=Task）の土台が二重で組みにくい。

| | track_task | standalone Task |
|---|---|---|
| 保存 | `Track.tasks_json`（Track 行の JSON、main DB） | per-persona tasks.db（独立 SQLite） |
| 1件の中身 | `{title, done}` のみ | id / goal / summary / **status / priority / steps / history** |
| 親 | Track 固定 | 親なし（persona_id キーのみ、Track 非依存） |
| 性質 | 超軽量チェックリスト | フル機能のタスクオブジェクト |
| 現状 | 稼働中 | スペルが import 失敗で実質休眠（2026-06-27 観測） |

まはーの既存方針: 旧 `task_*` は廃止予定だが**旧版にしかない機能（goal / steps / 親を選べる柔軟さ）を track_task 側に統合する**。③はこの統合の上に乗る。

---

## 2. 目標: 1枚の task テーブル + 親の任意バインド

standalone の豊かなフィールドを**正**とし、親を `note_id` / `track_id` のどちらか（or なし）で持てる**単一テーブル**に一本化する。

- **`note_id` あり = 候補**（desire ノート内のやりたいこと）← ③
- **`track_id` あり = Track 内の実行小目標** ← 旧 track_task
- **どちらもなし = 未所属**（一時的、生成直後 等）
- **昇格（候補→Track）= 親を `note_id` → `track_id` に張り替えるだけ**

これで「task は1種類、違いは親だけ」になり、③の二重土台が消える。

### 2.1 保存先は main DB（saiverse.db）

`note` / `action_tracks` は main DB にある。task が両者に FK で繋がるには、**統合 task テーブルは main DB に置く**（per-persona tasks.db を main DB に吸収）。per-persona 分離は `persona_id` 列で表現する。

### 2.2 スキーマ（standalone を踏襲 + 親バインド追加）

```
task:
  id, persona_id,
  parent_kind ('note' | 'track' | none), note_id?, track_id?,   ← 追加
  title, goal, summary, notes,
  status, priority, origin, active_step_id,
  due_at, created_at, updated_at, completed_at, version, last_actor
task_step:   (既存 standalone のまま — position/status/notes/history)
task_history:(既存 standalone のまま)
```

`parent_kind` + `note_id` / `track_id` で所属を表す（排他: note か track の一方）。

---

## 3. 移行（touch points は実地調査済み 2026-06-27）

### 3.1 データ移行（2ストアで安全バーが非対称 — まはー裁定 2026-06-27）

2つのストアは**監督できる相手か**で安全バーが違う。

| | track_task (`Track.tasks_json`) | 旧 standalone task (tasks.db) |
|---|---|---|
| 使用範囲 | **まはー環境のみ** | リリース版に同梱（他ユーザーの手元） |
| 移行の性質 | **破壊的（一方向）でOK** | **取りこぼし不可**（他人のデータを壊せない） |

- **track_task**: `Track.tasks_json` の各 `{title, done}` → task 行（`track_id` バインド、`status` は done→completed/else open、`goal` は title 流用）。まはー環境のみなので**一方向移行で可**（ロールバック層は作らない）。移行後 `tasks_json` 列は廃止。
- **旧 standalone task**: per-persona tasks.db の tasks/task_steps/task_history → main DB の統合テーブルへ（親なし or 既存の所属があれば付与）。**リリース済みで他ユーザーにデータが在りうるため、取りこぼさない正式なデータ移行を用意する**（「使ってる人いないはず」で消さない＝監督できない相手のデータは壊せない）。これが §4 不変条件3（backup + dry-run）の主対象。
- 実装上は**両者を同じ移行スクリプトに同居**させる（tasks.db→統合テーブルの読み口を1本足すだけで track_task 変換と共存でき、安く付く）。

### 3.2 コード touch points
- **track_task 系**: `saiverse/track_manager.py`（`get_tasks`/`add_task`/`complete_task`/`format_task_list`）、`database/models.py`（`tasks_json` 列削除）、`builtin_data/tools/track_list.py`（`"tasks": "2/5"` 集計）。
- **standalone 系**: `persona/tasks/{storage,creation,__init__}.py`、`persona/core.py`（インスタンス化）、`api/routes/people/tasks.py`（UI/API）。
- **スペル統合**: `track_task_add` / `track_task_done` + 旧 `task_request_creation` / `task_change_active` / `task_close` / `task_update_step` → 統合 task スペル群（親指定で candidate / track-task を兼ねる。壊れている旧スペルはここで作り直す）。
- **権限マップ**: [`sea/mode_spell_permissions.py`](../../sea/mode_spell_permissions.py) の `TASK_CONTROL_SPELLS` を統合後のスペル名に更新（②モード権限との整合）。

---

## 4. 不変条件

1. **task は1テーブル**。親（note/track/なし）の違いだけで candidate / 小目標を表す。別系統を再生させない（[[feedback_no_dead_code_via_flags]]）。
2. **親の張り替えが昇格**。候補→Track 化は task をコピー/破棄せず `note_id`→`track_id` の張り替えで表す（履歴連続性を保つ）。
3. **不可逆移行の前にバックアップ**。移行スクリプトは実行前バックアップ + dry-run を持つ（[[feedback_irreversible_action_safety]]）。**主対象は旧 standalone task（リリース済み・他ユーザーのデータ）**。track_task はまはー環境のみなので破壊的一方向で可。
4. **タスク行は物理削除しない（掃除は status での論理削除）**。タスクは persona 内連番 `short_id`（`task:N` 参照子、所属 track/note/なしを横断する単一参照空間）で指す。採番は `MAX(short_id)+1`。物理削除しないことで MAX が単調増加し、**番号は二度と再利用されない**（完了/キャンセル/腐敗掃除はすべて `status` 遷移、行は残す）。Track の `short_id` と対称。race は create の `SELECT MAX→INSERT` を1トランザクションに包んで Track 同水準に抑える。将来どうしても物理削除が要る場合のみ独立カウンタ（`AI` の per-persona 列）へ格上げ。

---

## 5. 実装順（③ の前段）

1. ✅ 統合 task テーブルを main DB に定義（親バインド列 + 既存 standalone フィールド）。
   → `database/models.py`: `PersonaTask` / `PersonaTaskStep` / `PersonaTaskHistory`。
2. ✅ 統合 TaskManager（main DB 版、親で candidate/track-task を兼ねる CRUD）。
   → `saiverse/persona_task_manager.py`: `PersonaTaskManager`。昇格 `promote_to_track`、
   旧 track_task 互換層（`get_track_tasks`/`add_track_task`/`complete_track_task`/
   `format_track_task_list`）込み。in-memory smoke test 済み。
3. ✅ 移行（backup + dry-run 付き、安全バー非対称 §3.1）。
   - track_task: `database/migrate.py` の post-migration フック
     `_migrate_track_tasks_json_to_persona_task`（source=backup から `tasks_json` を読み
     `persona_task` 行へ、冪等）。`tasks_json` 列ドロップ（step 6）が全書換を誘発した時に発火。
   - 旧 standalone: `scripts/migrate_tasks_db_to_unified.py`（per-persona tasks.db →
     統合テーブル、id/ステップ/履歴/タイムスタンプ保持、`--dry-run`、冪等）。end-to-end 検証済み。
4. read/write 経路を統合 Manager に張り替え（§3.2 の touch points）。**ここから live 経路に触る**。
   - 4a ✅ **track_task 側**: `TrackManager` のタスク系4メソッド（`get_tasks`/`add_task`/
     `complete_task`/`format_task_list`）を `PersonaTaskManager` の track_task 互換層へ委譲
     （persona_id は Track 行から導出）。`builtin_data/tools/track_list.py` の `tasks_json`
     直読みを `get_tasks()` 委譲に置換。スペル `track_task_add`/`track_task_done` は
     `TrackManager` 経由なので変更不要。順方向移行 `scripts/migrate_track_tasks_json.py`
     （live `tasks_json` → persona_task、列ドロップ前に実行、冪等）。e2e 検証済み。
   - 4b ✅ **standalone 側**: アダプタ方式を採用。`persona/tasks/store.py` に旧 `TaskStorage`
     互換 API（同メソッド署名・同 `TaskRecord`/`TaskStepRecord`/`TaskHistoryEntry` dataclass
     戻り値）を持つ persona バインドアダプタ `PersonaTaskStore` を新設し、内部で
     `PersonaTaskManager`（main DB）へ委譲。消費者は `TaskStorage` → `PersonaTaskStore` の
     差し替えのみ（属性アクセス・FastAPI 直列化契約 `TaskRecordModel` 無改変）:
     `persona/core.py` / `api/routes/people/tasks.py`（4 endpoint）/ `persona/tasks/creation.py`
     （task_request_creation backend）/ スペル3本 `task_change_active`/`task_close`/`task_update_step`。
     `get_task_summary` は core の `task_storage` 経由で自動追従。**重要**: アダプタの
     `list_tasks` は `parent_kind='track'` を除外（Track チェックリストが standalone 一覧に混入しない）。
     `tests/test_task_tools.py` を新アーキ  に追従（temp main DB + `SessionLocal` パッチ +
     LLM オフで決定論化）。api 直列化経路も e2e 検証済み。
5. ✅ スペル整合 + `TASK_CONTROL_SPELLS` 更新（まはー裁定 2026-06-28）。タスク生成は
   「add で直接追加 + decompose で steps 分解」に整理（旧「依頼→タスク化モジュール」間接構造を撤去）。
   - **アドレッシング統一**: タスクは `short_id`（`task:N`、所属横断）で指す。`PersonaTask.short_id`
     + `PersonaTaskManager.resolve_task_ref`。**不変条件**: 行は物理削除しない（§4-4）→ 番号再利用なし。
   - **温存スペル**: `task_add`（Track へ追加。旧 `track_task_add` から改名）/ `task_done`
     （task:N で完了。旧 `track_task_done`）/ `task_update_step`（task:N + step_position）。
     done/update_step は `index`/「アクティブタスク」起点から `task:N` addressing へ付替え。
     `track_` プレフィックスは Track 固有でなくなった（task:N で所属横断）ため外した。
   - **新規 `task_decompose`（純決定論スペル）**: 分解の知性は撃つ側 LLM（自律モノローグ）が担い、
     スペルは `task_ref` + `steps` 配列を受けて `set_steps` で記録するだけ（余計な LLM 呼び出しなし＝
     キャッシュ共用）。**配列引数のため canonical 形式 `/spell name='task_decompose' args={...}` 必須**
     （fuzzy KV は `[...]` を解析できないことを実測確認）。
   - **撤去**: `task_change_active`（アクティブタスク概念廃止＝Track + note open で代替）/ `task_close`
     （done と同義）/ `task_request_creation` + `persona/tasks/creation.py`（TaskCreationProcessor）+
     `scripts/process_task_requests.py` + `tests/test_task_creation.py`。
   - `TASK_CONTROL_SPELLS = {task_add, task_done, task_update_step, task_decompose}`
     （AUTONOMOUS+CONVERSATION 限定）。`track_autonomous.json` のスペル案内も更新。
6. ✅ `Track.tasks_json` 列と per-persona tasks.db を廃止（2026-06-28、まはー実 DB に適用済み）。
   - `ActionTrack.tasks_json` をモデルから削除 → `migrate.py` 全書換パスが列ドロップ、フック
     `_migrate_track_tasks_json_to_persona_task`（backup → persona_task、short_id 採番込み）が保全。
     **実 DB 検証**: 全テーブル行数完全一致（無損失）、tasks_json 消失、2 tracks のタスクを
     persona_task へ移行（standalone tasks.db は全ペルソナ空＝移行対象なし）。
   - per-persona `tasks.db` は production で生成されなくなった（4b で `TaskStorage` 廃止）。
     dead な `TaskStorage` クラスを撤去し `persona/tasks/storage.py` を値型（dataclass / 例外 / 定数）
     のみに slim 化。`tests/test_task_storage.py` 撤去。
   - `track_autonomous` playbook を DB に再 import（新 task:N スペル案内反映）。

**現状（2026-06-28）**: ③-0（Task 一本化）**全 step 完了・実 DB 適用済み**。Task は単一
`persona_task` テーブルに一本化され `task:N`（所属横断）で指す。③（candidate=task in desire note、
META が note_id→track_id 昇格）が乗る地面が完成した。残る派生課題は §6（UI 表示の出し分け）。

---

## 6. 残課題

- UI（`api/routes/people/tasks.py` + フロント）の統合 task 表示（candidate / track-task の出し分け）。
- 旧 `task_*` スペルの「旧版にしかない機能」の棚卸し（統合スペルに漏れなく移すため）。
