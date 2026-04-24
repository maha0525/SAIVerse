# MCP連携

SAIVerse は MCP (Model Context Protocol) クライアントとして外部ツールサーバーへ接続できます。

## 概要

- `mcp_servers.json` に定義したサーバーへ起動時に接続します。
- 見つかったツールは `server__tool` 形式で既存の `TOOL_REGISTRY` に登録されます。
- 必要なものだけ `spell_tools` で選ぶと、`/spell` からも使えます。
- `expansion_data/<addon>/mcp_servers.json` に置けば、アドオン配布物からも MCP サーバーを宣言できます。

## 設定場所

優先順位は次の通りです。

1. `~/.saiverse/user_data/mcp_servers.json`
2. `~/.saiverse/user_data/<project>/mcp_servers.json`
3. `expansion_data/<pack>/mcp_servers.json`
4. `builtin_data/mcp_servers.json`

同じサーバー名が複数箇所にある場合は、上位の定義が使われます。

## 設定例

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/workspace"],
      "timeout": 120,
      "spell_tools": [
        {
          "name": "read_file",
          "display_name": "ファイル参照"
        }
      ]
    }
  }
}
```

## spell_tools

`spell_tools` は以下のどちらでも書けます。

```json
{
  "spell_tools": ["read_file", "list_directory"]
}
```

```json
{
  "spell_tools": {
    "read_file": {
      "display_name": "ファイル参照"
    },
    "list_directory": true
  }
}
```

登録後のスペル名は `filesystem__read_file` のように名前空間付きになります。

## アドオン連携

アドオンで MCP を配布したい場合は、`expansion_data/<addon>/mcp_servers.json` を同梱します。  
この方式なら、既存のアドオン向け project/addon 読み込み規約のまま MCP サーバー定義と spell 化を運べます。

## API

- `GET /api/mcp/servers`
- `GET /api/mcp/tools`
- `POST /api/mcp/servers/{server_name}/reconnect`
