# APIエンドポイント

SAIVerseのREST APIエンドポイント一覧です。

## 概要

APIサーバーは `main.py` 起動時に自動で起動します（デフォルト: ポート8001）。

## エンドポイント

### チャット

| メソッド | パス | 説明 |
|----------|------|------|
| POST | `/chat/send` | メッセージを送信 |
| GET | `/chat/history/{building_id}` | 履歴を取得 |
| DELETE | `/chat/history/{building_id}` | 履歴をクリア |

### Building

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/buildings` | Building一覧 |
| GET | `/buildings/{id}` | Building詳細 |
| POST | `/buildings` | Building作成 |
| PUT | `/buildings/{id}` | Building更新 |
| DELETE | `/buildings/{id}` | Building削除 |

### ペルソナ

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/personas` | ペルソナ一覧 |
| GET | `/personas/{id}` | ペルソナ詳細 |
| POST | `/personas` | ペルソナ作成 |
| PUT | `/personas/{id}` | ペルソナ更新 |
| DELETE | `/personas/{id}` | ペルソナ削除 |
| POST | `/personas/{id}/summon` | 召喚 |
| POST | `/personas/{id}/return` | 帰還 |

### メモリ

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/memory/{persona_id}/threads` | スレッド一覧 |
| GET | `/memory/{persona_id}/messages` | メッセージ一覧 |
| POST | `/memory/{persona_id}/search` | セマンティック検索 |

### Memopedia

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/memopedia/{persona_id}/tree` | ツリー取得 |
| GET | `/memopedia/{persona_id}/page/{page_id}` | ページ取得 |
| POST | `/memopedia/{persona_id}/page` | ページ作成 |
| PUT | `/memopedia/{persona_id}/page/{page_id}` | ページ更新 |
| DELETE | `/memopedia/{persona_id}/page/{page_id}` | ページ削除 |

### City間通信

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/inter-city/buildings` | Building情報を公開 |
| POST | `/persona-proxy/{id}/think` | リモート思考リクエスト |

### 自律モード

| メソッド | パス | 説明 |
|----------|------|------|
| POST | `/autonomous/start` | 自律モード開始 |
| POST | `/autonomous/stop` | 自律モード停止 |
| GET | `/autonomous/status` | ステータス取得 |

### ユーザー

| メソッド | パス | 説明 |
|----------|------|------|
| POST | `/user/move` | Buildingへ移動 |
| GET | `/user/location` | 現在地取得 |

## リクエスト例

### メッセージ送信

```bash
curl -X POST http://localhost:8001/chat/send \
  -H "Content-Type: application/json" \
  -d '{
    "building_id": "user_room",
    "message": "こんにちは",
    "user_id": 1
  }'
```

### ペルソナ召喚

```bash
curl -X POST http://localhost:8001/personas/air/summon \
  -H "Content-Type: application/json" \
  -d '{
    "target_building_id": "user_room"
  }'
```
