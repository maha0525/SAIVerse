# アイテムタイプ拡張仕様書

## 概要

SAIVerseのアイテムシステムを拡張し、`picture`（画像）と`document`（文書）タイプを追加する。これにより、ペルソナが画像ファイルや文書ファイルをアイテムとして扱えるようになり、美術館や図書館のようなBuildingの構築が可能になる。

## 目的

- 生成画像が飾られている美術館のようなBuildingの実現
- 詩や小説が所蔵される図書館のようなBuildingの実現
- ペルソナが日記を自室に保管し、適宜書き足す運用
- SAIVerseの説明書をdocumentとして配置し、ペルソナが初心者ユーザーを案内できるようにする

## データベース変更

### 1. `item`テーブルへのカラム追加

```sql
ALTER TABLE item ADD COLUMN FILE_PATH TEXT;
```

- **FILE_PATH**: 実ファイルのパス（picture/documentの場合に使用）
- **nullable**: TRUE（objectタイプは使用しない）

### 2. `ai`テーブルへのカラム追加

```sql
ALTER TABLE ai ADD COLUMN LIGHTWEIGHT_VISION_MODEL TEXT;
ALTER TABLE ai ADD COLUMN VISION_MODEL TEXT;
```

- **LIGHTWEIGHT_VISION_MODEL**: 軽量なvision対応モデル（summary生成用）
- **VISION_MODEL**: 通常のvision対応モデル（将来の拡張用）
- 両方とも nullable、未設定時は環境変数やデフォルト値にフォールバック

### 3. マイグレーション

`database/migrate.py`に以下を追加：

```python
def migration_add_item_file_path(db_path: str):
    """Add FILE_PATH column to item table."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE item ADD COLUMN FILE_PATH TEXT")
        conn.commit()
        print("✓ Added FILE_PATH column to item table")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print("  FILE_PATH column already exists")
        else:
            raise
    finally:
        conn.close()

def migration_add_vision_models(db_path: str):
    """Add LIGHTWEIGHT_VISION_MODEL and VISION_MODEL columns to ai table."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE ai ADD COLUMN LIGHTWEIGHT_VISION_MODEL TEXT")
        print("✓ Added LIGHTWEIGHT_VISION_MODEL column to ai table")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print("  LIGHTWEIGHT_VISION_MODEL column already exists")
        else:
            raise

    try:
        cursor.execute("ALTER TABLE ai ADD COLUMN VISION_MODEL TEXT")
        print("✓ Added VISION_MODEL column to ai table")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print("  VISION_MODEL column already exists")
        else:
            raise

    conn.commit()
    conn.close()
```

## アイテムタイプ仕様

### 1. `object`（既存）

- **説明**: 一般的なオブジェクト
- **FILE_PATH**: 使用しない（NULL）
- **STATE_JSON**: 任意の状態を保存可能

### 2. `picture`（新規）

- **説明**: 画像ファイルを表すアイテム
- **FILE_PATH**: `~/.saiverse/image/` 配下のファイルパス
- **DESCRIPTION**: 自動生成されたsummary（300文字以内）
- **STATE_JSON**: メタデータ（生成プロンプト、mime_typeなど）を保存可能

### 3. `document`（新規）

- **説明**: テキスト文書を表すアイテム
- **FILE_PATH**: `~/.saiverse/documents/` 配下のファイルパス（.txt）
- **DESCRIPTION**: 自動生成されたsummary（300文字以内）
- **STATE_JSON**: メタデータ（作成日時、バージョン情報など）を保存可能

## ファイル保存ルール

### ディレクトリ構造

```
~/.saiverse/
├── image/               # 画像ファイル（既存）
│   ├── 20251204_120000_abcd1234.png
│   ├── 20251204_120000_abcd1234.png.summary.txt  # summary（既存）
│   └── ...
└── documents/           # 文書ファイル（新規）
    ├── 20251204_130000_efgh5678.txt
    ├── 20251204_130000_efgh5678.txt.summary.txt  # summary
    └── ...
```

### ファイル命名規則

- **画像**: `YYYYMMDD_HHMMSS_{uuid}.{ext}` （既存）
- **文書**: `YYYYMMDD_HHMMSS_{uuid}.txt`
- **summary**: `{original_filename}.summary.txt` （既存）

## Summary自動生成仕様

### 生成タイミング

- **picture**: アイテム作成時（ユーザーアップロード、`generate_image`実行後）
- **document**: アイテム作成時（`document_create`実行後）

### 使用モデル

- **picture**: `LIGHTWEIGHT_VISION_MODEL` → 環境変数 `SAIVERSE_LIGHTWEIGHT_VISION_MODEL` → デフォルト `gemini-2.0-flash`
- **document**: `LIGHTWEIGHT_MODEL` → 環境変数 `SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL` → デフォルト `gemini-2.5-flash-lite`

### 文字数制限

- **統一**: 300文字以内（日本語）

### プロンプト

#### Picture用（既存を踏襲）
```
以下の画像を詳しく説明するのではなく、内容を理解するための要点を300文字以内の日本語で1〜2文にまとめてください。
```

#### Document用（新規）
```
以下の文書の内容を300文字以内の日本語で要約してください。要点を簡潔にまとめてください。
```

## 新規ツール仕様

### 1. `item_view`

```python
def item_view(item_id: str) -> str:
    """
    View the full content of a picture or document item.

    Args:
        item_id: Identifier of the item to view.

    Returns:
        - picture: Base64 data URL or file path for display
        - document: Full text content of the file
        - object: Error message (not supported)
    """
```

**ツールスキーマ**:
```json
{
  "name": "item_view",
  "description": "View the full content of a picture or document item. Returns image data for pictures and full text for documents.",
  "parameters": {
    "type": "object",
    "properties": {
      "item_id": {
        "type": "string",
        "description": "Identifier of the item to view."
      }
    },
    "required": ["item_id"]
  }
}
```

**実装場所**: `tools/defs/item_view.py`

**振る舞い**:
- **picture**: ファイルパスからBase64 data URLを生成して返す、またはファイルパスをそのまま返す（UIで表示可能な形式）
- **document**: ファイルパスから全文を読み込んで返す
- **object**: `RuntimeError("このアイテムは view 操作に対応していません。")`

### 2. `document_create`

```python
def document_create(name: str, description: str, content: str) -> str:
    """
    Create a new document item and place it in the current building.

    Args:
        name: Name of the document.
        description: Brief description (will be used as initial summary).
        content: Full text content of the document.

    Returns:
        Success message with item ID.
    """
```

**ツールスキーマ**:
```json
{
  "name": "document_create",
  "description": "Create a new document item with text content and place it in the current building.",
  "parameters": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string",
        "description": "Name of the document (e.g., '私の日記', 'SAIVerse使い方ガイド')."
      },
      "description": {
        "type": "string",
        "description": "Brief description of the document. This will be visible in item lists."
      },
      "content": {
        "type": "string",
        "description": "Full text content of the document."
      }
    },
    "required": ["name", "description", "content"]
  }
}
```

**実装場所**: `tools/defs/document_create.py`

**処理フロー**:
1. 現在のbuilding_idを取得
2. `~/.saiverse/documents/`にテキストファイルを作成
3. contentからsummaryを自動生成（300文字以内）
4. summaryを`.summary.txt`として保存
5. DBに`Item`レコードを作成（type='document', FILE_PATH=ファイルパス, DESCRIPTION=summary）
6. `ItemLocation`レコードを作成（owner_kind='building', owner_id=building_id）
7. manager側のキャッシュを更新

## `item_use`ツールの拡張

### 現在の実装（object専用）

```python
def item_use(item_id: str, description: str) -> str:
    """Update the description of an object item."""
```

### 拡張後の実装

```python
def item_use(item_id: str, action_json: str) -> str:
    """
    Use an item to apply effects.

    Args:
        item_id: Identifier of the item to use.
        action_json: JSON string with action details.

    Action JSON schema:
        {
            "action_type": "update_description" | "patch_content",
            "description": "...",       # For update_description
            "patch": "..."              # For patch_content (document only)
        }
    """
```

**振る舞い**:

#### `object` / `picture`
- `action_type: "update_description"` → DESCRIPTIONを更新

#### `document`
- `action_type: "update_description"` → DESCRIPTIONを更新
- `action_type: "patch_content"` → FILE_PATHの内容に`patch`を追記・適用、summaryを再生成

**注意**: `action_json`のスキーマはLLMがその場で判断して生成する。複数の効果を持つアイテムの将来的な拡張を想定している。

## プレイブック設計

### 1. `item_view_playbook.json`

**説明**: アイテムを閲覧するだけのプレイブック

**ノード構成**:
```
START → llm:select_item → tool:item_view → memorize:record → END
```

- `select_item`: どのアイテムを見るか判断（response_schema: `{"item_id": "..."}`）
- `item_view`: 実際に閲覧
- `record`: 閲覧結果をSAIMemoryに記録

### 2. `item_edit_playbook.json`

**説明**: アイテムを閲覧してから編集するプレイブック

**ノード構成**:
```
START → llm:select_item → tool:item_view → llm:decide_action → router:action_router
  ├─ [edit] → tool:item_use → memorize:record → END
  └─ [view_only] → memorize:record → END
```

- `select_item`: どのアイテムを対象にするか判断
- `item_view`: 内容を確認
- `decide_action`: 編集するか閲覧だけかを判断（response_schema: `{"action": "edit" | "view_only", "action_json": {...}}`）
- `action_router`: routerノードで分岐
- `item_use`: 編集実行
- `record`: 結果を記録

### 3. `document_create_playbook.json`

**説明**: 新しい文書を作成するプレイブック

**ノード構成**:
```
START → llm:generate_content → tool:document_create → memorize:record → END
```

- `generate_content`: LLMが文書の内容を生成（response_schema: `{"name": "...", "description": "...", "content": "..."}`）
- `document_create`: 文書アイテムを作成
- `record`: 作成結果を記録

### 4. Meta playbookへの統合

`meta_user.json`と`meta_auto.json`のrouterノードで、以下のキーワードに応じて上記playbookを呼び出す：

- 「見る」「読む」「調べる」「確認する」→ `item_view_playbook`
- 「編集」「書き換える」「パッチを当てる」「更新する」→ `item_edit_playbook`
- 「文書を作る」「日記を書く」「文章を作成」→ `document_create_playbook`

## 画像生成ツールの拡張

### 現在の実装

```python
def generate_image(prompt: str) -> tuple[str, ToolResult, str | None, dict | None]:
    """Generate an image and return (text, snippet, file_path, metadata)."""
```

### 拡張後の処理

1. 画像生成（既存処理）
2. 生成後、**pictureタイプのアイテムを自動作成**
3. summary自動生成（`media_summary.py`の`ensure_image_summary()`を利用）
4. DBに`Item`レコード作成（type='picture', FILE_PATH=stored_path, DESCRIPTION=summary）
5. `ItemLocation`レコード作成（owner_kind='building', owner_id=現在のbuilding_id）
6. manager側のキャッシュ更新

**実装場所**: `tools/defs/image_generator.py`に処理を追加

**output_keys**: 既存の4値返却をそのまま活かす
```json
"output_keys": ["text", "snippet", "file_path", "metadata"]
```

## ユーザーファイルアップロードからのアイテム作成

### 現在の実装

`ui/chat.py`の`_store_uploaded_image()`で画像を保存、`metadata`に追加

### 拡張後の処理

1. 画像アップロード（既存処理）
2. **pictureタイプのアイテムを自動作成**
3. summary自動生成（既存の`ensure_image_summary()`を利用）
4. DBに`Item`レコード作成
5. `ItemLocation`レコード作成（owner_kind='building', owner_id=ユーザーの現在位置）
6. UIでアイテム作成通知を表示

**実装場所**: `ui/chat.py`の`respond_stream()`または`SAIVerseManager.handle_user_input_stream()`

## UI表示仕様

### 右サイドパネルでの表示

**現在の実装**:
- `ui/chat.py`の`get_building_details()`でアイテムリストを取得
- `format_building_details()`でMarkdown表示

**拡張後の実装**:
1. アイテム名をクリック可能なリンクとして表示
2. クリック時、モーダルウィンドウを表示
3. モーダル内容:
   - **picture**: 画像を表示（Gradio Image component）
   - **document**: 全文を表示（Gradio Textbox component）
   - **object**: 「閲覧不可」メッセージ

**実装場所**: `ui/app.py`にモーダル用のGradioコンポーネントを追加

**実装方針**:
- まずはコンパクトなモーダル対応
- 美術館のようなギャラリー表示は将来の拡張として保留（数か月後）

## 実装順序

### Phase 1: データベース準備
1. `database/models.py`にカラム定義追加（FILE_PATH, LIGHTWEIGHT_VISION_MODEL, VISION_MODEL）
2. `database/migrate.py`にマイグレーション追加
3. マイグレーション実行

### Phase 2: ファイル管理ユーティリティ
4. `~/.saiverse/documents/`ディレクトリ作成処理を追加（`media_utils.py`に類似）
5. document用のsummary生成処理を追加（`media_summary.py`に類似関数を追加）

### Phase 3: ツール実装
6. `tools/defs/item_view.py`を作成
7. `tools/defs/document_create.py`を作成
8. `tools/defs/item_use.py`を拡張（action_json対応）
9. `tools/__init__.py`にツール登録

### Phase 4: 画像生成とアップロード拡張
10. `tools/defs/image_generator.py`を拡張（pictureアイテム自動作成）
11. `ui/chat.py`または`saiverse_manager.py`を拡張（アップロード時のpictureアイテム作成）

### Phase 5: プレイブック作成
12. `sea/playbooks/item_view_playbook.json`を作成
13. `sea/playbooks/item_edit_playbook.json`を作成
14. `sea/playbooks/document_create_playbook.json`を作成
15. `sea/playbooks/meta_user.json`のrouterを更新
16. `scripts/import_playbook.py`で各プレイブックをDBにインポート

### Phase 6: UI対応
17. `ui/chat.py`のアイテム表示にクリック可能リンクを追加
18. `ui/app.py`にモーダルコンポーネントを追加
19. モーダル表示処理を実装

### Phase 7: テストと調整
20. 各ツールの単体テスト作成
21. プレイブックの動作確認
22. UI操作の動作確認

## 環境変数

### 追加する環境変数

```bash
# Lightweight vision model for summary generation
SAIVERSE_LIGHTWEIGHT_VISION_MODEL=gemini-2.0-flash

# Vision model for full vision tasks (future use)
SAIVERSE_VISION_MODEL=gemini-2.0-flash-thinking
```

### `.env.example`への追加

```bash
# Vision Models (optional)
# SAIVERSE_LIGHTWEIGHT_VISION_MODEL=gemini-2.0-flash
# SAIVERSE_VISION_MODEL=gemini-2.0-flash-thinking
```

## 注意事項

- **後方互換性**: 既存の`object`タイプアイテムは`FILE_PATH=NULL`として動作継続
- **summary再生成**: documentの内容を`patch_content`で変更した場合、自動的にsummaryを再生成する
- **権限管理**: 現時点では実装しない。将来的に`owner_persona_id`や編集権限を追加する可能性がある
- **ファイルサイズ制限**: 現時点では制限なし。将来的に必要に応じて追加
- **マルチモーダル対応**: picture閲覧時にLLMに画像を渡す機能は将来の拡張として保留

## 関連ドキュメント

- `docs/database_design.md`: データベース設計全体
- `docs/sea_integration_plan.md`: SEAフレームワーク統合計画
- `CLAUDE.md`: プレイブック設計哲学（router simplicity, arguments inside playbooks）
