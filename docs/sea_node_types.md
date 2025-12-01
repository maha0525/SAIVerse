# SEA Node Types Reference

SEA (Self-Evolving Agent) フレームワークで使用可能なノードタイプの完全なリファレンス。

## 定義場所

- **ノードタイプ定義**: `sea/playbook_models.py:10-18`
- **実装**: `sea/runtime.py`

---

## ノードタイプ一覧

### 1. LLM (`NodeType.LLM`)

**定義**: `sea/playbook_models.py:21-32` (LLMNodeDef)

**実装**:
- Lightweight executor: `sea/runtime.py:149-177`
- LangGraph: `sea/runtime.py:318-356` (_lg_llm_node)

**挙動**:
- LLM APIを呼び出してテキスト生成
- `action`フィールドがある場合、変数展開してuserメッセージとして追加
- `action`がnullの場合、現在の会話履歴のみでLLM呼び出し
- `response_schema`がある場合、構造化出力を要求
- 出力テキストを`variables["last"]`に格納
- 会話履歴バッファを更新（`variables["messages"]`に追加）

**SAIMemory記録**: なし（自動記録しない）

**Building履歴**: なし（自動記録しない）

**UI表示**: なし

**outputs配列**: 追加しない

**メタプレイブックでの特殊処理**:
- `meta_*`プレイブックかつノードIDが"router"の場合、選択されたプレイブック情報を抽出して`variables["selected_playbook"]`と`variables["selected_args"]`に格納

**パラメータ**:
- `action` (Optional[str]): プロンプトテンプレート。`{変数名}`でプレースホルダー使用可能
- `response_schema` (Optional[Dict]): 構造化出力用JSONスキーマ
- `output_key` (Optional[str]): 構造化出力を格納するstate keyの名前
- `next` (Optional[str]): 次のノードID

---

### 2. TOOL (`NodeType.TOOL`)

**定義**: `sea/playbook_models.py:34-39` (ToolNodeDef)

**実装**:
- Lightweight executor: `sea/runtime.py:179-211`
- LangGraph: `sea/runtime.py:492-538` (_lg_tool_node)

**挙動**:
- `tools/__init__.py`の`TOOL_REGISTRY`から指定されたツールを取得して実行
- `args_input`がある場合、state内の値をkwargsとしてツール関数に渡す
- `args_input`がない場合（レガシー）、`variables["last"]`を単一引数として渡す
- ツール実行結果を`variables["last"]`に格納
- `output_key`が指定されている場合、結果を`variables[output_key]`にも格納

**SAIMemory記録**: なし

**Building履歴**: なし

**UI表示**: なし

**outputs配列**: 追加しない

**パラメータ**:
- `action` (str): ツール名（TOOL_REGISTRYのキー）
- `args_input` (Optional[Dict[str, str]]): 引数名→state keyのマッピング
- `output_key` (Optional[str]): 結果を格納するstate keyの名前
- `next` (Optional[str]): 次のノードID

---

### 3. SPEAK (`NodeType.SPEAK`)

**定義**: `sea/playbook_models.py:49-52` (SpeakNodeDef)

**実装**:
- Lightweight executor: `sea/runtime.py:237-242`
- LangGraph: `sea/runtime.py:630-636` (_lg_speak_node)
- emit関数: `sea/runtime.py:782-792` (_emit_speak)

**挙動**:
- ペルソナの発話を表現（ユーザーとの会話）
- `action`フィールドがある場合、変数展開してテキスト生成
- `action`がnullの場合、`variables["last"]`をそのまま使用
- **SAIMemoryに記録**（conversationタグ + pulse:uuidタグ）
- **Building履歴に追加**（`persona.history_manager.add_message`）
- **gateway_handle_ai_repliesを呼び出し**（UIに表示、Discord連携）
- テキストを`outputs`配列に追加（親プレイブックに返す）

**SAIMemory記録**: あり（conversationタグ）

**Building履歴**: あり

**UI表示**: あり

**outputs配列**: 追加する

**パラメータ**:
- `action` (Optional[str]): テキストテンプレート。nullの場合は`{last}`を使用
- `next` (Optional[str]): 次のノードID

---

### 4. THINK (`NodeType.THINK`)

**定義**: `sea/playbook_models.py:54-57` (ThinkNodeDef)

**実装**:
- Lightweight executor: `sea/runtime.py:243-250`
- LangGraph: `sea/runtime.py:646-652` (_lg_think_node)
- emit関数: `sea/runtime.py:806-821` (_emit_think)

**挙動**:
- ペルソナの内部思考を記録（ユーザーには見えない）
- `action`フィールドがある場合、変数展開してテキスト生成
- `action`がnullの場合、`variables["last"]`をそのまま使用
- **SAIMemoryに記録のみ**（internalタグ + pulse:uuidタグ）
- Building履歴には追加しない
- gateway_handle_ai_repliesを呼び出さない（UIに表示されない）
- **⚠️ 問題: テキストを`outputs`配列に追加している**（親プレイブックに返してしまう）

**SAIMemory記録**: あり（internalタグ）

**Building履歴**: なし

**UI表示**: なし

**outputs配列**: ⚠️ 追加している（意図しない挙動の可能性）

**パラメータ**:
- `action` (Optional[str]): テキストテンプレート。nullの場合は`{last}`を使用
- `next` (Optional[str]): 次のノードID

**⚠️ 既知の問題**:
- `outputs.append(note)`しているため、THINKノードの出力が親プレイブックに返される
- UIに表示されないため、ユーザーから見ると「何も応答がない」ように見える
- 現在は`sub_think_meta.json`に移行しており、直接使用は推奨されない

---

### 5. MEMORY / MEMORIZE (`NodeType.MEMORY`)

**定義**: `sea/playbook_models.py:59-64` (MemorizeNodeDef)

**実装**:
- Lightweight executor: `sea/runtime.py:213-221`
- LangGraph: `sea/runtime.py:612-629` (_lg_memorize_node)
- 記録関数: `sea/runtime.py:736-759` (_store_memory)

**挙動**:
- SAIMemoryにメッセージを記録（任意のタグ付け可能）
- `action`フィールドがある場合、変数展開してテキスト生成
- `action`がnullの場合、`variables["last"]`をそのまま使用
- `role`で記録するメッセージのroleを指定（デフォルト: "assistant"）
- `tags`で任意のタグを付与
- **metaプレイブック以外の場合、`outputs`配列に追加**

**SAIMemory記録**: あり（指定されたタグ）

**Building履歴**: なし

**UI表示**: なし（記録のみ）

**outputs配列**: metaプレイブック以外では追加する

**パラメータ**:
- `action` (Optional[str]): テキストテンプレート。nullの場合は`{last}`を使用
- `role` (Optional[str]): メッセージのrole（デフォルト: "assistant"）
- `tags` (Optional[List[str]]): 付与するタグのリスト
- `next` (Optional[str]): 次のノードID

---

### 6. SAY (`NodeType.SAY`)

**定義**: `sea/playbook_models.py:66-69` (SayNodeDef)

**実装**:
- Lightweight executor: `sea/runtime.py:230-236`
- LangGraph: `sea/runtime.py:638-644` (_lg_say_node)
- emit関数: `sea/runtime.py:794-804` (_emit_say)

**挙動**:
- Building内の発言（ペルソナの履歴には残さない）
- `action`フィールドがある場合、変数展開してテキスト生成
- `action`がnullの場合、`variables["last"]`をそのまま使用
- **SAIMemoryには記録しない**
- **Building履歴にのみ追加**（`persona.history_manager.add_to_building_only`）
- **gateway_handle_ai_repliesを呼び出し**（UIに表示、Discord連携）
- テキストを`outputs`配列に追加

**SAIMemory記録**: なし

**Building履歴**: あり

**UI表示**: あり

**outputs配列**: 追加する

**用途**:
- Building内の他のペルソナに聞かせたいが、自分の記憶には残したくない発言
- 一時的なナレーションやシステムメッセージ

**パラメータ**:
- `action` (Optional[str]): テキストテンプレート。nullの場合は`{last}`を使用
- `next` (Optional[str]): 次のノードID

---

### 7. PASS (`NodeType.PASS`)

**定義**: `sea/playbook_models.py:71-73` (PassNodeDef)

**実装**: `sea/runtime.py:221-226`

**挙動**:
- 何もせず次のノードに遷移
- 条件分岐の実装時に使用（将来的な拡張のため）
- `variables["last"]`を変更しない
- `outputs`配列に何も追加しない

**SAIMemory記録**: なし

**Building履歴**: なし

**UI表示**: なし

**outputs配列**: 追加しない

**パラメータ**:
- `next` (Optional[str]): 次のノードID

---

### 8. SUBPLAY (`NodeType.SUBPLAY`)

**定義**: `sea/playbook_models.py:75-80` (SubPlayNodeDef)

**実装**:
- Lightweight executor: `sea/runtime.py:227-228, 580-610` (_run_subplay_node)
- LangGraph: ⚠️ 未対応（`_compile_with_langgraph`で早期リターン）

**挙動**:
- 別のプレイブックをサブプレイブックとして実行
- `playbook`または`action`でサブプレイブック名を指定
- `input_template`で変数展開してサブプレイブックの入力を生成
- サブプレイブックの出力を`variables["last"]`に格納
- `propagate_output=true`の場合、サブプレイブックの`outputs`を親の`outputs`に追加
- ノードIDが"router"の場合、出力からプレイブック選択情報を抽出

**SAIMemory記録**: サブプレイブック次第

**Building履歴**: サブプレイブック次第

**UI表示**: サブプレイブック次第

**outputs配列**: `propagate_output`次第

**パラメータ**:
- `playbook` (str): サブプレイブック名
- `input_template` (Optional[str]): サブプレイブックへの入力テンプレート（デフォルト: `{input}`）
- `propagate_output` (Optional[bool]): サブプレイブックの出力を親に伝播するか（デフォルト: false）
- `next` (Optional[str]): 次のノードID

**⚠️ 制限事項**:
- LangGraph版では未対応（SUBPLAYノードがあるとfallback executorが使用される）

---

## 特殊ノード: EXEC (metaプレイブック専用)

**定義**: NodeType定義にはない（metaプレイブック内で特殊処理）

**実装**:
- Lightweight executor: `sea/runtime.py:128-147`
- LangGraph: `sea/runtime.py:540-576` (_lg_exec_node)

**挙動**:
- `meta_*`プレイブックでのみ有効
- routerノードが選択したプレイブックを動的に実行
- `variables["selected_playbook"]`と`variables["selected_args"]`を使用
- サブプレイブックの出力をcontext_bundleに収集
- `_ingest_context_from_subplaybook`でcontext_bundle_textを更新

**パラメータ**: なし（meta専用の特殊処理）

---

## 挙動の比較表

| ノードタイプ | SAIMemory記録 | Building履歴 | UI表示 | outputs追加 | 用途 |
|------------|--------------|-------------|--------|------------|------|
| LLM | なし | なし | なし | なし | LLM推論 |
| TOOL | なし | なし | なし | なし | ツール実行 |
| SPEAK | あり (conversation) | あり | あり | あり | ユーザーとの会話 |
| THINK | あり (internal) | なし | なし | ⚠️あり | 内部思考 |
| MEMORY | あり (任意タグ) | なし | なし | meta外では追加 | 記憶保存 |
| SAY | なし | あり | あり | あり | Building内発言 |
| PASS | なし | なし | なし | なし | 遷移のみ |
| SUBPLAY | サブ次第 | サブ次第 | サブ次第 | propagate次第 | プレイブック呼び出し |

---

## 既知の問題点

### 1. THINKノードの`outputs`追加
**場所**: `sea/runtime.py:248`, `sea/runtime.py:651`

**問題**:
- THINKノードは内部思考のため、UIに表示されない
- しかし`outputs.append(note)`しているため、親プレイブックに返される
- basic_chatなどでTHINKノードを使うと、「AIが何か返したはずだが表示されない」状態になる

**影響**:
- ユーザーから見ると応答がないように見える
- metaプレイブックのfinalizeノードでsub_speak_metaが期待する入力がない

**推奨対応**:
- THINKノードの`outputs.append(note)`を削除
- または、THINKノードの使用を廃止してsub_think_metaに移行

### 2. LangGraphでSUBPLAYノード未対応
**場所**: `sea/runtime.py:271`

**問題**:
- SUBPLAYノードがあると、LangGraph版がスキップされfallback executorが使用される
- LangGraphの最適化が効かない

**推奨対応**:
- LangGraph版でSUBPLAY対応を実装
- または、SUBPLAYの使用を最小限にする

---

## 推奨される使い方

### ユーザーとの会話
```json
{
  "nodes": [
    {
      "id": "llm",
      "type": "llm",
      "action": null,
      "next": "speak"
    },
    {
      "id": "speak",
      "type": "speak",
      "action": null,
      "next": null
    }
  ]
}
```

### 内部思考 + 会話
```json
{
  "nodes": [
    {
      "id": "llm",
      "type": "llm",
      "action": "内部で考えてから応答してください",
      "next": "memorize"
    },
    {
      "id": "memorize",
      "type": "memorize",
      "action": "{last}",
      "role": "assistant",
      "tags": ["internal"],
      "next": "llm2"
    },
    {
      "id": "llm2",
      "type": "llm",
      "action": "ユーザーに話しかけてください",
      "next": "speak"
    },
    {
      "id": "speak",
      "type": "speak",
      "action": null,
      "next": null
    }
  ]
}
```

### ツール使用 + 応答
```json
{
  "nodes": [
    {
      "id": "tool",
      "type": "tool",
      "action": "memory_recall",
      "args_input": {
        "query": "input"
      },
      "output_key": "recall_result",
      "next": "llm"
    },
    {
      "id": "llm",
      "type": "llm",
      "action": "想起結果: {recall_result}\n\n上記を踏まえて応答してください",
      "next": "speak"
    },
    {
      "id": "speak",
      "type": "speak",
      "action": null,
      "next": null
    }
  ]
}
```

---

## 変更履歴

- 2025-11-27: 初版作成
