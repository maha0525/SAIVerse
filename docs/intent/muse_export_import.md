# Muse エクスポート取り込み — Meta のパーソナル AI の会話をペルソナ記憶へ

**ステータス**: 検証待ち（パーサー・CLI・テストを追加済み。workspace ファイルの復元は今回の範囲外）

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
  ChatGPT 版と同じく、取り込み後に埋め込みのバックフィルを試みる（失敗しても
  取り込み自体は完了している）。

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
- 未検証の境界: 実際の Muse エクスポートの揺れ（resources の実スキーマ、
  複数トランスクリプト、workspace の実パス）。将来の実データで確認する。
