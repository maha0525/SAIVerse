# SAIVerse ドキュメント

SAIVerseの公式ドキュメントへようこそ。

## 📖 目次

### はじめに
- [インストール](./getting-started/installation.md) - 環境構築手順
- [クイックスタート](./getting-started/quickstart.md) - 10分で動かす
- [設定](./getting-started/configuration.md) - 環境変数・オプション設定
- [Tailscale Runbook](./getting-started/tailscale-runbook.md) - スマホアクセス設定手順

### 基本概念

> 全体像は [俯瞰地図 landscape.md](./overview/landscape.md)、概念索引は [concepts/README.md](./concepts/README.md)。

**世界の構成**
- [ペルソナ](./concepts/persona.md) - 考え・選択し・行動する AI 主体
- [City と Building](./concepts/building-city.md) - 共有メッセージ場と世界の構造
- [Item](./concepts/item.md) - 物と拡張中の存在論（Fixture / Observer / Vessel）

**駆動と行動**
- [Pulse / PulseController](./concepts/pulse.md) - 認知サイクルと起動制御
- [Track / Handler](./concepts/track.md) - 進行中の作業文脈（行動の線）
- [Meta-Judgment](./concepts/meta-judgment.md) - どの Track を動かすか
- [line / aspect](./concepts/line.md) - 処理レーンとキャッシュ制御
- [Beat](./concepts/beat.md) - 最小行動単位
- [Playbook](./concepts/playbook.md) - 構造化された行動フロー
- [Spell](./concepts/spell.md) - 平文応答から Tool を呼ぶ構文
- [Tool](./concepts/tool.md) - 実行の単位
- [Phenomena](./concepts/phenomena.md) - 世界側からのイベント入口

**記憶**
- [SAIMemory](./concepts/saimemory.md) - 長期記憶の容れ物
- [Chronicle](./concepts/chronicle.md) - 時系列圧縮 / Track 再開
- [Memopedia](./concepts/memopedia.md) - 知識グラフ
- [Session / head](./concepts/session.md) - 短期記憶
- [Metabolism / Anchor](./concepts/metabolism.md) - 短期リフレッシュ + 長期結晶化の節目

**拡張**
- [Addon](./concepts/addon.md) - 拡張点を束ねる配布単位
- [MCP / Elicitation](./concepts/mcp.md) - 外部ツールサーバー接続
- [SDS](./concepts/sds.md) - 複数 City の発見（冬眠中）

### ユーザーガイド
- [ワールドビュー](./user-guide/world-view.md) - メイン画面の使い方
- [街マップ](./user-guide/city-map.md) - City の空間マップ
- [アイテムとファイル](./user-guide/items-and-files.md) - アイテム・添付・右サイドバー
- [チャットオプション](./user-guide/chat-options.md) - モデル・送信量・キャッシュ
- [ワールドエディタ](./user-guide/world-editor.md) - 世界の編集
- [ペルソナ設定](./user-guide/persona-settings.md) - ペルソナごとの設定
- [グローバル設定](./user-guide/global-settings.md) - 全体設定・モデル管理
- [メモリービュー](./user-guide/memory-view.md) - 記憶モーダル（全タブ）
- [Memopedia](./user-guide/memopedia.md) - ナレッジベースの使い方

### 機能詳細
- [自律行動モード](./features/autonomous-mode.md) - パルス駆動の仕組み
- [都市間連携](./features/inter-city.md) - マルチシティ構成
- [ツールシステム](./features/tools-system.md) - AIが使えるツール
- [Playbook/SEA](./features/playbooks.md) - 行動パターン定義
- [MCP連携](./features/mcp-integration.md) - 外部ツールサーバー接続
- [Discord連携](./features/discord-gateway.md) - Discordとの接続
- [Unity Gateway](./features/unity-gateway.md) - 3D/VR空間連携

### 開発者ガイド
- [コントリビューション](./developer-guide/contributing.md) - 貢献の方法
- [プロジェクト構造](./developer-guide/project-structure.md) - ディレクトリ構成
- [ツールの追加](./developer-guide/adding-tools.md) - 新しいツールの実装
- [Playbook作成](./developer-guide/creating-playbooks.md) - 独自Playbookの作成
- [テスト](./developer-guide/testing.md) - テストの実行

### リファレンス

> `database-schema` / `api-endpoints` / `tool-catalog` はコードから自動生成（[gen_reference_docs.bat](../gen_reference_docs.bat)・手編集禁止）。他は手書き。

- [データベース設計](./reference/database-schema.md) - 全テーブル定義（自動生成）
- [APIエンドポイント](./reference/api-endpoints.md) - REST API 全一覧（自動生成）
- [ツールカタログ](./reference/tool-catalog.md) - 全ツール（自動生成）
- [Playbook カタログ](./reference/playbook-catalog.md) - 同梱 Playbook 一覧
- [プロバイダ一覧](./reference/providers.md) - LLM 接続先定義
- [saiverse:// URI](./reference/saiverse-uri.md) - リソース参照スキーム
- [環境変数](./reference/environment-vars.md) - 設定変数
- [スクリプト](./reference/scripts.md) - 保守スクリプト

---

## 🔗 クイックリンク

- [README](../README.md) - プロジェクト概要
- [俯瞰地図 landscape.md](./overview/landscape.md) - 概念どうしの関係の全体像
- [進捗マップ roadmap_status.md](./overview/roadmap_status.md) - 何が予定され、いまどこにいるか
- [後回し案件 issues/](./issues/) - 未解決の課題管理
