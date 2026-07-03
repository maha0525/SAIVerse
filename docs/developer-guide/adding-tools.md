# ツールの追加

SAIVerse に新しいツールを追加する方法を説明する。概念的な位置づけは [concepts/tool.md](../concepts/tool.md)、平文から呼ぶ Spell 化は [concepts/spell.md](../concepts/spell.md) を参照。

## 概要

ツールは Python ファイルとして以下のいずれかに置く（**3層優先順位**で解決される）:

```
~/.saiverse/user_data/tools/   # カスタム（最優先）
expansion_data/<addon>/tools/  # アドオン（中間）
builtin_data/tools/            # 組み込み（最低）
```

> ⚠️ 古いドキュメントにあった `tools/defs/` や `@register_tool` デコレータ・`Tool` クラスは**現在の実装には存在しない**。実際は下記の `schema()` 関数方式。

`tools/` パッケージ（リポジトリ直下）はツールの**ロード機構**であって、ツール定義の置き場ではない。

## 基本構造：`schema()` + 同名の実装関数

1つのモジュールが `schema()` 関数で [`ToolSchema`](../../tools/core.py) を返し、**`schema.name` と同じ名前の callable** を実装する。ローダーは `getattr(module, schema.name)` で実装を解決して `TOOL_REGISTRY` に登録する。

```python
# builtin_data/tools/my_tool.py
from tools.core import ToolSchema


def my_tool(expression: str) -> str:
    """実装本体。関数名は schema().name と一致させる。"""
    result = f"処理結果: {expression}"
    return result


def schema() -> ToolSchema:
    return ToolSchema(
        name="my_tool",                 # ← 実装関数名と一致させる
        description="ツールの説明（LLM に提示される）",
        parameters={                    # JSON Schema
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "入力の説明"},
            },
            "required": ["expression"],
        },
        result_type="string",           # "string" / "number" / ...
    )
```

実例は [`builtin_data/tools/calculator.py`](../../builtin_data/tools/calculator.py)（AST ベースの安全な式評価）を参照。

### 1モジュールで複数ツール（サブディレクトリ）

git clone したツールパックなど、1つのサブディレクトリに複数ツールをまとめる場合は `schema.py` に **`schemas()`**（複数形、`List[ToolSchema]` を返す）を置く。各 `ToolSchema.name` と同名の実装関数をモジュールに定義する。

```python
# expansion_data/my_pack/tools/complex_tool/schema.py
from tools.core import ToolSchema

def tool_a(...): ...
def tool_b(...): ...

def schemas() -> list[ToolSchema]:
    return [
        ToolSchema(name="tool_a", ...),
        ToolSchema(name="tool_b", ...),
    ]
```

## ToolSchema のフィールド

| フィールド | 用途 |
|---|---|
| `name` | ツール ID（実装関数名と一致・必須） |
| `description` | LLM に提示される説明 |
| `parameters` | JSON Schema（引数定義） |
| `result_type` | `"string"` / `"number"` など |
| `spell` | `True` で [Spell](../concepts/spell.md) 化（平文応答から `/spell` で呼べる） |
| `spell_display_name` | Spell の UI 表示名（例: 「アイテム閲覧」） |
| `spell_visible` | `False` で実行可・システムプロンプト非表示（help spell で開示） |
| `availability_check` | `Callable[[persona_id], bool]`。ペルソナ単位で出し分け（OAuth 接続状態など） |
| `building_ids` | 特定 Building でのみ visible にする Building ID 群 |
| `addon_name` | アドオン所属識別子（`expansion_data/` 配下はローダーが自動設定） |

## 戻り値の形式

`parse_tool_result`（[`tools/core.py`](../../tools/core.py)）が以下を正規化する:

- `str` — テキストのみ
- `(str, dict)` — テキスト + メタデータ（`{"media": [...]}` 等、マルチモーダル添付に載る）
- `(str, snippet)` — テキスト + SAIMemory 追記用 snippet
- `dict` — `{"content", "history_snippet", "file", "metadata"}`

> **規約**: 戻り値テキストはキャラ付けせず、客観 + 丁寧語で書く（例: 「温度: 32.3°C」）。4-tuple は避ける（→ [issue](../issues/native_tool_return_4tuple_bug.md)）。

## コンテキストの利用

実行時のペルソナ・マネージャ参照は `tools/context.py` の**関数**で取得する（contextvars 経由）:

```python
from tools.context import (
    get_active_persona_id,
    get_active_persona_path,
    get_active_manager,
    get_active_playbook_name,
)

def context_aware_tool() -> str:
    persona_id = get_active_persona_id()
    manager = get_active_manager()
    # DB アクセスは manager 経由（try/finally で必ず close）
    db = manager.SessionLocal()
    try:
        ...
    finally:
        db.close()
    return f"{persona_id} の処理を実行しました"
```

## ペルソナが実際に使えるようにする

`TOOL_REGISTRY` に登録しただけでは、ペルソナはそのツールを呼べない。実際に使わせるには次のどちらかの経路に載せる:

- **Spell 化する（最も手軽）**: schema に `spell=True` + `spell_display_name` を設定すると、ペルソナが平文応答から `/spell <ツール名> ...` で直接呼べるようになる。可視性・出し分けは `spell_visible` / `availability_check` / `building_ids` で制御する。→ [concepts/spell.md](../concepts/spell.md)
- **Playbook の TOOL ノードで実行する**: Playbook に `action: "<ツール名>"` の TOOL ノードを置き、その Playbook を `run_playbook` Spell やメタ判断・`exec` から起動する。引数は Playbook 内の LLM ノードが決める。→ [Playbook 作成](./creating-playbooks.md)

> ⚠️ 旧ドキュメントにあった「`BuildingToolLink` テーブルに紐付ける」「ワールドエディタで Building に紐付ける」方式は**現在使われていない**。ツールをペルソナに届ける経路は上記の Spell / Playbook の2つ。

## 反映

ファイル編集後は `POST /api/config/reload-models` を叩くか、アプリを再起動するとロードされる。

## テスト

ツールは動的ロードされるため、テストでは `patch.object` で対象を差し替える（[テスト基盤の注意](./testing.md)）。

## 次のステップ

- [ツールカタログ](../reference/tool-catalog.md) - 既存ツールの参照
- [Playbook 作成](./creating-playbooks.md) - Playbook でツールを使う
- [concepts/spell.md](../concepts/spell.md) - 平文から呼ぶ Spell 化
