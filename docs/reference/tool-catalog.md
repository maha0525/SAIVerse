# ツールカタログ

SAIVerseで利用可能なツールの一覧です。

## 汎用ツール

### calculate_expression

数式を計算。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| expression | string | 計算式 |

```
例: calculate_expression("2 + 3 * 4") → 14
```

### generate_image

画像を生成（Gemini 2.5 Flash Image使用）。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| prompt | string | 画像の説明 |

※ 有料APIキーが必要

### web_search

Webを検索。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| query | string | 検索クエリ |
| limit | integer | 最大件数（デフォルト: 5） |

## アイテム操作

### item_pickup

アイテムを拾ってインベントリに追加。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| item_id | string | アイテムID |

### item_place

アイテムをBuildingに置く。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| item_id | string | アイテムID |
| building_id | string | 配置先Building |

### item_use

アイテムを使用。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| item_id | string | アイテムID |

## タスク管理

### task_request_creation

新しいタスクの作成をリクエスト。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| title | string | タスクタイトル |
| description | string | 説明 |
| priority | string | 優先度（low/medium/high） |

### task_change_active

アクティブなタスクを変更。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| task_id | string | タスクID |

### task_update_step

タスクのステップを更新。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| step_id | string | ステップID |
| status | string | ステータス |
| notes | string | メモ（オプション） |

### task_close

タスクを完了。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| task_id | string | タスクID |
| summary | string | 完了サマリー |

## メモリ操作

### switch_active_thread

アクティブなスレッド（話題）を切り替え。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| thread_id | string | スレッドID |
| create_if_not_exists | boolean | 存在しない場合作成 |

### memopedia_get_tree

Memopediaのページツリーを取得。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| (なし) | | |

### memopedia_open_page

指定したページを開いて内容を取得。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| page_id | string | ページID |

### memopedia_close_page

指定したページを閉じる。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| page_id | string | ページID |

## 配置先

ツールをBuildingで使用可能にするには、ワールドエディタのToolsタブで紐付けを設定してください。
