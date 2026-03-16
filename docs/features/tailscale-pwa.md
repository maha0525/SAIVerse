# Tailscale PWAアクセス機能 仕様書

## 概要

SAIVerseをローカル環境で実行しつつ、Tailscaleを経由してスマートフォンやタブレットから安全にアクセスし、PWA（Progressive Web App）としてインストール可能にする機能。

---

## 1. 目的と背景

### 1.1 目的

- **ローカルファースト**: ユーザーデータをローカルに保持しつつ、外出先からもアクセス可能にする
- **セキュアアクセス**: 公開ポートを開放せず、Tailscaleの暗号化ネットワーク経由で安全にアクセス
- **PWA対応**: スマートフォンでネイティブアプリのような体験を提供

### 1.2 背景

- SAIVerseはAIパートナーと長期的な関係を築くアプリケーションであり、外出先からも継続的に利用したいニーズがある
- クラウドホスティングはコストとプライバシーの観点から避けたい
- Tailscaleを使えば、自宅PCをサーバーとして、外出先から安全にアクセス可能

---

## 2. 機能要件

### 2.1 対応プラットフォーム

| プラットフォーム | サポート状況 |
|------------------|--------------|
| Windows 10/11    | 完全対応      |
| macOS            | 完全対応      |
| iOS (Safari)     | PWAインストール対応 |
| Android (Chrome) | PWAインストール対応 |

### 2.2 アクセス方式

#### 2.2.1 Tailscale Serve（推奨）

```
https://<machine>.<tailnet>.ts.net
```

- 自動HTTPS証明書取得
- PWAインストールに必要なHTTPS要件を満たす
- ポート転送不要

#### 2.2.2 直接Tailscale IPアクセス（非推奨）

```
http://<tailscale-ip>:3000
```

- HTTPSではないためPWAとしてインストール不可
- 一部のブラウザで制限される可能性あり

### 2.3 必要なポート

| 用途                | デフォルトポート |
|---------------------|------------------|
| Next.js フロントエンド | 3000            |
| FastAPI バックエンド   | 8000 (city_a)    |

### 2.4 PWA要件

- HTTPS接続（Tailscale Serveで提供）
- `manifest.json` の適切な設定
- Service Workerの登録
- 適切なアイコンサイズの提供

---

## 3. 技術仕様

### 3.1 Tailscale Serve 設定

#### Windows (PowerShell 管理者権限)

```powershell
tailscale serve --bg localhost:3000
```

#### macOS / Linux

```bash
tailscale serve --bg localhost:3000
```

### 3.2 URL形式

Tailscale Serveで公開されるURL:

```
https://<machine-name>.<tailnet-name>.ts.net
```

- `<machine-name>`: PCのTailscaleマシン名（設定で変更可能）
- `<tailnet-name>`: Tailscaleアカウント固有のネットワーク名

### 3.3 フロントエンド要件

Next.jsフロントエンドは以下の条件を満たす必要がある:

1. **相対パス使用**: APIリクエストは相対パスで行う
2. **ハードコードされたlocalhost回避**: `localhost:3000`等の固定URLを使用しない
3. **環境変数による設定**: 必要に応じて `NEXT_PUBLIC_API_URL` で調整

### 3.4 CORS設定

バックエンド（FastAPI）は以下のCORS設定を推奨:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 開発用。本番は具体的なオリジンを指定
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

---

## 4. 制約事項

### 4.1 技術的制約

- PCが起動している間のみアクセス可能
- スリープモードの場合、Wake-on-LAN等の別途設定が必要
- 同一Tailnetに参加しているデバイスからのみアクセス可能

### 4.2 既知の問題

- 複数端末で同時アクセスすると表示が崩れる場合がある（改善予定）
- 大きなファイルのアップロードでタイムアウトする可能性

---

## 5. セキュリティ考慮事項

### 5.1 認証

- Tailscale自体が認証層として機能
- Tailnetに参加できるユーザーのみアクセス可能
- 追加の認証が必要な場合は別途実装

### 5.2 データ保護

- 通信はTailscaleにより暗号化
- ユーザーデータはローカルの `~/.saiverse/` に保存
- APIキー等の機密情報は `.env` ファイルで管理

### 5.3 推奨設定

- TailscaleのACLで必要最小限のアクセス権を設定
- 定期的にTailscaleのデバイスリストを確認

---

## 6. トラブルシューティング

### 6.1 アクセスできない場合

1. 両デバイスがTailscaleにログインしているか確認
2. 同一Tailnetに参加しているか確認
3. `tailscale serve status` で公開状態を確認
4. ローカルアプリが実行中か確認

### 6.2 PWAインストールできない場合

1. HTTPS URLを使用しているか確認（`*.ts.net`）
2. `http://<tailscale-ip>:3000` ではなく、Tailscale ServeのURLを使用
3. ブラウザがPWAをサポートしているか確認

### 6.3 アセットが読み込まれない場合

1. フロントエンドコードで `localhost` がハードコードされていないか確認
2. `VITE_`、`NEXT_PUBLIC_` 環境変数の設定を確認
3. ミックスコンテンツ（HTTPS→HTTP）が発生していないか確認

---

## 7. 関連ドキュメント

- [Tailscale Runbook](../getting-started/tailscale-runbook.md) - セットアップ手順書
- [インストールガイド](./installation.md) - 環境構築の詳細
- [設定](./getting-started/configuration.md) - 環境変数設定

---

## 変更履歴

| 日付       | バージョン | 変更内容           |
|------------|------------|--------------------|
| 2026-03-07 | 1.0.0      | 初版作成           |