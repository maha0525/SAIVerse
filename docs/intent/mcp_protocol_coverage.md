# Intent: MCP プロトコル機能の対応範囲

**ステータス**: 整理済み（実装着手は機能ごとに別 Intent Doc を起こす）

## これは何か

MCP (Model Context Protocol) は Tools だけでなく Resources / Prompts / Sampling / Elicitation / Progress notifications / Cancellation など複数の機能柱を持つプロトコル。SAIVerse は現状 **Tools 機能のみ** 実装しており、他は未対応。

本ドキュメントは「何が対応済みで、何が未対応で、どれを優先すべきか」を整理する引き継ぎ資料。具体的な実装設計ではない（優先度の高いものが実装フェーズに入る時点で別途 Intent Doc を起こす想定）。

## これは何でないか

- MCP 全機能を実装する計画書ではない
- 個別機能の実装詳細ドキュメントではない

## 現状の対応範囲

### 実装済み

| 機能 | 実装場所 | 備考 |
|------|---------|------|
| **Tools** | `tools/mcp_client.py::MCPServerConnection._discover_tools` / `call_tool` | `tools/list` + `tools/call` |
| Transports | `_connect_stdio` / `_connect_sse` / `_connect_streamable_http` | 3 方式全て対応 |
| Initialize handshake | `connect()` 内 `session.initialize()` | |
| Tool discovery | 起動時 (global) + 初回有効化時 (per_persona) | |
| エラー分類 + backoff | `_classify_error` / `_record_failure` | 6 カテゴリ、exponential backoff (2–60 秒) |
| ペルソナ別インスタンス管理 | instance_key + refcount | `docs/intent/mcp_addon_integration.md` 参照 |

### 未実装（本ドキュメントの主題）

Resources, Prompts, Sampling, Elicitation, Progress notifications, Cancellation, Roots, Logging, Completion

## 各機能の概要と SAIVerse 文脈での価値

### Cancellation

- **プロトコル側**: JSON-RPC の `$/cancelRequest` で進行中のリクエストを停止
- **SAIVerse 文脈**: spell / tool call 中にユーザーが停止できない現状を解消する汎用機能
- **価値**: 高（すべてのツール実行に広く効く）
- **実装難易度**: 中（asyncio Task キャンセル + MCP サーバーへの cancel 送信 + subprocess への signal）

### Progress notifications

- **プロトコル側**: サーバーが `$/progress` で途中経過を送信、クライアントは継続受信
- **SAIVerse 文脈**:
  - `create_image` (Elyth / Gemini / GPT)、動画生成系 MCP などで「今何 % か」が見える
  - Kitchen サブシステム (LoRA 学習などの長時間処理、`docs/intent/kitchen.md`) と設計思想が親和的
  - 通知先として AddonEvents SSE (`emit_addon_event`) に乗せると自然
- **価値**: 高
- **実装難易度**: 中（tool wrapper の戻り値経路の変更 + UI 通知チャネル）

### Elicitation

- **プロトコル側**: サーバーが構造化リクエストで追加情報をクライアントから引き出す（2025 年仕様追加）
- **SAIVerse 文脈**:
  - Elyth / X / Mastodon 等の投稿系ツールで「この内容で投稿していい？」の確認ダイアログ
  - `docs/intent/x_integration.md` §3 で議論した投稿前確認を MCP 標準に寄せられる
- **価値**: 高
- **実装難易度**: 中（UI 側で確認ダイアログの汎用コンポーネントが必要、投稿前承認フローと統合）

### Resources

- **プロトコル側**: サーバーが `resources/list` / `resources/read` で URI ベースのデータを公開
- **SAIVerse 文脈**:
  - 既存の `saiverse://` URI スキーム (`uri_resolver.py`) と統合できれば、MCP 経由で他 SAIVerse インスタンスの Memopedia / Chronicle を読む等が可能
  - ただし URI resolver の再設計と namespace 設計が要る
- **価値**: 中-高
- **実装難易度**: 中-高

### Sampling

- **プロトコル側**: サーバーがクライアントに「この context で LLM を呼んで結果返して」と依頼、クライアントが実際に LLM を呼ぶ
- **SAIVerse 文脈**: MCP サーバーが SAIVerse 側ペルソナの LLM にアクセスできる → サーバーが「賢い」動作をとれる
  - 例: Elyth MCP サーバーが「このツイート案、SAIVerse 側 AI 判断でトーン確認してから投稿」
- **リスク**:
  - 悪意あるサーバーがペルソナ LLM を延々呼んでコスト消費させる攻撃面
  - ペルソナの秘密記憶を context 経由で exfiltrate させる恐れ
- **価値**: 高だが要検討
- **実装難易度**: 高（セキュリティ境界・allowlist・レート制限の設計が先、実装はその後）

### Prompts

- **プロトコル側**: サーバーが再利用可能なプロンプトテンプレートを配る
- **SAIVerse 文脈**: SEA の Playbook とデザイン思想が被る。アドオンは既に Playbook を同梱できるので、MCP Prompts を別ルートで持つ意味が薄い
- **価値**: 中（Playbook 同梱で実質代替可能）
- **実装難易度**: 低（取得だけなら簡単）

### Roots

- **プロトコル側**: クライアントが公開するファイルシステムルートをサーバーに通知
- **SAIVerse 文脈**: SAIVerse 側から積極的にファイルシステムを公開する必要性が薄い
- **価値**: 低

### Logging

- **プロトコル側**: サーバー側ログをクライアントで受信
- **SAIVerse 文脈**: デバッグ用途。`backend.log` に統合するかどうかは別議論
- **価値**: 低-中

### Completion

- **プロトコル側**: 引数候補の補完提案
- **SAIVerse 文脈**: ペルソナが LLM で引数を決めるため補完ニーズが薄い。UI 側補完機構も未整備
- **価値**: 低

## 優先順位（次に手を付けるなら）

1. **Cancellation** — 効果範囲が広い、コスト中。spell/tool 中断の UX 改善
2. **Progress notifications** — Kitchen と統合して長時間処理の可視化、AddonEvents SSE に相乗り
3. **Elicitation** — 投稿系アドオンの安全性向上、`x_integration.md` の投稿前確認とマージ
4. **Resources** — `saiverse://` との統合設計が要る、中期課題
5. **Sampling** — セキュリティ設計を先に固める必要、allowlist 前提
6. **Prompts** 以下 — 優先度低

## SAIVerse を MCP サーバー側にする構想（追記 2026-04-25）

クライアントとして外部 MCP に接続するだけでなく、**SAIVerse 自身が MCP サーバーになる**方向も将来構想として扱う。

### ユースケース
- Claude Code（このセッション）から SAIVerse 内のペルソナと直接コミュニケーションを取る
- SAIVerse のペルソナが Claude Code に対して「こういう機能が欲しい」と提案・要望を出せる
- 他 SAIVerse インスタンス間で、Resources / Prompts / Sampling を相互に利用する経路を作る

### 提供する機能候補
- **Resources**: `saiverse://{city}/{persona}/...` の URI 空間を MCP Resources として外部公開
- **Tools**: 一部の SAIVerse ツール（memopedia 検索、chronicle 検索等）を外部から呼べる形で公開
- **Prompts**: Playbook を MCP Prompts として外部公開（Playbook と MCP Prompts の機能重複の話とは別。SAIVerse 側から外部へ提供する側に立つので意味がある）

### 設計上の難所
- **認証**: 外部からアクセスする際のペルソナ識別と権限制御
- **公開範囲**: 全 Memopedia を見せるのか、公開フラグ付きのもののみか
- **Sampling 提供側**: 外部クライアントが SAIVerse のペルソナ LLM を借りるパターン。クライアント側 Sampling と対称的なセキュリティ設計が要る
- **トランスポート**: stdio (Claude Desktop / Claude Code) と HTTP/SSE（Web 経由）両方サポートが必要

### タイミング
クライアント側 Resources 統合（`saiverse://external/...`）と並行して検討開始する。実装は中期〜長期。

## ペルソナ認知モデルとの依存関係（追記 2026-04-25）

Cancellation と Elicitation の実装は、より根本的な「ペルソナの並列行動線とメタレイヤー」の設計に依存する。これらを単なる MCP 機能として実装すると個別特化型になり、SAIVerse 全体の認知モデルと整合しない。

別途 Intent Document を起こす予定:
- `docs/intent/persona_cognitive_model.md` — 行動の線、メタレイヤー、単一主体の認知モデル
- `docs/intent/persona_action_tracks.md` — 線の永続化・切り替え・再開時の記憶復元
- 応答待ちの汎用化（旧 `persona_async_wait.md` 構想）は `persona_action_tracks.md` の「応答待ちトラックの仕組み」セクションに統合された

これらが固まってから Cancellation / Elicitation の実装に入る。

## 実装時のヒント（既知の注意点）

- **MCP 専用 event loop**: クライアントは `SAIVerse-MCP` 専用スレッド/loop を持つ。新機能でも stdio pipes や SSE 接続まわりは `run_on_mcp_loop()` 経由で実行すること。2026-04-25 の lazy start cross-loop silent fail 問題がこれ（`tools/mcp_client.py` コミット `81e0405`）。
- **per_persona スコープ**: サーバーがペルソナごとに独立プロセスなので、Progress / Elicitation 等の通知はその通知を出したインスタンスのペルソナ文脈で配信する必要がある。`instance_key` から persona_id を抽出する `_persona_id_from_instance_key` が使える。
- **Elicitation / Progress の UI 側**: AddonEvents SSE チャネル (`emit_addon_event`) に乗せるのが素直。frontend は既に `useAddonEvents` フックで購読している。
- **Cancellation の 3 段構造**: asyncio.Task cancellation だけでは subprocess は残る。subprocess signal の送信 + MCP サーバーへの `$/cancelRequest` + spell runner の state cleanup、の 3 段を考える必要。
- **Sampling のセキュリティ境界**: allowlist をアドオンの `addon.json` で宣言する形にすると自然。`params_schema` に `"sampling_allowed": true` のようなフィールドを追加する案。

## 関連

- 現状の MCP 対応機能ドキュメント: `docs/features/mcp-integration.md`
- アドオン統合設計: `docs/intent/mcp_addon_integration.md`
- 既存の投稿前確認議論: `docs/intent/x_integration.md`
- Kitchen (長時間処理サブシステム): `docs/intent/kitchen.md`
- MCP 公式仕様: https://modelcontextprotocol.io/
