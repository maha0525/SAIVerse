# Memopedia

Memopediaは、SAIMemoryに記録されたペルソナの会話ログから知識を抽出し、Wikipediaのような構造化されたドキュメント群として管理する機能。

## 概要

### 解決する課題

従来のSAIMemoryでは発言そのものの想起は可能だが、トピックに関する体系的な知識が抜け落ちやすい。例えば「SAIVerseについての話」と言ったときにSAIVerseをクエリにして想起しても、SAIVerseについての重要な情報が欠落する可能性がある。

Memopediaでは、特定の重要なトピックに対してその情報を1ページにまとめる。ペルソナはページ一覧から関連するものを選び、必要な知識をコンテキストに展開できる。

### 設計思想

- **木構造によるページ管理**: 3つのルートカテゴリ（人物/出来事/予定）から階層的にページを配置
- **概要の常時提示**: ページタイトルと概要は常にペルソナに渡し、詳細を読むかどうかを判断可能に
- **開閉状態の保持**: セッション（スレッド）単位でページの開閉状態を保存し、話題が継続している限り操作不要
- **バッチ更新**: コスト削減のため、約10会話ごとにまとめて知識を抽出・更新
- **Markdown形式**: ページ内容はMarkdownで記述し、人間にも読みやすく

## データ構造

### テーブル

Memopediaは既存のSAIMemory（`memory.db`）に以下のテーブルを追加：

```sql
-- ページ本体
memopedia_pages (
  id TEXT PRIMARY KEY,
  parent_id TEXT,           -- 親ページID (NULLならRoot)
  title TEXT NOT NULL,      -- ページタイトル
  summary TEXT,             -- 概要（常にペルソナに渡す部分）
  content TEXT,             -- 本文（Markdown形式）
  category TEXT NOT NULL,   -- "people" / "events" / "plans"
  keywords TEXT,            -- キーワード（JSON配列）
  is_deleted INTEGER,       -- ソフトデリートフラグ
  created_at INTEGER,
  updated_at INTEGER
)

-- 開閉状態（セッション単位）
memopedia_page_states (
  thread_id TEXT NOT NULL,
  page_id TEXT NOT NULL,
  is_open INTEGER DEFAULT 0,
  opened_at INTEGER,
  PRIMARY KEY (thread_id, page_id)
)

-- 更新履歴（バッチ処理の追跡用）
memopedia_update_log (
  id TEXT PRIMARY KEY,
  last_message_id TEXT,
  last_message_created_at INTEGER,
  processed_at INTEGER NOT NULL
)

-- 編集履歴（ページ変更の追跡・参照メッセージ範囲記録）
memopedia_page_edit_history (
  id TEXT PRIMARY KEY,
  page_id TEXT NOT NULL,
  edited_at INTEGER NOT NULL,
  diff_text TEXT NOT NULL,         -- unified diff形式
  ref_start_message_id TEXT,       -- 参照範囲の開始メッセージID
  ref_end_message_id TEXT,         -- 参照範囲の終了メッセージID
  edit_type TEXT NOT NULL,         -- 'create' / 'update' / 'append' / 'delete'
  edit_source TEXT                 -- 編集経路: 'ai_conversation', 'manual', 'api' など
)
```

### ルートカテゴリ

初期状態で以下の3つのルートページが作成される：

| ID | タイトル | カテゴリ | 説明 |
|---|---|---|---|
| `root_people` | 人物 | people | 関わりのある人物についての記録 |
| `root_events` | 出来事 | events | 過去に起きた出来事の記録 |
| `root_plans` | 予定 | plans | 進行中や計画中のプロジェクト・予定 |

## 使い方

### 既存メモリからMemopediaを構築

```bash
# 基本的な使い方（最初の100件のメッセージから構築）
python scripts/build_memopedia.py <persona_id> --limit 100

# dry-runで確認（DBに書き込まない）
python scripts/build_memopedia.py <persona_id> --limit 100 --dry-run

# 使用するモデルを指定
python scripts/build_memopedia.py <persona_id> --model claude-opus-4-20250514
python scripts/build_memopedia.py <persona_id> --model gpt-4o
python scripts/build_memopedia.py <persona_id> --model gemini-2.5-pro

# 利用可能なモデル一覧を表示
python scripts/build_memopedia.py --list-models

# バッチサイズを調整（1回のLLMコールで処理するメッセージ数）
python scripts/build_memopedia.py <persona_id> --batch-size 10
```

#### オプション一覧

| オプション | デフォルト | 説明 |
|---|---|---|
| `--limit N` | 100 | 処理するメッセージの最大数 |
| `--model MODEL` | gemini-2.0-flash | 使用するLLMモデル |
| `--provider PROVIDER` | (自動検出) | プロバイダーを明示指定（openai/anthropic/gemini/ollama） |
| `--batch-size N` | 20 | 1回のLLMコールで処理するメッセージ数 |
| `--dry-run` | - | DBに書き込まずプレビューのみ |
| `--list-models` | - | 利用可能なモデル一覧を表示 |

### UIで確認

SAIVerseを起動後、サイドバーから「Memory & Knowledge」→「Memopedia」タブを選択：

- **Knowledge Tree**: ページの階層構造を表示（初期状態で全展開）
  - `>`マークをクリックで展開/格納を切り替え
  - ページ名をクリックで右側に内容を表示
- **履歴ボタン**: ページ選択時に表示される。編集履歴を確認可能
  - 編集タイプ（作成/更新/追記/削除）、日時、編集経路を表示
  - 参照メッセージ範囲（どのメッセージを見て編集したか）を表示
  - クリックでdiff（差分）を展開表示

### ツール（Persona用）

ペルソナが会話中にMemopediaを操作するためのツール：

| ツール名 | 説明 |
|---|---|
| `memopedia_get_tree` | ページツリーをMarkdown形式で取得 |
| `memopedia_open_page` | 指定したページを開き、内容を取得 |
| `memopedia_close_page` | 指定したページを閉じる |

## Python API

### 基本的な使い方

```python
from sai_memory.memory.storage import init_db
from sai_memory.memopedia import Memopedia

# 既存のmemory.dbに接続
conn = init_db("/path/to/memory.db", check_same_thread=False)
memopedia = Memopedia(conn)

# ツリー構造を取得
tree = memopedia.get_tree(thread_id="main")
# => {"people": [...], "events": [...], "plans": [...]}

# Markdown形式でツリーを取得
markdown = memopedia.get_tree_markdown(thread_id="main")

# ページを作成（参照メッセージ範囲を記録）
page = memopedia.create_page(
    parent_id="root_people",
    title="まはー",
    summary="SAIVerseの開発者",
    content="## 基本情報\n\n- 名前: まはー\n- 役割: 開発者",
    ref_start_message_id="msg_001",  # 参照した最初のメッセージ
    ref_end_message_id="msg_020",    # 参照した最後のメッセージ
    edit_source="ai_conversation"     # 編集経路
)

# ページを更新（同様に参照範囲を記録可能）
memopedia.update_page(
    page.id,
    content="更新された内容",
    ref_start_message_id="msg_021",
    ref_end_message_id="msg_030",
    edit_source="ai_conversation"
)

# ページに追記
memopedia.append_to_content(
    page.id,
    "\n\n## 追加情報\n\n新しい内容",
    ref_start_message_id="msg_031",
    ref_end_message_id="msg_040",
    edit_source="manual"
)

# 編集履歴を取得
history = memopedia.get_page_edit_history(page.id)
for h in history:
    print(f"{h.edit_type} at {h.edited_at}")
    print(f"  refs: {h.ref_start_message_id} -> {h.ref_end_message_id}")
    print(f"  source: {h.edit_source}")
    print(f"  diff: {h.diff_text[:100]}...")

# ページを開く（セッション単位）
result = memopedia.open_page(thread_id="main", page_id=page.id)

# 開いているページの内容を取得
content = memopedia.get_open_pages_content(thread_id="main")

# ページを閉じる
memopedia.close_page(thread_id="main", page_id=page.id)

# ページを削除（ソフトデリート、履歴は保持）
memopedia.delete_page(
    page.id,
    ref_start_message_id="msg_050",
    ref_end_message_id="msg_055",
    edit_source="manual"
)

# 全ページをMarkdownでエクスポート
full_export = memopedia.export_all_markdown()
```

### 主要メソッド

#### ツリー操作

- `get_tree(thread_id=None)` - ページツリーを辞書形式で取得
- `get_tree_markdown(thread_id=None)` - ページツリーをMarkdown形式で取得

#### ページ操作

- `get_page(page_id)` - ページを取得
- `get_page_full(page_id)` - ページと子ページ一覧を取得
- `create_page(parent_id, title, summary, content, ref_start_message_id=None, ref_end_message_id=None, edit_source=None)` - 新規ページ作成
- `update_page(page_id, title=None, summary=None, content=None, ref_start_message_id=None, ref_end_message_id=None, edit_source=None)` - ページ更新
- `append_to_content(page_id, text, ref_start_message_id=None, ref_end_message_id=None, edit_source=None)` - ページに内容を追記
- `delete_page(page_id, ref_start_message_id=None, ref_end_message_id=None, edit_source=None)` - ページ削除（ソフトデリート、ルートページは削除不可）
- `find_by_title(title, category=None)` - タイトルでページ検索
- `search(query, limit=10)` - タイトル/概要/内容で検索

#### 編集履歴操作

- `get_page_edit_history(page_id, limit=50)` - ページの編集履歴を取得（新しい順）

各履歴エントリには以下の情報が含まれる：
- `edit_type`: 編集タイプ（create/update/append/delete）
- `edited_at`: 編集日時（Unixタイムスタンプ）
- `diff_text`: 変更差分（unified diff形式）
- `ref_start_message_id`, `ref_end_message_id`: 参照メッセージ範囲
- `edit_source`: 編集経路

#### 開閉状態操作

- `open_page(thread_id, page_id)` - ページを開く
- `close_page(thread_id, page_id)` - ページを閉じる
- `get_open_pages(thread_id)` - 開いているページ一覧を取得
- `get_open_pages_content(thread_id)` - 開いているページの内容をMarkdownで取得

#### エクスポート

- `get_page_markdown(page_id)` - 単一ページをMarkdownで取得
- `export_all_markdown()` - 全ページをMarkdownでエクスポート

## 今後の予定

### バッチ更新の自動化

現在は手動でスクリプトを実行する必要があるが、将来的には：

- 10会話ごとに自動で更新処理を実行
- Playbook内のノードとして統合

### ページ探索Playbook

会話開始時に自動でページを探索するPlaybook：

1. `memopedia_get_tree`でツリーを取得
2. 軽量LLMで「開くべきページはあるか」を判定
3. 必要なページを`memopedia_open_page`で開く
4. 満足するまで繰り返し
5. 開いているページの内容をコンテキストに追加

### 子ページ自動分割

ページ内容が長くなった場合（例: 2000文字超）に自動で子ページに分割する機能。

## ファイル構成

```
sai_memory/
  memopedia/
    __init__.py      # モジュールエントリーポイント
    storage.py       # テーブル定義とCRUD操作
    core.py          # Memopediaクラス（高レベルAPI）

scripts/
  build_memopedia.py # 既存メモリからMemopedia構築

tools/defs/
  memopedia_get_tree.py    # ツリー取得ツール
  memopedia_open_page.py   # ページを開くツール
  memopedia_close_page.py  # ページを閉じるツール

ui/
  memopedia.py       # Gradio UIタブ
```
