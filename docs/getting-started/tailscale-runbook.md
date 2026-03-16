# Tailscale PWAアクセス Runbook

SAIVerseをTailscale経由でスマートフォン・タブレットからアクセスするための運用手順書。

---

## 前提条件

- [x] SAIVerseがインストール済み
- [x] Python 3.11-3.13 がインストール済み
- [x] Node.js 18以上がインストール済み
- [x] SAIVerseがローカルで正常に動作している

---

## 1. Tailscaleのインストール

### 1.1 PCへのインストール

1. [Tailscale公式サイト](https://tailscale.com/download)からダウンロード
2. Windows: インストーラーを実行
3. macOS: `.dmg` ファイルからインストール
4. Google、Microsoft、GitHub等のアカウントでサインアップ/ログイン

### 1.2 スマートフォンへのインストール

1. App Store (iOS) または Google Play Store (Android) で「Tailscale」を検索
2. インストールして起動
3. PCと同じアカウントでログイン

---

## 2. SAIVerseの起動

### 2.1 Windows

```powershell
# プロジェクトディレクトリに移動
cd C:\path\to\SAIVerse

# 起動
.\start.bat
```

または手動起動:

```powershell
# 仮想環境を有効化
.\.venv\Scripts\Activate.ps1

# バックエンド起動
python main.py city_a

# 別ターミナルでフロントエンド起動
cd frontend
npm run dev
```

### 2.2 macOS / Linux

```bash
# プロジェクトディレクトリに移動
cd /path/to/SAIVerse

# 起動
./start.sh
```

または手動起動:

```bash
# 仮想環境を有効化
source .venv/bin/activate

# バックエンド起動
python main.py city_a

# 別ターミナルでフロントエンド起動
cd frontend
npm run dev
```

### 2.3 起動確認

ブラウザで `http://localhost:3000` を開き、SAIVerseのUIが表示されることを確認。

---

## 3. Tailscale Serveの設定

### 3.1 Windows (管理者権限必要)

1. **Windows Terminal** または **PowerShell** を管理者として実行
2. 以下のコマンドを実行:

```powershell
tailscale serve --bg localhost:3000
```

### 3.2 macOS / Linux

```bash
tailscale serve --bg localhost:3000
```

### 3.3 公開URLの確認

```powershell
# Windows / macOS / Linux 共通
tailscale serve status
```

出力例:

```
https://my-laptop.my-tailnet.ts.net
```

このURLがスマートフォンからのアクセスに使用します。

---

## 4. スマートフォンからのアクセス

### 4.1 Tailscale接続確認

1. スマートフォンでTailscaleアプリを開く
2. VPNが「Connected」になっていることを確認
3. PCがデバイスリストに表示されていることを確認

### 4.2 ブラウザでアクセス

1. スマートフォンのブラウザを開く
2. 手順3.3で確認したURLを入力:

```
https://my-laptop.my-tailnet.ts.net
```

3. SAIVerseのUIが表示されれば成功

---

## 5. PWAインストール

### 5.1 iPhone / iPad (Safari)

1. 上記URLをSafariで開く
2. 共有ボタン（四角から矢印が出るアイコン）をタップ
3. 「ホーム画面に追加」をタップ
4. 名前を確認して「追加」をタップ

### 5.2 Android (Chrome)

1. 上記URLをChromeで開く
2. メニューボタン（︙）をタップ
3. 「アプリをインストール」または「ホーム画面に追加」をタップ
4. 確認画面で「インストール」をタップ

### 5.3 macOS (Safari)

1. 上記URLをSafariで開く
2. メニューバーから「ファイル」→「Dockに追加」
3. または、アドレスバーの共有ボタンから「Dockに追加」

### 5.4 macOS (Chrome/Edge)

1. 上記URLを開く
2. アドレスバー右端のインストールアイコンをクリック
3. またはメニューから「SAIVerseをインストール」

---

## 6. 終了手順

### 6.1 Tailscale Serveの停止

```powershell
# Windows / macOS / Linux 共通
tailscale serve reset
```

### 6.2 SAIVerseの停止

1. バックエンド/フロントエンドのターミナルで `Ctrl+C` を押下
2. または、`start.bat` / `start.sh` で開いたウィンドウを閉じる

---

## 7. トラブルシューティング

### 7.1 アクセスできない場合

| 確認項目 | 確認方法 |
|----------|----------|
| Tailscale接続状態 | PC・スマホ両方でTailscaleアプリを確認 |
| 同一Tailnet参加 | Tailscale管理画面でデバイス一覧を確認 |
| SAIVerse起動状態 | `http://localhost:3000` でPC上で動作確認 |
| Tailscale Serve状態 | `tailscale serve status` コマンドを実行 |

### 7.2 PWAインストールできない場合

| 原因 | 対策 |
|------|------|
| HTTP接続 | Tailscale Serveの `https://...ts.net` URLを使用 |
| ブラウザ非対応 | Safari (iOS) / Chrome (Android) を使用 |
| PWA非対応 | Next.jsのPWA設定を確認 |

### 7.3 ページが空白・アセットエラー

| 確認項目 | 対策 |
|----------|------|
| localhostハードコード | フロントエンドコードで絶対パスを使用していないか確認 |
| ミックスコンテンツ | HTTPS→HTTPのリクエストが発生していないか確認 |
| 環境変数 | `NEXT_PUBLIC_API_URL` 等の設定を見直し |

### 7.4 ポート競合

ポート3000が使用されている場合:

```bash
# ポートを指定して起動
npm run dev -- -p 3001

# Tailscale Serveも変更
tailscale serve --bg localhost:3001
```

---

## 8. コマンドリファレンス

### よく使うコマンド

| 操作 | Windows | macOS/Linux |
|------|---------|-------------|
| Tailscale Serve開始 | `tailscale serve --bg localhost:3000` | 同左 |
| ステータス確認 | `tailscale serve status` | 同左 |
| Serve停止 | `tailscale serve reset` | 同左 |
| デバイス一覧 | `tailscale status` | 同左 |

### Tailscale Serve オプション

| オプション | 説明 |
|------------|------|
| `--bg` | バックグラウンドで実行 |
| `--http` | HTTPで公開（非推奨） |
| `--https` | HTTPSで公開（デフォルト） |

---

## 9. セキュリティベストプラクティス

1. **Tailnet ACLの設定**: 不要なデバイスからのアクセスを制限
2. **定期的なデバイス確認**: Tailscale管理画面で不要なデバイスを削除
3. **APIキーの管理**: `.env` ファイルを適切に管理
4. **バックアップ**: 定期的に `~/.saiverse/` をバックアップ

---

## 10. 関連リンク

- [Tailscale公式ドキュメント](https://tailscale.com/kb/)
- [Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve)
- [PWAの概要](https://web.dev/progressive-web-apps/)

---

## 変更履歴

| 日付       | バージョン | 変更内容           |
|------------|------------|--------------------|
| 2026-03-07 | 1.0.0      | 初版作成           |