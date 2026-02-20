# MCP (Model Context Protocol) 連携

SAIVerseのペルソナが外部MCPサーバーのツールを使用できるようにする機能です。

## 概要

[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) は、LLMアプリケーションが外部のツールやデータソースと標準化された方法で接続するためのプロトコルです。

SAIVerseはMCPクライアントとして動作し、外部のMCPサーバーが提供するツールをペルソナの既存ツールシステムに統合します。MCPサーバーのツールは、ビルトインツールと同じようにプレイブックから利用できます。

```
┌─────────────────┐         stdio / HTTP          ┌───────────────────┐
│    SAIVerse      │◄────────────────────────────►│  MCP Server       │
│  (MCP Client)    │    list_tools / call_tool     │  (filesystem等)   │
└─────────────────┘                               └───────────────────┘
```

## セットアップ

### 1. 依存パッケージ

`mcp` パッケージが必要です（`requirements.txt` に含まれています）：

```bash
pip install mcp>=1.10.0
```

### 2. MCPサーバーの準備

使いたいMCPサーバーをインストールします。多くのMCPサーバーは `npx` で直接実行可能です：

```bash
# Node.js が必要（npx経由で実行するため）
# https://nodejs.org/

# 例: ファイルシステムサーバー
npx -y @modelcontextprotocol/server-filesystem /path/to/dir

# 例: テスト用のeverythingサーバー
npx -y @modelcontextprotocol/server-everything
```

利用可能なMCPサーバーの一覧：https://github.com/modelcontextprotocol/servers

### 3. 設定ファイルの作成

`~/.saiverse/user_data/mcp_servers.json` を作成します：

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/yourname/Documents"],
      "enabled": true
    }
  }
}
```

SAIVerseを再起動すると、設定されたMCPサーバーに自動的に接続し、ツールが登録されます。

## 設定ファイル

### 配置場所

設定ファイルは SAIVerse の3層優先度システムに従います：

| 優先度 | パス | 用途 |
|--------|------|------|
| 高 | `~/.saiverse/user_data/mcp_servers.json` | 個人設定 |
| 中 | `expansion_data/<pack>/mcp_servers.json` | 拡張パック同梱 |
| 低 | `builtin_data/mcp_servers.json` | デフォルト設定 |

同名のサーバーは高優先度のファイルが優先されます。

### フォーマット

[Claude Desktop](https://claude.ai/download) と互換性のあるフォーマットです：

```json
{
  "mcpServers": {
    "サーバー名": {
      // --- stdio トランスポート（ローカルプロセス） ---
      "command": "実行コマンド",
      "args": ["引数1", "引数2"],
      "env": {
        "環境変数": "値"
      },
      "enabled": true,
      "timeout": 120

      // --- または HTTP トランスポート（リモートサーバー） ---
      // "url": "http://localhost:3001/mcp",
      // "transport": "streamable_http"
    }
  }
}
```

### フィールド一覧

| フィールド | 型 | 必須 | 説明 |
|-----------|------|------|------|
| `command` | string | stdio時必須 | 実行するコマンド（`npx`, `python`, `node` 等） |
| `args` | string[] | - | コマンドの引数 |
| `env` | object | - | サーバープロセスの環境変数 |
| `url` | string | HTTP時必須 | リモートサーバーのURL |
| `transport` | string | - | `"streamable_http"` または `"sse"`（URLがある場合のデフォルト: `"streamable_http"`） |
| `enabled` | boolean | - | `false` で無効化（デフォルト: `true`） |
| `timeout` | number | - | ツール実行タイムアウト秒数（デフォルト: `120`） |

### トランスポートの判定

- `command` キーがある → **stdio**（ローカルプロセスを起動）
- `url` キーがある → **HTTP**（`transport` フィールドで種類を指定）

### 環境変数の展開

`env` フィールドで `${ENV_VAR}` 構文を使うと、`.env` やシステムの環境変数から値を読み込めます。APIキーなどの秘密情報をJSONに直書きする必要がありません：

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
      }
    }
  }
}
```

`.env` ファイルに `GITHUB_TOKEN=ghp_xxxx...` と書いておけば、起動時に自動展開されます。

## ツールの命名規則

MCPサーバーから登録されるツールには、サーバー名のプレフィックスが付与されます：

```
{サーバー名}__{ツール名}
```

例：
- `filesystem` サーバーの `read_file` → `filesystem__read_file`
- `github` サーバーの `create_issue` → `github__create_issue`

ダブルアンダースコア (`__`) で区切ります（Claude Desktop と同じ慣例）。

## プレイブックでの使用

MCPツールはビルトインツールと同じ方法でプレイブックから使用できます。

### LLMノードの `available_tools` で指定

```json
{
  "type": "llm",
  "id": "use_filesystem",
  "available_tools": ["filesystem__read_file", "filesystem__write_file"],
  "prompt": "ユーザーの要求に応じてファイルを読み書きしてください。"
}
```

### TOOLノードで直接呼び出し

```json
{
  "type": "tool",
  "id": "read_config",
  "action": "filesystem__read_file",
  "args_input": {
    "path": "config_file_path"
  },
  "output_key": "file_content"
}
```

## API エンドポイント

MCPの状態確認と管理用のAPIが利用可能です：

| メソッド | エンドポイント | 説明 |
|---------|---------------|------|
| GET | `/api/mcp/servers` | 接続中のサーバー一覧と状態 |
| GET | `/api/mcp/tools` | 登録済みMCPツール一覧 |
| POST | `/api/mcp/servers/{name}/reconnect` | サーバーの再接続 |

### レスポンス例

**GET /api/mcp/servers**
```json
[
  {
    "name": "filesystem",
    "transport": "stdio",
    "connected": true,
    "tool_count": 3,
    "tools": ["read_file", "write_file", "list_directory"]
  }
]
```

**GET /api/mcp/tools**
```json
[
  {
    "name": "filesystem__read_file",
    "description": "[MCP:filesystem] Read the contents of a file",
    "parameters": {
      "type": "object",
      "properties": {
        "path": { "type": "string", "description": "Path to the file" }
      },
      "required": ["path"]
    }
  }
]
```

## 設定例

### ファイルシステムアクセス

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/yourname/Documents"]
    }
  }
}
```

### GitHub 連携

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
      }
    }
  }
}
```

### テスト用サーバー

開発時のテストには `@modelcontextprotocol/server-everything` が便利です：

```json
{
  "mcpServers": {
    "everything": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-everything"]
    }
  }
}
```

### 複数サーバーの同時使用

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/workspace"]
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
      }
    },
    "remote_db": {
      "url": "http://localhost:3001/mcp",
      "transport": "streamable_http",
      "timeout": 60
    }
  }
}
```

## トラブルシューティング

### サーバーが接続できない

起動ログを確認してください：

```
WARNING MCP: server 'filesystem' failed to start, skipping: ...
```

よくある原因：
- `npx` がPATHにない（Node.jsがインストールされていない）
- MCPサーバーパッケージが見つからない
- コマンドのパスが間違っている

### ツールが登録されない

`GET /api/mcp/servers` でサーバーの接続状態を確認：

```bash
curl http://localhost:8000/api/mcp/servers
```

`connected: false` の場合はサーバーが起動に失敗しています。バックエンドログで詳細を確認してください。

### ツール実行がタイムアウトする

`timeout` フィールドでタイムアウト秒数を延長できます（デフォルト: 120秒）：

```json
{
  "mcpServers": {
    "slow_server": {
      "command": "python",
      "args": ["my_server.py"],
      "timeout": 300
    }
  }
}
```

### サーバーの再接続

接続が切れた場合、APIから再接続できます：

```bash
curl -X POST http://localhost:8000/api/mcp/servers/filesystem/reconnect
```

## 技術的な詳細

### アーキテクチャ

```
main.py (起動)
  └─ initialize_mcp()
       └─ MCPClientManager.start_all()
            ├─ load_mcp_configs()       ← mcp_servers.json 読み込み
            ├─ MCPServerConnection.connect()  ← サーバーに接続
            ├─ session.list_tools()     ← ツール発見
            └─ register_external_tool() ← TOOL_REGISTRY に登録
                 ├─ OPENAI_TOOLS_SPEC
                 ├─ GEMINI_TOOLS_SPEC
                 └─ TOOL_SCHEMAS
```

### 関連ファイル

| ファイル | 役割 |
|---------|------|
| `tools/mcp_config.py` | 設定ファイルの読み込みと検証 |
| `tools/mcp_client.py` | MCPクライアント管理（接続、ツール登録、実行） |
| `tools/__init__.py` | `register_external_tool()` — 動的ツール登録 |
| `sea/runtime.py` | 非同期ツール実行対応 |
| `api/routes/mcp.py` | MCP管理APIエンドポイント |

## 次のステップ

- [ツールシステム](tools-system.md) - SAIVerseのツールの仕組み
- [ツールの追加](../developer-guide/adding-tools.md) - カスタムツールの開発
- [プレイブック](playbooks.md) - ツールを使うワークフローの設計
