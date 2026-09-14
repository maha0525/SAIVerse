# 隔離環境での実測 — ペルソナ削除後の記憶の再利用 (2026-09-10 第三段 / 実験担当)

## この報告の読み方

全部、**隔離環境で実際にコードを走らせた結果**です。読んだだけの推測には
「静的な疑い」と付けてあります。実行していない範囲は「未実施」と書きました。

各実験の再現コマンドと入力・観測は節ごとに載せてあります。

---

## 0. 実験環境と、守った境界

**確定**（すべて実行して確認）

- `SAIVERSE_HOME` は一時ディレクトリへ向けた。共通セットアップの中で、
  本番を指していないことを `guard_not_production()` が毎回検査する。
- `~/.saiverse/` には読み書きしていない。実験後に
  `ls ~/.saiverse/personas | grep probe` を実行し、合成ペルソナのフォルダが
  1 つも作られていないことを確認した。
- 製品ファイル・テスト・git の状態は一切変更していない。実験後の
  `git status --porcelain` は、実験開始前と同じ未追跡ファイルだけを出した。
- ペルソナ ID はすべて合成 (`probe_a_testcity` / `probe_b_testcity` /
  `probe_c_testcity` / `probe_deleted_testcity`)。実ペルソナの ID・名前・
  本文は種にしていない。
- python は `.venv/Scripts/python.exe`。
- **LLM は一度も呼んでいない。** ただし埋め込み (ベクトル) の生成は
  一部で使った。使ったのはリポジトリ内にあるローカルの ONNX モデル
  (`sbert/multilingual-e5-small`) だけで、ネットワークにも課金にも触れない。
  実験 3 では `skip_embed=True` / `embedding_chunks: 0` で生成そのものを止めた。
  実験 2 で止めなかったのは、**意味検索での想起が差し替え後にどう振る舞うかを
  見るのが今回いちばん知りたいことだったから**で、そこはベクトルが無いと
  何も観測できない。

---

## 1. 実験 2 — 新規ペルソナに memory.db だけ差し替えたら動くか

これがまはーの原文の質問です。

**実行したもの** (スクリプトは一時領域。組み直し方は §8)

```
cd C:/Users/shuhe/workspace/SAIVerse
./.venv/Scripts/python.exe "<一時領域>/exp2_swap_memory_db.py"
./.venv/Scripts/python.exe "<一時領域>/exp2b_unified_recall_after_swap.py"
```

観測結果は下に転記した。生の JSON: `<一時領域>/exp2_result.json`, `<一時領域>/exp2b_result.json`

**入力**

- ペルソナ A (`probe_a_testcity`) に、製品と同じ書き込み口で 5 件のメッセージを書いた。
  内訳は、自分自身のスレッド 2 件 (`append_persona_message`)、建物での会話 2 件
  (`append_building_message`)、題名を付けた別スレッド 1 件。
  さらにコア記憶 1 件 (`sai_memory/core_memory.py:281` の `add_core_memory`) と、
  手帳のページ 1 件 (`sai_memory/memopedia/storage.py:536` の `create_page`)。
- ペルソナ B (`probe_b_testcity`) を別に作り、B 自身の記憶を 1 件書いた。
- B のフォルダにある `memory.db` を消して、A の `memory.db` をそのままコピーした
  (`shutil.copy2`)。
- adapter の作り方は本番のペルソナ登録経路 (`persona/bootstrap.py:60`) と同じ
  引数の形にした (`persona_id` / `persona_dir` / `resource_id` を渡す)。

### 結果 — 「半分だけ動く」。そしてこの半端さが一番危ない

**確定**

差し替えた後、B として読み出した結果を経路ごとに並べます。

| 読み出し経路 | 実際に呼んだもの | 差し替え後の結果 |
|---|---|---|
| 会話の続き (自分のスレッド) | `SAIMemoryAdapter.recent_persona_messages` (`saiverse_memory/adapter.py:1203`) | **0 件** |
| 会話の続き (件数指定) | `recent_persona_messages_by_count` (同 1241) | **0 件** |
| 建物での会話履歴 | `recent_messages` (同 1157) | **0 件** |
| スレッド一覧 (記憶設定 UI のブラウズ) | `list_thread_summaries` (同 1489) | **A のスレッド 3 本が全部出る**。題名も件数も付いたまま |
| スレッド ID を直接指定した取得 | `get_thread_messages` (同 1562) | **A のメッセージが全部返る** (2+2+1 件) |
| 意味検索での想起 | `recall_snippet` (同 1682) | **A のメッセージ 5 件が返る** |
| 自動想起 (本番の Pulse が使う芯) | `sai_memory/unified_recall.py:668` の `unified_recall` | **A のメッセージがヒットする** (実験 2b) |
| コア記憶 | `sai_memory/core_memory.py:455` の `list_core_memories` | **A のコア記憶が返る** |
| 手帳のページ | `memopedia_pages` テーブル | **A のページが返る** |

**0 件になる理由**（コードで確認済み）

会話を続ける経路は、必ず `_thread_id()` (`saiverse_memory/adapter.py:2038`) を通って
`f"{self.persona_id}:{suffix}"` という文字列を組み立てます。B の adapter は
`probe_b_testcity:__persona__` を探しに行きますが、コピーしてきた DB の中身は
`probe_a_testcity:__persona__` なので、一致するものが無い。ブリーフの予想どおりでした。

**予想と違ったところ — ここが今回いちばん重要**

想起の経路は **ID の一致を見ていません**。`recall_snippet` の中にこう書いてあります
(`saiverse_memory/adapter.py:1699-1701`):

```python
# Disable both thread_id and resource_id filters to search across all threads
search_thread_id = None
search_resource_id = None
```

自動想起の `unified_recall` も同じで、`persona_id` 引数は URI を組み立てるためだけに
使われ、検索の絞り込みには使われません (`sai_memory/unified_recall.py:706` の注記)。

つまり **B は、A の会話を「自分の記憶」として想起して喋ります**。
コア記憶と手帳も同じで、これらのテーブルには持ち主 ID の欄がそもそも無いので、
DB を差し替えた瞬間に丸ごと B のものになります。

一方で、会話の続きだけが空っぽになる。
**「思い出せるが、続きが無い」という壊れ方をします。**

**「動くか」への答え（実測に基づく）**

まはーの疑いは当たっていました。ただし理由は「読めない」ではなく、
**「読める経路と読めない経路が混ざる」**です。
記憶を残して別のペルソナに差し替える運用を選ぶなら、
「memory.db を置くだけ」は成立しません。

---

## 2. 実験 3 — JSON で書き出して、別のペルソナへ移植する

`saiverse_memory/native_export.py` の書き出し・取り込みを実際に呼びました。

**実行したもの** (スクリプトは一時領域。組み直し方は §8)

```
./.venv/Scripts/python.exe "<一時領域>/exp3_native_export_import.py"
```

観測結果は下に転記した。生の JSON: `<一時領域>/exp3_result.json`、書き出したファイル自体は
`<一時領域>/exp3_archive_A.json`

**入力**

A に実験 2 と同じ 5 件のメッセージ・コア記憶・手帳ページを書いた上で、さらに
Chronicle (あらすじ) のエントリ 1 件、クリップ 1 件、目的タグ 1 件、覚え書き 1 件、
Pulse ログ 1 件、知覚バッファ 1 件、スルース未通過の区間 1 件、作業記憶 1 件、
机に開いた項目 1 件を、それぞれの製品 API で書き込みました。

### 2-1. 移植 (別 ID へ) は成立する

**確定**

- `import_threads_native(C, archive, transplant=True, skip_embed=True)` は
  スレッド 3 本・メッセージ 5 件を取り込んだ。
- **移植先 C で `recent_persona_messages` が 2 件返った。**
  建物の会話も 2 件返った。差し替え (実験 2) で 0 件だったものが、ここでは読める。
  取り込みのときにスレッド ID の頭を `probe_a_testcity:` から
  `probe_c_testcity:` へ書き換えているからです
  (`saiverse_memory/native_export.py:333` の `_remap_archive_for_transplant`)。
- 元の持ち主は各メッセージに記録として残る。実際の値:
  `{"transplanted_from": {"persona_id": "probe_a_testcity", "thread_id": "probe_a_testcity:__persona__"}}`
- 別 ID へ「復元」(`transplant=False`) を投げると、**書き込む前に拒否された**。
  実際のメッセージ: `restore rejected: archive persona 'probe_a_testcity' != target
  'probe_c_testcity'. 別 persona へ移すには transplant (移植) を明示指定してください。`

### 2-2. しかし運ばれるのは会話だけ — 運ばれなかったテーブルの名指し

**確定**（A の memory.db の全テーブル行数と、取り込み後の行数を突き合わせた）

移植でも、同一 ID への復元でも、**まったく同じ 10 個が運ばれませんでした**。

| テーブル | 中身 | A | 移植後 | 復元後 |
|---|---|---|---|---|
| `memopedia_pages` | 手帳・コア記憶・Chronicle のページ本体 | 9 | 6 | 6 |
| `memopedia_page_edit_history` | ページの編集履歴 | 1 | 0 | 0 |
| `clips` | 会話から切り出したクリップ | 1 | 0 | 0 |
| `purpose_tags` | 目的タグ | 1 | 0 | 0 |
| `memory_notes` | 覚え書き | 1 | 0 | 0 |
| `desk_items` | 机に開いている項目 | 1 | 0 | 0 |
| `perception_buffer` | 未消費の知覚 | 1 | 0 | 0 |
| `pulse_logs` | Pulse の内省ログ | 1 | 0 | 0 |
| `sluice_skipped_spans` | スルースを通っていない区間 | 1 | 0 | 0 |
| `working_memory` | 作業記憶 | 1 | 0 | 0 |

`memopedia_pages` の 6 は、取り込み先を開いたときに初期化が作る土台のページ
(時間の地図 / コア記憶 / 出来事 / 人物 / 計画 / 用語) だけです。
**A が書いたコア記憶・手帳のページ・Chronicle のあらすじは、3 件とも消えています。**
移植先で `list_core_memories` を呼んだ結果は空リストでした。

理由は構造的です。書き出しの形式が持てる箱は
`threads` / `messages` / `stelis_threads` の三つしかありません
(`saiverse_memory/native_export.py:183-190` の戻り値と、
同 `_write_thread_scaffold_in_txn` / `_insert_message_in_txn` が書く先)。
memory.db には他に 28 個のテーブルがありますが、形式に対応する欄がありません。

**言い換えると**: この書き出し・取り込みは「会話の書き起こしの引っ越し」であって、
「ペルソナの記憶の引っ越し」ではありません。

### 2-3. 同一 ID への復元も、失うものは同じ

**確定**

A の `memory.db` を消して作り直し、同じアーカイブを `transplant=False` で戻した結果、
会話 5 件とスレッド 3 本は正しく戻りましたが、上の表のとおり**同じ 10 テーブルが空のまま**でした。

なお、`active_state.json` はファイルなので DB を消しても残り、復元後に
`recent_persona_messages` が `probe_a_testcity:probe_named_thread` を見に行きました
(削除前に開いていたスレッドが残っていたため)。DB とファイルで消え方が揃っていません。

---

## 3. 実験 1 — 同じ ID でペルソナを作り直したら記憶は戻るか

**縮めた範囲（依頼の指示どおり明記します）**

- SAIVerseManager 全体は組み立てていません。
  `manager/persona.py` の `PersonaMixin._create_persona` と
  `manager/admin.py:1431` の `AdminService.delete_ai` を、
  既存テスト `tests/test_persona_creation_wiring.py` と同じ形の最小ホストに載せて
  直接呼びました。`_is_seeded_entity` は `AdminService` の実装をそのまま借りています。
- `PersonaCore` はスタブに差し替えました (既存テストと同じ理由 — 本物は SAIMemory と
  埋め込みとプロンプトファイルを巻き込む)。**再現しなかったのは、
  PersonaCore の初期化・`_on_persona_registered` の中身・
  OccupancyManager 経由の移動です。**
- memory.db は `SAIMemoryAdapter` を直接使って自分で書き、自分で読みました。
- main DB は一時ファイルの SQLite (`Base.metadata.create_all`)。

**実行したもの** (スクリプトは一時領域。組み直し方は §8)

```
rm -rf "<一時領域>/exp1_home"
./.venv/Scripts/python.exe "<一時領域>/exp1_delete_and_recreate_same_id.py"
```

観測結果は下に転記した。生の JSON: `<一時領域>/exp1_result.json`

### 3-1. 削除は、main DB の行をほとんど残す

**確定**

削除の前に、ペルソナ ID の欄を持つ 26 個のテーブルへ 1 行ずつ置きました
(`persona_schedule` / `session_anchor` / `episodes` / `persona_task` など)。
`delete_ai` を呼んだ後に数え直した結果:

- 消えたのは `ai` 行 (1 → 0) と `task_book` (1 → 0) だけ。
- **残った 25 テーブル**:
  `session_anchor` / `user_ai_link` / `ai_tool_link` / `building_occupancy_log` /
  `thinking_request` / `visiting_ai` / `persona_event_log` / **`persona_schedule`** /
  `llm_usage_log` / `addon_persona_config` / `persona_building_state` /
  `line_head_snapshot` / `session_head_snapshot` / `action_track` /
  `meta_judgment_log` / `persona_pulse_cursor` / `building_messages` /
  `feed_read_cursor` / `persona_task` / `persona_day_plan` /
  `persona_timetable_template` / `episodes` / `episode_inheritance` /
  `execution_ledger` / `execution_outbox`
- 私室の Building は残った (`probe_a_testcity_room` /「プローブ甲の部屋」)。
- ペルソナのフォルダは残った (`memory.db` がそのまま)。

### 3-2. 同じ名前では作り直せない。同じ ID では作り直せる

**確定**

- **同じ ID・同じ名前での作り直しは失敗しました。**
  失敗の理由は名前の重複検査ではなく、**私室 Building の名前の一意制約**です。
  実際のエラー: `UNIQUE constraint failed: building.CITYID, building.BUILDINGNAME`
  (作ろうとした名前が「プローブ甲の部屋」で、削除で残った古い私室と同じだった)。
  この失敗は rollback され、DB に中途半端な行は残りませんでした。
- **同じ ID・違う名前での作り直しは成功しました。**
  ID の重複検査は `ai` テーブルだけを見るので、行が消えていれば ID は空きます
  (ブリーフの「要検証」に対する答え: **空きます**)。

### 3-3. 削除したはずのアラームは復活する

**確定 — 必ず報告するよう指示された項目です**

作り直した後にもう一度数えた結果、**上の 25 テーブルの行が、そのまま新しいペルソナに
結びつきました**。`persona_schedule` の行も 1 件残っており、新しいペルソナの ID と
一致します。つまり **削除したつもりの起床・就寝のアラームは、同じ ID で作り直すと
戻ってきます**。同じことが、予定表・エピソード・タスク・カーソル・
アドオン設定にも起きます。

### 3-4. memory.db は同じパスを指し、中身も読める

**確定**

- 作り直した後のペルソナの `memory.db` のパスは、削除前とまったく同じでした。
- `recent_persona_messages` が削除前に書いた記憶 1 件を返しました。
- `list_core_memories` が削除前のコア記憶 1 件を返しました。
- **記憶は「残る」どころか「黙って戻ってくる」。** 同じ ID を選んだ瞬間に、
  利用者に何も確認せずそうなります。

### 3-5. 私室は古い部屋が残り、別 ID の新しい部屋ができる

**確定**（ブリーフの「要検証」に対する答え）

作り直した後の Building 一覧:

| BUILDINGID | 名前 |
|---|---|
| `probe_a_testcity_room` | プローブ甲の部屋 (**削除された方の遺物**) |
| `persona_1_testcity_room` | プローブ甲・弐の部屋 (新しい私室) |

新しい私室の ID が `probe_a_testcity_room` にならないのは、その ID が古い部屋に
取られているためです (`manager/persona.py` の私室 ID 導出が `ensure_unique=True` で
別名へ逃げる)。逃げた先が `persona_1_testcity_room` で、**ペルソナ ID との対応が
名前から読めなくなります**。

### 3-6. `ai` 行の中身は戻らない

**確定**

作り直したペルソナの `ai` 行は、削除前の値を引き継ぎませんでした。すべて新規作成の
既定値です。今回の実験では、システムプロンプトは呼び出し側が渡した文字列、
`DEFAULT_MODEL` はホストの既定値、`PERSONA_ROLE` は `None`、
`AUTONOMY_ENABLED` と `CHRONICLE_ENABLED` は `True`、`DESCRIPTION` は
自動生成の英文でした。`LIGHTWEIGHT_MODEL` は `None` です。

**つまり「人格の設定は初期化されるのに、記憶とアラームは戻る」**という組み合わせになります。

---

## 4. 実験 4 — 削除した後に記憶を取り出せるか

**実行したもの** (スクリプトは一時領域。組み直し方は §8)

```
rm -rf "<一時領域>/exp4_home"
./.venv/Scripts/python.exe "<一時領域>/exp4_export_after_delete.py"
```

観測結果は下に転記した。生の JSON: `<一時領域>/exp4_result.json`

FastAPI の TestClient に `api/routes/people/memory.py` と
`api/routes/people/native_export_import.py` のルータだけを載せ、
「main DB に `ai` 行が無く、`manager.personas` にも居ない」偽の manager を
差し込んで、実際にリクエストを投げました。

**確定**

| 呼んだもの | 結果 |
|---|---|
| ライブラリの `export_threads_native(persona_id)` | **通る**。スレッド 1 本・メッセージ 1 件を書き出した |
| `GET /api/people/{id}/threads` (スレッド一覧) | **404** `Persona probe_deleted_testcity not found` |
| `GET /api/people/{id}/threads/{tid}/export-native` | **200**。本文に記憶がそのまま入っていた |
| `POST /api/people/{id}/import/native` (取り込み) | **404** `Persona not found` |

読み解くとこうなります。

- 記憶の**書き出し**は、削除後も止められていません。書き出しの入口
  (`api/routes/people/native_export_import.py:36`) は、ペルソナが存在するかを
  検査していません。ファイルを読むだけだからです。
- ところが**スレッドの一覧が 404 を返す**ので、画面からは書き出しボタンに
  たどり着けません。一覧の入口 (`api/routes/people/memory.py:18`)
  は `manager.personas` に居ることを要求します。
  **つまり「スレッド ID を自分で知っていれば取り出せるが、画面では探せない」。**
- **取り込みは塞がれています** (`ensure_persona_exists`、
  `api/routes/people/utils.py:69`)。削除したペルソナへ記憶を戻す道は、
  API には現状ありません。
- 画面を通さない道としては `scripts/export_saimemory_native.py` があり、
  こちらは `export_threads_native` を直接呼ぶので削除後も動くはずです
  — **ただしこのスクリプト自体は実行していません（未実施）**。

---

## 5. 追加実験 — 同じ ID のまま丸ごと写すと、ちゃんと読める

実験 2 の対照として、既存の `scripts/clone_persona_to_test_env.py` の
`clone_persona` を、**合成した写し元と合成した写し先の間で**実際に走らせました
(本番の DB と home は引数で完全に置き換えたので参照していません)。

**実行したもの** (スクリプトは一時領域。組み直し方は §8)

```
rm -rf "<一時領域>/exp5_home"
./.venv/Scripts/python.exe "<一時領域>/exp5_clone_same_id.py"
```

観測結果は下に転記した。生の JSON: `<一時領域>/exp5_result.json`

**確定**

写し先で読み出した結果:

- `recent_persona_messages` → 写す前に書いた記憶 1 件が返った
- `list_core_memories` → 写す前のコア記憶 1 件が返った
- `get_current_thread` → 開いていたスレッドまで復元された
- ペルソナのフォルダは 2 ファイル (462KB) 丸ごとコピーされた

**この経路が成立するのは、ID を変えていないからです。** 写すのは
「`ai` 行 + `session_anchor` 行 + ペルソナのフォルダ丸ごと」の三点セットで、
`memory.db` だけではありません。

なお、写し先に対応する Building が無かったので `PRIVATE_ROOM_ID` は
`null` に落ちました (居場所は失われる)。記憶は残り、世界での位置は残らない、という切れ方です。

---

## 6. 三つの経路それぞれについて、実測から言えること

### 経路 1: 記憶を残して削除し、後で再利用する

**確定**

- 別のペルソナへ `memory.db` を置くだけでは成立しません (実験 2)。
  会話の続きが 0 件になる一方、想起とコア記憶と手帳は前の持ち主のものが出ます。
- JSON での移植は会話の続きを成立させます (実験 3) が、
  **コア記憶・手帳・Chronicle・クリップ・覚え書きなど 10 テーブルを落とします**。
- 削除後に記憶を書き出す道はライブラリ層には残っていますが、
  **画面からは一覧が 404 なので辿り着けず、取り込みは 404 で塞がれています** (実験 4)。

この経路を採るなら、少なくとも次の三つが未解決のまま残ります。
どれも「フラグを一つ足す」では済まない範囲です。

1. 会話以外の記憶 (コア記憶・手帳・Chronicle) を運ぶ器が、書き出し形式に無い
2. 削除後のペルソナの記憶を画面から見つける入口が無い
3. 想起がペルソナ ID で絞られていないので、DB を混ぜると前の持ち主の記憶を喋る

### 経路 2: 無効化して、同じペルソナを再有効化する

**未実施**。無効化・再有効化の機構そのものは私の担当範囲ではないので、
実験を組んでいません。

ただし実験 1 と実験 5 から、**この経路が構造的にいちばん軽い**ことは言えます。
記憶が読める条件はただ一つ、**ペルソナ ID が変わらないこと**でした
(スレッド ID の頭が ID そのものなので)。無効化なら ID は動きません。
削除して作り直すと、ID を同じにしても `ai` 行の中身は失われ、私室は二重になり
(実験 1)、記憶とアラームだけが黙って戻ります。

### 経路 3: 完全削除する

**確定**

現在の `delete_ai` は「完全削除」ではありません。実測で残るのは:

- main DB の **25 テーブル**の行 (`persona_schedule` を含む)
- 私室の Building
- ペルソナのフォルダと `memory.db` の全部

完全削除を仕様として選ぶなら、この 25 テーブル + Building + フォルダの
後始末を新たに書く必要があります。

**なお、共有された記録の扱いは決めていません。** `building_messages` は
削除後も残る 25 テーブルの一つですが、これは他者と共有された建物の会話に
ペルソナの発言が含まれるものです。消すかどうかは利用者の判断が要る領域なので、
私は既定を提案しません (依頼元の注意 7 に従いました)。

---

## 7. 未実施・未検証として残した範囲

- **無効化・再有効化の経路そのもの** (経路 2 の機構) は実験していません。
- **本物の `PersonaCore` を通した作成・削除**は実験していません
  (実験 1 はスタブ)。`_on_persona_registered` の中で起きること、
  OccupancyManager 経由の移動、Pulse の再開は未検証です。
- **`scripts/export_saimemory_native.py` の実行**はしていません
  (呼んでいる関数は実験 4 で確かめました)。
- **画面 (フロントエンド) の挙動**は一切触っていません。
  API の応答コードまでしか見ていません。
- **本番規模のデータでの挙動** (大量のスレッド、Stelis の入れ子、
  埋め込みの再生成にかかる時間) は測っていません。合成データは各 1〜5 件です。
- **`stelis_threads` が実際に運ばれるか**は、Stelis を張った状態を作らなかったので
  行数 0 のままで、経験的には確かめていません。
  形式に欄がある (`saiverse_memory/native_export.py:105-116`) ことはコードで確認済みですが、
  **これは「静的な疑い」ではなく「未検証」です。**
- 実験 3 の埋め込みは `skip_embed=True` で止めたので、
  **移植後に想起が使える状態になるまでの工程**は確かめていません。

---

## 8. 実験スクリプトの置き場と、再実行のしかた

**スクリプトと観測結果は [repro/](repro/) にある。** 当初は調査セッションの一時領域に
置いていたが (消えると再現できなくなるため) 2026-09-10 に成果物へ移した。

```bash
./.venv/Scripts/python.exe docs/audits/2026-09-10_persona_removal_recovery/repro/exp2_swap_memory_db.py
```

`probe_env.py` からセッション固有のパスを外し、**隔離ホームを OS の一時領域
(`<temp>/saiverse_probe/`) へ作る**ようにした (`SAIVERSE_PROBE_ROOT` で変えられる)。
`guard_not_production()` が、そこが本番 (`~/.saiverse`) でないこと・本番の下でないことを
毎回検査してから走る。

移設後に実験 2 を再実行し、**当初と同じ結果が出ることを確認した**
(差し替え後: 会話の続き 0 件 / A のスレッド 3 本が見える / 想起は A の内容を返す)。

以下は各実験に共通の骨組み:

1. `SAIVERSE_HOME` を一時ディレクトリへ向ける (本番を指していないことを毎回検査する)
2. main DB は一時ファイルの SQLite に `Base.metadata.create_all` で作る
3. 合成ペルソナの ID は `probe_*_testcity` の形にする (実ペルソナを種にしない)
4. 記憶の読み書きは `SAIMemoryAdapter` を本番のペルソナ登録経路
   (`persona/bootstrap.py:60`) と同じ引数の形で組み立てて行う
5. 実験のたびに `git status --porcelain` と `ls ~/.saiverse/personas` を確認し、
   作業ツリーと本番に何も足していないことを検算する

再現に使った実験の一覧 (詳細は各節):

| 実験 | 何を確かめたか |
|---|---|
| 実験 2 / 2b | memory.db だけ差し替えたときの、読み出し経路ごとの結果 |
| 実験 3 | JSON の書き出し → 移植 / 復元で、運ばれるテーブルと落ちるテーブル |
| 実験 1 | 削除 → 同じ ID で作り直したときに戻るもの・戻らないもの |
| 実験 4 | 削除後に記憶を取り出せるか (API 層の応答) |
| 実験 5 | 同じ ID のままフォルダごと写したときの結果 (対照) |
