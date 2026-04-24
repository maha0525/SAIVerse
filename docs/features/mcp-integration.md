# MCP連携

SAIVerse は MCP (Model Context Protocol) クライアントとして外部ツールサーバーへ接続できます。本ドキュメントは実装された機能を利用者視点でまとめたものです。設計の意図や不変条件については `docs/intent/mcp_addon_integration.md` を参照してください。

## 概要

- `mcp_servers.json` に定義したサーバーへ起動時に接続します。
- 見つかったツールは `server__tool` 形式で既存の `TOOL_REGISTRY` に登録されます。
- 必要なものだけ `spell_tools` で選ぶと、`/spell` からも使えます。
- `expansion_data/<addon>/mcp_servers.json` に置けば、アドオン配布物からも MCP サーバーを宣言できます。
- ペルソナごとに別アカウントを持たせたいサービス（Elyth 等）向けに `scope: "per_persona"` が選べます。
- アドオン由来サーバー名は自動で `<addon_name>__` にプレフィックスされ、他アドオンやユーザー設定と衝突しないよう隔離されます。
- API キー等の秘密情報は、`mcp_servers.json` に直書きせず AddonConfig/AddonPersonaConfig から参照構文で注入できます。

## 設定場所

優先順位は次の通りです。

1. `~/.saiverse/user_data/mcp_servers.json`
2. `~/.saiverse/user_data/<project>/mcp_servers.json`
3. `expansion_data/<pack>/mcp_servers.json`
4. `builtin_data/mcp_servers.json`

**1, 2, 4** は特権領域扱いで、宣言した `server_name` がそのまま登録キーになります。同名があれば優先順位で上位が使われます。

**3（アドオン由来）** は自動的に `<addon_name>__<server_name>` へリネームされて登録されます。これによりアドオン制作者は衝突を気にせず任意の名前を使えます。

## 設定例（基本）

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/workspace"],
      "timeout": 120,
      "spell_tools": [
        {"name": "read_file", "display_name": "ファイル参照"}
      ]
    }
  }
}
```

## スコープ (`scope`)

サーバー定義に `scope` フィールドを指定できます。省略時は `"global"` です。

### `scope: "global"` (既定)

起動時に1プロセス立ち上がり、全ペルソナで共有します。ファイルシステム、共有キャッシュ、共有ベクトル DB のように state を共有すべきサーバー向き。

### `scope: "per_persona"`

ペルソナごとに独立プロセスを起動します。AIエージェント単位でアカウント発行する外部サービス（Elyth, Twitter, Mastodon 等）向き。

- 起動時に一度だけ tool discovery を行います（任意の 1 ペルソナの env で接続 → 切断）。
- 実際のツール呼び出し時、`tools.context.get_active_persona_id()` で取得したペルソナ用のインスタンスを遅延起動します。
- ペルソナごとに異なる API キーが必要な場合は env 値を `${persona.addon.<addon>.<key>}` で参照します。

```json
{
  "mcpServers": {
    "elyth": {
      "command": "npx",
      "args": ["-y", "elyth-mcp-server@latest"],
      "env": {
        "ELYTH_API_KEY": "${persona.addon.saiverse-elyth-addon.api_key}",
        "ELYTH_API_BASE": "https://elythworld.com"
      },
      "scope": "per_persona"
    }
  }
}
```

## env 参照構文

サーバー定義の文字列値（`env` の値、`args` の要素、`url` など）に `${...}` プレースホルダーを書けます。

| 構文 | 解決元 |
|------|--------|
| `${env.VAR_NAME}` | OS環境変数（推奨形） |
| `${VAR_NAME}` | OS環境変数（既存互換、`.` を含まないキーに限る） |
| `${addon.<addon_name>.<key>}` | `AddonConfig` のグローバルパラメータ |
| `${persona.addon.<addon_name>.<key>}` | `AddonPersonaConfig` → グローバル → `addon.json` デフォルトの順で解決 |

`${persona.addon.*}` は `scope: "per_persona"` サーバーでのみ意味を持ちます（global スコープでは persona 文脈がないため解決されません）。

未解決のプレースホルダーが残った状態では **サーバーは起動せず、「missing_config」カテゴリの失敗**として記録されます。silent に空文字列に置換されることはありません。

## spell_tools

`spell_tools` は以下のどちらでも書けます。

```json
{ "spell_tools": ["read_file", "list_directory"] }
```

```json
{
  "spell_tools": {
    "read_file": {"display_name": "ファイル参照"},
    "list_directory": true
  }
}
```

スペル名は `filesystem__read_file` のように名前空間付きになります。アドオン由来の場合は `<addon_name>__<server_name>__<tool_name>` の形になります。

## ライフサイクル (参照カウント)

各サーバーインスタンスは、参照元 (`referenced_by`) の集合で管理されます。

- アドオンから宣言されたサーバーは、そのアドオンが有効な間だけ参照されます。
- ユーザー設定 (`user_data/`) と builtin からのサーバーは起動中ずっと参照されます。
- アドオンを **無効化** すると、そのアドオン由来サーバーの参照が外れ、refcount がゼロになったプロセスは停止します。
- アドオンを **有効化** し直すと、global スコープは再起動し、per_persona スコープは tool discovery を再実行します。
- UI から**手動停止**することもできます（次回呼び出しで再起動可能）。

## エラー分類

サーバー起動失敗は以下のカテゴリで記録されます。

| カテゴリ | 意味 |
|----------|------|
| `runtime_missing` | `npx`/`uvx`/`python` 等のランタイムが PATH にない |
| `missing_config` | 必須の env 値が未解決（参照構文が解決できない等） |
| `auth_failed` | サーバー側の認証失敗（401/403 等） |
| `command_error` | 起動コマンドエラー（npm パッケージ名誤り、リポジトリ消滅等） |
| `network` | ネットワーク到達性の問題（タイムアウト、DNS 等） |
| `process_crash` | 子プロセスが異常終了 |
| `unknown` | 分類不能 |

連続失敗時は exponential backoff（初期 2 秒、最大 60 秒）で再試行を抑制します。UI の手動リトライや `POST /api/mcp/instances/retry` でバックオフを強制解除できます。

失敗時のメッセージは **ペルソナ向け（簡潔、行動変更を促す）** と **ユーザー向け（詳細、対処方法付き）** で分離されます。前者はツール応答として返り、後者はログと API レスポンスに載ります。

## API

### 参照系

- `GET /api/mcp/servers` — 全インスタンスのステータス一覧（instance_key 単位）
- `GET /api/mcp/tools` — 登録済みツール一覧
- `GET /api/mcp/failures` — 起動失敗中のインスタンス（バックオフ情報付き）

### 制御系

- `POST /api/mcp/servers/{server_name}/reconnect` — 指定した qualified_server_name の全インスタンスを再接続
- `POST /api/mcp/instances/stop?instance_key=<key>` — 指定インスタンスを手動停止（refcount 無視）
- `POST /api/mcp/instances/retry?instance_key=<key>` — バックオフ中のインスタンスを即座にリトライ可能にする

### instance_key のフォーマット

```
<qualified_server_name>:global
<qualified_server_name>:persona:<persona_id>
```

`qualified_server_name` はアドオン由来の場合 `<addon_name>__<server_name>`、それ以外は `<server_name>` そのまま。

## アドオン制作者向けガイド

アドオンで MCP サーバーを配布する基本形：

```
expansion_data/<your-addon>/
├── addon.json            # params_schema で AddonConfig スキーマを宣言
├── mcp_servers.json      # MCP サーバー定義（自動プレフィックスされる）
├── tools/                # 通常の SAIVerse ツール
└── playbooks/public/     # 推奨 playbook（あれば）
```

### AddonConfig とのつなぎ方

1. `addon.json` の `params_schema` に API キー等の入力欄を宣言します。ペルソナごとに値を変えたい場合は `persona_configurable: true` を付けます。

    ```json
    {
      "params_schema": [
        {
          "key": "api_key",
          "type": "password",
          "label": "API Key",
          "persona_configurable": true
        }
      ]
    }
    ```

2. `mcp_servers.json` の env で参照構文を使います：

    ```json
    {
      "mcpServers": {
        "elyth": {
          "command": "npx",
          "args": ["-y", "elyth-mcp-server@latest"],
          "env": {
            "ELYTH_API_KEY": "${persona.addon.your-addon-name.api_key}"
          },
          "scope": "per_persona"
        }
      }
    }
    ```

    `your-addon-name` はアドオンディレクトリ名（`expansion_data/` 直下）と一致させてください。

### サーバー名の衝突について

`mcp_servers.json` で宣言する `server_name` には他アドオンとの衝突を意識する必要はありません。SAIVerseが自動的に `<addon_name>__<server_name>` へ内部リネームするので、汎用名（`filesystem`、`database` 等）を使っても安全です。

ただし、LLM に提示されるツール名も `<addon_name>__<server_name>__<tool_name>` と長くなるため、`server_name` は用途を示す簡潔な名前にすると良いです（`elyth-social` より `elyth` の方が良い等）。

### 同時AITuber数などサービス側制限について

外部サービス側が「同時アカウント数」等の上限を設けている場合、SAIVerseはそれをチェックしません（各ペルソナが自分の API キーを持つ per_persona スコープなら、キー発行時点で上限が効きます）。**同じ API キーを複数ペルソナに割り当てる運用は技術的には可能ですが、サービス側が意図しない共有になり得るため、READMEで注意喚起してください。**

## 関連

- 設計意図と不変条件: `docs/intent/mcp_addon_integration.md`
- 既存実装: `tools/mcp_client.py`, `tools/mcp_config.py`
- AddonConfig 読み取り API: `saiverse/addon_config.py`
