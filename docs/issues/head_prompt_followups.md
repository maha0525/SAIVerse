# head システムプロンプトの残課題（2026-07-07 全文レビューの積み残し）

2026-07-07 の head 全文レビュー（common.txt v2 化・記憶総論節の新設・autonomy_modes の判断点語彙化・item:N 参照への更新）で対応を見送った項目。

## 1. スペル一覧のダイエット

`SpellListSection`（`sea/head_pipeline/sections/spell_list.py`）が描画するスペル一覧が実測 179 行・約 21,500 文字あり、head 本文（約 10,000 文字）の 2 倍を占める。head はキャッシュ常駐なので恒久課金。

検討方向の候補:
- description の短文化（tool schema 側の記述を要約制約する）
- parameters JSON Schema の描画省略・簡約（引数名と型だけにする等）
- 使用頻度の低いスペルの `visible=false` 化 + `addon_spell_help` 型の遅延開示を builtin にも適用

## 2. City 全景の把握手段がない

ペルソナは「今いる Building の同居人」（`building_occupants`、order 1100）しか見えず、City 内にどんな Building があり誰が住んでいるかを知る恒常手段がない。`building_move` Playbook 実行時に一覧が出るのみ。

- head 新セクションにするか、resolve_uri で引ける URI（`saiverse://building/...` の一覧版）+ 教示にするかは未決
- 保護対象・キャッシュサイズとのトレードオフ検討が必要

## 3. order 800 以降のセクション文言レビュー

2026-07-07 レビューのスコープは order 720（open_notes）まで。以下は未レビュー:
- visual_context (800) / building_items (1000) / building_occupants (1100) / memopedia_index (1200) / chronicle_index (1300)

コンテキストプレビューでは別枠表示のため、レビュー時は組み上がりテキストを別途取得すること。
