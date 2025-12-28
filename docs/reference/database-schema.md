# データベース設計

SAIVerseのデータベーススキーマを説明します。

## 概要

SAIVerseは SQLite を使用し、`database/data/saiverse.db` にデータを保存します。

## 主要テーブル

### user

ユーザー情報。

| カラム | 型 | 説明 |
|--------|-----|------|
| ID | INTEGER | 主キー |
| NAME | TEXT | ユーザー名 |
| CURRENT_BUILDING_ID | TEXT | 現在いるBuilding |

### city

City（SAIVerseインスタンス）情報。

| カラム | 型 | 説明 |
|--------|-----|------|
| ID | TEXT | 主キー |
| NAME | TEXT | 表示名 |
| UI_PORT | INTEGER | フロントエンドポート |
| API_PORT | INTEGER | APIポート |
| API_BASE_URL | TEXT | 外部公開URL |

### building

Building（場所）情報。

| カラム | 型 | 説明 |
|--------|-----|------|
| ID | TEXT | 主キー |
| CITYID | TEXT | 所属City (FK) |
| NAME | TEXT | 表示名 |
| CATEGORY | TEXT | カテゴリ |
| SYSTEM_PROMPT | TEXT | Building固有プロンプト |
| ENTRY_PROMPT | TEXT | 入室時プロンプト |
| AUTO_PROMPT | TEXT | 自律行動時プロンプト |
| CAPACITY | INTEGER | 定員 (0=無制限) |
| AUTO_PULSE_INTERVAL | INTEGER | パルス間隔（秒） |
| INTERIOR_IMAGE_PATH | TEXT | 内装画像パス |

### ai

ペルソナ情報。

| カラム | 型 | 説明 |
|--------|-----|------|
| ID | TEXT | 主キー |
| BUILDINGID | TEXT | 現在のBuilding (FK) |
| NAME | TEXT | 表示名 |
| MODEL | TEXT | 使用LLMモデル |
| SYSTEM_PROMPT | TEXT | 性格定義プロンプト |
| INTERACTION_MODE | TEXT | auto/user/sleep |
| PRIVATE_ROOM_ID | TEXT | 個室Building ID |
| IS_DISPATCHED | INTEGER | 他City派遣中フラグ |
| APPEARANCE_IMAGE_PATH | TEXT | 外見画像パス |

### tool

ツール定義。

| カラム | 型 | 説明 |
|--------|-----|------|
| ID | TEXT | 主キー |
| NAME | TEXT | ツール名 |
| DESCRIPTION | TEXT | 説明 |

### building_tool_link

BuildingとToolの紐付け。

| カラム | 型 | 説明 |
|--------|-----|------|
| BUILDING_ID | TEXT | Building ID (FK) |
| TOOL_ID | TEXT | Tool ID (FK) |

### item

アイテム情報。

| カラム | 型 | 説明 |
|--------|-----|------|
| ID | TEXT | 主キー |
| NAME | TEXT | アイテム名 |
| TYPE | TEXT | picture/document/object |
| DESCRIPTION | TEXT | 説明 |
| FILE_PATH | TEXT | ファイルパス |

### item_location

アイテムの所在。

| カラム | 型 | 説明 |
|--------|-----|------|
| ITEM_ID | TEXT | Item ID (FK) |
| LOCATION_TYPE | TEXT | building/persona/world |
| LOCATION_ID | TEXT | 所在ID |

### visiting_ai

City間訪問の管理。

| カラム | 型 | 説明 |
|--------|-----|------|
| ID | TEXT | 主キー |
| PERSONA_ID | TEXT | ペルソナID |
| SOURCE_CITY_ID | TEXT | 出発City |
| DESTINATION_CITY_ID | TEXT | 到着City |
| TARGET_BUILDING_ID | TEXT | 目的Building |
| STATUS | TEXT | requested/accepted/rejected |
| PROFILE_JSON | TEXT | ペルソナ情報 |

### thinking_request

リモート思考リクエスト。

| カラム | 型 | 説明 |
|--------|-----|------|
| ID | TEXT | 主キー |
| PERSONA_ID | TEXT | ペルソナID |
| CONTEXT_JSON | TEXT | コンテキスト情報 |
| RESPONSE_JSON | TEXT | 応答結果 |
| STATUS | TEXT | pending/completed |

## ER図

詳細なER図は `docs_legacy/database_design.md` を参照してください。
