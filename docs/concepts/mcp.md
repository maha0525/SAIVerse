# MCP / Elicitation（外部ツールサーバー）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §7](../overview/landscape.md)、**設計意図**は intent [`mcp_addon_integration.md`](../intent/mcp_addon_integration.md) / [`mcp_protocol_coverage.md`](../intent/mcp_protocol_coverage.md) を参照。

## 一言で

Model Context Protocol を実装した外部ツールサーバーに接続し、そのツールをペルソナの [Spell](spell.md) として使えるようにする仕組み。

## 役割

SAIVerse 本体にコードを書かずに、MCP サーバー（外部プロセス）が提供するツール群をペルソナの能力として取り込む。[Addon](addon.md) が MCP サーバーを内包する形で配布される。

## 仕組み

- `tools/list` + `tools/call` で外部サーバーのツールを取得
- `mcp_servers.json` の **`spell_tools[]`** で [Spell](spell.md) として登録（`visible` フラグで表示制御）
- **`spell_tools_default`** を宣言すると、サーバーが後から増やしたツールも自動で Spell 化される（ツールの入れ替えが速い外部サービス向け）
- これにより MCP ツールがペルソナの平文応答から呼べる
- `scope: "per_persona"` でペルソナ単位の独立プロセス管理に対応

> **登録の落とし穴**: `mcp_servers.json` の `spell_tools` に書かないと `spell=False` になり平文から呼べない（`spell_tools_default` を宣言したサーバーを除く）。なお `visible: false` でも呼び出し自体は可能で、system prompt のスペル一覧に出ないだけ（ペルソナは `addon_spell_help` で発見できる）。
>
> **設計方針**: SAIVerse 固有の挙動を upstream の MCP サーバーに焼き込まない。生 MCP は addon 側の native tool でラップし、生ツールは `visible:false` にする。

### 対応範囲

現状は **Tools のみ**。今後の優先度: Cancellation > Progress > Elicitation > Resources > Sampling。

## Elicitation（応答待ち・未実装）

MCP プロトコルの「応答待ち」機能。サーバーが構造化リクエストでクライアント（SAIVerse）から追加情報（承認・入力値）を引き出す。投稿系アドオン（X 等）の投稿前確認を MCP 標準で実装できるが、**現在未実装**（優先度3位）。

## 実装

- クライアント: `tools/mcp_client.py`
- 設定: `tools/mcp_config.py`、`mcp_servers.json`
- 起動時解決: `_server_meta["raw_config"]` は起動時に interpolate 済み（reconnect の再 interpolate は source JSON を再 load）

## 関連概念

- [Spell](spell.md) — MCP tool は `spell_tools[]` 経由で Spell 化される
- [Tool](tool.md) — MCP tool は Tool の一種として扱われる
- [Addon](addon.md) — MCP サーバーを内包・配布する単位

## 参照

- intent: [`mcp_addon_integration.md`](../intent/mcp_addon_integration.md) / [`mcp_protocol_coverage.md`](../intent/mcp_protocol_coverage.md)
- 地図: [`landscape.md`](../overview/landscape.md) §7
