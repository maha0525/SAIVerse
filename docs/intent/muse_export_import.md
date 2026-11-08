# Muse エクスポート取り込み — Meta のパーソナル AI の会話をペルソナ記憶へ

**ステータス**: 検証待ち（前段 = 会話取り込みのパーサー・CLI・API・UI は実機確認まで完了。後段 = SOUL / IDENTITY / MEMORY / AGENTS のファイル取り込みは実装・自己検証まで完了し、メモリー画面からの実機取り込みは成功したが、Muse のファイル形式に付随する説明文が本文へ混入する問題が見つかり、除去の修正と再確認が残っている。2026-11-08 から、ファイル取り込みを取り込みの専用ウィンドウ (一本道) の Muse の流れにも必須二択として入れる対応が進行中 — [import_flow_redesign.md](import_flow_redesign.md) の「Muse のファイル取り込み」。アイコンの取り込みと workspace ファイルの復元は引き続き範囲外）

## 利用者が得るものと経路

Muse（Meta のパーソナル AI）の「Download your agent data」で取得した ZIP を、
SAIVerse のペルソナの記憶（SAIMemory）へ取り込めるようにする。ChatGPT の公式
エクスポートを取り込む既存の導線（`tools/utilities/chatgpt_importer.py` /
`scripts/import_chatgpt_conversations.py`）と同じ操作感で、`list` で中身を確認し、
`import` で選んだ会話をペルソナのスレッドとして書き込む。

経路:

```
Muse のエクスポート ZIP
  → manifest.json の turns（機械可読の正）
  → MuseExport / ConversationRecord / iter_memory_payloads()
  → scripts/import_muse_conversations.py
  → SAIMemoryAdapter.append_persona_message()
  → ペルソナの memory.db（thread = エクスポートの識別子）
```

人間可読の `Conversation with Muse AI_...txt` は manifest と内容が重複するため
パースに使わない。正は常に manifest.json の turns に置く。

## 設計原則（不変条件）

1. **manifest.json の turns が唯一の正。** トランスクリプト txt の文面を
   解釈して会話を組み立てない。turns の並びと `turn_id` をそのまま記録の順序・
   識別子として使う（並べ替えない）。
2. **既存の会話取り込みと同じ形のレコード／ペイロードにする。**
   `ConversationMessage` / `ConversationRecord` と `iter_memory_payloads()` を
   共有し、ChatGPT 版と同水準の `role` / `content` / `timestamp` / `metadata`
   を作る。取り込み側の分岐を増やさない。
3. **時刻はタイムゾーン付き ISO 8601 として読み、UTC の `Z` に正規化する。**
   Muse の `created_at` は `+09:00` のようなオフセットを持つ。保存形式は
   ChatGPT 版と同じ「秒まで・UTC・`Z`」に揃える。
4. **未知の揺れで落とさない。** `resources` はスキーマ未確認なので中身を
   解釈せず、そのまま metadata に保持する。`channels` は将来 `["main"]` 以外が
   増えても捨てずに保持する。ターンの未知キーは `muse_extra` に退避する。
   `[[hatch_widget:widget-...]]` のような本文中のトークン行は加工しない。
5. **workspace_files はメタデータとして保持するが、ファイルは復元しない。**
   今回の主眼は会話（turns）の取り込みで、ワークスペースのスナップショットを
   SAIVerse のどこへ置くかは別の設計判断（§範囲外）。パーサーは
   `source` / `workspace_path` / `bundled_file_path` / `size_bytes` /
   `unresolved_reason` と未知キーを保持し、`list` で件数と内訳を確認できる
   ようにする。
6. **取り込んだ会話の先頭には見出し行を置く。** 既存の ChatGPT 取り込みと
   同じく `system_notice_message()` で role=system のシステム通知として書き、
   系統名は `MUSE_IMPORT_NOTICE_KIND = "muse_import"` とする。見出しの本文は
   `[Imported Muse conversation "<題>" (<id>) created <UTC>]`。

## 入出力と境界

- **入力**: ZIP ファイル、展開済みディレクトリ、または manifest.json ファイル。
  ZIP では最深部でない `manifest.json` を選ぶ（ChatGPT 版の conversations.json と
  同じ探索方針）。ディレクトリでは直下の `manifest.json` を探し、無ければ
  浅い順に探索する。
- **識別子**: manifest の `file_name`（例 `EYI Package_01-15-2027_0000000001.zip`）
  から拡張子を除いたものを会話の `identifier` / `conversation_id` にし、
  スレッドの suffix に使う。無ければ `transcript_file` の stem、それも無ければ
  取り込み元のパスから作る。
- **題**: `transcript_file` の stem（例
  `Conversation with Muse AI_01-15-2027_0000000001`）を使う。無ければ識別子。
- **時刻**: 先頭ターンの `created_at` を会話の作成時刻、末尾ターンのものを
  更新時刻とする。ターンが無い場合は `exported_at` に落とす。
- **書き込み**: `SAIMemoryAdapter.append_persona_message(payload, thread_suffix=...)`。
  エンベディングは取り込みでは作らず（UI は常に `skip_embedding=true` で送る）、
  専用ウィンドウの④で「作成する」を選ぶと `POST /reembed` が呼ばれ、まだ
  エンベディングの無いメッセージの分だけが作られる（2026-11-08 の一本道化から —
  [import_flow_redesign.md](import_flow_redesign.md)）。

## UI（引っ越しタブ / ペルソナ作成ウィザード）

引っ越しタブとペルソナ作成ウィザードが共有する取り込みコンポーネント
(`frontend/src/components/memory/`) に Muse 枠を追加した。どちらの入口からでも
取り込み元の選択 (ImportSourcePicker) で「Muse 公式エクスポート」を選ぶと
取り込みの専用ウィンドウ (ImportFlowModal) が開き、その②が `MuseImportPanel` になる
([import_flow_redesign.md](import_flow_redesign.md))。

エンドポイント:

- `POST /api/people/{persona_id}/import/muse/preview`: アップロードされた ZIP を
  一時ファイルへ保存し、`MuseExport` で manifest.json を解析する。会話一覧、
  `workspace_file_count`、`exported_at` と、後続の import で使う `cache_key` を返す。
- `POST /api/people/{persona_id}/import/muse`: preview の `cache_key` と選択した
  会話 ID（フロントは idx を文字列で送る）を受け取り、背景タスクを開始する。
  identifier で見つからない selector は `.conversations` の数値インデックスとして
  解決する。
- `GET /api/people/{persona_id}/import/muse/status`: 背景取り込みと、後追いの
  埋め込み生成の進捗を返す。

パネルのフェーズは `home | select` の二つ。home では Muse の Settings >
Data Controls > 「Download your agent data」から `EYI_Package_*.zip` を取得する
手順とドロップ枠を出す。select では Claude 版と同じ会話テーブルに加えて
workspace ファイル件数とエクスポート日時を表示する。取り込みの実行・進捗の
定期確認・スレッドの有効化と選択・エンベディングとあらすじの選択は、すべて
ImportFlowModal（③以降）の側で行う。SOUL.md などの個別ファイルの取り込みは、
Muse の流れではスレッド選択の後・エンベディング選択の前に必須二択
（ファイルを取り込む / あとで）として ImportFlowModal の中に出る。あとから
取り込む口として、メモリー画面の独立した節（MuseFileMigrationPanel）も残す
（当初はこの節だけに置いてウィザードから見えず、2026-11-08 のまはーの実機確認で
流れの中へ入れ直した — [import_flow_redesign.md](import_flow_redesign.md)）。

対象範囲は会話のみ:

- SOUL・長期記憶（MEMORY.md）・アイコンは Muse のエクスポート自体に含まれない
  ため、この取り込みでは移らない。UI の home にこの注意を常時表示する。
- workspace ファイルは ZIP 内に実体があっても、一覧のメタデータだけを扱い、
  ファイル自体は復元しない。UI の home と select にこの注意を表示する。

## 後段：ファイル取り込み（SOUL / IDENTITY / MEMORY / AGENTS）

Muse のエクスポート ZIP には SOUL.md・IDENTITY.md・MEMORY.md・AGENTS.md が
含まれない（実エクスポートの構成で確認済み）。そこで会話取り込みとは独立に、
これらのファイルを個別アップロードで受けてペルソナへ取り込む導線を Muse
パネルに追加する。割り付けはまはーの裁定（2026-11-07）:

- **SOUL.md → ペルソナのシステムプロンプト**
- **IDENTITY.md → システムプロンプト（SOUL の後ろに続ける）**
- **MEMORY.md → 項目に分割してコア記憶**
- **AGENTS.md → Memopedia のページ「作業ルール(Muse時)」**
- アイコン・外見画像は対象外（既存の受け口で足りるため、この取り込みでは扱わない）

### 経路とエンドポイント

- `POST /api/people/{persona_id}/import/muse/files/preview`: 複数ファイルを
  受け、ファイル名（ベース名、大小文字は不問）で soul / identity / memory /
  agents を自動判定する。各ファイルの取り込み先と件数（MEMORY.md は分割後の
  項目数）を返し、解析結果は `cache_key` で保持する。判定できないファイルは
  対象外としてプレビューに列挙するだけでエラーにしない。同じ種別が複数
  アップロードされた場合は最後のものを採用する。空のファイルは件数 0 として
  数え、取り込みでは何も書かない。
- `POST /api/people/{persona_id}/import/muse/files`: `cache_key` と選択した
  種別を受け、同期で取り込む（Claude の memories 転記と同じく少件数・同期の
  作法）。会話取り込みの背景タスクや進捗ポーリングとは独立に実行できる。

### 書き込みの規則

- **Muse のファイル形式に付随する説明文は取り込まない。** 各ファイルには
  形式側の文言（「About this file」で始まり "This note is not part of the
  file." で終わる引用ブロックの注記や、ファイルの使い方を説明する HTML
  コメント）が含まれうる。これらはペルソナの本文ではないので、解析時に
  取り除いてから書き込む。本文の引用ブロックは注記の文言を含む場合だけ
  落とし、通常の引用には触らない。あわせて、MEMORY の分割では見出し行
  だけで本文の無い節（ファイル名の H1 など）は項目にしない。
- **システムプロンプトは上書きせず追記する。** Claude 取り込みのプロジェクト
  指示と同じ経路（`import_claude._append_instructions_to_system_prompt`）を使い、
  既存文の末尾に区切り（空行）を挟んで SOUL、IDENTITY の順で足す。片方だけの
  取り込みも可。プロンプト追記が失敗したら記憶側の書き込みは始めない
  （中途半端な適用を避ける。Claude の指示追記と同じ方針）。
- **MEMORY.md の分割は見出し優先。** ATX 見出し（`#`〜`######`）で節に分け、
  節ごとに 1 項目にする。見出し行は項目の本文に含め、項目のタイトルは見出し
  文字列とする。最初の見出しより前の前文も、空でなければ 1 項目にする。
  節が 1 つだけになる場合（見出しが無い、または 1 個だけ）は空行区切りの
  段落単位にフォールバックし、それでも 1 項目しか得られなければ改行単位に
  分ける。空の項目は作らない。コア記憶への追加は Claude の全体記憶と同じ
  作法（`confirmed=1`、生存中のコア記憶に同じ本文があればスキップ、追加した
  事実を知覚バッファ経由でペルソナへ通知する）。
- **AGENTS.md は create-or-update。** Memopedia の幹「Muse引っ越し」
  （`root_terms` の下、無ければ作る。Claude の「Claude引っ越し」幹と同じ置き方）
  の下に「作業ルール(Muse時)」ページを置く。同名ページが既にその幹の下に
  あれば本文と要約を差し替え、無ければ作る。再取り込みで本文が積み上がらない
  ようにするため、`upsert_page_by_title` の追記式は使わない。

### UI

入口は二つ。(1) 取り込みの専用ウィンドウ (ImportFlowModal) の Muse の流れの中の
必須二択ステップ、(2) メモリー画面の「ファイルからの引っ越し」の節
(MuseFileMigrationPanel)。どちらも同じ preview / import の API を使う:
複数ファイルを選び（`.md`）、プレビューでファイルごとに「どこへ・何件入るか」を
確認してから実行する。実行は同期で、結果は件数の内訳として出す。

### 検証（後段）

- 架空の SOUL / IDENTITY / MEMORY / AGENTS を使う。実在のペルソナファイルは
  テストに使わない。
- preview がファイル名から種別を判定し、MEMORY の分割件数（見出しあり・
  見出しなし・前文あり）が設計どおりになること。
- 取り込みで、システムプロンプトに既存文を残したまま SOUL → IDENTITY の順で
  追記されること、MEMORY の項目が重複なくコア記憶に入ること（再実行で
  スキップされること）、AGENTS ページが作成され、再実行では更新になること。

## workspace_files の扱い（今回の範囲）

ZIP 内の `workspace/...` の実体は取り込まない。理由は、SAIVerse 側に
「Muse のワークスペース」に対応する保存先が無く、会話が参照するファイルの
パスを移す規則も未確定だから。中途半端にコピーすると、ペルソナが元の環境の
パスを記憶として持つだけで、次の取り込みやエクスポートで壊れる。

代わりに、manifest の `workspace_files` を `MuseWorkspaceFile` の一覧として
保持し、`list --output json` と表の列で件数・`source`・`workspace_path`・
未解決理由を確認できるようにする。復元を実装するときは、このメタデータを
入力に、コピー先とパスの書き換え規則をこの intent に追記する。

## resources / channels の扱い

- `resources` は中身のスキーマが未確認なので、要素を解釈せず `metadata["resources"]`
  にそのまま保持する。未知の dict / list / 文字列が入っていても、JSON として
  読めたものをそのまま写すだけで、例外にしない。
- `channels` は `metadata["channels"]` に保持する。`["main"]` 以外が来ても
  価値を捨てない。会話のタグ（`["conversation"]`）とは別の欄に置く。
- ターンの未知キーは `metadata["muse_extra"]` にまとめて退避する。既知の
  キーと同じ名前の衝突を避け、取り込み元の情報を失わない。

## 変更の置き場所

- パーサーは `tools/utilities/muse_importer.py`。manifest の解釈・レコード化・
  ペイロード化を一か所に閉じ、CLI や将来の API から同じ変換を使えるようにする。
- CLI は `scripts/import_muse_conversations.py`。ChatGPT 版と同じ
  `list` / `import` の構成と persona の指定方法に揃える。
- 見出し行の系統名は CLI が持つ。SAIMemory の書き込み形式（role=system +
  `__system_notice__`）は `sai_memory.memory.storage.system_notice_message()` が
  所有し、CLI はその契約に従う。

## 帰結（この変更がうまくいったとき、残るもの）

- 会話は取り込めるが、Muse のワークスペースファイルは SAIVerse に現れない。
  利用者には `list` で件数を見せ、`import` の結果にも「復元していない」ことを
  明示する。この制限は workspace 復元の設計ができるまで残る。
- ターンの `source` や `transcript_file` は metadata に残るが、SAIMemory の
  スレッド概念とは対応しない。会話の時系列は保たれるが、Muse 側のスレッド
  分割（もし将来複数になる場合）は manifest の単位でしか表現できない。
- 埋め込みモデルが無い環境では、取り込みは成功しても意味検索の索引が遅れる。
  これは ChatGPT 版と同じ既知の挙動で、後から再埋め込みで回復する。

## 検証の旅

- テスト内で生成する架空フィクスチャ ZIP（`EYI_Package_01-15-2027_0000000001.zip`
  相当。中身は全て作り話で実データを含まない）をパーサーに通し、14 ターン
  （user 7 / assistant 7）が role・timestamp・content を保ってレコード化される
  ことを確認する。
- resources 非空・未知キー・`[[hatch_widget:...]]` を含む合成 ZIP を通し、
  例外なくペイロードへ運ばれることを確認する。
- 一時ディレクトリの `SAIVERSE_HOME` に CLI で import し、memory.db の行数が
  「14 ターン + 見出し 1 行 = 15」になり、role と timestamp が元データの
  秒に一致することを確認する。
- ZIP と展開済みディレクトリの両方から同じレコードが得られることを確認する。
- API テスト (`tests/test_muse_import_api.py`) で、架空フィクスチャ ZIP の preview が
  200 を返して会話 1 件・workspace ファイル 3 件を返すこと、Muse 形式でない
  テキスト／壊れた ZIP が 400 になることを確認する。背景取り込みは偽アダプタで
  直接 `_run_muse_import_task` を呼び、14 ペイロードの保存、埋め込みの deferred
  指定、タイトル設定、adapter の引き渡しを確認する。
- 未検証の境界: フロントエンドの実ビルド（この環境には node_modules が無い）と、
  実際の Muse エクスポート ZIP を使った実機確認。パーサー側の揺れ（resources の
  実スキーマ、複数トランスクリプト、workspace の実パス）も実データで確認する。

## 経緯

- 前段（会話取り込み）: パーサー・CLI を先に作り、続いて API と引っ越しタブ /
  ペルソナ作成ウィザードの Muse 枠を追加した。Codex レビューの P2 指摘
  （取り込み開始の競合、API 経由で出典見出しが落ちる）を直し、フロントの
  型エラーも追随 PR で直したあと、まはーが実機で本物のエクスポート ZIP を
  UI から取り込んで会話が入ることを確認した。手動で SOUL / IDENTITY /
  MEMORY を移植したペルソナでも会話が成立している。
- 後段（ファイル取り込み）: 会話取り込みの実機確認が取れたので、割り付けの
  裁定（SOUL / IDENTITY → システムプロンプト、MEMORY → コア記憶、
  AGENTS → Memopedia）に従って着手した。API（preview / 同期 import）、
  フロントの「ファイルからの引っ越し」節、架空データのテストまで実装し、
  自己検証を通した。現在はまはーによる実機確認待ち。
