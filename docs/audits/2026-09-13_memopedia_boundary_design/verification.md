# 検証 — Memopedia の一括操作を、隔離した環境で実際に動かして見たこと

この文書は「いまの作りが何をするか」を、読んだ結果ではなく**動かした結果**で記録する。
設計案そのものは `design.md`、推奨と判断の一点は `README.md` にある。

2026-09-14: [Astraレビュー](astra_review.md)と追加プローブが加わった。以下の6本はOpusの実験記録。Astraが再実行したのはexp1・exp4で、別途保存失敗と通常データの往復を追加した。修正後の検査は引き続き未実装。

実験のコードと結果は同じディレクトリの `repro/` にある。結果の JSON は実行のたびに
上書きされる (識別子だけは実行ごとに変わる)。

---

## 0. 実行の条件

| 項目 | 内容 |
|---|---|
| 作業ツリー | `C:/Users/shuhe/workspace/SAIVerse/.worktrees/product-normalization` (ブランチ `codex/product-normalization`、HEAD `19ea2010`) |
| 追加したもの | `docs/audits/2026-09-13_memopedia_boundary_design/` の下だけ。製品コード・既存テスト・既存文書は一行も変えていない |
| 他担当の未コミット変更 | `docs/audits/2026-09-09_product_normal_behavior/review_progress.md` と `docs/handoff/2026-09-13_memopedia_boundary_design_opus_brief.md` の 2 件。読むだけで、巻き戻していない |
| Python | `C:/Users/shuhe/workspace/SAIVerse/.venv/Scripts/python.exe` (ワークツリーには `.venv` が無いので本体のものを使った) |
| 記憶の保存先 | OS の一時領域の下に実験ごとの隔離ホームを作った。本番 (`~/.saiverse`) には触れていない |
| LLM | 一度も呼んでいない。埋め込みは 0 ベクトルを返す偽物に差し替えた |
| バックエンド | 起動していない。FastAPI のルート関数を Python から直接呼んだ |

**本番に触れそうになったら止まることを、実際に試して確かめた。**
隔離ホームの置き場を本番の下 (`~/.saiverse/probe_should_refuse`) に指定して `repro/probe_env.py` を
読み込むと、ディレクトリを 1 つも作らないまま次の行で止まる:

```
AssertionError: REFUSED: probe root is under production C:\Users\shuhe\.saiverse\probe_should_refuse
```

実行後に `~/.saiverse/personas/` を見ても、実験で使った合成ペルソナ (`probe_` で始まる名前) は
1 体も作られていない。

---

## 1. 整えた再現の一覧

`repro/` に置いたファイル。どれも単独で走り、結果を同じディレクトリの
`<実験名>_result.json` に残す。実行例:

```
cd docs/audits/2026-09-13_memopedia_boundary_design/repro
PYTHONIOENCODING=utf-8 <repo>/.venv/Scripts/python.exe exp1_delete_all.py
```

(`PYTHONIOENCODING=utf-8` を付けないと、画面に出る日本語が化ける。JSON ファイルの側は
常に UTF-8 で書かれる。)

| ファイル | 何を確かめる実験か | 観測表のどの行か |
|---|---|---|
| `probe_env.py` | 隔離ホームの用意、本番でないことの検査、埋め込みの差し替え。実験そのものではなく、全実験が使う土台 | — |
| `memopedia_fixture.py` | 合成ペルソナの用意 (通常ページ・その断片・机に開いた参照・コア記憶の生存とごみ箱・あらすじ・テーマページを 1 つの記憶 DB に同居させる) と、正規の読み取りによる観測 | — |
| `exp1_delete_all.py` | 「Memopedia 全削除」が何を消し、何を残すか | 1 行目 |
| `exp2_export_roundtrip.py` | 書き出しの中身と、それを別のペルソナへ取り込んだ結果 | 2 行目 |
| `exp3_replace_import.py` | 「インポート前に既存ページを削除」を付けた取り込みが、先に何を消すか | 3 行目 |
| `exp4_invalid_and_partial_failure.py` | 入力がおかしいとき・一部の行が入らなかったときに何が残り、何が返るか | 4 行目 |
| `exp5_next_presentation.py` | 操作のあと、ペルソナの頭と、ペルソナ宛の通知に何が出るか | 5 行目 |
| `exp6_recovery.py` | 壊れたあと、利用者が自力で戻せるか | 依頼書 §4 (旧ファイルと途中失敗) |

各ファイルの冒頭には「確かめること」と「確かめないこと」を書いてある。

`memopedia_fixture.py` が用意する合成ペルソナの中身 (これが全実験の出発点):

- 手帳の通常ページ 1 枚 (カテゴリ `terms`、表題「知識ページ」) と、そのページに紐づく断片 1 件、
  そのページを指す机の参照 1 件
- コア記憶 3 件 — 生存 2 件 (`c:1` `c:2`) と、利用者が自分でごみ箱へ入れた 1 件 (`c:3`)
- あらすじ 1 件
- テーマページ 1 枚

---

## 2. 実際に回した結果

数字と文言は、結果 JSON に入っている原文をそのまま写した。

### 2-1. 「Memopedia 全削除」 (`exp1_delete_all.py`)

呼んだもの: `api.routes.people.memopedia.delete_all_memopedia_pages("probe_delete_all", manager=<偽 manager>)`

API が返した応答の原文:

```json
{"success": true, "deleted_count": 4, "message": "Deleted 4 Memopedia pages"}
```

利用者の画面には、この `deleted_count` がそのまま
`Memopedia: 4件のページを削除しました` と出る
(`frontend/src/components/memory/MemoryRecall.tsx:624`)。

正規の読み取りの前後:

| 読み取り | 操作前 | 操作後 |
|---|---|---|
| コア記憶の一覧 (`list_core_memories`) | `["c:1 まはーは野菜がほぼ食べられない", "c:2 工房の棚は三段で、上段に定規を置く"]` | `[]` |
| コア記憶のごみ箱 (`list_deleted_core_memories`) | `["c:3 自分でごみ箱へ入れたコア記憶"]` | `["c:3 自分でごみ箱へ入れたコア記憶"]` |
| あらすじの一覧 (`get_all_entries_ordered`) | `["あらすじ"]` | `["あらすじ"]` |

ペルソナの頭のコア記憶の章 (`sea/head_pipeline/sections/core_memory.py` の `CoreMemorySection`) を
実際に撮って描画させた結果:

- 操作前 — `## コア記憶（自分で選んで刻んだ、常に携えている知識）\n- [core:1] まはーは野菜がほぼ食べられない\n- [core:2] 工房の棚は三段で、上段に定規を置く`
- 操作後 — `None` (章ごと消える)

ページに紐づく付随データ:

| | 操作前 | 操作後 |
|---|---|---|
| 生きているページ | 12 | 8 |
| 断片 (`memopedia_fragments`) | 1 | 0 |
| ページの編集来歴 (`memopedia_page_edit_history`) | 4 | 4 |
| 机に開いた参照 (`desk_items`) | `["memopedia:6"]` | `["memopedia:6"]` |

**観測できたこと**

1. 消えた 4 枚の内訳は、手帳の通常ページ 1 枚・**生きているコア記憶 2 件**・テーマページ 1 枚だった。
   応答の `deleted_count` にはこの内訳が出ない。
2. 利用者が自分でごみ箱へ入れたコア記憶 (`c:3`) は残った。
   **利用者から見ると結果が反転している** — 使っていた記憶が消えて、捨てた記憶が残る。
3. あらすじは無傷。2026-08-19 に入った除外がそのまま効いている。
4. 断片は道連れで消えた。**編集来歴は残った** —
   `api/routes/people/memopedia.py:294` の説明文
   `"""Delete ALL non-root Memopedia pages (and their edit history)."""` は、この点で実態と違う。
5. 机に開いた参照 `memopedia:6` は、指していたページが消えたあとも残った。

### 2-2. 書き出しと再取り込み (`exp2_export_roundtrip.py`)

呼んだもの: `export_memopedia` → その結果をそのまま
`import_memopedia("probe_export_target", body=<書き出し>, clear=False, manager=<偽 manager>)`

書き出しに入ったカテゴリ: `["core", "terms", "theme"]`
(`chronicle` は除外されている。`core` = コア記憶が入っている。)

書き出しの 1 ページが運ぶ項目:
`category / content / created_at / id / parent_id / summary / title / updated_at` の 8 つ。

書き出しが**運ばなかった**項目:
`is_deleted / is_important / is_trunk / keywords / last_referenced_at / metadata / short_id`。

コア記憶の識別子 (`core_id`)・種別 (`kind`)・承認済みの印 (`confirmed`)・ごみ箱の時刻 (`deleted_at`) は
すべて `metadata` の中にあるので、この 1 行で全部落ちる。

取り込みの応答の原文:

```json
{"success": true, "imported_count": 4, "message": "Imported 4 pages"}
```

取り込み後の読み取り:

| 読み取り | 取り込み前 | 取り込み後 |
|---|---|---|
| コア記憶の一覧 (`list_core_memories`) | `["c:1 取り込み先にもとからあった記憶"]` | `EXCEPTION TypeError: int() argument must be a string, a bytes-like object or a real number, not 'NoneType'` |
| 頭のコア記憶の章 (`CoreMemorySection.capture`) | 1 件を載せた章が描画される | 同じ `TypeError` で落ちる |
| コア記憶の一覧 API (`GET /people/{id}/core-memory`) | — | 同じ `TypeError` で落ちる |

**観測できたこと**

1. 「Memopedia のエクスポート」と書いてあるファイルに、**コア記憶の本文がそのまま入る**。
   画面の説明文は `Memopedia の全ページを JSON ファイルとしてエクスポート、またはインポートします。`
   (`MemoryRecall.tsx:1030`) で、コア記憶を指す語はどこにも無い。
2. そのファイルを取り込むと、`parent_id='root_core'` / `category='core'` で `metadata` が空のページができる。
   これは**コア記憶として読み出せない行**で、読もうとすると `TypeError` で落ちる。
3. 落ちる場所は 3 つとも「コア記憶を読む正規の口」だった — ペルソナの頭の章、コア記憶の一覧 API、
   そしてライブラリの `list_core_memories` そのもの。
4. 応答は `success: true` で、4 件入ったとしか言わない。**壊したことはどこにも出ない。**
5. 副産物の観測: テーマページも同じ形で壊れる。書き出しに入るのに `metadata`
   (`origin` と `member_refs`) が運ばれず、取り込み先に `root_theme` が無ければ
   親のいないページとして作られる。テーマページを読む側がこれで困るかは**確かめていない**。

### 2-3. 既存ページを置き換えて取り込み (`exp3_replace_import.py`)

呼んだもの: `import_memopedia("probe_replace_target", body=<別ペルソナの書き出し>, clear=True, manager=<偽 manager>)`

応答の原文:

```json
{"success": true, "imported_count": 2, "message": "Imported 2 pages"}
```

画面にはこれが `2 ページをインポートしました（既存データを削除後）` と出る
(`MemoryRecall.tsx:104`)。

先行削除で消えたもの・残ったもの (カテゴリごとのページの表題):

| カテゴリ | 取り込み前 | 取り込み後 |
|---|---|---|
| `terms` (手帳の通常ページ) | 用語 / 知識ページ | 用語 / 持ち出し元の知識ページ |
| `theme` (テーマ) | テーマ / テーマページ | テーマ |
| `core` (コア記憶) | コア記憶 / コア記憶 c:1 / コア記憶 c:2 / コア記憶 c:3 | コア記憶 / コア記憶 c:1 / コア記憶 c:3 |
| `chronicle` (あらすじ) | chronicle:1 / 時間の地図 | chronicle:1 / 時間の地図 |

(「用語」「テーマ」「コア記憶」「時間の地図」はカテゴリの器で、中身ではない。
取り込み後の `コア記憶 c:1` は、取り込みで新しく作られた `metadata` の無い行。)

読み取りの前後は 2-2 と同じで、`list_core_memories` と頭の章が `TypeError` で落ちる。
ごみ箱の `c:3` は残り、あらすじも無傷。

**観測できたこと**

1. 「インポート前に既存ページを削除」というラベル (`MemoryRecall.tsx:1083`) が指す範囲に、
   **生きているコア記憶が入っている**。
2. 取り込みから除外されている記憶 (あらすじ) は、先行削除でも除外されている。
   **取り込みに巻き込まれる記憶 (コア記憶) は、先行削除でも巻き込まれる。**
   つまり除外と削除は同じ 1 行の条件を共有していて、片方だけずれてはいない。
3. 消したうえで壊れた行を入れるので、2-2 より状態は悪い。元の 2 件は行ごと消えている。

### 2-4. 不正な入力・途中の失敗 (`exp4_invalid_and_partial_failure.py`)

5 つの場面を、それぞれ別の合成ペルソナで、置き換え取り込み (`clear=True`) を使って試した
(場面 5 だけ `clear=False`)。

| 場面 | API が返した応答の原文 | 先行削除は走ったか | コア記憶の一覧 (操作後) |
|---|---|---|---|
| 1. `pages` の項目が無い入力 | `{"success": true, "imported_count": 0, "message": "Imported 0 pages"}` | **走らない** | 操作前のまま 2 件 |
| 2. `pages` が空配列の入力 | 同上 | **走らない** | 操作前のまま 2 件 |
| 3. 取り込める行が 1 つも無い入力 (中身が全部あらすじ) | 同上 | **走る** | `[]` |
| 4. 一部の行が作成に失敗する入力 (3 行のうち 1 行が失敗) | `{"success": true, "imported_count": 2, "message": "Imported 2 pages"}` | **走る** | `[]` |
| 5. 既にある識別子と同じ行を含む入力 (`clear=False`) | `{"success": true, "imported_count": 1, "message": "Imported 1 pages"}` | — | 操作前のまま 2 件 |

**観測できたこと**

1. **空のファイルを入れて全部消える形にはならない。** `pages` が無い・空のときは、
   削除の手前で戻る (場面 1・2)。ここは守られている。
2. **取り込める行がゼロでも、先行削除は走る** (場面 3)。利用者の画面には
   `0 ページをインポートしました（既存データを削除後）` と出る。
   これが「置換前のデータを消してから、入るものが何も無い」形の実例。
3. **一部の行が入らなくても `success: true` で返る** (場面 4)。
   入らなかった 1 行は応答にも画面にも出ない。記録に残るのは
   バックエンドのログ 1 行 (`Failed to import page {'壊れた': '表題'}: Error binding parameter 3: type 'dict' is not supported`) だけ。
   利用者は「3 行のファイルを入れたのに 2 件と言われた」という差でしか気づけない。
4. **識別子が既存とぶつかった行は、黙って飛ばされる** (場面 5)。上書きもされず、
   応答は残りの 1 件だけを数える。
5. 場面 3・4 のどちらも、先行削除でコア記憶が消えている。
   **「途中で失敗した」場面の被害が、失敗と無関係な記憶に及んでいる。**

### 2-5. 操作後の次の提示 (`exp5_next_presentation.py`)

ペルソナ宛の通知は、`sea/head_pipeline/sections/memopedia_index.py` の
`MemopediaIndexSection` が `capture_changes_since` で変化した行を拾い、
`diff_to_notifications` で文にする。その 2 つを直接呼んだ結果:

| 場面 | ペルソナへ届く文 |
|---|---|
| 1. ペルソナがコア記憶を 1 件刻む | `Memopedia「コア記憶 c:4」が作成されました` |
| 2. あらすじが 1 件編まれる | `Memopedia「chronicle:2」が作成されました` |
| 3. 手帳の通常ページを 1 枚作る | `Memopedia「新しい知識ページ」が作成されました` |
| 4. 手帳の全削除 | (通知は 1 つも出ない) |
| 5. 置き換え取り込み | `Memopedia「コア記憶 c:1」「持ち出し元の知識ページ」が作成されました` |

**観測できたこと**

1. **コア記憶を刻んだことが「Memopedia が作成された」としてペルソナに届く** (場面 1)。
   コア記憶の章そのものは「編集主体がペルソナ自身なので通知しない」と決めて空を返す作りなのに、
   隣の章が同じ出来事を別の名前で告げている。
2. **あらすじが編まれたことも「Memopedia が作成された」として届く** (場面 2)。
   届く表題は `chronicle:2` という内部の呼び名で、世界の中の言葉になっていない。
3. **手帳の通常ページの作成は正しく届く** (場面 3)。ここは壊してはいけない既存の通知。
4. **全削除は 1 つも通知を出さない** (場面 4)。消えた行はもう表に無いので、変化として拾われない。
   ペルソナから見ると、記憶が消えたことは知らされない。
5. **置き換え取り込みは、入れ直した全ページを「いま作成された」として 1 つの文にまとめて届ける**
   (場面 5)。取り込みは書き出しに載っている作成時刻を復元せず、今の時刻を入れるため。

同じ実験で、操作後の頭と読み取りも取った:

- 全削除の後 — 頭のコア記憶の章は空になり、章ごと消える (落ちない)。あらすじは読める。
- 置き換え取り込みの後 — 頭のコア記憶の章は `TypeError` で落ちる。あらすじは読める。
  コア記憶の合計字数だけは壊れた行の本文を数えて `10` を返す。
  **「合計 10 字」と「一覧は読めない」が同時に成り立つ状態**になる。

### 2-6. 壊れたあと自力で戻せるか (`exp6_recovery.py`)

壊れた行に残っているもの (復旧の材料):

```json
[{"title": "コア記憶 c:1", "content": "持ち出し元のコア記憶 A", "metadata": null},
 {"title": "コア記憶 c:2", "content": "持ち出し元のコア記憶 B", "metadata": null}]
```

**本文は残っている。** 失われたのは識別子・種別・承認済みの印・ごみ箱の時刻。

4 通りの手当てを、それぞれ別の合成ペルソナで試した結果:

| 手当て | 結果 |
|---|---|
| 1. 手帳の木を返す API (`GET .../memopedia/tree`) に見えるか | **見えない。** 木に出るカテゴリは `people / terms / plans / events / theme` の 5 つで、`core` は出ない。利用者は壊れた行の識別子を知る手段が無い |
| 2. 手帳の 1 件削除 (`DELETE .../memopedia/pages/{page_id}`) で取り除けるか | **識別子さえ分かれば取り除ける** (`{"success": true}`)。生きているコア記憶の一覧は読めるように戻る。**ただしごみ箱の一覧が今度は落ちる** — この削除はごみ箱へ移す形なので、壊れが生存側からごみ箱側へ移るだけ |
| 3. コア記憶の 1 件削除 (`DELETE .../core-memory/{memory_id}`) で取り除けるか | **取り除けないうえに、別の記憶を消す。** `core:1` を指定すると、壊れた行ではなく**取り込み先がもともと持っていた本物のコア記憶**が消えて `{"ok": true, ...}` が返った。`core:2` は `404: core:2 が見つからないか、既に削除されています。`。一覧は依然として落ちたまま |
| 4. もう一度「Memopedia 全削除」を押す | `{"success": true, "deleted_count": 5, "message": "Deleted 5 Memopedia pages"}`。壊れた行は消え、読み取りは静かに空へ戻る。**ただし壊れていない本物のコア記憶も一緒に消える** |

**観測できたこと** — 壊れた状態から利用者が自力で戻れる道は、いまのところ無い。
画面に壊れた行は出ず、コア記憶の口からは触れず、全部消す以外の手当ては
「壊れをごみ箱へ移す」か「無関係な記憶を消す」かのどちらかになる。
本文は DB に残っているので、**壊れた記憶の内容そのものは復旧可能**。
失われた識別子と種別は推測で埋めてはならないもので、**この部分は復旧不可能**。

---

## 3. 層単体の実験をどこまで越えたか

**越えたところ**

| 何を | 詳しく |
|---|---|
| 保存の層だけでなく、API の入口から呼んだ | `api.routes.people.memopedia` の `delete_all_memopedia_pages` / `export_memopedia` / `import_memopedia` / `delete_memopedia_page` / `get_memopedia_tree`、および `api.routes.people.core_memory` の `list_core_memory` / `delete_core_memory_item` を、それぞれのルート関数として呼んだ |
| 表の行だけでなく、正規の読み取りを通した | `list_core_memories` / `list_deleted_core_memories` / `total_core_memory_chars` / `get_all_entries_ordered` を毎回通し、落ちたら例外を観測値として残した |
| ペルソナの頭への提示まで見た | `CoreMemorySection.capture` と `render` を実際に呼び、頭に載る文字列そのものを記録した |
| ペルソナ宛の通知まで見た | `MemopediaIndexSection.capture_changes_since` と `diff_to_notifications` を呼び、届く文をそのまま記録した |
| 複数の種類の記憶を同居させた | 通常ページ・その断片・机の参照・コア記憶 (生存とごみ箱)・あらすじ・テーマページを 1 つの記憶 DB に用意した |
| 本番でないことを実行時に検査した | 隔離ホームが本番を指していたら、ディレクトリを 1 つも作らずに止まることを実際に確かめた |

**越えていないところ (この検証では確かめていない)**

| 何を | なぜ確かめていないか |
|---|---|
| 実際の HTTP | ルート関数を Python から直接呼んでいる。FastAPI のルーティング・依存解決・リクエスト検証・応答のシリアライズは通っていない。`body: dict` を受ける入口なので、壊れた JSON ファイルを選んだときの挙動は見ていない |
| 画面の操作 | ボタンの 2 段階クリック、チェックボックス 2 段 (`インポート前に既存ページを削除` と `削除を確認`)、ファイル選択は動かしていない。画面の文言はソースから引いた |
| 画面の表示 | コア記憶の一覧 API が落ちたとき、画面が `まだコア記憶はありません。` と出す分岐は、コードを読んだだけで動かしていない |
| バックエンドの起動 | 起動していない。錠前の競合、複数プロセスからの同時操作は見ていない |
| ペルソナの発話 | Pulse を回していない。頭の章の撮り直しが落ちることは見たが、そこから実際に発話が止まるところまでは追っていない。**頭には「前の値を据え置く」逃げ道があるので、「必ず喋れなくなる」とは書けない** |
| 頭の据え置きと時間差 | 実際には操作の直後には何も起きず、次に頭を撮り直すまで症状が出ない。その遅れは再現していない |
| 埋め込みと意味検索 | 埋め込みを 0 ベクトルの偽物に差し替えているので、想起の順位は何も確かめていない |
| 机の参照が後で何を指すか | 全削除のあとに参照が残ることは見たが、新しいページが同じ番号を取って**別のページを指す**ところまでは再現していない |
| スクリプト経路 | `scripts/export_memopedia.py` / `scripts/build_memopedia.py --clear` / `scripts/build_arasuji.py --clear-memopedia` は動かしていない |

---

## 4. 観測表の 5 行を、具体的な呼び出し・fixture・判定へ落とす

ここに書く判定は、**正常化した後に成り立っていてほしいこと**。いまはどれも成り立っていない
(§2 の観測がその反証)。

共通の用意 (以下「基本の合成ペルソナ」と呼ぶ) は `repro/memopedia_fixture.py` の
`ProbePersona.seed()` が作るもの:
手帳の通常ページ 1 枚 + その断片 1 件 + 机の参照 1 件 + コア記憶 生存 2 件・ごみ箱 1 件 +
あらすじ 1 件 + テーマページ 1 枚。

### 行1 — Memopedia 全削除

**呼び出し**: `delete_all_memopedia_pages(persona_id, manager=manager)`
**用意**: 基本の合成ペルソナ

| 判定 | 何を見るか |
|---|---|
| コア記憶が残る | `list_core_memories(conn)` が操作前と同じ 2 件 (`c:1` `c:2`) を返す |
| ごみ箱のコア記憶が残る | `list_deleted_core_memories(conn)` が操作前と同じ 1 件 (`c:3`) を返す |
| あらすじが残る | `get_all_entries_ordered(conn)` が操作前と同じ 1 件を返す |
| 頭の提示が変わらない | `CoreMemorySection().capture(ctx)` が例外を出さず、`render` の返す文字列が操作前と一致する |
| 手帳の操作自体は使える | 通常ページが `get_page(conn, page_id)` で引けなくなる (= ちゃんと消える) |
| 数字が文言と合う | `deleted_count` が、実際に消えた手帳のページの数と一致する (コア記憶の件数を含まない) |
| 断片の道連れは消えたページに限る | 消えた通常ページの断片は消え、残ったページに紐づく断片は残る |

**テーマページについて**: `design.md` §1.1 は、テーマページ (`category='theme'`) を
一括操作の対象に**含める**と決めている (手帳の木に並ぶ 5 カテゴリが対象)。
その線なら、この行の判定に「テーマページも通常ページと同じく消える」が加わる。

ただし、テーマページについて私が観測した事実がもう 1 つある — **書き出しと取り込みの往復で
`metadata` (`origin` と `member_refs`) を失う** (§2-2)。取り込み先に `root_theme` が
無ければ、親のいないページとして作られる。
テーマページの読み手がそれで困るかは**確かめていない**ので、
行 2・行 3 の判定にテーマページを入れるかは `design.md` 側の判断に委ねる。

### 行2 — 書き出しと再取り込み

**呼び出し**: `export_memopedia(source_id, manager=manager)` →
`import_memopedia(target_id, body=<その書き出し>, clear=False, manager=manager)`
**用意**: 書き出し元は基本の合成ペルソナ。取り込み先はコア記憶 1 件を持つ別の合成ペルソナ

| 判定 | 何を見るか |
|---|---|
| 提供すると言った範囲だけが出る | 書き出しの `pages` に `category == "core"` の行が 1 つも無い (あらすじと同じ扱い) |
| 手帳の中身は往復する | 書き出しに通常ページが入り、取り込み先で同じ表題・同じ本文のページとして引ける |
| 対象外の記憶を不完全な形で新設しない | 取り込み後、`parent_id == "root_core"` かつ `metadata IS NULL` の行が 1 つも無い |
| 読み取りが壊れない | 取り込み後の `list_core_memories(conn)` が例外を出さず、取り込み先がもともと持っていた 1 件だけを返す |
| 頭の提示が壊れない | `CoreMemorySection().capture(ctx)` が例外を出さず、`render` が取り込み前と同じ文字列を返す |
| 画面の一覧が壊れない | `list_core_memory(target_id, manager=manager)` が例外を出さずに一覧を返す |

### 行3 — 既存ページを置き換えて取り込み

**呼び出し**: `import_memopedia(target_id, body=<書き出し>, clear=True, manager=manager)`
**用意**: 基本の合成ペルソナ

| 判定 | 何を見るか |
|---|---|
| 置換対象が説明と一致する | 先行削除で消えるのは手帳の通常ページだけ。操作前後で `list_core_memories` と `list_deleted_core_memories` と `get_all_entries_ordered` の結果が一致する |
| 除外した記憶を先行削除しない | 「取り込みで作らない記憶」と「先行削除で消さない記憶」が同じ集合であること。コア記憶が取り込みの対象外なら、先行削除の対象からも外れている |
| 取り込み自体は使える | 入力にあった通常ページが取り込み先で引ける。`imported_count` がその数と一致する |

### 行4 — 不正な入力・途中の失敗

**呼び出し**: `import_memopedia(persona_id, body=<各場面の入力>, clear=True, manager=manager)`
**用意**: 場面ごとに別の基本の合成ペルソナ

| 場面 | 判定 |
|---|---|
| `pages` が無い / 空 | 先行削除が走らない (操作前後で生きているページの一覧が一致する)。応答が「何も起きなかった」と分かる形である |
| 取り込める行が 1 つも無い | **入力を確かめる前に既存データを消さない。** 消すなら、消す価値のある取り込みが成立することを先に確かめている |
| 一部の行が作成に失敗する | 応答が「成功したように見せない」。入力の件数・入った件数・入らなかった件数が判別できる。`success` が無条件に `true` にならない |
| 識別子が既存とぶつかる | 飛ばした行が応答に現れる。黙って数から消えない |
| どの場面でも | コア記憶とあらすじの読み取りが、操作前後で変わらない |

**2026-09-14 Astraレビューで補完対象とした。** 置換が保存途中で失敗した場合は操作前のデータを保つ案を具体化する。初稿では部分適用か巻き戻しかをまはーの判断待ちにしたが、まず担当が既存の「影響範囲を利用者の期待と合わせる」に照らして設計する。現行はper-pageで確定し、途中で止めても巻き戻らない (§2-4 場面4)。具体的な確定責任と検証は補完依頼に引き継ぐ。

### 行5 — 操作後の次の提示

**呼び出し**: `MemopediaIndexSection().capture_changes_since(ctx, since)` →
`MemopediaIndexSection().diff_to_notifications(old, new)`
**用意**: 基本の合成ペルソナ。基準時刻を秒単位で切り下げて取り、次の秒に入るまで待ってから操作する
(用意した行と操作で作った行が同じ秒に並ぶと区別できないため — `repro/exp5_next_presentation.py` の `wait_past`)

| 判定 | 何を見るか |
|---|---|
| コア記憶を刻んでも手帳の通知が出ない | `add_core_memory` の後、通知が 0 件 |
| あらすじが編まれても手帳の通知が出ない | `create_entry` の後、通知が 0 件 |
| 既存の正当な通知は維持する | 手帳の通常ページを 1 枚作ったら `Memopedia「<表題>」が作成されました` が 1 件出る |
| 操作後もコア記憶が読める | 各操作の後に `CoreMemorySection().capture(ctx)` が例外を出さない |
| 操作後もあらすじが読める | 各操作の後に `get_all_entries_ordered(conn)` が操作前と同じ結果を返す |

---

## 5. 実装と同じ変更で足す検査

**ここに書いた検査はまだ 1 つも書かれていない。** 実装と同じ変更で足すものとして書く。
既存のテストファイルに検査を足す前提で、置き場所ごとに使える土台を挙げる。

置き場所は 3 つ。保存の層・利用者の導線・ペルソナへ届く通知で、守る対象が違う。
保存の層だけを直しても、API の入口に別経路が残っていれば気づけない。
逆に API の入口だけを見ていると、画面を通らない経路 (スクリプト 3 本) が守られているか分からない。
通知はどちらの層にも属さないので、3 つ目が要る。

### 5-1. `tests/test_core_memory_storage.py` — 保存の層の契約

いまある土台 (`tests/test_core_memory_storage.py:21-27`):

```python
class CoreMemoryStorageTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_core_memory_table(self.conn)

    def tearDown(self):
        self.conn.close()
```

ファイルも一時ディレクトリも要らない。現在 19 件、実行 0.34 秒
(`python -m pytest tests/test_core_memory_storage.py -n 0 -q` で実測)。

**`Memopedia(conn)` がこの土台の上でそのまま動くことを確かめた** (この点は前段の調査で
未確認として残っていた)。`init_core_memory_table` が内部で `init_memopedia_tables` を
呼ぶので、素の `sqlite3.connect(":memory:")` から作った接続で

```
export のカテゴリ: ['core']
clear_all_pages の戻り値: 1
clear 後の list_core_memories: []
```

が得られる。追加の初期化は要らない。

足す検査 (3 件):

| 検査 | 中身 |
|---|---|
| 全削除がコア記憶を消さない | `add_core_memory(self.conn, "…")` → `Memopedia(self.conn).clear_all_pages()` → `[c.id for c in list_core_memories(self.conn)] == [1]` |
| 書き出しにコア記憶が入らない | `Memopedia(self.conn).export_json()["pages"]` に `category == "core"` の行が無い |
| 取り込みが壊れたコア記憶を作らない | `category="core"` を含む JSON を `Memopedia(self.conn).import_json(...)` に渡した後、`list_core_memories(self.conn)` が例外を出さない。かつ `parent_id='root_core'` で `metadata IS NULL` の行が 0 件 |

### 5-2. `tests/test_perception_rendering.py` の `MemopediaChronicleGuardApiTest` — 利用者の導線

いまある土台 (`tests/test_perception_rendering.py:952-987`)。あらすじを一括操作から守る検査が
既に置かれている場所で、**同じ理由が当てはまる隣**としてコア記憶の検査が並ぶ。

土台が用意しているもの:

- `tempfile.TemporaryDirectory` と `patch.dict(os.environ, {"SAIVERSE_HOME": …, "SAIMEMORY_MEMORY": "1"})`
  による隔離 (`:959-967`)
- 埋め込みを `_DummyEmbedder` (`:1609`) に差し替え (`:968-972`)
- 合成ペルソナ `SAIMemoryAdapter("tester")` (`:975-976`)
- あらすじ 1 件 (`self.entry`、`:977-983`)
- 偽の manager (`:984-987`) — `SimpleNamespace(personas={"tester": SimpleNamespace(sai_memory=self.adapter)}, SessionLocal=None)`

現在 59 件、実行 7.03 秒 (`python -m pytest tests/test_perception_rendering.py -n 0 -q` で実測)。

コア記憶を用意するには、既存の検査がページを作っているのと同じ形
(`:1045-1050` の `with self.adapter._db_lock:` の中) で
`add_core_memory(self.adapter.conn, "…")` を足すだけでよい。

足す検査 (4 件):

| 検査 | 中身 |
|---|---|
| 全削除がコア記憶とごみ箱を残す | `delete_all_memopedia_pages("tester", manager=self.manager)` の後、`list_core_memories` と `list_deleted_core_memories` が操作前と同じ。通常ページは `get_page` で引けない |
| 置き換え取り込みがコア記憶を先に消さない | `import_memopedia("tester", body=…, clear=True, manager=self.manager)` の後、同上 |
| 書き出しが出してよいものだけを出す | `export_memopedia("tester", manager=self.manager)` の `pages` の `category` の集合が、**出してよいカテゴリの集合と一致する** (含まれていないことの確認ではなく、集合の一致で書く) |
| 操作後も頭が組める | 各操作の後に `CoreMemorySection().capture(LineHeadInput(persona_id="tester", model_key=…, persona=SimpleNamespace(sai_memory=self.adapter)))` が例外を出さない |

**3 つ目の書き方について**: いまある `test_export_excludes_chronicle`
(`tests/test_perception_rendering.py:1079`) は
`all(p.get("category") != "chronicle" for p in data["pages"])` という
「この 1 つが混ざっていないこと」の形で書かれている。この形だから、
3 つ目のカテゴリ (コア記憶) が混ざっても緑のまま通った。
**出してよいカテゴリの集合との一致**で書けば、新しいカテゴリが増えたときに落ちて気づける。

### 5-3. `tests/test_p4d_memopedia_index_section.py` — ペルソナへ届く通知

通知 (§2-5) を固定する検査は、上の 2 つのどちらにも属さない。置き場所はここ。
`capture_changes_since` と `diff_to_notifications` を見る検査が既に 4 件ある
(`CaptureChangesSinceTest`: `tests/test_p4d_memopedia_index_section.py:204`、
`DiffNotificationBundlingTest`: `:232`)。現在 15 件、実行 6.42 秒
(`python -m pytest tests/test_p4d_memopedia_index_section.py -n 0 -q` で実測)。

いまある土台:

- `_make_mem_conn()` (`:27`) — `sqlite3.connect(":memory:")` に `init_memopedia_tables` だけを掛けた接続
- `_make_ctx(index_enabled, mem_conn)` (`:34`) — `LineHeadInput` の代わりになる偽の文脈
- `DiffNotificationBundlingTest._snapshots` / `_page` (`:240` / `:246`) —
  ページの並びから前後の snapshot を組み立てる小道具

コア記憶とあらすじを同じ接続に足すには、`_make_mem_conn` が返した接続に
`init_core_memory_table(conn)` と `init_arasuji_tables(conn)` を掛ければよい
(どちらも冪等で、`init_core_memory_table` は内部で `init_memopedia_tables` も呼ぶ)。

足す検査 (3 件):

| 検査 | 中身 |
|---|---|
| コア記憶を刻んでも手帳の通知が出ない | `add_core_memory(conn, "…")` の後、`capture_changes_since(ctx, since)` → `diff_to_notifications(old, new)` が空 |
| あらすじが編まれても手帳の通知が出ない | `create_entry(conn, …)` の後、同じく空 |
| 手帳のページの通知は出続ける | 通常ページを 1 枚作った後、`Memopedia「<表題>」が作成されました` が 1 件出る (既存の `test_detects_newly_created_page` の隣に、ラベルまで見る形で足す) |

**時刻の扱いに注意**: `capture_changes_since` は基準時刻以上の行を拾うが、
`diff_to_notifications` は基準時刻より**大きい**行だけを通知にする。
行の作成時刻は秒に丸められるので、基準時刻と同じ秒に作った行は通知に出ない。
検査を書くときは、基準時刻を秒単位で切り下げてから次の秒に入るまで待つか、
既存の `DiffNotificationBundlingTest` のように作成時刻を明示した snapshot を組み立てる
(後者のほうが速くて揺れない)。

### 5-4. 直すと落ちる既存テストについて

前段の調査 (担当 E) は「修正で書き換えが必要になる既存テストは 1 件も無い」と報告している。
私が確かめたのはその手前まで — 上の 3 ファイルが**現時点で緑である**ことは実測した
(19 件 / 59 件 / 15 件、どれも全部通る)。
**修正を入れた後も緑のままかは、実装が無いので確かめようがない。**

フルスイートは回していない (依頼の指示どおり、対象ファイル単位で `-n 0` を付けて回した)。

---

## 6. 未検証の境界

この検証で答えを出していないもの。設計と実装のレビューでは、ここを埋めた前提で読まないこと。

1. **ペルソナが実際に喋れなくなるか。** 頭のコア記憶の章の撮り直しが `TypeError` で落ちることは
   実測した。そこから Pulse が止まるまでは追っていない。頭には「前の値を据え置く」逃げ道があり、
   据え置く値があるうちは表面化しない。**「往復させると喋れなくなることがある」までが言えることで、
   「必ず止まる」とは言えない。**
2. **症状が出るまでの時間差。** コア記憶の章は Metabolism のときにしか撮り直されない。
   操作の直後には何も起きず、いつ表面化するかはモデルの種類とキャッシュの寿命に依る。
   この遅れは再現していない。
3. **画面の表示。** コア記憶の一覧 API が落ちたとき、画面が `まだコア記憶はありません。` と
   出すことは、コードを読んだだけで動かしていない。
4. **実際の HTTP と壊れた JSON ファイル。** ルート関数を直接呼んでいるので、
   FastAPI のリクエスト検証は通っていない。
5. **スクリプトからの一括操作。** 画面以外に一括削除の入口が 3 本ある
   (`scripts/export_memopedia.py` / `scripts/build_memopedia.py --clear` /
   `scripts/build_arasuji.py --clear-memopedia`)。どれも動かしていない。
   修正を `Memopedia` クラスの中に置けばここも直るはずだが、**確かめていない**。
6. **机の参照が別のページを指すか。** 全削除のあと机の参照が残ることは実測した。
   そのあと新しいページが同じ番号を取って、残った参照が無関係なページを指す経路は再現していない。
7. **テーマページの読み手。** テーマページが全削除で消えること、往復で `metadata` を失うことは
   実測した。それで困る読み手がいるかは調べていない。
8. **ページの埋め込みの孤児。** `delete_page` がページ本体の埋め込みを消さないことは
   前段の調査の指摘だが、この検証では埋め込みを偽物に差し替えているので**確かめていない**。
9. **並行して動いているバックエンドとの競合。** バックエンドを起動していないので、
   操作中にペルソナが同じ DB を触る状況は見ていない。
10. **既存の利用者の手元にあるファイル。** 既に書き出された JSON にコア記憶の行が
    入っていることは仕組みから確実だが、実際のファイルは 1 つも見ていない
    (本番データに触れないため)。
