# Discordアーキテクチャ実装〜リリース チェックリスト

Discordベースアーキテクチャの仕様（`docs/implementation_discord.md`）をもとに、実装完了からリリースまでの作業を段階的に整理したチェックリストです。各フェーズの出口条件を満たしながら順番に進めてください。

## フェーズ0: キックオフ & 前提条件整理
- [ ] 仕様ドキュメントを精読し、未確定事項の洗い出しとオーナー割り当てを完了する（`docs/implementation_discord.md:1-172`）
- [ ] Discord開発者ポータル、ホスティング、機密情報管理など外部依存の準備計画をレビューする（`docs/implementation_discord.md:65-171`）
- [ ] 技術スタック・ライブラリ（`discord.py`、ASGI、WebSocketクライアント等）を確定し PoC スコープを合意する
- [ ] リスク / レート制限 / 法務観点のチェックリストを整備し、対応方針に合意する

**Exit Criteria**
- 開発計画と責任分担表が共有され、主要マイルストーンがコミットされている
- Discordアプリ/サーバ利用条件とホスティング制約についてステークホルダー間で合意済み

## フェーズ1: SAIVerse Bot基盤構築
- [ ] Discordアプリケーション・Botの作成と必要なIntent/権限の有効化（`docs/implementation_discord.md:65-71`）
- [x] Bot用コードベースとCIセットアップ（lint/format/ユニットテスト）
- [x] WebSocketサーバ（複数クライアント接続・認証・再接続ハンドリング）の実装（`docs/implementation_discord.md:65-68`）
- [x] 構造情報とトークンを保存するデータベース設計／マイグレーション適用（`docs/implementation_discord.md:68-115`）
- [ ] Render 等 PaaS へのデプロイパイプライン構築と Secrets 連携（`docs/implementation_discord.md:71`）

**Exit Criteria**
- ステージング環境でBotがDiscordイベントを受信・応答し、WebSocket経由でラウンドトリップできる
- 監視・ログ収集が有効化され、障害時のアラート経路が定義済み

## フェーズ2: 認証・セキュリティ実装
- [x] OAuth2認可コードフローのWeb UXとバックエンド実装（`docs/implementation_discord.md:134-155`）
- [x] SAIVerse認証トークンの発行・ハッシュ保存・失効API整備（`docs/implementation_discord.md:147-155`）
- [x] `wss://`接続時のトークン検証と資格情報マネージャ連携ガイドの実装（`docs/implementation_discord.md:125-159`）
- [x] 入力値サニタイズ・レート制御・監査ログなど追加の防御策を適用（`docs/implementation_discord.md:125-132`）
- [x] セキュリティテスト（脆弱性スキャン／認証フロー異常系／トークン漏洩シナリオ）計画の策定

**Exit Criteria**
- OAuth2 → トークン発行 → ローカル登録 → 再接続の一連フローが自動テストで成功している
- セキュリティレビュー結果が承認済みで、重大指摘がクローズされている

## フェーズ3: `DiscordGateway` モジュール実装（単体完結）
- [x] `discord_gateway` ディレクトリのスキャフォールドと設定ファイルを充実させる（`docs/implementation_discord.md:179-197`）
- [x] Gateway内部のWebSocketクライアント／再接続／キュー連携ロジックを実装する（`docs/implementation_discord.md:200-207`）
- [x] `.env` や設定ドキュメントに Discord / OAuth 関連項目を追記し、ローカル起動手順を更新する
- [x] Gatewayモジュールの単体・結合テスト雛形を追加し、CIに統合する（`docs/implementation_discord.md:209-216`）

**Exit Criteria**
- Gateway単体で Discord イベント → 内部イベント変換 → 発話コマンド送信のループが自動テストで検証済み
- 主要コンポーネントのカバレッジが目標値を満たし、Gateway専用CIが安定して緑化している

## フェーズ4: コア機能フロー実装
- [ ] Public/Private City と Building へのイベントマッピング実装（`docs/implementation_discord.md:81-98`）
- [ ] 会話進行ハンドリング（訪問Persona調停、次発言者決定、Bot中継）の実装（`docs/implementation_discord.md:93-99`）
- [ ] 記憶持ち帰りプロトコル（ハンドシェイク・チャンク・検証・リトライ）の実装（`docs/implementation_discord.md:100-119`）
- [ ] 招待制コミュニティとロールベース権限の整備（`docs/implementation_discord.md:89-92`, `docs/implementation_discord.md:123-131`）
- [ ] Bot／ローカル双方で再接続・リプレイ・エラーハンドリングを備え、イベント欠落時の再同期仕様を確定する

**Exit Criteria**
- Persona訪問・会話・記憶同期など代表的なストーリーがエンドツーエンドで成功する
- エラー／再送シナリオの仕様と実装がドキュメント化され、レビュー済みである

## フェーズ5: テスト & 品質保証
- [ ] Gateway／Bot双方でユニット・統合・負荷テストを自動化（`docs/implementation_discord.md:209-216`）
- [ ] 記憶同期プロトコルの大容量試験とエッジケース検証（チャンク欠損・リトライ等）を実施
- [ ] Discordレートリミット・接続上限を想定した負荷試験とチューニングを実施
- [ ] セキュリティペネトレーションテスト／脆弱性スキャン／依存関係更新確認を実行（`docs/implementation_discord.md:125-132`）
- [ ] ドキュメント・ユーザーフローの手動受け入れテストを実施し、フィードバックを反映

**Exit Criteria**
- 重要テストがCI/CDに組み込まれ、継続的に合格している
- 既知の重大バグ／性能問題が解消され、受け入れサインオフを取得済み

## フェーズ6: 本体統合（リリース直前に実施）
- [ ] SAIVerse本体（`main.py` 等）に Gateway 初期化と `asyncio.Queue` ブリッジを追加する（`docs/implementation_discord.md:202-207`）
- [ ] 本体側設定ファイル／起動スクリプト／テレメトリ連携を更新し、Gateway依存を明示する
- [ ] 本体側の統合テスト（最低限の回帰テストを含む）を最終リリースブランチで実行する

**Exit Criteria**
- 本体とGatewayの最新ブランチが衝突なくマージされ、統合テストが成功している
- 共同開発者とのコンフリクトが解消された状態で、リリース候補ブランチが確定している

## フェーズ7: デプロイ準備 & リリース運用
- [ ] ステージング環境で本番相当構成のリハーサルデプロイと回帰テストを実施する
- [ ] モニタリング／アラート／ダッシュボード整備とRunbook策定（`docs/implementation_discord.md:65-171`）
- [ ] ロールバック手順とトークン失効緊急対応フローを文書化（`docs/implementation_discord.md:147-155`）
- [ ] `#announcements` 運用ポリシーとメンテナンス連絡テンプレートを作成（`docs/implementation_discord.md:165-170`）
- [ ] リリースノートとユーザー向けセットアップガイドを公開準備する

**Exit Criteria**
- 本番デプロイ手順が自動／半自動化され、ロールバック試験も成功している
- コミュニケーション計画とサポート窓口が稼働準備完了している

## フェーズ8: ポストリリース & 継続運用
- [ ] 本番リリースを実施し、初期モニタリングでメトリクス／ログを確認して異常対応する
- [ ] ユーザーフィードバック／サポート問い合わせを収集するチャンネル運用を開始する
- [ ] 定期的な依存アップデート・脆弱性スキャン・証明書更新のスケジュールを策定（`docs/implementation_discord.md:130-132`）
- [ ] リリース後レトロスペクティブを実施し、次リリースへの改善項目を整理する
- [ ] SLA/SLOレポートとバックアップ／復旧テストを定期レビューする

**Exit Criteria**
- 初回運用サイクル（監視・サポート・改善計画）が定常運用に乗り、KPIが目標レンジに収束している
- 継続的改善のロードマップが最新化され、次フェーズのタスクへ反映されている

