# ツールシステム

ペルソナが「LLM の外で実際に何かをする」ための仕組み。概念は [concepts/tool.md](../concepts/tool.md)、平文から呼ぶ仕組みは [concepts/spell.md](../concepts/spell.md) を参照。

## 概要

ペルソナは [Tool](../concepts/tool.md)（`tools/` registry に登録された処理）を、次の2経路で使う：

- **Spell** — 平文応答の中に `/spell <ツール名> key='value'` と書いて呼ぶ（`spell=True` のツール）
- **Playbook の TOOL ノード** — Playbook が `action: "<ツール名>"` の TOOL ノードで固定実行する

> ⚠️ **ネイティブ Function Calling は使わない**（意図的）。ネイティブツールコールは必ずコンテキスト最上部に置かれ、平文応答と構造化応答でプロンプトキャッシュが必ずミスするため。Spell 化すれば平文・構造化の両応答でキャッシュを共用できる（→ [concepts/spell.md](../concepts/spell.md)）。旧 `llm_router.py`（Gemini で tool call の是非を JSON 判定する経路）はこの Spell 方式に置き換わっている。

## Spell の流れ

1. 発話ノード（LLM）が平文中に `/spell ...` の行を書く
2. `_run_spell_loop`（`sea/runtime_llm.py`）が各ラウンドで `/spell` 行を検出・実行
3. 結果を `<user_only>` ブロックに包んで次の LLM ラウンドに渡す
4. Spell が出なくなるまで繰り返し、結果を [Beat](../concepts/beat.md) に統合

重い処理は `/spell run_playbook name='...'` で [Playbook](../concepts/playbook.md) をサブラインとして起動できる。

## ペルソナが使えるようにするには

> ⚠️ 「ツールを Building に紐付ける」`building_tool_link` テーブルや、ワールドエディタの Tools タブは**現在使われていない**。ツールをペルソナに届ける経路は上記の Spell / Playbook の2つ。

- **Spell 化**: ツールの schema に `spell=True`（+ `spell_display_name`）を設定
- **Playbook**: Playbook の TOOL ノードで実行し、その Playbook を `run_playbook` / メタ判断から起動

## 組み込みツール

全ツールの網羅一覧（自動生成）は [ツールカタログ](../reference/tool-catalog.md) を参照（130 ツール、うち Spell 87）。代表例:

- 汎用: `calculate_expression`（計算）/ `generate_image`（画像生成）
- アイテム: `item_move`（移動）/ `item_view`（閲覧）/ `item_annotate`（名前・概要の編集）
- 記憶: `memory_recall`（想起）/ `switch_active_thread`（スレッド切替）/ Chronicle・Memopedia 系
- Playbook: `run_playbook`（サブライン起動）

MCP サーバー由来のツールも `spell_tools[]` 経由で Spell 化される（→ [MCP連携](./mcp-integration.md)）。

## ツールの追加

新しいツールの作り方は [ツールの追加](../developer-guide/adding-tools.md) を参照。

## 次のステップ

- [ツールカタログ](../reference/tool-catalog.md) - 全ツール（自動生成）
- [concepts/spell.md](../concepts/spell.md) - Spell の仕組みと目的
- [ツールの追加](../developer-guide/adding-tools.md) - 開発者向け
