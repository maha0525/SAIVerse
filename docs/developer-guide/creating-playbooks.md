# Playbook 作成

独自の Playbook を作成する方法を説明する。概念的な位置づけは [concepts/playbook.md](../concepts/playbook.md)、Spell との接続は [concepts/spell.md](../concepts/spell.md) を参照。スキーマの正は [`sea/playbook_models.py`](../../sea/playbook_models.py)。

## 配置とインポート（重要）

Playbook は JSON ファイルとして以下に置く:

```
~/.saiverse/user_data/playbooks/   # カスタム（優先）
builtin_data/playbooks/public/     # 組み込み
```

> ⚠️ **ファイルを置く/編集するだけでは反映されない。** Playbook は DB の `playbooks` テーブルに取り込まれて実行される。編集後は必ずインポートする:
>
> ```bash
> python scripts/import_playbook.py --file <path>      # 単一
> python scripts/import_all_playbooks.py               # 全 Playbook（安全・personas 等は不変）
> ```
>
> グラフ検証込みで保存する `save_playbook` ツール経由でも可。

## トップレベル構造（PlaybookSchema）

```json
{
  "name": "my_playbook",
  "display_name": "マイPlaybook",
  "description": "カスタム Playbook の説明",
  "input_schema": [
    { "name": "input", "description": "呼び出し時の入力" }
  ],
  "output_schema": ["result"],
  "router_callable": true,
  "can_run_as_child": false,
  "nodes": [ ... ],
  "start_node": "start"
}
```

| フィールド | 説明 |
|---|---|
| `name` | Playbook ID（`^[a-z0-9_]+$`・必須） |
| `display_name` | UI 表示名 |
| `input_schema` | 入力パラメータ定義（`List[InputParam]`）。`{var}` で参照する変数は**ここに宣言が必要** |
| `output_schema` | 親 Playbook に伝播する state キー群（サブライン完了時） |
| `report_template` | サブライン完了時の `report_to_parent` を機械的にレンダリング（LLM 不要な定型報告向け） |
| `context_requirements` | 読み込むコンテキスト。指定できるのは `history_depth` / `history_balanced` / `realtime_context` の 3 つだけ。**未指定が既定**（履歴フル）で、通常は指定しない |
| `router_callable` | メタ判断・`run_playbook` / `exec` から呼び出し可能にするか（フィールド名は名残で「router」だが LLM ルーターは無い） |
| `can_run_as_child` | サブライン（`/run_playbook` / subplay `line="sub"`）として呼べるか。**True なら `report_to_parent` の産出が必須**（`report_template` か、LLM ノードの `response_schema.properties` に `report_to_parent`） |
| `user_selectable` / `dev_only` | UI ユーザー選択可 / 開発者モード限定 |
| `nodes` / `start_node` | ノード配列と開始ノード ID |

> **`input_schema` 宣言漏れの罠**: `action` テンプレートで `{var}` を使うなら `input_schema` に宣言する。宣言漏れだと引数が state に昇格せず `{var}` がリテラル落ちする（`{input}` のみ特別扱い）。

> **head（人格・部屋・呪文一覧などの前置き）の章立ては Playbook から選べない**: head は (persona, model) ごとに一つで固定するのが prefix キャッシュ共有の土台で、用途やラインで出し分けると同一モデルで head が変わってキャッシュが壊れる。章の集合は `sea/runtime_context.py` の `PERSONA_HEAD_SECTIONS` に固定されている（2026-07-23 に `system_prompt` / `available_playbooks` / `memory_weave` / `visual_context` フラグを撤去）。
>
> **`history_depth: 0` はペルソナ名義の稼働では使えない**: 会話履歴なしで走らせた出力を本人の発話・思考として記録するのはペルソナ倫理違反（記憶の連続性が無い存在は別の人格）。メインラインで走る Playbook が履歴ゼロを指定すると `PersonaVoiceWithoutHistoryError` で LLM 到達前に落ちる。履歴を外してよいのは、出力がペルソナ本人の言葉にならない「機構名義」の処理だけ。

## ノードタイプ（実際の type 名とフィールド）

主要なノードは以下（全型は `playbook_models.py` の `NodeType`）。**全ノード共通**で `id` / `next` / `conditional_next` を持つ。

### llm — LLM 呼び出し

| フィールド | 説明 |
|---|---|
| `action` | プロンプトテンプレート（`{var}` 展開） |
| `response_schema` | 構造化出力を強制する JSON Schema |
| `output_key` | 構造化出力を保存する state キー（既定はノード id） |
| `output_mapping` | 構造化出力のフィールドを state 変数へマップ（例: `{"decision.next_action": "chosen_action"}`） |
| `available_tools` | ネイティブ tool calling を許可するツール名リスト（通常は使わない。下記「設計哲学」参照） |
| `output_keys` | 出力タイプ→state キー（`text` / `function_call` / `thought`） |
| `memorize` | `true` か dict で prompt/response を SAIMemory 保存 |
| `speak` | `true` で応答を Building（UI）へ出力（既定でストリーミング） |
| `important` | `true` で pulse_logs と messages に二重書き込み |

### tool — ツールの固定実行

| フィールド | 説明 |
|---|---|
| `action` | 実行するツール名（registry の名前） |
| `args_input` | 引数名→state キー or リテラル。**文字列は state キーとして解決**される。リテラル文字列を渡すには `{"$literal": "値"}` |
| `output_key` / `output_keys` | 結果の保存先。タプル戻り値は `output_keys` で複数 state 変数に展開 |

### memorize — SAIMemory へ保存

`action`（保存テキスト）/ `role`（既定 `assistant`）/ `tags`（意味分類タグのみ。`internal` 等の context 制御タグは書かない）/ `metadata_key`。

### subplay — サブ Playbook を静的に呼ぶ

`playbook`（名前）/ `args`（テンプレート文字列）/ `line`（`"main"` or `"sub"`。`sub` は軽量モデルの子ライン、完了時 `report_to_parent` を親へ）/ `execution`（`inline` or `subagent`）。

### その他

- **pass** — 分岐のみ（`conditional_next`）
- **set** — state 変数の代入（リテラル・テンプレート・算術式 `"{count} + 1"`）
- **exec** — LLM ノードが state 変数（`selected_playbook`）に入れた Playbook 名を動的に実行（選択→実行パターン）
- **say** — UI 出力のみ（SAIMemory 非記録）
- **think** — 内的メモの記録
- **speak** — 最終発話（既定 `important=true`）
- **tool_call** — LLM が選んだツールを動的実行（`available_tools` + `output_keys` と組む agentic loop）
- **stelis_start / stelis_end** — 階層コンテキスト管理（Stelis スレッド）

### conditional_next の形（旧記法と別物）

```json
"conditional_next": {
  "field": "decision.action",
  "operator": "eq",
  "cases": { "recall": "do_recall", "default": "fallback" }
}
```

`operator` は `eq`(既定) / `ne` / `gt` / `gte` / `lt` / `lte`。`cases` の値に `null` を置くと実行終了。指定時は `next` を上書きする。

## 変数の参照

- `action` テンプレート内は `{var}` / `{nested.key}` で state を参照
- `args_input` の文字列値は state キーパス、リテラル文字列は `{"$literal": "..."}`

## canonical 実例（LLM → TOOL → MEMORIZE）

[`builtin_data/playbooks/public/generate_image_playbook.json`](../../builtin_data/playbooks/public/generate_image_playbook.json) が模範パターン:

1. `decide_prompt`（**llm** + `response_schema` で引数を構造化 → `output_key: "gen_params"`）
2. `generate`（**tool** `action: "generate_image"` + `args_input` で `gen_params.*` をツール引数にマップ + `output_keys` で戻り値を展開）
3. `record`（**memorize** で結果を SAIMemory に保存）

`report_template` で機械的な完了報告を返し、`can_run_as_child: true` の契約（`report_to_parent` 産出）を満たしている。

## Spell との接続（run_playbook）

メインライン LLM が発話中に `/spell run_playbook name='my_playbook'` と書くと、`router_callable: true` の Playbook が**サブライン**として動的起動される。引数は呼ばれた Playbook の最初の LLM ノードが決める（呼び出し側は名前のみ）。→ [concepts/spell.md](../concepts/spell.md)

## 設計哲学

- **メタ判断の Playbook 選択は決定論的**: `meta_judgment_*` の選択は MetaLayer が Track/persona 状態から決める（`saiverse/meta_layer.py` の `_SITUATION_PLAYBOOK_MAP`）。軽量 LLM ルーターは廃止された
- **引数は Playbook 内で決める**: 各 Playbook がコンテキストを見て tool 引数を組む LLM ノードを持つ
- **function calling を使わない**: ネイティブ tool call はプロンプトキャッシュを壊す。structured output + tool ノード固定実行が正道
- **新フィールドを足すときは `sea/playbook_models.py` の node 定義も更新**。しないと `import_playbook.py` / `save_playbook` が Pydantic 検証で黙って落とす

## 検証・デバッグ

- グラフ検証: `validate_playbook_graph`（到達不能ノード・欠落 `next` を検出）
- 実行トレース: `SAIVERSE_SEA_TRACE=1` で `sea_trace.log` に詳細出力
- DB 反映確認: `sqlite3 ~/.saiverse/user_data/database/saiverse.db "SELECT nodes_json FROM playbooks WHERE name='<name>'"`

## 次のステップ

- [Playbook/SEA 概要](../features/playbooks.md)
- [concepts/playbook.md](../concepts/playbook.md) - 概念と実装入口
- [テスト](./testing.md)
