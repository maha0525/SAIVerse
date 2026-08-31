# ハンドオフ: Memory Atlas P2c 消費者監査（2026-07-10）

> **監査完了・コード変更なし。** `docs/intent/concept_consolidation.md` の P2c
> （旧スペル撤去・purpose 動詞・`task:N` 解決・life API `/marks`→`/photos`）へ
> 入る前に、現リポジトリの消費者を読み取り専用で棚卸しした。
>
> 調査時点: branch `feature/autonomous-behavior-v2`, HEAD `79dc501`。
> 停止中の DeskSection WIP（`saiverse/memory_atlas.py` / `sea/head_pipeline/sections/`
> / 対応テスト）は**変更していない**。

## 0. 結論

旧ツール群は、現時点では**一括削除できない**。名称だけを置換できるものと、統一 API 側に
まだ等価な操作がないものが混在している。

- 旧対象は **26 ツール**。
  - ペルソナ向け `spell=True`: **18**
  - Playbook TOOL ノード等からだけ使う非 Spell: **8**
- 新 Atlas Spell は `memory_read/open/close/search/write/clip` の **6 本**。
- `core_memory_remove`（削除）と `core_memory_add_scene`（全文転写）には、現行6本の中に
  等価な置換先がない。
- `memory_write m:N` は既存 Memopedia ページへの**追記だけ**。ページ新規作成、全文置換、
  summary/category/keywords 更新、移動、削除は代替できない。
- Note / Task / Desire は `task:N` の Atlas 解決と purpose 動詞が未実装なので、旧スペルを
  先に消すと MetaLayer・判断点・目的の木の操作が切れる。
- 公開 Playbook 6本が旧名を持つ。うち `track_autonomous` は「移行」ではなく**削除対象**、
  残りは休眠・旧自律系と庭仕事候補が混在する。
- `/marks` は保存層だけ既に photos 化済み。残る消費者は API 名・レスポンス名・
  MemoryBrowser・テスト・APIリファレンス。

したがって P2c は、次の順が安全:

1. 等価操作の無い4論点を設計決定
2. `task:N` + purpose 動詞を実装
3. live prompt/code consumer を新名へ原子的に切替
4. 公開 Playbook を「移行 / 退役 / P4へ保留」に分類して処理
5. 旧 tool ファイル削除、`/photos` 一括切替、参照ドキュメント再生成

## 1. 監査対象の旧ツール面

### ペルソナに露出する旧 Spell（18本）

| 群 | Spell |
|---|---|
| core memory (4) | `core_memory_add`, `core_memory_add_scene`, `core_memory_update`, `core_memory_remove` |
| Memopedia (5) | `memopedia_get_page`, `memopedia_note`, `memopedia_list_fragments`, `memopedia_edit_fragment`, `memopedia_delete_fragment` |
| Note (4) | `note_create`, `note_open`, `note_close`, `note_search` |
| Task / Desire (5) | `task_add`, `task_decompose`, `task_update_step`, `task_done`, `desire_add` |

これらは `builtin_data/tools/*.py` の `ToolSchema(spell=True)` により自動登録され、
`SpellListSection` を通じて head に載る。新 Atlas Spell 6本と旧18本が現在は同時に露出するため、
P2c の撤去は head スペル一覧ダイエットにも直接効く。

### 非 Spell の旧ツール（8本）

`memopedia_close_page`, `memopedia_get_tree`, `memopedia_health`, `memopedia_manage`,
`memopedia_open_page`, `memopedia_save_page`, `memopedia_search`, `get_task_summary`。

これらはペルソナの平文 Spell 一覧には出ないが、公開 Playbook の TOOL ノード、
内部の案内文、テストから参照されるため、Spell を隠すだけでは撤去できない。

## 2. 置換可否マトリクス

| 旧操作 | 現行の候補 | 判定 / 差分 |
|---|---|---|
| `core_memory_add` | `memory_write(ref="core")` | **等価。置換可能** |
| `core_memory_update` | `memory_write(ref="c:N")` | **等価。置換可能** |
| `core_memory_remove` | なし | **設計穴**。`memory_close` は削除ではない。`memory_delete` 新設等の判断が必要 |
| `core_memory_add_scene` | `memory_clip`? | **非等価**。旧SCENEは transcript を本文へ転写、`memory_clip` は参照貼り＋抜粋のみ。現コードも「転写は旧SCENEの役割」と明記 |
| `memopedia_get_page` | `memory_read(m:N)` | **等価。置換可能** |
| `memopedia_open_page` / `close_page` | `memory_open` / `memory_close` | **等価。置換可能** |
| `memopedia_search` | `memory_search` | **検索範囲拡張の上位互換**（Memopedia + Chronicle） |
| `memopedia_note` | `memory_write(m:N)` | **一部のみ**。既存ページ追記は可、新規ページ作成・タイトル検索追記・summary更新は不可 |
| `memopedia_save_page` | `memory_write(m:N)` | **一部のみ**。新規作成、全文更新、category/summary/keywords は不可 |
| `memopedia_get_tree` | なし | 目次/head または URI tree の扱いを決める必要あり |
| `memopedia_health` | なし | P4の決定論検知・庭仕事候補へ移す性質。persona 共通動詞ではない |
| `memopedia_manage` | なし | 削除・移動・重要フラグ・vividness。vividness は廃止予定だが他操作は残る |
| Fragment 3本 | なし | P4の分割/統合ワーカーへ寄せる候補。現時点で削除すると `fragment_organize` が壊れる |
| `note_open/close/search` | Atlas + `task:N` | **P2c実装後に置換可能**。現行 `memory_*` の `task:N` は stub |
| `note_create` | purpose 動詞? | **設計差分あり**。知識ページ作成か目的候補作成かを category/nature で決める必要あり |
| `task_update_step` | `purpose_step` | intent 上の置換先。新 tool は未実装 |
| `task_done` | `purpose_close(status=completed)` | intent 上の置換先。新 tool は未実装 |
| `task_add` / `task_decompose` | `purpose_adopt` / `purpose_step` | 引数・意味の1:1対応を要設計（Trackへの追加と細分化は別操作） |
| `desire_add` | なし（`purpose_adopt` は候補の採用） | **設計穴**。候補を新規に生む操作と、既存候補を採用する操作は別 |
| `get_task_summary` | `memory_read(task:N)`? | 一覧/集約を返す等価操作なし。headの目的目次で代替するか判断が必要 |

### P2c 着手前に決める4点

1. **ページ削除**: `core_memory_remove` と Memopedia delete を統一する動詞を作るか。
2. **転写**: `core_memory_add_scene` を残すか、`memory_clip` に参照貼り/転写モードを持たせるか。
3. **ページ作成・構造編集**: `memory_write` を create/replace/metadata まで広げるか、庭仕事用の別動詞を残すか。
4. **候補生成**: `desire_add` 相当（候補を生む）と `purpose_adopt`（候補を木に接ぐ）を分けるか。

## 3. 公開 Playbook の消費者

`builtin_data/playbooks/public/*.json` で旧ツール名を持つのは6本。全て起動時同期の対象で、
`router_callable=false`。ファイルを `public/` から外すと `playbook_sync` が source_file 付き
DB行と PlaybookPermission を prune する。

| Playbook | 旧ツール | 現在地 | P2c 方針 |
|---|---|---|---|
| `track_autonomous` | task 4本 + `desire_add`、説明文に `note_open` | v1自律駆動。landscape §9で死亡確定。ただし JSON は `user_selectable=true` で残存 | **移行せず退役**。public JSON除去 + DB prune + 選択設定/手書きカタログ監査 |
| `meta_autonomy_decision` | `memopedia_health` | v1自律能力選択。コード直参照なし | ④オートノミー整理の結論に合わせて退役候補 |
| `autonomy_creation` | `memopedia_search` | 休眠（phase3 issue/revisions に明記） | 復活させるなら `memory_search` へ。不要なら archive |
| `autonomy_web_research` | `memopedia_get_tree`, `memopedia_save_page` | 休眠 | `memory_search`/新規ページ作成契約が決まるまで機械置換不可 |
| `autonomy_memory_organization` | get_tree/get_page/save/search/health/manage | 休眠 | P4庭仕事への再設計対象。現行のまま名前だけ替えない |
| `fragment_organize` | list/edit/delete fragment | コード直参照なし。手動/子Playbookとしてはファイル同期される | P4ワーカーに吸収するか、それまで旧内部ツールを保留 |

### `track_autonomous` 退役時の追加注意

- `user_selectable=true` のため、DBに残る間は `get_selectable_meta_playbooks()` の候補になり得る。
- `UserSettings.SELECTED_META_PLAYBOOK` に旧名が保存されている可能性がある。既存 upgrade handler の
  deprecated 集合には `track_autonomous` が含まれていない。
- `docs/reference/playbook-catalog.md`, `docs/features/autonomous-mode.md`,
  `docs/features/playbooks.md`, `docs/concepts/playbook.md`, `docs/user-guide/memopedia.md` は
  v1 系を現役として説明している。
- `saiverse/activity_view.py` の `_SKIP_PLAYBOOK_NAMES` と対応テストは「過去ログ表示の互換」として
  残す意味があるため、JSON削除と同時に機械的に消さない。

## 4. live code / prompt 消費者

| 消費者 | 参照 | 必要な追従 |
|---|---|---|
| `builtin_data/prompts/common.txt` | `memopedia_get_page`, core_memory 3本 | ペルソナへの主要教示。新 Atlas 6動詞へ原子的に更新 |
| `sea/auto_recall.py` | Memopedia/Fragment の深掘り先=`memopedia_get_page` | `memory_read` に変更。`tests/test_auto_recall.py` 期待値追従 |
| `builtin_data/tools/get_memory_weave_context.py` | `memopedia_open_page` の説明、深掘り案内=`memopedia_get_page` | Desk/`memory_read` 語彙へ更新 |
| `builtin_data/tools/memory_recall_unified.py` | 結果後の案内=`memopedia_get_page` | `memory_read` へ更新 |
| `saiverse/meta_layer.py` | `_META_LAYER_SPELL_NAMES` に Note 4本、prompt に `note_open` | purpose 動詞導入と同じ変更で更新。先に旧 Note を消さない |
| `sea/mode_spell_permissions.py` | task 4本 + `desire_add` のモード制御 | purpose 動詞名・権限へ更新 |
| `builtin_data/tools/judgment_finalize.py` | `_fire_spell("desire_add", ...)` | **現役の機械消費者**。候補生成の新契約へ更新必須 |
| `saiverse/track_manager.py` | 候補補充の指示に `desire_add` | 新しい候補生成動詞へ更新 |
| `saiverse/activity_view.py` | 旧Memopedia/Note/Task名の人間向け表示・非表示集合 | 新名のテンプレート追加。旧名は履歴表示互換として残すか判断 |
| `sea/head_pipeline/sections/core_memory.py` | docstring に core_memory 3本 | 新語彙へ更新 |

`api/routes/people/core_memory.py`, `sai_memory/core_memory.py`, `memory_atlas.py`,
`memory_clip.py` にも旧名があるが、主に共有実装・互換の説明である。旧 tool 削除時に、
実装共有ヘルパは消さずコメントを現契約へ直す。

## 5. `/marks` → `/photos` 消費者

保存層は既に `sai_memory/photos.py`。APIは点写真だけを旧 `MarkItem` へ詰め直している。

| 層 | 現在の場所 | 追従内容 |
|---|---|---|
| Backend route | `api/routes/people/life.py` | route `/marks`、`MARKS_BATCH_LIMIT`, `MarkItem`, `MarksResponse`, `marks` field を photo 語彙へ |
| Frontend | `frontend/src/components/memory/MemoryBrowser.tsx` | URL、型名、state、renderer/コメントを photo 語彙へ。描画は点写真のみのままでよい |
| API tests | `tests/test_life_view_api.py` | `/photos` と response `photos` へ更新 |
| Reference | `docs/reference/api-endpoints.md` | endpoint と説明更新 |

`tests/test_photos.py` 内の `marks` テーブルは**旧DBからの一回きり migration fixture**なので、
API改称時にも消したり `photos` に書き換えたりしない。

互換 alias を残すと死んだ名称を再び背負うため、外部公開互換の要件が無い限り、Backend +
Frontend + tests + reference を1コミットで切り替えて旧 route は削除する方が本 intent に合う。

## 6. テスト消費者

旧名に直接依存するテストファイル:

- core: `test_core_memory_tools.py`, `test_core_memory_scene.py`
- Note/Purpose: `test_cognitive_model_tools.py`, `test_desire_types.py`,
  `test_judgment_points.py`, `test_mode_spell_permissions.py`, `test_open_notes.py`,
  `test_purpose_tree.py`, `test_spell_args_parsing.py`, `test_task_tools.py`
- Memopedia guidance: `test_auto_recall.py`
- API: `test_life_view_api.py`
- migration（残す）: `test_photos.py`

旧 tool 単体テストは削除するだけでなく、同じ不変条件を新 Atlas / purpose tool のテストへ
移植する。特にコア記憶の容量制約、SCENEの原文忠実性、目的操作のモード権限、
`judgment_finalize → 候補生成` は回帰保護を失わないこと。

## 7. ドキュメント消費者

実装と同時更新が必要な現役ドキュメント:

- `CLAUDE.md`（Playbook例・旧語彙）
- `docs/reference/tool-catalog.md`（自動生成。旧 tool 削除後に再生成）
- `docs/reference/playbook-catalog.md`（手書き。Playbook退役/再設計を反映）
- `docs/reference/api-endpoints.md`（`/photos`）
- `docs/user-guide/memopedia.md`（旧自律Playbookを現役とする説明）
- `docs/features/autonomous-mode.md`, `docs/features/playbooks.md`,
  `docs/concepts/playbook.md`（`track_autonomous` 退役追従）
- `docs/overview/landscape.md`（本文/リレーション表はv1記述が残る。§9だけは退役済み）

`docs/intent/`, `docs/issues/`, `docs/handoff/`, `docs/old/` の過去名は設計履歴なので、
検索ゼロを目的に一括改稿しない。現況を述べる status/log だけ追従させる。

## 8. 外部・ローカル上書き

- `expansion_data/*/playbooks/public` に旧 tool 名の Playbook 消費者は見つからなかった。
  voice-tts の `queue.task_done()` は名前の偶然一致で無関係。
- `~/.saiverse/user_data/playbooks` に旧 tool 名の JSON は見つからなかった。
- ただし P2c 実装時点で追加されている可能性があるため、削除直前に同じ監査を再実行する。

## 9. 推奨実装分割

### P2c-0: 仕様穴の決定（コード前）

削除、転写、ページ作成/構造編集、候補生成の4点を intent に追記してから着手する。

### P2c-1: `task:N` + purpose 動詞

- Atlas facade の `task:N` stub を解消。
- purpose tool を追加。
- `meta_layer`, `mode_spell_permissions`, `judgment_finalize`, `track_manager` を同時切替。
- `open_notes` をいつ Desk/目的ページへ畳むかを明示（P2cかP3cか）。

### P2c-2: 等価な記憶動詞の切替

- common prompt / auto recall / memory weave helper / activity view を新名へ。
- 等価部分の旧 Spell を削除。
- 等価でない Memopedia庭仕事ツールは P4まで内部専用で残すか、Playbookごと休眠させる。

### P2c-3: v1 Playbook の退役・休眠整理

- `track_autonomous` は削除（移行しない）。設定値の巻き取りを追加。
- 旧 `meta_autonomy_decision` と autonomy 3本は④オートノミー整理の結論に従う。
- `fragment_organize` はP4の庭仕事へ接続するまで保留またはarchive。

### P2c-4: API と最終撤去

- `/marks`→`/photos` を Backend/Frontend/tests/reference 同時切替。
- 旧 tool ファイル削除後、`gen_reference_docs.bat`（非Windowsは
  `python scripts/gen_reference_docs.py`）で tool catalog を再生成。
- `scripts/import_all_playbooks.py --dry-run` で退役 Playbook の prune 対象を確認。

## 10. 完了条件

1. 現役 source / public Playbook / common prompt に撤去対象名が残っていない。
2. 歴史 doc と migration fixture だけが意図的残存として説明できる。
3. `track_autonomous` が selectable DB 行・保存設定・公開カタログに残らない。
4. `memory_*` / `purpose_*` で旧操作の必要な不変条件を回帰テストできる。
5. `/photos` が MemoryBrowser で点写真ハイライトを維持する。
6. changed Python に `ruff check`、対象 pytest、Playbook graph/import 検証、参照doc再生成が通る。

## 11. 再監査用コマンドの考え方

削除直前には、次の名前集合を source / `builtin_data/playbooks/public` / tests / user_data
Playbook で再検索する:

```text
core_memory_(add|add_scene|update|remove)
memopedia_(close_page|delete_fragment|edit_fragment|get_page|get_tree|health|
           list_fragments|manage|note|open_page|save_page|search)
note_(close|create|open|search)
task_(add|decompose|done|update_step)
desire_add|get_task_summary|/marks
```

検索結果は「ゼロ件」ではなく、歴史文書・旧DB migration test・activity history互換など、
残す理由を1件ずつ説明できる状態をゴールにする。
