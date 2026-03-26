# Tailscaleアクセス Runbook

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

## 3. スマートフォンからの接続先確認

1. スマートフォンでTailscaleアプリを開く
2. PC名をタップ
3. `Tailscale addresses` を開く
4. `MagicDNS` と表示されているアドレスを確認する

このアドレスに `:3000` を付けたものをスマートフォンからのアクセス先として使用します。

---

## 4. スマートフォンからのアクセス

### 4.1 Tailscale接続確認

1. スマートフォンでTailscaleアプリを開く
2. VPNが「Connected」になっていることを確認
3. PCがデバイスリストに表示されていることを確認

### 4.2 ブラウザでアクセス

1. スマートフォンのブラウザを開く
2. 手順3で確認した `MagicDNS` のアドレスに `:3000` を付けて入力:

```
http://my-laptop.my-tailnet.ts.net:3000
```

3. SAIVerseのUIが表示されれば成功

---

## 5. 終了手順

### 5.1 SAIVerseの停止

1. バックエンド/フロントエンドのターミナルで `Ctrl+C` を押下
2. または、`start.bat` / `start.sh` で開いたウィンドウを閉じる

---

## 6. トラブルシューティング

### 7.1 アクセスできない場合

| 確認項目 | 確認方法 |
|----------|----------|
| Tailscale接続状態 | PC・スマホ両方でTailscaleアプリを確認 |
| 同一Tailnet参加 | Tailscale管理画面でデバイス一覧を確認 |
| SAIVerse起動状態 | `http://localhost:3000` でPC上で動作確認 |
| MagicDNSアドレス | スマホのTailscaleアプリでPC名をタップして確認 |

### 6.2 ページが空白・アセットエラー

| 確認項目 | 対策 |
|----------|------|
| localhostハードコード | フロントエンドコードで絶対パスを使用していないか確認 |
| URL形式 | `MagicDNS:3000` でアクセスしているか確認 |
| 環境変数 | `NEXT_PUBLIC_API_URL` 等の設定を見直し |

### 6.3 ポート競合

ポート3000が使用されている場合:

```bash
# ポートを指定して起動
npm run dev -- -p 3001

# スマホからは MagicDNS に :3001 を付けてアクセス
```

---

## 7. コマンドリファレンス

### よく使うコマンド

| 操作 | Windows | macOS/Linux |
|------|---------|-------------|
| デバイス一覧 | `tailscale status` | 同左 |
| SAIVerse起動 | `.\start.bat` | `./start.sh` |

---

## 8. セキュリティベストプラクティス

1. **Tailnet ACLの設定**: 不要なデバイスからのアクセスを制限
2. **定期的なデバイス確認**: Tailscale管理画面で不要なデバイスを削除
3. **APIキーの管理**: `.env` ファイルを適切に管理
4. **バックアップ**: 定期的に `~/.saiverse/` をバックアップ

---

## 9. 関連リンク

- [Tailscale公式ドキュメント](https://tailscale.com/kb/)

---

## 変更履歴

| 日付       | バージョン | 変更内容           |
|------------|------------|--------------------|
| 2026-03-07 | 1.0.0      | 初版作成           |
