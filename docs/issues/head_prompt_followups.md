# head システムプロンプトの残課題（2026-07-07 全文レビューの積み残し）

2026-07-07 の head 全文レビュー（common.txt v2 化・記憶総論節の新設・autonomy_modes の判断点語彙化・item:N 参照への更新）で対応を見送った項目。

## 1. スペル一覧のダイエット

`SpellListSection`（`sea/head_pipeline/sections/spell_list.py`）が描画するスペル一覧が実測 179 行・約 21,500 文字あり、head 本文（約 10,000 文字）の 2 倍を占める。head はキャッシュ常駐なので恒久課金。

検討方向の候補:
- description の短文化（tool schema 側の記述を要約制約する）
- parameters JSON Schema の描画省略・簡約（引数名と型だけにする等）
- 使用頻度の低いスペルの `visible=false` 化 + `addon_spell_help` 型の遅延開示を builtin にも適用

### 進捗: スペル棚卸し → 統合ダイエット（2026-07-08 統合 / 2026-07-09 実機確認済）

「隠す」前にまず「削る」を実施。builtin スペルを棚卸しし、役割被り・二重実装を統合した。

- **document 編集系 7→4**: `document_edit` を書き直し、旧 `document_replace_content` /
  `document_patch_content` / `document_append_content` を1本に統合（部分置換 / 追記 / 全置換の3操作）。
  同時に、旧 `document_edit` が生ファイル直書き（item ref 未解決・アクセス制御なし）だった系統Aから、
  manager サービス経由の系統B（`resolve_item_ref_for_persona` + `_validate_document_access`）へ寄せ、
  二重実装とアイソレーション漏れを解消。行範囲置換モードは廃止（部分置換が上位互換）。
- **item メタ 2→1**: `item_change_name` + `item_write_description` を `item_annotate(name?, description?)` に統合。

これで builtin Spell は 91→87（当環境の addon 込みカタログ値）、ペルソナ常駐分は実質 -5。

**残タスク（本 issue 本線）**: 使用頻度の低いスペルの `visible=false` 化 + 遅延開示。棚卸しで挙がった
head 常駐だが出番の稀な候補 — `life_purpose_set`（一生に一度）/ `observer_read`（Observer 建物限定）/
`messagelog_get_around`（ニッチ）/ `send_email_to_user`（SMTP 前提）。

**関連の積み残し（別 issue 化候補）**: `document_read` / `document_search` は依然として系統A（アクセス制御なし・
item ref 未解決）。読み取り専用で低リスクだが、`document_edit` と挙動が非対称。統合の完遂には要修正。

## 2. City 全景の把握手段がない

ペルソナは「今いる Building の同居人」（`building_occupants`、order 1100）しか見えず、City 内にどんな Building があり誰が住んでいるかを知る恒常手段がない。`building_move` Playbook 実行時に一覧が出るのみ。

- head 新セクションにするか、resolve_uri で引ける URI（`saiverse://building/...` の一覧版）+ 教示にするかは未決
- 保護対象・キャッシュサイズとのトレードオフ検討が必要

## 3. order 800 以降のセクション文言レビュー

2026-07-07 レビューのスコープは order 720（open_notes）まで。以下は未レビュー:
- visual_context (800) / building_items (1000) / building_occupants (1100) / memopedia_index (1200) / chronicle_index (1300)

コンテキストプレビューでは別枠表示のため、レビュー時は組み上がりテキストを別途取得すること。
