# ハンドオフ: 参照アドレッシング統一 — 一括切替(Plan B)【完了】

> **✅ 完了 (2026-07-06, commit 6ea9d44)**: (A)item / (B)URI平坦化+messagelog→message /
> (C)day_plan slots_json 移行 / (D)プロンプト / (E)テスト を全て実装し、参照系テスト
> 258 緑・ruff クリーンで 1 コミット。以下は着手時のメモ（履歴として残す）。次は実機検証。
> 着手時の想定から踏み込んだ点: **memopedia も short_id 化**（page.id=uuid が recall/戻り
> でペルソナに漏れていたため item と同基準に）、`_format_pickable_tracks` の t:N 残存
> (表示/enum ドリフト) と memopedia ツール6箇所の m: 残存を修正。

次に引き継ぐエア向け。設計の正典は `docs/intent/reference_addressing.md`（v1.0、
コミット済み）。この文書は**実装の進捗と作業ツリーの状態**だけを扱う。

## 0. 一言

参照記法（`t:`/`task:`/`desire:`/`b:`/`i:`/`m:` + `saiverse://` URI）を統一規格へ
切り替える作業。**Plan B（全種類を1整合状態でまとめて切替、フルスイート緑で1コミット）**。
track・memopedia・task/desire 統合の**コード側**が済み、item・URI・移行・プロンプト・
テストが残っている。

## 1. ⚠️ 作業ツリーに未コミット WIP がある（絶対に失うな）

**この切替のコード変更は未コミット**（Plan B なので緑になるまでコミットしない）。
`git stash` / `git checkout` / `git reset` / `git restore` を**これらのファイルに対して
使わないこと**（[[feedback_no_git_checkout_on_uncommitted]]）。私の WIP は次の18ファイル:

```
api/routes/people/tasks.py
builtin_data/tools/_track_common.py
builtin_data/tools/desire_add.py
builtin_data/tools/judgment_finalize.py
builtin_data/tools/track_create.py
builtin_data/tools/track_list.py
sai_memory/memopedia/storage.py
saiverse/day_plan.py
saiverse/day_scenario.py
saiverse/desire_engine.py
saiverse/judgment_points.py
saiverse/meta_layer.py
saiverse/pulse_scheduler.py
saiverse/track_handlers/autonomous_track_handler.py
saiverse/track_handlers/user_conversation_handler.py
saiverse/track_manager.py
sea/auto_recall.py
tests/test_track_short_id.py
```

**混ぜない別セッションの差分**: `docs/intent/screen_avatar.md` /
`docs/reference/api-endpoints.md`（触らない・stage しない）。コミットは必ずファイル明示 add。

## 2. コミット済み（Phase 0 の土台 + 設計）

| commit | 内容 |
|---|---|
| 2165d3f | item 安定 short_id（`Item.SHORT_ID` 列 + before_insert リスナー + backfill）+ intent doc |
| 72ad3f9 | `saiverse/references.py`（書式・相互変換の中央モジュール、26 tests）+ test_references |
| 057a336 | 移行スコープ・Plan B 確定（doc） |
| 95db8c4 | task/desire 統合の成立確認（doc） |

`saiverse/references.py` は `to_short_ref` / `to_uri` / `parse_ref` を持つ純粋な文字列層。
生成側は `to_short_ref`/`to_uri` を、解析側は `parse_ref` を通すのが理想（まだ全面委譲は
していない。各 kind の既存 resolver の regex/文字列を新書式に直す形で進めている）。

## 3. WIP で済んだこと（コード側・ruff クリーン・track テスト緑）

### track: `t:N` → `track:N`
- 解決器 `track_manager.py`: `_SHORT_REF_RE = re.compile(r"^track:(\d+)$", re.IGNORECASE)`
  （旧 `[Tt]:` の case-insensitive を踏襲）+ docstring/エラーメッセージ。
- 生成子（全部 `f"t:{...}"`→`f"track:{...}"`）: `_track_common.format_short_id`、
  `meta_layer._short_ref`、`pulse_scheduler`、`autonomous_track_handler`、
  `user_conversation_handler`、`track_list`、`track_create`、`judgment_finalize`、
  `judgment_points`(264/307/713)、`api/routes/people/tasks.py`。
- `tests/test_track_short_id.py` 更新（`track:1`/`Track:1`）→ **11 tests 緑**。

### memopedia: `m:N` → `memopedia:N`
- `sai_memory/memopedia/storage.py`: `_SHORT_ID_RE = re.compile(r"^memopedia:(\d+)$", re.IGNORECASE)` + docstring。
- 生成子 `sea/auto_recall.py:751` `(m:{short_id})`→`(memopedia:{short_id})`。
- 未了の小物: storage.py:70 の short_id コメント `(m:1, m:2, ...)` の字面。

### task/desire 統合: 符号を `task:N` に一本化（`desire:` 廃止）
- `judgment_points.py`: `collect_slot_ref_enum` / `collect_promotion_refs` /
  `collect_today_touched_desire_refs` の `"desire:" + ref[len("task:"):]` を `ref` に（＝
  そのまま `task:N`）。`to_desire_ref` の import 削除、1147 の使用も `task_ref` 直取りに。
- `desire_engine.desire_summary_for_prompt` / `desire_add.py`: `to_desire_ref` 使用を廃し
  `task:N` 直出し（import も削除）。
- **挙動保存の要点**: `day_plan._fire_slot` は旧 `ref.startswith("desire:")` で欲求再訪を
  記録していた。統合後は全 ref が `task:N` なので、`ref.startswith("task:")` で
  `touch_desire` を呼ぶ形に変更。`touch_desire` は `parent_kind != PARENT_NOTE`（＝
  非 desire）を内部で安全に no-op するので、バックログタスクのコマ発火で呼んでも害なし。
  その no-op ログは正常系になるので `WARNING`→`DEBUG` に下げた。
- `day_scenario.py`: seeded_desire_refs の `task:`→`desire:` 変換を撤去。
- **判断が要る残り**: `desire_ref` という**スキーマのフィールド名**は残してある（値だけ
  `task:N` 化）。`_REF_RE`(`^(task|desire):`) と `normalize_task_ref`/`_normalize_ref` は
  `desire:` を受理するlenientなまま残した（防御的パース。Q4=B を厳密にやるなら、
  slots_json 移行後に task: のみへ絞ってよい）。

## 4. 残り（未着手・重い順）

### (A) item: `b:N`/`i:N`（位置）→ `item:N`（同一性） ← 最大の山・実ロジック変更
Phase 0 で `Item.SHORT_ID` は入れた（world 全体連番、`self.items` ローダーは `short_id`
を載せる）。残り:
- **`manager/items.py::resolve_slot_ref`(:1337)**: いまは `b:N`/`i:N`/`b:N>M`（位置）を
  解決。これを `item:N`（short_id で item を引く）解決に置換。位置(slot)は識別子から外す。
  バッグ入れ子 `>` は、入れ子アイテムも自分の `item:N` を持つので不要になる（要検討）。
- **生成/表示**: `sea/head_pipeline/sections/building_items.py`（通知ラベル。今 `b:{slot}`）、
  `builtin_data/tools/get_visual_context.py`(571/584/156 + `_render_bag_contents`)、
  `image_generator.py`(709)/`generate_image_local.py`(284) の戻り文言。**位置は locator
  として残す**（同一性は `item:N`）。
- **Phase 0 で保留した分**: item 生成メソッド5つ（`create_document_item` 等）のインメモリ
  キャッシュ `self.items[item_id]` に `short_id` を載せる（今はローダーのみ）。5メソッドを
  共通の仕上げに寄せてまとめて入れると綺麗。
- **URI**: `saiverse://item/b:3` → `saiverse://item/N`。`content_tags.py` の item URI
  正規表現 `_ITEM_SLOT_URI_RE` と `resolve_item_slot_uris`。
- item ツール（`item_move`/`item_view`）の description・引数説明を `item:N` に。

### (B) URI 平坦化 + ペルソナ path
- `sai_memory/unified_recall.py`（8箇所）: `saiverse://self/chronicle/entry/{id}`→
  `.../chronicle/{id}`、`.../memopedia/page/{id}`→`.../memopedia/{id}`、message はそのまま。
- `chronicle_context_up.py`/`chronicle_context_down.py`、`memopedia_note.py`(135 生成 + 25 正規表現)、
  `memopedia_list_fragments.py`(13 正規表現)、`content_tags.py` の item URI 正規表現。
- `saiverse/uri_resolver.py`: `PERSONA_RESOURCE_TYPES`（`messagelog`→`message`）と、
  `chronicle/entry`・`memopedia/page` の階層を平坦化。`_core_memory_common.parse_message_ref`。
- 理想は `references.to_uri`/`parse_ref` へ委譲（`saiverse://self/<kind>/<key>`）。

### (C) 保存済みデータ移行（`database/migrate.py`、構造化のみ）
- `persona_day_plan.slots_json` の各コマ ref を `desire:`→`task:`。JSON を読み各 slot の
  `ref` を書き換える backfill 関数を追加し、起動時（main.py）に呼ぶ。**過去の自然文は触らない**
  （intent §5 確定）。item の SHORT_ID backfill は Phase 0 で済み。

### (D) ペルソナへの記法提示（プロンプト文言）
- `judgment_points.py` のプロンプト文字列（「ref に `task:N` / `desire:N` を指定」→ `task:N` のみ）、
  `day_plan.py` の指示書テンプレート（`KIND_*`）、item ツール description、visual_context 凡例。

### (E) テスト ~9本の期待値更新
- `test_judgment_points.py`: 303/1393（`desire_ref` enum `["desire:2"]`→`["task:2"]`）、
  832（slots ref `["desire:2"]`→`["task:2"]`）、1408（`touched_desire_refs`）、picked track 系。
- `test_desire_types.py`: 264/389（`replace("task:","desire:")` を除去）、367
  （`startswith("desire:")`→`task:`）、**370（`assert "task:" not in text` は反転する** —
  統合後は task: が出るので期待を変える）。
- `test_day_plan.py`(294 等)、`test_budget_gate.py`、`test_work_session.py`、
  `test_open_notes.py`、`test_tasks_api.py`、`test_auto_recall.py`、`test_core_memory_scene.py`。
- 新規: item(`item:N`) と URI 平坦化の往復テスト。`references` の往復テストは済（test_references）。

## 5. 再開手順

1. §1 の WIP を失っていないか `git status` で確認（18ファイルが M のはず）。
2. (A)→(B)→(C)→(D) の順でコード、各 kind ごとに (E) の該当テストを緑にしていく。
3. 全部揃ったらフルスイート（`test_searxng_search.py` は既知の collection error なので
   `--ignore=tests/test_searxng_search.py`）を緑に。
4. 緑になったら**ファイル明示 add で1コミット**（別セッションの screen_avatar/api-endpoints は混ぜない）。

## 6. 罠・原則の再掲

- Plan B: 途中でコミットしない。全緑まで1つの整合状態。
- 移行は構造化データのみ（day_plan slots_json）。自然文の旧 URI/短縮参照は書き換えない。
- 単語 prefix で統一（P6）。ペルソナ依存の URI はペルソナを含む（P7）。位置(slot)は
  同一性でなく locator（I5）。表示と enum の表記は必ず一致（I3）。
- python は `.venv/Scripts/python.exe`。
