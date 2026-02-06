# Playbook手動選択機能 実装設計書

作成日: 2026-01-29

## 概要

metaプレイブックに汎用的な引数システムを導入し、ユーザーがUIから動的にパラメータ（実行するサブPlaybook、移動先Building、操作対象Item等）を指定できるようにする。

## 設計

### 1. InputParam の拡張

`sea/playbook_models.py` の `InputParam` クラスを拡張:

```python
class InputParam(BaseModel):
    name: str
    description: str
    source: Optional[str] = None  # 既存（親stateからの取得先）

    # === 新規追加 ===
    param_type: str = Field(
        default="string",
        description="Parameter type: string, number, boolean, enum"
    )
    required: bool = Field(default=True)
    default: Optional[Any] = Field(default=None)

    # enum用選択肢
    enum_values: Optional[List[str]] = Field(
        default=None,
        description="Static list of allowed values for enum type"
    )
    enum_source: Optional[str] = Field(
        default=None,
        description="Dynamic enum source in format 'collection:scope'. "
                    "Examples: 'playbooks:router_callable', 'buildings:current_city', "
                    "'items:current_building', 'personas:current_city'"
    )

    # UI表示制御
    user_configurable: bool = Field(
        default=False,
        description="If true, this parameter is shown in UI for user input"
    )
    ui_widget: Optional[str] = Field(
        default=None,
        description="UI widget type: text, textarea, dropdown, radio"
    )
```

### 2. enum_source 仕様

形式: `<collection>:<scope_or_filter>`

| collection | scope | 説明 | 返却値 |
|------------|-------|------|--------|
| `playbooks` | `router_callable` | router_callable=Trueのplaybook | name |
| `playbooks` | `user_selectable` | user_selectable=Trueのplaybook | name |
| `buildings` | `current_city` | 現在のCity内のBuilding | ID |
| `personas` | `current_city` | 現在のCity内のPersona | ID |
| `personas` | `current_building` | 現在のBuilding内のPersona | ID |
| `items` | `current_building` | 現在のBuilding内のItem | ID |
| `items` | `persona_inventory` | ペルソナの所持品 | ID |
| `tools` | `available` | 利用可能なTool | name |

### 3. API変更

#### 3.1 GET /api/config/playbooks

レスポンスに `input_schema` を追加:

```json
[
  {
    "id": "meta_user_manual",
    "name": "meta_user_manual",
    "description": "ユーザーが実行するPlaybookを手動選択",
    "input_schema": [
      {
        "name": "selected_playbook",
        "description": "実行するPlaybook",
        "param_type": "enum",
        "enum_source": "playbooks:router_callable",
        "user_configurable": true,
        "required": false
      }
    ]
  }
]
```

#### 3.2 GET /api/config/playbooks/{name}/params

新規エンドポイント。enum_source を解決した選択肢を返す:

```json
{
  "name": "meta_user_manual",
  "params": [
    {
      "name": "selected_playbook",
      "description": "実行するPlaybook",
      "param_type": "enum",
      "enum_source": "playbooks:router_callable",
      "user_configurable": true,
      "required": false,
      "default": null,
      "resolved_options": [
        {"value": "basic_chat", "label": "基本会話"},
        {"value": "searxng_search_playbook", "label": "Web検索"},
        {"value": "memory_recall_playbook", "label": "記憶想起"}
      ]
    }
  ]
}
```

#### 3.3 POST /api/chat

リクエストに `playbook_params` を追加:

```json
{
  "message": "こんにちは",
  "meta_playbook": "meta_user_manual",
  "playbook_params": {
    "selected_playbook": "basic_chat"
  }
}
```

### 4. バックエンド実装

#### 4.1 enum_source resolver

`api/utils/enum_resolver.py` (新規):

```python
def resolve_enum_source(
    enum_source: str,
    context: dict  # city_id, building_id, persona_id等
) -> list[dict]:
    """enum_sourceを解決して {value, label} のリストを返す"""
    collection, scope = enum_source.split(":", 1)

    resolvers = {
        "playbooks": _resolve_playbooks,
        "buildings": _resolve_buildings,
        "personas": _resolve_personas,
        "items": _resolve_items,
        "tools": _resolve_tools,
    }

    resolver = resolvers.get(collection)
    if not resolver:
        raise ValueError(f"Unknown collection: {collection}")

    return resolver(scope, context)
```

#### 4.2 SEARuntime の変更

`sea/runtime.py`:

```python
async def run(
    self,
    input_text: str,
    initial_params: Optional[Dict[str, Any]] = None,  # 新規追加
    ...
) -> ...:
    state = {"input": input_text}
    if initial_params:
        state.update(initial_params)
    # ... 以降の処理
```

### 5. 新規 meta playbook

`builtin_data/playbooks/public/meta_user_manual.json`:

```json
{
  "name": "meta_user_manual",
  "description": "ユーザーがPlaybookを手動選択して実行",
  "input_schema": [
    {
      "name": "input",
      "description": "User input"
    },
    {
      "name": "selected_playbook",
      "description": "実行するPlaybook（未選択時は自動判定）",
      "param_type": "enum",
      "enum_source": "playbooks:router_callable",
      "user_configurable": true,
      "required": false,
      "default": null,
      "ui_widget": "dropdown"
    }
  ],
  "user_selectable": true,
  "router_callable": false,
  "nodes": [
    {
      "id": "check_selection",
      "type": "pass",
      "conditional_next": {
        "field": "selected_playbook",
        "operator": "eq",
        "cases": {
          "null": "auto_route",
          "": "auto_route",
          "default": "exec"
        }
      }
    },
    {
      "id": "auto_route",
      "type": "subplay",
      "playbook": "sub_router_user",
      "input_template": "{input}",
      "propagate_output": false,
      "next": "exec"
    },
    {
      "id": "exec",
      "type": "exec",
      "playbook_source": "selected_playbook",
      "args_source": "selected_args",
      "next": "finalize"
    },
    {
      "id": "finalize",
      "type": "subplay",
      "playbook": "sub_speak_meta",
      "input_template": "{input}",
      "propagate_output": true,
      "next": null
    }
  ],
  "start_node": "check_selection"
}
```

### 6. フロントエンド変更

#### 6.1 ChatOptions.tsx

1. playbook選択時に `input_schema` をチェック
2. `user_configurable=true` のパラメータがあれば追加UI表示
3. `enum_source` があれば `/api/config/playbooks/{name}/params` から選択肢取得
4. パラメータ値を親コンポーネントに伝播

#### 6.2 page.tsx

1. `playbookParams` state追加
2. chat送信時に `playbook_params` をリクエストに含める

## 実装順序

1. **Phase 1: バックエンド基盤**
   - [ ] `InputParam` モデル拡張
   - [ ] `enum_resolver.py` 作成
   - [ ] API エンドポイント追加・変更

2. **Phase 2: Runtime統合**
   - [ ] `SEARuntime.run()` に `initial_params` 追加
   - [ ] chat API で `playbook_params` を runtime に渡す

3. **Phase 3: Playbook作成**
   - [ ] `meta_user_manual.json` 作成
   - [ ] DBにインポート・テスト

4. **Phase 4: フロントエンド**
   - [ ] ChatOptions でパラメータUI動的生成
   - [ ] page.tsx でパラメータ送信対応

5. **Phase 5: テスト・調整**
   - [ ] 手動選択の動作確認
   - [ ] 未選択時の自動ルーティング確認
   - [ ] 各enum_source（buildings, items等）の動作確認

## 備考

- 既存の `meta_user` は変更しない（後方互換）
- `meta_user_manual` を新規デフォルトとして推奨するかは運用で判断
- Schedule機能への統合は本機能完成後に別途対応
