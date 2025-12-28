# ツールシステム

ペルソナが使用できるツールについて説明します。

## 概要

SAIVerseのペルソナは、Function Calling を通じて様々なツールを使用できます。ツールはBuilding単位で有効/無効を設定可能です。

## 仕組み

### ツール呼び出しフロー

1. ペルソナがLLMに応答を依頼
2. LLMがツール呼び出しを決定（JSON形式）
3. ツールレジストリから該当ツールを取得
4. ツールを実行し結果を取得
5. 結果をペルソナにフィードバック

### ツールルーター

`llm_router.py` がGemini 2.0 Flashでツール呼び出しの是非を判定：

```json
{
  "call": "yes",
  "tool": "generate_image",
  "args": {"prompt": "青い空と海"}
}
```

## 組み込みツール

### 汎用

| ツール | 説明 |
|--------|------|
| `calculate_expression` | 数式を計算 |
| `generate_image` | 画像を生成 |
| `web_search` | Web検索 |

### アイテム操作

| ツール | 説明 |
|--------|------|
| `item_pickup` | アイテムを拾う |
| `item_place` | アイテムを置く |
| `item_use` | アイテムを使用 |

### タスク管理

| ツール | 説明 |
|--------|------|
| `task_request_creation` | タスク作成リクエスト |
| `task_change_active` | アクティブタスクを変更 |
| `task_update_step` | タスクステップを更新 |
| `task_close` | タスクを完了 |

### メモリ操作

| ツール | 説明 |
|--------|------|
| `switch_active_thread` | スレッドを切り替え |
| `memopedia_get_tree` | Memopediaツリー取得 |
| `memopedia_open_page` | ページを開く |
| `memopedia_close_page` | ページを閉じる |

## Buildingへの紐付け

ワールドエディタのToolsタブで設定：

1. 対象Buildingを選択
2. 使用可能にしたいツールをチェック
3. 保存

データベースでは `building_tool_link` テーブルで管理されます。

## ツールの追加

新しいツールを追加するには、[ツールの追加](../developer-guide/adding-tools.md) を参照してください。

## 次のステップ

- [ツールカタログ](../reference/tool-catalog.md) - 全ツールの詳細
- [ツールの追加](../developer-guide/adding-tools.md) - 開発者向け
