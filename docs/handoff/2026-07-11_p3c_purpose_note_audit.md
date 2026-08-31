# P3c（目的の木 + Note 物理統合）着手前 消費者棚卸し

2026-07-11 実施（読み取り専用監査、コード変更なし）。対象: `persona_task` /
`persona_task_step` / `persona_task_history`（main DB）と `note` /
`note_page` / `note_message` / `track_open_note`（main DB）を
`memopedia_pages`（per-persona `memory.db`）へ物理統合する P3c の着手前判断材料。

前提ドキュメント: `docs/intent/concept_consolidation.md`「P3 物理統合 — 写像設計
v0.1」（P3a コア記憶・P3b Chronicle は実装済み。P3c は cross-DB のため
「最重量」と明記されている）。

---

## 0. 結論（事実の要約。推奨は書かない）

1. **FK制約は実行時に強制されていない。** `database/session.py` は
   `PRAGMA foreign_keys=ON` を一切設定しない（`journal_mode=WAL` /
   `busy_timeout` のみ）。`models.py` の `ForeignKey(...)` 宣言は
   SQLAlchemy のスキーマメタデータ・`migrate.py` のスキーマ生成にのみ効き、
   SQLite ランタイムでは何も強制しない。→ 「FK が壊れる」という形の技術的
   ブロッカーはそもそも存在しない。
2. **本物の SQL JOIN は存在しない。** `PersonaTask` / `Note` /
   `ActionTrack` を横断する `.join()` はコードベース全体で1件も見つからな
   かった（§1 参照）。全ての「関連取得」は Python 側で ID を集めて2段階の
   別クエリを打つパターン（例: `NoteManager.list_open_notes` が
   `TrackOpenNote` → `Note` を2クエリで引く、`api/routes/people/tasks.py`
   が `PersonaTask` → `ActionTrack` を2クエリで引く）。この形は
   persona_task/note が別ファイル（memory.db）に移っても**そのまま動く**
   （セッションが1つから2つに増えるだけ）。
3. **同一トランザクションでの複数テーブル atomic 更新も存在しない。**
   `PersonaTaskManager` / `NoteManager` の全メソッドは呼び出しごとに
   `SessionLocal()` を開閉する設計（クラスdocstringに明記）。
   `desire_engine.py` / `judgment_finalize.py` 等の複合処理も「読んで
   判定してから別呼び出しで書く」を繰り返すだけで、Note 更新と Task 更新が
   1コミットにまとまる箇所はない。→ cross-DB 化で失われる「原子性」は
   現状ゼロ（今も無い）。
4. **note_page / note_message は本番消費者ゼロ。** `NotePage` /
   `NoteMessage`（Note↔ページ・Note↔メッセージの多対多）を読み書きする
   のは `saiverse/note_manager.py` 自身と `tests/test_note_manager.py` の
   みで、`add_page` / `list_pages` / `add_message` / `list_messages` を
   呼ぶ本番コード（head section・tool・API route）は見つからなかった
   （§5, §6 の grep 参照）。設計はあるが配線されていない。
5. **persona_task ↔ memopedia の cross-DB 参照は既に実運用で機能している。**
   写真の `pasted_to="task:N"`（`sai_memory/photos.py` 管理、memory.db 側）
   は main DB 側の `persona_task` を FK なしの文字列参照で指しており、
   `saiverse/memory_atlas.py:235-274` が `PersonaTaskManager.resolve_task_ref`
   で解決している。「オパークな文字列 ref で他 DB の実体を指す」という
   cross-DB パターンは既に本番で動いている前例であり、P3c が新規に発明する
   必要はない。
6. **P3a/P3b と P3c は移行の「形」が根本的に違う。** P3a（core_memories）・
   P3b（arasuji_entries）は、そもそも**すでに per-persona memory.db 内の
   テーブル**を同じ memory.db 内の `memopedia_pages` に畳んだだけ（1対1、
   同一ファイル内）。persona_task / note は **main DB 1枚に全ペルソナの行が
   同居する単一テーブル**であり、これを N 人分の memory.db に「振り分けて
   移す」ファンアウト移行になる。P3a/b の「adapter init で一回きり冪等
   migration → 旧テーブル DROP」パターン（`sai_memory/core_memory.py`
   `_ensure_root_core` / `init_core_memory_table`）は persona 単位で
   lazy に発火するため、persona_task に同じパターンを使うなら「移行済み
   persona_id の行だけ DELETE」という**部分的な**移行になり、全ペルソナが
   一度は memory.db を開くまで main DB 側の旧テーブルを DROP できない
   （P3a/b の「移行後は旧テーブル DROP」不変条件がそのままは効かない）。
7. **設計哲学として「テーブル統合はしない」と明言する既存コードがある。**
   `saiverse/purpose_tree.py`（P1 時点で無配線・休眠）冒頭に
   「**テーブル統合はしない**（life_concept_map.md §10.1）」とあり、
   ActionTrack と persona_task を写像 (node dict) で束ねるだけの設計に
   なっている。P3c の物理統合方針（persona_task の実体を memopedia に
   差し替える）と、この既存設計判断（Track とテーブルを統合しない）は
   矛盾しないが、**Track (action_track) 自体は main DB に残る**ことが
   前提になっている点は P3c のスコープ確認が必要（実際
   `docs/intent/concept_consolidation.md` の P3c 記述も
   `persona_task`/`note` のみで `action_track` は対象外）。
8. **障壁の実体は「FK/JOIN」ではなく「呼び出し規模」。** `PersonaTaskManager(...)`
   / `NoteManager(...)` は `saiverse/*.py` 12ファイル・`builtin_data/tools/*.py`
   11ファイル・API route 2ファイル・head section 1ファイルから
   その場でインスタンス化されて使われている（§1, §2 一覧）。全呼び出しが
   `manager.SessionLocal`（main DB の共有 session factory）を渡す前提で
   書かれているため、persona_task が memory.db に移ると「persona 単位の
   接続」を渡す形に**全呼び出し元を書き換える**必要がある。これは
   P3a/P3b の strangler-fig（モジュール API のシグネチャは変えず中身だけ
   差し替え）が成立しにくい理由でもある — `PersonaTaskManager` の
   コンストラクタが `session_factory: Callable[[], Session]`（SQLAlchemy
   main DB 前提）を取る形のままでは、memory.db（sqlite3 生 conn、
   persona 単位）を裏で使うことができない。ファサード関数のシグネチャに
   `persona_id` を通して「その呼び出し元が使うべき adapter/conn を
   内部で解決する」形への変更が必要（P3a/b は元々 persona 単位の
   `adapter.conn` を受け取る関数だったため、この問題が起きなかった）。

---

## 1. persona_task / persona_task_step / persona_task_history の消費者

### モジュール本体
- `saiverse/persona_task_manager.py`（1044行）— `database.models` から
  `PersonaTask` / `PersonaTaskHistory` / `PersonaTaskStep` を直接 import
  する**唯一**のファイル（ORM 直叩きはここに閉じている）。
- 公開メソッド: `create_task`(L310) / `get_task`(L499) / `list_tasks`(L508)
  / `update_task_status`(L546) / `set_active_task`(L583) /
  `set_active_step`(L624) / `update_step_status`(L656) /
  `set_steps`(L698, decompose相当) / `append_artifact_ref`(L761) /
  `promote_to_track`(L808) / `detach_parent`(L844) /
  `set_purpose_fields`(L884) / `fetch_history`(L941) /
  `resolve_task_ref`(L229)
- 旧 track_task 互換層: `get_track_tasks`(L974) / `add_track_task`(L1000)
  / `complete_track_task`(L1015) / `format_track_task_list`(L1030)

### `PersonaTaskManager(...)` インスタンス化箇所（呼び出し規模）

| ファイル:行 | 用途 | トランザクション形 |
|---|---|---|
| `saiverse/track_manager.py:123` | `self._task_manager` として保持。Track内チェックリスト互換4メソッドに委譲（L1077-1094） | 個別呼び出しごと |
| `api/routes/people/tasks.py:20` | `GET /{persona_id}/tasks` の一覧（parent_kind ラベル付け） | 個別 |
| `api/routes/people/tasks.py:27-34` | 上記で `ActionTrack` を `track_id.in_(track_ids)` バッチ取得（**PersonaTaskとActionTrackの唯一の「二段階join的」読み**、SQL JOINではない） | 個別 |
| `api/routes/people/life.py:279`, `260-269` | プロフィールツリー（`ProfileTreeResponse`）の candidates 一覧 + `PersonaTask.id.in_(task_ids)` バッチで last_activity 解決 | 個別 |
| `persona/tasks/store.py:97` | `PersonaTaskStore`（旧 `TaskStorage` 互換ラッパ、dict→dataclass変換のみ）。`api/routes/people/tasks.py` の POST/PATCH/history 3エンドポイントが使用 | 個別 |
| `builtin_data/tools/track_create.py:30` | Track作成スペル | 個別 |
| `builtin_data/tools/judgment_finalize.py:490` | `_apply_task_verdict`（セッション終了判断のdone/continue/blocked裁定適用） | 個別（read→判定→write） |
| `builtin_data/tools/judgment_finalize.py:589` | `_finalize_post_session` の `track_op:'complete'` ガード（`get_track_tasks`で未消化タスク確認） | 個別 |
| `builtin_data/tools/judgment_finalize.py:802` | （同ファイル内 `_apply_new_desires` 系） | 個別 |
| `builtin_data/tools/purpose_seed.py:32` | 候補生成（旧desire_add後継） | 個別 |
| `builtin_data/tools/purpose_step.py:18` | ステップ操作 | 個別 |
| `builtin_data/tools/purpose_decompose.py:18` | 分解 | 個別 |
| `builtin_data/tools/purpose_close.py:23` | 完了/中止 | 個別 |
| `saiverse/meta_layer.py:1329` | `_get_desire_candidates`（メタ判断の状況構築、L1314-1349） | 個別（読み取りのみ） |
| `saiverse/judgment_points.py:151` | `_task_ref_status`（ref→status解決） | 個別 |
| `saiverse/judgment_points.py:201` | `_list_backlog_tasks` | 個別 |
| `saiverse/judgment_points.py:214` | `_list_desire_tasks` | 個別 |
| `saiverse/judgment_points.py:992` | post_session状況テキスト構築（task_line） | 個別 |
| `saiverse/judgment_points.py:1601` | 時間割コマの `ref` 検証（`_TRACK_REF_RE`/`task:N`解決） | 個別 |
| `saiverse/desire_engine.py:121,178,231,284` | `_list_desires` / `decay_desires` / `apply_desire_reviews` / `touch_desire`+`promotion_candidates` | 個別 |
| `saiverse/desire_engine.py:141`（`db.query(PersonaTask)`直叩き） | `_update_ledger_fields`（帳簿カラムのみ、履歴を書かない専用パス。**Manager経由せず生クエリ**） | 個別 |
| `saiverse/day_scenario.py:852` | 一日シミュレータのタスク操作 | 個別 |
| `saiverse/day_plan.py:1255,1330` | 時間割コマ発火（`touch_desire`配線元） | 個別 |
| `saiverse/day_report.py:230` | `_list_all_desires`（日報のdesire一覧） | 個別 |
| `saiverse/memory_atlas.py:251` | Atlasファサード: `task:N` ref 解決（写真pasted_to・目的ノード表示） | 個別 |
| `saiverse/purpose_tree.py:158,201,222,253,298,349` | `list_first_tier`/`resolve_ref`/`list_children`/`create_candidate`/`adopt`/`name_theme`。**モジュール自身のdocstringに「P1時点ではどこからも呼ばれない(休眠)」と明記**、実際の呼び出し元は`tests/test_purpose_tree.py`のみ確認 | 個別 |

### 生SQL / 直接ORM（Manager非経由）
- `database/migrate.py:422-530` `_migrate_track_tasks_json_to_persona_task`
  — 旧 `ActionTrack.tasks_json` → `persona_task` 行への一回きり移行
  （raw SQL `INSERT INTO "persona_task"`、L496）。post-migration フック。
- `scripts/migrate_tasks_db_to_unified.py` — 旧 per-persona `tasks.db`
  （sqlite3直）→ main DB `persona_task` への一回きり移行スクリプト
  （ORM `PersonaTask`/`PersonaTaskHistory`/`PersonaTaskStep` を import）。
- `scripts/inspect_world.py:449` — サンドボックス検分CLIの読み取り専用
  raw SQL `SELECT ... FROM persona_task WHERE ...`。
- `scripts/run_day_sim.py:581` — シムスクリプトの `db.query(PersonaTask)`直叩き。

### テストからの直接クエリ（実装検証用、移行時に書き換えが要る）
`tests/test_judgment_points.py`(L217,1379,1469) / `tests/test_purpose_tree.py`
(L104,126) — いずれも `db.query(PersonaTask).filter(...)` で物理カラムを
直接検査するアサーション（P3a/P3bの「書き換えテスト」に相当するカテゴリ）。

---

## 2. note / note_page / note_message / track_open_note の消費者

### モジュール本体
- `saiverse/note_manager.py`（494行）— `Note`/`NoteMessage`/`NotePage`/
  `TrackOpenNote` を import する**唯一**のファイル（L32）。
- 公開メソッド: `create`(L78) / `ensure_desire_note`(L125) / `get`(L166) /
  `list_for_persona`(L177) / `archive`(L199) / `unarchive`(L219) /
  `close_project`(L235) / `touch_opened`(L260) / `add_page`(L277) /
  `remove_page`(L300) / `list_pages`(L318) / `add_message`(L331) /
  `remove_message`(L370) / `list_messages`(L388) /
  `get_notes_for_message`(L402) / `attach_to_track`(L415) /
  `detach_from_track`(L441) / `list_open_notes`(L459) /
  `list_tracks_with_note`(L476)

### `NoteManager(...)` インスタンス化箇所

| ファイル:行 | 用途 |
|---|---|
| `saiverse/saiverse_manager.py:228` | `self.note_manager` として永続保持（マネージャ属性） |
| `builtin_data/tools/note_create.py:18` | note_create スペル |
| `builtin_data/tools/note_open.py:17` | note_open スペル（`attach_to_track`+`touch_opened`） |
| `builtin_data/tools/note_close.py:13` | note_close スペル（`detach_from_track`） |
| `builtin_data/tools/note_search.py:18` | note_search スペル |
| `builtin_data/tools/purpose_seed.py:31` | `ensure_desire_note`（候補生成の入れ物確保） |
| `sea/head_pipeline/sections/open_notes.py:88` | head section本体（後述） |
| `saiverse/meta_layer.py:1325` | `_get_desire_candidates`（メタ判断状況構築） |
| `saiverse/judgment_points.py:210` | `_list_desire_tasks` の note 解決 |
| `saiverse/judgment_points.py:1371` | 欲求接触関連 |
| `saiverse/desire_engine.py:111` | `_desire_note_id` |
| `saiverse/day_scenario.py:864` | シム用 `ensure_desire_note` |
| `saiverse/day_report.py:225` | `_list_all_desires` |

### note スペル4本と open_notes head section（退役候補、intent doc既記載）
`docs/intent/concept_consolidation.md` P3c 記述に「Note→テーマノード統合・
TrackOpenNote→机の掛け替え・note スペル4本と open_notes section の退役も
ここ」と明記済み。実装コードにも先取りの記述があった:
`sea/head_pipeline/sections/desk.py:1-7`
「OpenNotesSection（旧Noteの開きっぱなし制御）の直系後継 — Noteがテーマ
ノードとしてAtlasに吸収されたらopen_notesは本セクションに畳まれる予定
（P2c/P3c）」— **P3c着手前から、後継のDeskSection（`memory_open`による
机の物理、memory_atlas.snapshot_deskがLRU管理）が既に本番稼働している**。

### note_page.page_id の解決コード
`NoteManager.add_page`/`remove_page`/`list_pages` は文字列 `page_id` を
そのまま保存/返却するのみ（L277-325）。**この page_id を実際に
memopedia 側へ引いて内容を取得するコードは見つからなかった**
（§0-4、§6 grep 参照）。`note_page`／`note_message` テーブルは
本番消費者ゼロ（`tests/test_note_manager.py` のみが exercise）。

### track_open_note の消費者
- `saiverse/note_manager.py`: `attach_to_track`(L415) / `detach_from_track`
  (L441) / `list_open_notes`(L459, `TrackOpenNote`→`Note`の二段階クエリ)
  / `list_tracks_with_note`(L476)
- 呼び出し元: `builtin_data/tools/note_open.py` / `note_close.py`
  （ユーザー操作の開閉）、`sea/head_pipeline/sections/open_notes.py:129-140`
  （`TrackManager.get_running`→`track_id`→`list_open_notes`で現在Trackの
  attachノート一覧をhead注入。desireノート自体は除外しL133、type/title/
  descriptionのポインタのみ表示・本文は出さない）

---

## 3. ActionTrack (TrackManager) の消費者規模

- `saiverse/track_manager.py`: 1157行、`def ` 40個（public寄り約28個:
  `create`/`get`/`set_title`/`list_for_persona`/`get_running`/`activate`/
  `pause`/`complete`/`abort`/`set_alert`/`forget`/`recall`/
  `set_parameter`/`resolve_track_ref`/`get_tasks`(旧互換)/`add_task`/
  `complete_task`/`format_task_list`/observer登録系6個 等）。
- `TrackManager` / `.track_manager属性` 参照ファイル数: **55ファイル**
  （`tests/`含む。本番コードだけでも `saiverse/track_handlers/*` 3本、
  `builtin_data/tools/track_*.py` 6本、`saiverse/judgment_points.py`、
  `saiverse/purpose_tree.py`、`saiverse/day_scenario.py`、
  `saiverse/autonomy_wiring.py`、`saiverse/game_lifecycle.py`、
  `api/routes/people/{tracks,activity,debug,info}.py` 等）。
- **P3cのスコープには含まれない**: `docs/intent/concept_consolidation.md`
  のP3c記述は`persona_task`/`note`のみを対象にしており、`action_track`
  自体の物理移動は明記されていない。`saiverse/purpose_tree.py`（休眠中）
  も「ActionTrackの行データは残す — 第一階層の目的ノードへ写像。
  **テーブル統合はしない**」（§10.1）と明言しており、既存設計判断として
  Track自体を第一階層構成のまま main DB に残す前提がある。
- 判断材料としての含意: Track (55ファイル規模) を今回巻き込まないことで
  P3cの実質的な変更面積は「persona_task消費者(§1: 約27箇所) + note消費者
  (§2: 約13箇所) + track_open_note(4箇所)」に絞られ、TrackManagerの
  55ファイル分の呼び出し元は**無改修で温存できる**（persona_task.track_id
  という文字列参照が指す先がmain DBのままなら、Track側は何も変わらない）。

---

## 4. 物理移動しない場合 / する場合の得失（事実列挙）

### 移動しない場合に既に得られているもの
- cross-DB ref 解決の実例が既にある: 写真`pasted_to="task:N"`
  （memory.db側）→ `PersonaTaskManager.resolve_task_ref`（main DB側）
  の解決は `saiverse/memory_atlas.py:251-274` で実装済み・実運用中。
- desk（机）の開閉制御はrefベースで既に実装済み: `sea/head_pipeline/sections/desk.py`
  が `memory_open` で開いたページを ref ポインタとしてhead注入し、
  Metabolism跨ぎで保持・LRU棚戻しまで面倒を見る（`memory_atlas.snapshot_desk`）。
  これは「object自体が物理的にmemopediaにある」ことを前提にした機構だが、
  「note_id という間接参照層」を挟まなくても `task:N` / `track:N` を直接
  desk に乗せる設計は既存パターンの延長で成立しうる（実装なしでの推測は
  避けるが、**同型の前例がある**という事実は指摘できる）。
- `note_page`/`note_message`（Note↔ページ/メッセージの多対多）は本番未配線
  （§2）なので、これらのテーブルを畳む・畳まないの判断は実質「使われて
  いない設計をどうするか」に近い — 移行の実利用インパクトは小さい。

### 物理移動する場合に手当てが要るもの（事実）
- `PersonaTaskManager`/`NoteManager`のコンストラクタは
  `session_factory: Callable[[], Session]`（main DB前提）を取り、
  全27+13箇所の呼び出し元が`manager.SessionLocal`を渡している。
  memory.dbは「persona単位のsqlite3生conn」（`adapter.conn`）なので、
  ファサードのシグネチャそのものを「persona_id→内部でconn解決」の形に
  変える必要がある（P3a/bの「関数シグネチャ・dataclassは変えない」
  strangler-figがそのままは効かない一番の理由）。
- `persona_task.note_id`（FK→note.note_id）と`persona_task.track_id`
  （FK→action_track.track_id）: FK自体は実行時未強制（§0-1）だが、
  **note が main DB に残り persona_task だけ memory.db に移る**シナリオ
  では、「候補(candidate)がどの desire ノートに属すか」を cross-DB
  文字列参照で持つ形に変わる（実害はないが設計上の非対称が生まれる:
  note→track の紐付けは main DB 内で完結するが、note→task は cross-DB
  になる）。
- 全ペルソナの`persona_task`行を含む main DB 単一テーブルを N人分の
  memory.dbへ振り分ける移行は、P3a/bの「1ファイル内一回きり移行→DROP」
  よりスコープが広い（§0-6）。「未移行ペルソナが1人でも残っていると
  旧テーブルをDROPできない」という運用上の制約が新たに生まれる。
- `api/routes/people/tasks.py:27-34`の`ActionTrack.track_id.in_(track_ids)`
  バッチ解決は、`persona_task`から得た`track_id`集合をmain DBに投げる
  パターン。persona_task が memory.db 化しても「先にmemory.dbからtrack_id
  集合を集めて、次にmain DBへ問い合わせる」という2段階に組み替えれば
  動作は保てる（§0-2と同型）。

---

## 5. Note→テーマノード統合（Noteだけ先に畳む場合）の面

- 実データの使われ方: 「desire ノート」1種のみが実質的にactiveな消費
  経路を持つ（`ensure_desire_note`経由、`purpose_seed`が候補生成時に
  作る）。person/project/vocationの3種（`note_create`スペルで作成可能）
  は`open_notes` head section の「いま開いているノート」表示
  （L127-146,177-186）と`note_search`/`note_open`/`note_close`スペル
  経由でしか触れられておらず、実LLMシムでの実使用頻度は本監査の範囲
  （静的コード調査）では確認できない（要:実行ログでの裏取り）。
- Note を畳んだ場合に追従が要る消費者（§2の一覧から）:
  1. `sea/head_pipeline/sections/open_notes.py`（全体）— 退役予定と
     intent docに明記済み。後継の`sea/head_pipeline/sections/desk.py`
     が既に本番稼働中のため、追従というより「置き換え」に近い。
  2. `builtin_data/tools/{note_create,note_open,note_close,note_search}.py`
     4本 — 同じく退役予定と明記済み。
  3. `saiverse/meta_layer.py:_get_desire_candidates`、
     `saiverse/judgment_points.py:_list_desire_tasks`、
     `saiverse/desire_engine.py:_desire_note_id`、
     `saiverse/day_report.py:_list_all_desires`、
     `saiverse/day_scenario.py`のens ure_desire_note呼び出し —
     いずれも「desireノートのnote_idを起点にparent_kind='note'の
     persona_task行を引く」という同一パターン（`NOTE_TYPE_DESIRE`で
     `list_for_persona`→`note_id`→`ptm.list_tasks(note_id=...)`）。
     Noteをテーマノード化するとこの「note_id起点」の解決を
     「テーマノードref起点」に置き換える改修が全5箇所で必要になる
     （形は同一なので機械的に追従可能そうに見えるが、実装前提の確認は
     未実施）。
  4. `note_page`/`note_message`（§2, §0-4）: 本番消費者ゼロのため、
     畳む/畳まないの判断は実装リスクよりも「設計として残すか削るか」の
     問題に近い。

---

## 6. 再監査用 grep パターン

```bash
# persona_task 系 ORM 消費者（唯一の import 元を確認）
grep -rn "from database.models import.*PersonaTask" --include=*.py .
grep -rn "PersonaTaskManager(" --include=*.py .
grep -rn "db\.query(PersonaTask)" --include=*.py .

# note 系 ORM 消費者
grep -rn "from database.models import.*\bNote\b" --include=*.py .
grep -rn "NoteManager(" --include=*.py .
grep -rn "\.add_page(\|\.list_pages(\|\.add_message(\|\.list_messages(\|\.get_notes_for_message(" --include=*.py .

# track_open_note / ActionTrack 結合の有無（本物のJOINが後から増えていないか）
grep -rn "query(PersonaTask)\..*\.join(\|query(Note)\..*\.join(\|query(ActionTrack)\..*\.join(" --include=*.py .

# FK強制の有無（session.py の PRAGMA を再確認）
grep -n "foreign_keys\|PRAGMA" database/session.py

# desire ノート起点パターン（Note畳み時に追従が要る箇所の再列挙）
grep -rn "NOTE_TYPE_DESIRE" --include=*.py .

# 生SQL / 直接ORM（Manager非経由のバイパス箇所の再確認）
grep -rn "persona_task\b" --include=*.py database/ scripts/
grep -rn "note_page\|note_message\|track_open_note" --include=*.py database/ scripts/

# purpose_tree.py が実際に配線されたかの再確認（現状: 休眠）
grep -rn "purpose_tree" --include=*.py .

# TrackManagerのスコープ確認（Track自体がP3cに巻き込まれていないか）
grep -n "action_track\|ActionTrack" docs/intent/concept_consolidation.md
```

---

## 付記: 調査範囲の限界

- 本監査は静的コード調査（grep/read）のみで、実DBの行数・実LLMシムでの
  実使用頻度（例: person/project/vocation Noteが実際にどれだけ作られて
  いるか）は確認していない。§5の「実使用頻度は確認できない」は事実として
  明記した。
- `saiverse/purpose_tree.py`が「どこからも呼ばれない」という判定は
  grepベース（呼び出し元ファイル一覧に`tests/test_purpose_tree.py`以外
  現れないこと）による。実行時に動的import等で呼ばれる経路がないかは
  完全には排除できない。
