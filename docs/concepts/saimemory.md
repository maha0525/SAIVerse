# SAIMemory

ペルソナの記憶システムについて説明します。

## 概要

SAIMemoryは、ペルソナの長期記憶を管理するシステムです。会話履歴の保存、セマンティック検索による関連記憶の想起、構造化された知識管理を提供します。

## データ構造

各ペルソナは `~/.saiverse/personas/<persona_id>/` にデータを保存：

```
~/.saiverse/personas/<persona_id>/
├── memory.db        # SQLiteデータベース
├── log.json         # 会話ログ（履歴用）
├── tasks.db         # タスク管理
└── attachments/     # 添付ファイル
```

## 主要機能

### メッセージ保存

会話の各メッセージを記録：

```python
storage.add_message(
    thread_id="main",
    role="assistant",
    content="こんにちは！",
    timestamp=...,
    metadata={"emotion": "happy"}
)
```

### セマンティック検索

SBERTによる埋め込みを使用して関連記憶を検索：

```python
results = storage.search_messages(
    query="旅行の思い出",
    limit=10,
    thread_id=None  # 全スレッドから検索
)
```

### スレッド管理

会話を話題（スレッド）単位で整理：

- デフォルトスレッド: `main`
- スレッド間のリンク機能
- アクティブスレッドの切り替え

## Memopedia

構造化された知識ベース。詳細は [Memopedia](../user-guide/memopedia.md) を参照。

### 3つのルートカテゴリ

| カテゴリ | 説明 |
|----------|------|
| 人物 (people) | 関わりのある人物の情報 |
| 出来事 (events) | 過去の出来事の記録 |
| 予定 (plans) | 進行中のプロジェクト・計画 |

### ページ操作

```python
from sai_memory.memopedia import Memopedia

memopedia = Memopedia(conn)

# ページ作成
page = memopedia.create_page(
    parent_id="root_people",
    title="まはー",
    summary="SAIVerseの開発者",
    content="## 基本情報\n\n- 名前: まはー\n- 役割: 開発者"
)

# ツリー取得
tree_md = memopedia.get_tree_markdown(thread_id="main")
```

## 埋め込みモデル

デフォルトでは `intfloat/multilingual-e5-base` を使用。

### 設定

```env
SAIMEMORY_EMBED_MODEL=intfloat/multilingual-e5-base
SAIMEMORY_EMBED_MODEL_PATH=/path/to/local/model
```

### オフライン利用

`sbert/` ディレクトリにモデルを配置すると、ネットワーク接続なしで利用可能：

```
sbert/
└── intfloat/
    └── multilingual-e5-base/
        ├── config.json
        ├── model.safetensors
        └── ...
```

## 保守スクリプト

| スクリプト | 説明 |
|-----------|------|
| `scripts/backup_saimemory.py` | rdiff-backupで差分バックアップ |
| `scripts/export_saimemory_to_json.py` | JSON形式でエクスポート |
| `scripts/import_persona_logs_to_saimemory.py` | JSONログをインポート |
| `scripts/build_memopedia.py` | 会話からMemopediaを構築 |

## 次のステップ

- [Memopedia](../user-guide/memopedia.md) - ナレッジベースの使い方
- [スクリプト一覧](../reference/scripts.md) - 保守スクリプトの詳細
