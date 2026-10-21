# ペルソナまるごとパックのストリーミング化 — 調査レポート

対象: worktree `persona-data-map` (ブランチ feature/persona-archive)。コードは変更していない。

略号:
- **I** = `saiverse_memory/persona_pack/importer.py`
- **E** = `saiverse_memory/persona_pack/export.py`
- **F** = `saiverse_memory/persona_pack/format.py`
- **API** = `api/routes/people/persona_pack.py`
- **T-imp / T-exp / T-api** = `tests/test_persona_pack_{import,export,api}.py`

印:
- **[確認]** = コードを読んで確かめた事実。
- **[推測]** = 読んだ事実から引いた見立て。確かめてはいない。

## 0. 発注文と実物の食い違い (先に共有)

- [確認] 発注文の属性名のうち `attachments_index` の実名は `attachment_index` (I 850)、`ext_persona_dir` の実名は `ext_persona_files` (I 860)。`manifest` は属性として保持されない。`_load_pack` の局所変数として使われ、属性に残るのは `source_id` / `contents` / `version` だけ (I 882-895)。`chatgpt_compat` は importer のどこからも読まれない (I 全体を grep して 0 件。ZIP の名前一覧 `reader.names` に載るだけ: I 437)。
- [確認] 発注文の背景には「書き出しは各表を丸ごとリストに起こしてから JSON 文字列にして ZIP へ書く」とあるが、実物は大半の表がすでに逐次書きになっている。`_PackWriter.write_json_array` が `zf.open(arcname, "w", force_zip64=True)` に 1 要素ずつ書いている (E 409-423)。丸ごと保持が残っている箇所は D 節に列挙した。

---

## A. 取り込み側: `_PackData` の全属性の消費パターン

### A-1. 読み込み時 (`_load_pack`) の共通の振る舞い

- [確認] 配列の JSON はどれも `_PackReader.rows` → `load_json` → `json.loads(zf.read(name).decode("utf-8"))` で丸読みしている (I 442-461)。そのうえで要素ごとに「dict であること」と「必須キーがあること」を検査する。これが各配列の **1 回目の全走査** (I 455-460)。
- [確認] messages と pages には、さらに `_refuse_structured_raw_text` の全走査がかかる (I 901-909, 562-609)。この関数は行を書き換え (bytes の包みを解く: I 590) つつ、除いた行を落とした新しいリストを返す (I 578, 593)。
- [確認] extensions の memory_db の表には `_unwrap_bytes_perception_columns` の全走査がかかり、行を書き換える (I 959-960, 612-651)。
- [確認] 書き込み前に全部を検査する、という契約が module docstring に書かれている (I 147-148)。`validate_pack` も同じ `_load_pack` を通す (I 1069-1083)。

### A-2. 属性ごとの表

分類の凡例:
- **逐次1** = 先頭から 1 回回すだけ
- **逐次N** = 複数回回す
- **全体** = len / 添字 / ソート / 全体を使う判定がある
- **一枚** = 一枚物

ここでの「回数」は、取り込みの工程 (`_ImportJob`) の中での走査の数で、A-1 の読み込み時の検査走査は含めない。「量」の列は特記しない限り [推測] で、根拠を括弧に添えた。

| 属性 (定義行) | 消費箇所 (関数: 行) | 分類 | 量の性質 | 順序への依存 |
|---|---|---|---|---|
| `source_id` (837) | `import_persona_pack`: 1131 / `_ImportJob.__init__`: 1195 (以後は `self.source` として remap_resource 1376・remap_exact 1382・decode_message 1962-1969・印 1497・check_pending_marker 1684・plan_items 1536・stage_day_reports 3046 で使う) | 一枚 | スカラー | なし |
| `contents` (838) | `_load_pack` 内の `has` だけ: 896 | 一枚 | 有界 (フォルダ名 10 種: E 99-110) | なし |
| `version` (839) | `write_pending_marker`: 1499 | 一枚 | スカラー | なし |
| `threads` (840) | `stage_chatlog`: 1827 (thread_map を作る), 1836 (真偽), 1838-1848 (挿入) | 逐次N (2 回) | 部屋数 + 器だけのスレッド。緩やかに増える | [確認] なし。写像は先に全部作ってから挿入する (I 1825-1833) |
| `stelis_threads` (841) | `stage_chatlog`: 1829-1831 (写像), 1851 (真偽), 1855 (existing_keys への生成器), 1857-1867 (挿入) | 逐次N (3 回) | Stelis を使うたびに増える (中規模) | [確認] なし。親の写像は 1829-1831 で全部決まる |
| `messages` (842) | `stage_chatlog`: 1832 (写像), 1870 (**全 id を集合 `self.message_ids` に保持**), 1871, 1877 (existing_keys), 1880-1885 (新規分を **リスト `new_messages` に保持**)。その後 `new_messages` を plan_attachments 2002-2007・1891-1899 (器のスレッド)・1908 (**全体ソート** `sorted(enumerate(new_messages), key=_order)`)・`seq_rowids_if_free` 1926-1941 (**全 seq の一意性** `len(set(seqs))`: 1932)・挿入 1913-1920 で回す。`self.message_ids` は stage_ext_saiverse 3283 (addon_message_metadata の許可集合) が使う | **全体** (ソートと seq の一意性の判定) + 逐次N | [確認] 寿命とともに際限なく育つ (intent §3-6、docs/intent/persona_pack.md 54) | [確認] **あり**。挿入順を正典順 (created_at, seq) に並べ直す (I 1901-1908)。seq が無い行の並びの鍵は `new_messages` 内の位置 (= ZIP の並び順) (I 1906)。ZIP 内の id の重複は先に来た方を採る (I 1879-1885)。seq を rowid に使う判定は全件がそろってからでないとできない (I 1922-1942) |
| `pages` (843) | `stage_knowledge`: 2253 (全 id のリスト), 2265→`plan_chronicle_merge` 2547-2569 (chronicle の候補を dict `candidates` に保持) と 2575-2596 (level でソートして判定), 2270-2330 (本走査), 挿入する分は `to_insert` / `decoded_pages` / `metas` / `zip_numbers` の 4 つのリストに保持して (2336-2343) 番号を計画し (2346-2385。`_plan_numbers` は全件をソートする: 704-707)、挿入する (2390-2407) | **全体** (番号の計画と Chronicle の判定) | 中規模。ただし Chronicle (あらすじ) は会話量に比例して寿命とともに増える [推測] | [確認] **あり**。Chronicle の Lv1 は「ZIP での並び順で先の一本だけ」(I 2571-2583、intent 87)。`_plan_numbers` の同点は挿入候補の中の位置で決まる (I 704-707)。ZIP 内の id の重複は先勝ち (I 2273-2276, 2551-2553) |
| `activities` (844) | `stage_activities_and_memos`: 2778, 2792 (**id でソート** `sorted(pack.activities, ...)`)。`id_map` (ZIP の id → 取り込み先の id) を全件ぶん保持し、memos が使う (2777, 2837) | **全体** (ソート) | 手帳の行。寿命とともに増える (中規模) | [確認] **あり**。id 順に処理し、開いている同名へまとめる判定は先に来た行が勝つ (I 2805-2825) |
| `memos` (845) | `stage_activities_and_memos`: 2827, 2829 (uuid の existing_keys), 2830 (idem_key の existing_keys), 2831 (**id でソート**して挿入) | **全体** (ソート) + 逐次N | 寿命とともに際限なく増える | [確認] **あり**。id 順。ZIP 内の uuid / idem_key の重複は先勝ち (I 2847-2849) |
| `clips` (846) | `stage_clips`: 2882 (existing_keys), 2894-2907 (挿入する分を **リスト `to_insert`** に保持), 2910-2915 (**`_plan_numbers` で全件の番号を計画**), 2917-2932 (挿入)。commit 後の `repair_resumed_item_refs`: 1581 (clip_id の全リスト) | **全体** (番号の計画) + 逐次N | 寿命とともに増える | [確認] **あり**。`_plan_numbers` の同点 (I 704-707)。ZIP 内の重複は先勝ち (I 2903-2906) |
| `purpose_tags` (847) | `stage_purpose_tags`: 2944 (existing_keys), 2947-2968 (挿入)。commit 後の `repair_resumed_item_refs`: 1599 (tag_id の全リスト)。※取り込み先の全組 `pairs` を DB から読む (2945)。これは取り込み先側のメモリ | 逐次N (2 回 + 修復時に 1 回) | 寿命とともに増える | [確認] **あり**。(target_ref, purpose_ref) の組の重複は先勝ち (I 2961-2967) |
| `task_book` (848) | `stage_task_book`: 3003 (TASK_ID の全リスト), 3004 (ORIGIN の全リスト), 3005-3006 (keys と派生 ID の dict), 3014 (IDEM_KEY の existing_keys), 3016-3038 (挿入) | 逐次N (リストを 3 本作る + 2 回走査) | 約束帳。緩やかに増える | [確認] **あり**。ZIP 内の重複は先勝ち (I 3034-3037) |
| `day_reports` (849) | `stage_day_reports`: 3041-3073 (1 ファイルずつ指紋を取って計画)。保持しているのは (ZIP 名, 相対パス) だけで中身ではない (917-919) | 逐次1 | 日数に比例 (名前だけなので小さい) | [確認] 弱い。同じ日付で中身が違うときの `.from_<label>_<n>` の採番が、先に計画したものから名前を取る (I 3058-3073) |
| `attachment_index` (850) | `plan_attachments`: 1996 (真偽), 2017-2040 (計画) | 逐次1 | 添付の数に比例 (寿命とともに増える) | [確認] 弱い。`choose_media_dest` の `_n` の採番は、先に計画したもの (`reserved`) が名前を取る (I 2058-2061, 1388-1390) |
| `profile` (851) | `stage_profile`: 3078-3144 / `occupancy_rooms`: 3354-3355。※API も別に丸読みする (API 933) | 一枚 | 有界 (ai の 1 行。SYSTEMPROMPT の長さには依存する) | なし |
| `private_room` (853) | `occupancy_rooms`: 3356-3357 / `stage_items`: 3432-3441 / `plan_items`: 3519-3520 / `restore_private_room`: 3621-3695 / `count_merge_skips`: 3847 | 一枚 (fixtures のリストを含む) | 有界 (私室の中の物の数) | [確認] 弱い。fixture は ZIP の順に入れる (I 3667) |
| `items` (854) | `pack_items` 3457 (`note_item_numbers` 3476 と `restore_items` 3706 から計 **2 回**) / `plan_items` 3516-3589 (locations の dict と不動点ループ。`note_item_numbers` 3481 と `restore_items` 3713 から計 **2 回**) / `restore_items` 3705-3775 / `count_merge_skips` 3846 | **全体** (不動点ループと計画の再計算・照合: 3713-3718) | 有界 (所持品の数)。ただし ITEM が増え続ける運用ならそのぶん増える [推測] | [確認] 弱い。plan_items は `sorted(pending)` で ID 順に判定する (I 3532)。ファイルの順には依存しない |
| `ext_memory_tables` (855) | `perception_media_rows` 1988-1992 (知覚台帳の 2 表、plan_attachments から: 2008) / `room_state_digest_map` 2174-2183 (ROOM_STATE の 2 表) / `stage_ext_memory` 3163-3202 (全表の挿入) / `stage_merge_recall_edges` 3800-3834 (chunk_page_edges: **全行を decode したリスト 3808** + existing_keys 3809 + 挿入 3814) / `count_merge_skips` 3855-3858 (len) | 逐次N (知覚台帳は取り込み工程で 3 回走査: 添付の計画、指紋の対応、挿入) | [確認] 知覚台帳は削除しない永続台帳 (E 136-139、intent 73)。edit_history・pulse_logs・chunk_page_edges も増え続ける [推測]。desk_items は有界 | [確認] **あり**。差分の記帳の `base_digest` は別の行 (別の表もありうる) の土台の束を指す。だから指紋の対応表は全行から先に作る必要がある (I 2163-2184)。挿入は `INSERT OR IGNORE` で、元の id 列ごと入れる (I 3178, 3193)。行の順は結果に効かない [推測] |
| `ext_memory_refused` (858) | `report_refused_ext_memory_tables` 3150 (`stage_ext_memory` 3159 と `count_merge_skips` 3854 から) | 一枚 (表名 → 件数) | 有界 | なし |
| `ext_saiverse_tables` (859) | `stage_ext_saiverse`: 3235-3241 (許可外の表), 3243-3324 (表ごとの挿入)。子表の判定で **親の表を再走査して `zip_parents` を集合に** (3266-3270) / `count_merge_skips` 3850-3851 (len) | 逐次N (親の表は 2 回) | [推測] persona_event_log・building_occupancy_log・persona_task_history・episodes は寿命とともに増える。schedule と timetable は有界 | [確認] 表の順序には依存する (SAIVERSE_DB_TABLE_OWNERS の並びで親が先: I 3243、E 218-234)。行の順序にも弱く依存する。BIGINT 主キーの `next_pk` 採番は ZIP の行の順 (I 3317-3319) |
| `ext_persona_files` (860) | `stage_persona_dir_files`: 3408-3420 / `count_merge_skips`: 3859-3860 | 逐次1 | ファイル数に比例 (voice_calls/ の wav が増える) | なし |
| `refused_json_text` (864) | `report_refused_json_text`: 1483 | 逐次1 | 正規のパックでは 0。細工したパックでは行数まで育つ [推測] | なし |
| `ext_memory_nulled` (868) | `stage_ext_memory`: 3160 | 逐次1 | 同上 | なし |

### A-3. `_PackData` の外で、パックの量に比例して育つ `_ImportJob` の状態

作業用の控えへ移すかを決めるとき、属性だけでなくこれらも同じ判定の対象になる。

- [確認] `thread_map` (I 1199): スレッド ID の数だけ。
- [確認] `message_ids` (I 1237, 1870): **全メッセージの id**。merge で取り込み先がすべて持っていても、パックの全件ぶん持つ。
- [確認] `existing_keys` の戻り値の集合 (I 1313-1326): messages の 1877 では、再合流 (全件既存) のときに全件ぶんになる。
- [確認] `relocations` / `path_relocations` / `invalidated_media_keys` (I 1200-1206): 添付の数だけ。
- [確認] `file_plans` / `reserved` / `created_files` / `created_dirs` (I 1207-1210): 書くファイルの数だけ (パスだけで中身は持たない)。
- [確認] `ref_numbers` (I 1223): ページ・切り抜き・所持品の番号の対応。ページ数 + 切り抜き数に比例する。
- [確認] `inserted_page_ids` / `reparented` / `imported_chronicle_ids` (I 1214-1218)。
- [確認] `check_page_tree` は取り込み先の memopedia_pages 全行の (id, parent_id, category, is_trunk) をメモリに読む (I 2715-2718)。パックではなく取り込み先側だが、中規模の全体保持。
- [確認] `stage_purpose_tags` は取り込み先の全組を集合に読む (I 2945)。取り込み先側のメモリ。

---

## B. 取り込み側: メディアと指紋の丸読み箇所 (全数)

`reader.read(` と `_sha256_bytes(` を grep した全結果。定義は I 439-440 (`zf.read`) と I 788-789。

| # | 箇所 | 読むもの | ストリーム化 | 周辺への影響 |
|---|---|---|---|---|
| 1 | I 444 `load_json` (`zf.read`) | JSON (manifest・profile・private_room・items・attachments/index と、`rows()` 経由の全配列) | 配列は E 節の逐次読みに置換できる。一枚物 (manifest 882・profile 936・private_room 978) はメモリに持ったまま。items.json (1000) は配列ではなく `{"items":[…], "locations":[…], "files":{…}}` の dict (E 1122-1126) なので、配列用の逐次読みはそのまま使えない [確認] | attachments/index.json は配列 (E 976) なので逐次読みに乗る [確認] |
| 2 | I 1797 `write_files`: `data = self.reader.read(plan.zip_name)` | **全ファイル** (添付・アイコン・所持品・私室画像・day_reports・persona_dir のファイル) | 可能。`zf.open(name)` → `partial.open("wb")` へ分割コピー (shutil.copyfileobj 相当) → flush → fsync → os.replace | [確認] T-imp 2196-2222 が `Path.open` を差し替えて、一時ファイルを `Path.open` の "w" 系モードで開き `.write` を呼ぶ前提で書かれている。差し替え後も `partial.open("wb")` (Path.open) で開き、`.write` で書く形を保つ必要がある (builtins.open や os.sendfile にすると、このテストの注入が効かなくなる)。テストの `_StopMidWrite` には `write` と with 文の口しか無い (2199-2212)。最初の `.write` で例外になるので、flush / fileno に届かず問題ない [確認] |
| 3 | I 2026 `plan_attachments`: `_sha256_bytes(self.reader.read(zip_path))` | 添付メディア | 可能 (`zf.open` + 1 MiB ずつ `hashlib.update`。`_sha256_file` 792-797 と同じ形) | [確認] 結果は `choose_media_dest` に渡り、取り込み先の同名ファイルの `_sha256_file` (既にストリーム: 792-797) と比べる (2063)。比較の意味は変わらない。[推測] 同じ zip_path を別の参照 (uri の鍵と path の鍵) が共有しうる (E 427-429 の `_copied` が同じ実体を同じ ZIP 名にまとめる) ので、zip_name をキーにした指紋のキャッシュがあると二重計算を避けられる。ZIP は計画時の指紋で 1 回、write_files で 1 回、計 2 回読まれる。メモリではなく I/O の問題 |
| 4 | I 3048 `stage_day_reports` | day_reports の Markdown | 可能 | [確認] 結果は取り込み先の `_sha256_file(dest)` / `_sha256_file(candidate)` との比較だけに使う (3054, 3067) |
| 5 | I 3105 `stage_profile` | アイコン・外見画像 | 可能 | [確認] `choose_media_dest` へ (3112)。#3 と同じ |
| 6 | I 3600 `plan_media_restore` | 私室の画像・fixture と所持品のファイル | 可能 | [確認] `choose_media_dest` へ (3604)。#3 と同じ |
| 7 | I 3784 `plan_keep_existing` | persona_dir のファイル (log.json・tasks.db・voice_calls の wav など。大きくなりうる) | 可能 | [確認] 保存先が既にあるときだけ読む (3782-3784) |

- [確認] ZIP 全体の指紋 `_sha256_file(zip_path)` (I 1125) は既に 1 MiB ずつ読んでいる。
- [確認] API 側の `_read_pack_json` (API 142-146) は `zf.read` の丸読みで、manifest (API 166) と profile/profile.json (API 933) にしか使っていない。どちらも一枚物。

---

## C. トランザクションと一時ファイルの既存の作法

### C-1. 既存の一時ファイルの流儀

- [確認] 取り込みのファイル書き込みは、保存先と同じフォルダの `<保存先の名前>.<import_id 先頭16字>.partial` に書き切ってから os.replace で置く (I 1769-1775, 1795-1805)。一時ファイルは書く前に `created_files` に積み (1796)、失敗したら `remove_created_files` が消す (1461, 1807-1819)。同じ取り込みのやり直しは同じ名前を上書きして使う (I 1772-1773、T-imp 2241-2242)。
- [確認] 印は `<印>.tmp` に書いて os.replace する (I 1510-1514)。
- [確認] 書き出しは出力先の隣の `<出力>.partial` に書き、失敗したら unlink、成功したら os.replace する (E 748, 785-791)。
- [確認] API はアップロードを `tempfile.NamedTemporaryFile(delete=False, prefix="persona_pack_", suffix=".zip")` で作った名前に書き (API 112-122)、`finally` で消す (API 944 ほか)。書き出しは `tempfile.mkdtemp(prefix="persona_pack_export_")` に書き、送信後に rmtree する (API 197, 222)。どちらも **OS の一時フォルダ** に置く。
- [確認] `saiverse/data_paths.py` に一時フォルダの規約は無い ("tmp"/"temp" の grep が 0 件)。
- [確認] ペルソナフォルダに置いたファイルは、書き出しで丸ごと `extensions/saiverse/persona_dir/` に入る。除外されるのは memory.db 一式・印・day_reports だけ (E 1225-1228)。取り込みでも復元の対象になる (I 3407-3420)。

**提案 (採否は発注側):**

- 作業用の一時 SQLite は **ZIP と同じフォルダ** に置く。名前は `<zip名>.<import_id 先頭16字>.work.sqlite`。`import_persona_pack` / `validate_pack` の `finally` で必ず消し、開くときに同名があれば消してから作る。理由は次の三つ。
  1. ZIP がそこに置ける以上、展開分の空き容量の見込みも同じ場所で立つ。
  2. 再開の合流は ZIP を読み直して作業 DB を作り直すので、作業 DB を取り込みの外へ持ち越す必要が無い [確認: 再開の判定材料は印と両 DB の適用済み記録だけ。I 1648-1725]。
  3. 名前に import_id を入れるのは既存の `.partial` の流儀 (I 1775) に揃えるため。
- **ペルソナフォルダには置かない**。取り残しが次の書き出しに入り、取り込みでも復元されるため (E 1225-1233, I 3407-3420)。置くなら E 1225 と I 3414 の除外に足す必要がある。
- 引数で場所を差し替えられるようにする案もある。API から SAIVERSE_HOME 配下を渡せるようにするため。
- [推測・要確認] Linux の配布によっては OS の一時フォルダ (/tmp) が tmpfs (= RAM) で、Debian 13 は既定で tmpfs になったはず。そうだと、2 GB VPS で /tmp に置いた作業 DB はメモリを食い、本件の目的に反する。**現状でもアップロードの ZIP (最大 2 GiB) が同じ /tmp に置かれている** (API 114) ので、これは作業 DB 以前の既存リスクでもある。
- [推測] 作業 DB は捨てるものなので `PRAGMA journal_mode=OFF` / `synchronous=OFF` で足りる。`temp_store=FILE` と小さめの `cache_size` を明示しておくと、索引作成やソートが RAM へ寄らない。SQLite の一時ファイルの置き場も環境 (TMPDIR) 次第で、/tmp が tmpfs なら同じ問題が出る。

### C-2. memory.db / saiverse.db の書き込みのトランザクション

- [確認] memory.db は `_open_memory_db` で init を全部済ませて commit した後、`isolation_level = None` (自動 commit。BEGIN を明示したときだけトランザクション) にする (I 1161-1169)。saiverse.db も `isolation_level=None` で開く (I 1398)。
- [確認] memory.db は `BEGIN IMMEDIATE` (I 1419) から `COMMIT` (I 1450) までの単一トランザクション。saiverse.db は `BEGIN IMMEDIATE` (I 1429) から `commit_world` の `COMMIT` (I 1452, 1487-1489) までの単一トランザクション。途中で例外が出たら両方 ROLLBACK し、作ったファイルを消す (I 1453-1469)。memory.db の commit 後に saiverse.db が失敗した場合は印で再開する (I 1462-1468)。
- [確認] 途中で呼ぶ外部の書き込み関数は commit しない形で呼んでいる: `write_deferred_message_embedding(..., commit=False)` (I 1918)、`record_page_edit(..., commit=False)` (I 2327)。
- [確認] commit 後の `repair_resumed_item_refs` は別の単一トランザクション (I 1580-1625)。
- **結論 [確認]**: どちらの DB も BEGIN で囲んだ中で `conn.execute` を重ねているだけなので、挿入を 1 行ずつ・細切れにしても、同じ接続・同じ BEGIN の中にある限り原子性は変わらない。守るべきことは次の二つ。
  - 作業 DB は **別の接続・別のファイル** にする。取り込み先の接続に ATTACH すると、その接続の BEGIN が複数ファイルのトランザクションになり、作業 DB への書き込みまで ROLLBACK の対象に入る。
  - 作業 DB を作る (ZIP を読み解いて積む) 工程は、取り込み先の BEGIN より前に終える。これで「書き込み前に全部検査」の契約 (I 147-148) と `validate_pack` (I 1069-1083) の意味が保たれる。
- [推測] memory.db と saiverse.db はどちらも WAL (sai_memory/memory/storage.py 30-33、database/session.py 27)。大きな書き込みトランザクションの未確定のページは、ページキャッシュがあふれると WAL へ書き出される (SQLite の cache spill)。だからトランザクションが大きくてもメモリは頭打ちになり、WAL ファイル (ディスク) が commit まで膨らむ。
- [確認] memory.db の書き込みロックは、世界側の工程とファイル書き込みの間も保持したまま (BEGIN IMMEDIATE 1419 → COMMIT 1450。その間に 1429-1449)。ストリーミング化で変わる点ではないが、取り込みの時間が長くなるとロックの保持時間も延びる [推測]。合流は Beat ロックの内側で走る (API 554-560)。

---

## D. 書き出し側: export.py の全表の書き出しパターン

### D-1. `zipfile` の逐次書き (Python 3.13.2 の実装を読んで確認)

- [確認] `zf.open(str, "w")` は ZipFile のその時点の `compression` / `compresslevel` を引き継ぐ (stdlib `ZipFile.open`: `zinfo.compress_type = self.compression`)。この書き出しの ZipFile は `ZIP_DEFLATED` (E 759) なので、逐次書きのエントリも DEFLATE になる。
- [確認] 大きさが事前に分からないので、`force_zip64=False` のまま 4 GiB を超えると close 時に `RuntimeError("File size too large, try using force_zip64")` になる (stdlib `_ZipWriteFile.close`)。`write_json_array` は `force_zip64=True` を渡している (E 413)。出力は通常のファイルで seek できるので、ローカルヘッダは close 時に書き戻され、データ記述子は使わない (stdlib `_open_to_write`、`_ZipWriteFile.close`)。
- [確認] `zf.write(src, arc)` は内部で `shutil.copyfileobj(src, dest, 1024*8)` を使うので、メディアのファイルの書き出しは **既に丸読みしていない** (stdlib `ZipFile.write`。E 439 `copy_file` はこれを使う)。
- [確認] 書き込みハンドルを開いている間は、同じ ZipFile への他の書き込み (`write` / `writestr` / 別の `open("w")`) が `ValueError` になる (stdlib `_open_to_write` の `self._writing` 検査)。今の messages の生成器 (E 863-892) は、ハンドルの内側で添付の参照を集めるだけで、ファイルを書いていない。添付のコピーは書き終えた後に行っている (E 944-976)。ストリーミング化でも「配列を書いている途中に別のエントリを書かない」制約は残る。
- [確認] `writestr` (E 407 `write_json`、E 783 manifest) は文字列全体を作ってから書く。一枚物には問題ない。

### D-2. 表ごとの現状

| ZIP エントリ | 読み方 (行) | 書き方 | メモリに丸ごと持つもの |
|---|---|---|---|
| `profile/profile.json` | ai 1 行 (E 754) | write_json (E 822) | 一枚物。問題なし |
| `profile/icon|appearance/*` | — | copy_file = zf.write の分割コピー (E 821) | なし |
| `chatlog/threads.json` | `_iter_table_rows` (rowid 順) | write_json_array | **`thread_rows = list(...)` で全スレッドを保持** (E 847)。タイトルの dict `thread_titles` (E 845-849) は chatgpt_compat 用。スレッドは中規模 [推測] |
| `chatlog/stelis_threads.json` | `_iter_table_rows` | write_json_array (E 855-858) | なし |
| `chatlog/messages.json` | `SELECT rowid AS __pack_rowid, * FROM messages ORDER BY created_at ASC, rowid ASC` (E 864-868) | write_json_array に生成器 (E 894-896) | 生成器の中で **全メッセージ id の集合 `message_ids`** (E 872)・**添付の参照の dict `attachment_refs`** (E 876-881)・**会話メッセージの本文を全部ためる `conversations`** (E 882-891) を作る。`conversations` が書き出し側で最大の保持で、ユーザーとアシスタントの会話本文のほぼ全量になる [推測]。[確認] messages には created_at 単独の索引が無い (索引は (thread_id, created_at) と (resource_id, created_at) など: sai_memory/memory/storage.py 132-133, 191-201)。このため ORDER BY は全件の外部ソートになる。[推測] SQLite のソーターは一定量を超えると一時ファイルへ逃がすのでメモリは頭打ちになるが、一時ファイルの置き場は C-1 の tmpfs の問題と同じ |
| `knowledge/pages.json` | `_iter_table_rows` | write_json_array (E 900-903) | なし |
| (知覚台帳と部屋の様子の記帳の media の走査) | `SELECT id, media ... ORDER BY id` (E 911-920)、`SELECT id, <列> ... ORDER BY id` (E 928-941) | (エントリなし) | `attachment_refs` に参照ごとに `perception_rows` のリストを積む (E 915-920, 934-941)。添付の数 × 参照元の数で育つ |
| `attachments/<種類>/*` | — | copy_file (E 960-965) | なし (分割コピー) |
| `attachments/index.json` | `attachment_refs` から | **write_json (全体を文字列化。indent=2)** (E 976) | `index` のリスト全体 (E 945-975) + json.dumps の文字列。添付の数に比例する。index.json は配列なので write_json_array に差し替えられる [確認: E 945 で list を作っている] |
| `journal/{activities,memos,clips,purpose_tags}.json` | `_iter_table_rows` | write_json_array (E 980-985) | なし |
| `extensions/saiverse/memory_db/<表>.json` | `_iter_table_rows` | write_json_array (E 988-998) | なし |
| `chatgpt_compat/conversations.json` | `conversations` から | **write_json (全体を文字列化)** (E 1001-1007) | `built` (全会話の mapping ツリー) + json.dumps の文字列。`conversations` と合わせて会話本文がメモリに 2〜3 重に載る [推測]。差し替え案 [推測]: messages を `ORDER BY thread_id, created_at, rowid` でもう一度読む (索引 idx_messages_thread_created が先頭 2 列に効く: storage.py 132)。スレッドごとに build_conversation して JSON 配列へ逐次書きすれば、保持は 1 スレッドぶんになる。ただし 1 スレッドが巨大なら mapping も巨大で、chatgpt_compat.build_conversation (chatgpt_compat.py 50-110) を逐次化しない限り 1 スレッドぶんは残る。会話の並びは「最初に現れた順」(dict の挿入順 E 886) からスレッド ID 順に変わる。T-exp 279-294 は by_id の dict で見ているので、並びには依存していない [確認] |
| `items/private_room.json` | building 1 行 + `fixtures = list(...)` (E 1025, 1040) | write_json (E 1053) | 有界。問題なし |
| `items/items.json` | item_location を持ち主ごとに辿る (E 1074-1094)。item は 1 行ずつ SELECT (E 1086) | write_json (E 1122) | `locations` リストと `items` dict (E 1070-1071)。所持品の数に比例 (有界寄り) [推測] |
| `items/*/` のファイル | — | copy_file | なし |
| `journal/task_book.json` | `_iter_table_rows(... PERSONA_ID = ?)` | write_json_array (E 1159-1166) | なし |
| `journal/day_reports/*` | rglob (E 1173) | copy_file (E 1177) | なし |
| `extensions/saiverse/saiverse_db/<表>.json` (14 表) | `_iter_table_rows(world, table, where, (persona_id,))` (E 1191-1199) | write_json_array | なし |
| `extensions/saiverse/saiverse_db/addon_message_metadata.json` | **全ペルソナ分の表を全走査**し、Python で `message_ids` に含まれる行だけ通す (E 1201-1209) | write_json_array | 走査そのものは逐次。ただし `message_ids` (全メッセージ id の集合) の保持が前提になっている。[推測] 1 件あたり 100 バイト強なので 100 万件で 100 MB 規模。代案: 書き出しの memory.db の読み取り接続 (mode=ro、同じスナップショット: E 315-328) に、addon_message_metadata の行の message_id を 500 件ずつの `IN` で問い合わせて照合する。そうすれば集合を持たずに済む |
| `extensions/saiverse/persona_dir/*` | rglob (E 1223) | copy_file (E 1231) | なし |
| `manifest.json` | — | writestr (E 783) | 一枚物 |

- [確認] 二つの DB はそれぞれ一つの読み取りトランザクションで読む (E 320-325, 793-797)。同じ DB の中を二度読んでも同じスナップショットを見るので、messages を二度読む案 (chatgpt_compat) は整合性を壊さない [推測: SQLite の WAL の読み取りトランザクションの性質に依る]。

---

## E. 標準ライブラリだけでの逐次 JSON 読み

### E-1. パックの JSON の書式をどこまで仮定してよいか

- [確認] 書き出しの配列は `"["` → 要素ごとに `"\n"` か `",\n"` + `json.dumps(item, ensure_ascii=False)` (インデントなし、1 要素 1 行) → `"\n]\n"` の形 (E 413-421)。一枚物と index.json は `json.dumps(obj, ensure_ascii=False, indent=2)` (E 407)。
- [確認] 取り込みが検査するのは manifest の `format == "persona-pack"` と、`version` の上位桁が `"1"` であることだけ (I 883-887)。`generator` は見ていない。
- [確認] intent は「SAIVerse Lite が書き出した ZIP を本体へ取り込む」ことを予定している (docs/intent/persona_pack.md 36-45, 126)。形式の仕様を docs/reference/ に書く約束もある (同 45)。ただし docs/reference/ に persona-pack の書式の文書はまだ無い (grep で api-endpoints / database-schema だけが該当)。
- [確認] テストは配列を `json.dumps(value, ensure_ascii=False)` の **1 行の詰めた形** に書き換えてパックを作り直している (T-imp 718, 1444)。壊れた JSON も差し込んでいる (T-imp 703 `b"[{\"clip_id\": "`、T-api 529 `b"{broken"`)。
- **判定**: SAIVerse の書き出しの「1 要素 1 行」には依存できない。テストが詰めた形を使っていることと、Lite という別の書き手が予定されていることが理由。任意の空白・改行を許す、正規の JSON 配列一般を読む実装が要る。

### E-2. 実装案 (raw_decode + スライド窓)

[確認: Python 3.13.2 で動作を確かめた]
- `json.JSONDecoder().raw_decode(s, idx)` は位置 `idx` から 1 値を読み、`(値, 終端位置)` を返す。
- `idx` が空白を指すと `Expecting value` になるので、空白は自分で飛ばす必要がある。
- 途中で切れた値は `JSONDecodeError` (例: `Unterminated string`) になる。
- 末尾の数値 `"123"` は、続きがあっても `(123, 3)` で成功する。
- BOM 付きの文字列は `json.loads` が `Unexpected UTF-8 BOM` で拒否する。

```python
def iter_json_array(raw: BinaryIO, name: str, *, max_element_chars: int) -> Iterator[Any]:
    text = io.TextIOWrapper(raw, encoding="utf-8")   # strict。分割された多バイト文字は TextIOWrapper が繋ぐ
    dec = json.JSONDecoder()
    buf, pos, eof, want = "", 0, False, 1 << 20
    def fill(n):            # 消費済みを捨ててから n 文字読み足す (窓をずらす)
        nonlocal buf, pos, eof
        chunk = text.read(n); buf = buf[pos:] + chunk; pos = 0; eof = eof or not chunk
    def skip_ws():          # 空白を飛ばす (必要なら読み足す)。json の空白は " \t\n\r" のみ
        ...
    skip_ws(); expect("[")
    skip_ws()
    if peek() == "]": advance(); finish(); return
    index = 0
    while True:
        want = 1 << 20
        while True:
            try:
                obj, end = dec.raw_decode(buf, pos)
                if end == len(buf) and not eof:       # 末尾の数値・true 等が窓の端で切れている可能性
                    fill(want); continue
                break
            except json.JSONDecodeError as exc:
                if eof: raise ValueError(f"persona pack: {name} is not valid JSON: {exc}") from exc
                if len(buf) - pos > max_element_chars:
                    raise ValueError(f"persona pack: {name}[{index}] is larger than ...")
                fill(want); want *= 2                 # 読み足し量を倍々にして再解析の二乗を抑える
        yield obj; index += 1; pos = end
        skip_ws(); c = take()
        if c == "]": finish(); return                 # finish: 残りが空白だけでなければ Extra data で拒否
        if c != ",": raise ValueError(...)
        skip_ws()
```

- [確認] 既存の挙動との一致点として守るもの:
  - エラー文にファイル名を含める (T-imp 706 `match="clips.json"`、T-imp 1257 `"desk_items.json"`、T-api 548 `"chatlog/messages.json"`)。
  - UnicodeDecodeError を ValueError にする (I 445-446)。
  - 要素が dict でない・必須キーが無いときの文言 (I 453-460)。
  - 配列でないトップレベルを拒否する (I 453-454)。
- [推測] `raw_decode` は `json.loads` と同じスキャナなので、NaN / Infinity の受理 (確認済み: `json.loads("[NaN]")` → `[nan]`) と重複キーの後勝ちは一致する。
- [推測] 失敗のたびに窓の先頭から読み直すので、巨大要素では再解析が起きる。読み足し量を倍々にすれば償却で線形に収まる。

### E-3. 要素 1 つが巨大な場合

- [推測] 1 メッセージに巨大な base64 (thought_signature) や長文があると、その 1 要素の文字列と Python オブジェクトは必ず同時にメモリに載る。そこで「1 要素の上限 (文字数)」を設け、超えたら **切り詰めずに拒否** する (原則 2「本人名義のテキストは無改変」: intent 50)。拒否の文言はファイル名と添字を含める。
- [確認] 今は 1 エントリの展開サイズの上限 `MAX_ENTRY_UNCOMPRESSED_BYTES = 2 GiB` が、事実上この上限を兼ねている (I 369-372)。
- [推測] 値の目安は数十〜数百 MB。2 GB の VPS なら、要素の文字列と dict の数倍で 1 GB を超えない範囲に収める。

### E-4. 上限の定数の意味の変化 (B と E の帰結)

- [確認] `MAX_JSON_TOTAL_BYTES` (I 373-376) は「JSON は全ファイルを同時にメモリへ読む」ことを、`MAX_ENTRY_UNCOMPRESSED_BYTES` (I 369-372) は「1 ファイルを丸ごとメモリに読む」ことを、根拠としてコメントに書いている。ストリーミング化するとどちらの根拠も消える。
- [確認] intent が残すと明記しているのは「ディスクと細工 ZIP への保護」としての `check_uncompressed_size` (docs/intent/persona_pack.md 54)。
- [推測] 合計の上限 (I 364-368) はディスクの保護として残る。1 エントリの上限と JSON の合計の上限は、残すか、作業 DB のディスク使用量の上限として意味を付け直すかを決める必要がある。作業 DB の大きさは展開後の JSON と同程度かそれ以上になる [推測]。

---

## F. テストへの影響 (内部の形に触っている箇所)

挙動 (入出力) だけを見ているテストは列挙していない。

### F-1. `_ImportJob` のメソッドを差し替えているもの (名前・引数・呼ばれる順序に依存)

- [確認] `write_files(self)` を包む: T-imp 732-739 (後で `self.created_files` が空でないことを assert する: 736)、T-imp 2060-2068、T-imp 2137-2150、T-imp 3526-3535。
- [確認] `remove_created_files(self)` を無効化: T-imp 2068, 2150, 2226, 3535。
- [確認] `partial_path(self, path)` を包む: T-imp 2257-2264。
- [確認] `commit_world(self, world)` を差し替え: T-imp 1626-1634, 1968-1976, 2285, 3419, 3543、T-api 660, 669。
- [確認] `remove_pending_marker(self)` を無効化: T-imp 1937, 3500。
- [確認] `repair_resumed_item_refs(self, memory, world)` を包む: T-imp 3473-3499。このメソッドは commit 後に `self.pack.clips` / `self.pack.purpose_tags` の id のリストを読む (I 1581, 1599)。差し替えでは、作業 DB からの読み出しに変える必要がある。commit 後に作業 DB がまだ開いている (消していない) ことも前提になる。
- [確認] `stage_task_book(self, world)` を包み、その中で取り込み先に item を足す: T-imp 3569-3580。「`note_item_numbers` (計画: I 1416) の後で、`stage_items` (登録と照合: I 1434, 3713-3718) の前に `stage_task_book` (I 1430) が呼ばれる」という **run の工程順** に依存している。

### F-2. `Path.open` の差し替え (書き方に依存)

- [確認] T-imp 2178-2245 (params 2171-2177) は `Path.open` を差し替え、保存先かその `.partial` を "w" 系で開いた最初の `.write` で半分だけ書いて `SystemExit` を投げる。前提は三つ。
  1. write_files が一時ファイルを `Path.open` で開く。
  2. 書き込みが `.write` を通る。
  3. 該当の open がちょうど 1 回 (2231)。
- 分割コピーにしても、`partial.open("wb")` の `.write` を使えば成り立つ [推測]。最初の `.write` に渡る塊がファイルより小さい場合、半分書いた時点の長さは「0 より大きく、完全な長さより小さい」(2236) を満たす [推測]。

### F-3. モジュールの内部の名前・定数を参照しているもの

- [確認] `importer._ID_NAMESPACE` と derived_id の式: T-imp 301-305。
- [確認] `importer._sha256_file` / `importer._import_id`: T-imp 1942-1943, 2108, 2157。
- [確認] `importer.MAX_UNCOMPRESSED_BYTES`: T-imp 1269, 1275、T-api 846。`importer.MAX_ENTRY_UNCOMPRESSED_BYTES`: T-imp 1309, 1316-1317。`importer.check_uncompressed_size`: T-imp 1311, 1320, 1334, 1342。
- [確認] **T-imp 1323-1342 は JSON の合計の上限 (`"JSON files in the archive"`) を検査している**。E-4 で JSON の合計の上限を外すなら、このテストを直す必要がある。
- [確認] `importer.validate_pack` を monkeypatch: T-api 565-583。API は関数の中で import している (API 826-828) ので、属性の差し替えが効く。
- [確認] `importer._ACTIVITY_MERGES_TABLE`: T-imp 574。`importer.WORLD_STRING_KEY_TABLES`: T-imp 2402。export の定数 (`NOT_CARRIED_MEMORY_TABLES` T-imp 573、`SAIVERSE_DB_EXTENSION_TABLES` / `SAIVERSE_DB_TABLE_OWNERS` T-imp 1503, 2401、`WORLD_PENDING_MARKER` T-api 625、`_resolve_stored_path` T-imp 2795)。ストリーミング化では変わらない見込み [推測]。
- [確認] `export._open_read_only`: T-exp 530-539。

### F-4. 内部の形には触れていないが、書式の前提として効くもの

- [確認] パックを 1 行の詰めた JSON に書き換えて取り込ませるテスト (T-imp 695-724, 1437-1444)。逐次読みが書式に依存すると落ちる (E-1)。
- [確認] `_PackData` / `_load_pack` / `_PackReader` を直接触るテストは **無い** (tests/ 全体を grep して 0 件)。

---

## この調査から見える実装の束の切り方 (提案。採否は発注側)

1. **逐次 JSON 読み器** (E-2) を単独の束にする。対象は任意の空白の配列・ファイル名入りのエラー・BOM の拒否・要素の上限・末尾ゴミの拒否で、単体テストと一緒に入れる。他の束の前提になる。
2. **メディアと指紋のストリーム化** (B)。write_files の分割コピーと、5 箇所の `_sha256_bytes(reader.read())` の置換。zip_name をキーにした指紋のキャッシュも入れる。作業 DB と独立に入れられ、F-2 のテストの前提 (Path.open + .write) を守るだけで済む。
3. **作業 DB の器と `_load_pack` の置換**。配列を `(ordinal, 行の JSON)` で積み、積む時点で必須キー・`_refuse_structured_raw_text`・bytes の解きを済ませる。一枚物 (manifest・profile・private_room・items) はメモリのまま。ordinal を必ず持たせて「ZIP の並び順」に依存する判定 (A-2 の順序の列) を ORDER BY ordinal で再現する。`validate_pack` も同じ器を通す。
4. **取り込みの各工程を作業 DB からの走査へ**。対象は「全体」に分類したもの (messages のソートと seq の一意性 → SQL の ORDER BY と COUNT DISTINCT、activities と memos の id ソート、`message_ids` → 作業 DB の表か取り込み先 memory.db への照会)。pages と clips の番号計画と Chronicle の判定は中規模の全体保持として残すか、ここで決める。Chronicle が寿命とともに増える点を判断材料に。
5. **書き出しの残り** (D-2)。`conversations` / `built` の全量保持 (最大)、`message_ids` の集合、`thread_rows`、index.json の write_json。
6. **上限の定数の意味の付け直し** (E-4) と T-imp 1323-1342 の改訂。作業 DB のディスク使用量と、/tmp が tmpfs の環境での置き場所 (C-1) の扱いもここで決める。
7. **検証**: 合成の大きなパック (例: メッセージ数十万件) で、取り込みと書き出しのピークメモリ (tracemalloc か RSS) が件数に比例しないことを確かめるテストを置く。逐次化の後戻りを機械で止めるため。
