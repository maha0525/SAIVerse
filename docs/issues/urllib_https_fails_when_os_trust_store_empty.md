# macOS で OS の証明書が読めず、urllib 経由の通信がすべて失敗する

**状態**: 実装済み・検証待ち (2026-09-02 実装。報告者の macOS 環境での再確認が残る)
**起票**: 2026-09-02 (v0.3.1 利用者の「カタログ取得に失敗しました」報告を、診断ツールで特定)
**関連**: `saiverse/tls_trust.py` (新設)、`saiverse/addon_registry.py`、`saiverse/addon_installer.py`、`api/routes/system.py`

## 症状

アドオンカタログを開くと失敗する。

```
registry fetch failed: 503 {"detail":"failed to fetch registry from
https://raw.githubusercontent.com/maha0525/saiverse-addon-registry/main/registry.json:
<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
unable to get local issuer certificate (_ssl.c:1010)>"}
```

同じバージョンの他のユーザー (Windows) は問題なくカタログを見られる。

## 実測 (2026-09-02、報告者の環境)

macOS (Darwin 25.5.0) / SAIVerse 0.3.1 / Python 3.12.10。診断ツールの結果:

| 確認 | 結果 |
|---|---|
| 本体と同じ方法 (OS の証明書ストア) で接続 | **失敗** — 読めた証明書 **0 個** |
| 同梱の証明書束 (certifi) で接続 | 成功 |
| 相手が名乗った証明書の発行元 | `CN=YR1, O=Let's Encrypt, C=US` (正規のもの) |

通信そのものは正常で、**検証に使う根拠が 1 つも無い**状態だった。

## 原因

macOS の Python (python.org 版・pyenv ビルド等) は、OpenSSL からキーチェーンを
読めない。そのため `ssl.create_default_context()` が証明書を 1 枚も持たず、
`urllib` 経由の HTTPS が**接続先を問わず必ず**失敗する。

Windows は `ssl.enum_certificates()` で OS のストアを読めるので、この症状が出ない。
だから「Windows ユーザーは全員問題なし、macOS ユーザーは全滅」という分かれ方になる。

`requests` / `httpx` は自前で certifi を見るため影響を受けない。壊れるのは標準
ライブラリの `urllib` を直接使っている経路だけで、バックエンドでは 3 つ:

| 場所 | 機能 |
|---|---|
| `saiverse/addon_registry.py` | アドオンカタログの取得 |
| `saiverse/addon_installer.py` | アドオン本体のダウンロード |
| `api/routes/system.py` | 最新リリースの確認 / お知らせの取得 |

報告はカタログについてだけだったが、**同じ理由でアドオンのインストールも、更新の
通知も、お知らせも失敗していた**はず。

`scripts/update_engine.py` も `urlopen` を使うが、こちらは再起動後の localhost
ヘルスチェック (TLS を使わない) なので対象外。

## 直したこと

`saiverse/tls_trust.py` を新設し、起動時に一度だけ判断する。呼び出し箇所を数え
上げて 1 つずつ context を渡す形は採らない — 数え落とした経路が黙って壊れたまま
になるため。

- OS のストアから証明書を読めるなら**何もしない**。会社や学校のネットワーク、
  ウイルス対策ソフトが通信を検査している環境では、その中間 CA は OS のストアに
  だけ入っている。無条件に certifi へ差し替えると、**いま繋がっている環境を壊す**。
- 読めない (0 枚) ときだけ、同梱の certifi を既定の信頼元にする。

certifi は `requests` の依存として必ず入るので、追加インストールは要らない。

### 効かなかった方法 (記録)

最初 `ssl._create_default_https_context` を差し替えたが、**効かなかった**。
`http.client` が import 時にその値を自分のモジュール変数へ写しており、後から
`ssl` 側を変えても参照されない。テストで実際に `urlopen` を通して初めて分かった
(差し替えただけで通ったことにしていたら、直っていないものを直したと報告していた)。

採ったのは `urllib.request.install_opener` に証明書束付きの `HTTPSHandler` を
入れる形。`urlopen` も `urlretrieve` もこの opener を通るので、アドオン本体の
ダウンロードまで一緒に直る。あわせて `SSL_CERT_FILE` も立てる (すでに指定が
あれば利用者の指定を優先)。

テスト: `tests/test_tls_trust_fallback.py`。ローカルに使い捨ての HTTPS サーバーを
立て、**切り替えたあとに `urlopen` が実際に通る**ところまで確認する (外部へは
接続しない)。

## 残っている宿題

- **診断ツールの判定文が Windows 前提だった。** macOS の報告者に「Windows Update を
  すべて適用すると直ることが多いです」と表示させてしまった。ツールは
  リポジトリ外 (スクラッチパッド) にあり、正式に配る形になっていない。
- 通信を検査するソフト・機器が挟まっている環境 (OS のストアには入っているが
  certifi には無い) は、この修正の対象外。OS 側が読めていればそのまま通るので
  退行はしないが、**OS 側も読めない macOS + 通信検査**の組み合わせは救えない。
  `truststore` を入れて OS ネイティブの検証を使えば両方カバーできるが、依存が
  1 つ増えるので実例が出るまで見送る。
