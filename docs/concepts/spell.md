# Spell

> 開発者向け概念リファレンス。**なぜ**作られたかは [intent](../intent/persona_cognition/nested_subline_spell.md)、**全体の位置づけ**は [landscape §4](../overview/landscape.md) を参照。ここは「何で・どう動き・どこに実装され・どう増やすか」のナビ。

## 一言で

Tool を平文応答の中で `/spell <スペル名> key='value'` 構文で呼べるようにする仕組み。

## 役割

本質的な目的は **ネイティブツールコール（function calling）の撲滅**。ネイティブツールコールは必ずコンテキスト最上部に置かれるが、構造化応答ではそれが使えない。そのため平文応答と構造化応答が混在するとプロンプトキャッシュが必ずミスする。Tool を Spell 化して平文で呼べるようにすれば、**平文・構造化の両応答でキャッシュを共用できる**。

## 仕組み

ペルソナの [Beat](beat.md) 内の発話ノード（LLM）が平文中に **`/spell ` で始まる行**を書く → `_run_spell_loop` が各 round で全 `/spell` 行を検出 → **テキスト順に逐次実行**（以前は並列 gather だったが `_line_stack` 破壊・デバイス spell 等のレースで撤去）→ 結果を `<user_only>` ブロックに包んで次の LLM round に渡す → spell が出なくなるまで繰り返す。結果は Beat に統合されて表示・記憶される。

構文には2形式がある（`_parse_spell_lines`）:
- **正規形**: `/spell name='ツール名' args={...JSONオブジェクト...}`（`_normalize_spell_line` が生成する形。SAIMemory にはこの形で保存し、ペルソナが正しい構文を学習する）
- **略式（fuzzy）**: `/spell ツール名 key='value' key2='value2'`（ペルソナが書きやすい形。パーサが正規形に正規化する）

例: `/spell item_view item_id='it_3'` / `/spell track_complete track_id='t:3'`

軽い処理は1往復の Spell で完結し、重い処理は `run_playbook` Spell（`/spell run_playbook name='memory_research'`）で [Playbook](playbook.md) をサブラインとして起動できる（Spell × Playbook の接続点）。

## ユーザーからの見え方（UI 接合点）

- **発動確認**: 投稿系など確認が要る Spell は `SpellConfirmDialog.tsx` でユーザーに確認を求める（タイトル/本文、`editable` なら送信前にテキスト編集可、120秒タイムアウト）
- **モード選択**: `ToolModeSelector.tsx` で、どの playbook / spell モードで動かすかを選べる
- **発動表示**: チャット上では Spell 結果が `<user_only>` ブロックとして Beat の一部に表示される
- `spell_display_name`（例: 「アイテム閲覧」）が UI 上の表示名になる

> ⚠️ **不足**: ペルソナが**今どの Spell を使えるか**を一覧する専用 UI が無く、コンテキストプレビューでシステムプロンプトを覗くしかない（→ [issue: Spell の管理・可視化 UI](../issues/spell_management_ui.md)）。

## 増やし方（新しい Spell の追加）

1. `builtin_data/tools/`（または `~/.saiverse/user_data/tools/`）に Tool を定義する
2. Tool の登録 schema に **`spell=True`** を設定する（+ `spell_display_name="表示名"`、任意で `spell_visible=True`）:
   ```python
   # 例: builtin_data/tools/item_view.py
   ...
   result_type="string",
   spell=True,
   spell_display_name="アイテム閲覧",
   ```
3. MCP tool の場合はコードでなく `mcp_servers.json` の `spell_tools[]` に entry を追加する（`visible: true` で UI 表示。詳細は MCP リファレンス）
4. `POST /api/config/reload-models` 等で reload、または再起動

> ⚠️ **拡張 UX の課題**: 現状は「Tool を作る → schema で `spell=True` → reload」とコード作業が必要で煩雑。Tool 管理 UI からワンタッチで spell 化を切り替えられるようにしたい（→ [issue: Spell の管理・可視化 UI](../issues/spell_management_ui.md)）。

## 実装

- 主要ファイル: `sea/runtime_llm.py`（`_run_spell_loop` / `_parse_spell_lines` / `_parse_spell_line` / `_normalize_spell_line` / `_run_spell_tool_async`）
- Tool 登録: `tools/` registry。schema の `spell` / `spell_display_name` / `spell_visible` フィールド
- 接続点: `builtin_data/tools/run_playbook.py`（`run_playbook` Spell）
- UI: `frontend/src/components/SpellConfirmDialog.tsx` / `ToolModeSelector.tsx`

## 関連概念

- [Beat](beat.md) — Spell は Beat 内の平文から発動する
- [Tool](tool.md) — Spell が呼ぶ実行単位
- [Playbook](playbook.md) — `run_playbook` Spell でサブラインとして起動（接続点）
- [Addon](addon.md) — MCP tool は `spell_tools[]` 経由で Spell 化される

## 参照

- intent: [`docs/intent/persona_cognition/nested_subline_spell.md`](../intent/persona_cognition/nested_subline_spell.md)
- 地図: [`landscape.md`](../overview/landscape.md) §4
