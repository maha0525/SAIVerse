# SearXNG の導入版を固定する

ステータス: 完了 (2026-10-16 実装・Windows 隔離検証、PR #403 を 2026-10-15 ごろ develop へマージ。既存環境の挙動は変えない — 効くのは次のまっさらな導入)

## 目的と合意

2026-10-16、初回導入で SearXNG 用 Python 環境の tzdata が欠ける報告を調査した。まはーは、検証した SearXNG の版を固定し、master との乖離への対策は別途検討する方針を選んだ。調査: [tzdata の導入漏れ](../issues/windows_searxng_missing_tzdata.md)。

利用者が導入日によって異なる未検証のソースを受け取らず、Windows / Linux と Git の有無によらず同じコミットを使えることを目標にする。本番ペルソナ・世界のデータには触れない。

## 導入から利用まで

固定コミットの正典を scripts に置く。セットアップはそのコミットのソースと依存定義を取得し、SearXNG 専用 venv に導入する。Windows の環境差を補う tzdata もここへ導入し、外部コマンドの失敗を成功扱いしない。設定マージと起動側も同じソースを参照する。

正典は `scripts/searxng_pin.json`。取得は Git の有無によらず、完全な SHA を指定した公式 ZIP に一本化した。初回採用コミットは `ff8f5ddcc9f2c58d8b0276ec030f6a3adfa17525`、追加する tzdata は本体と同じ `2025.3`。上流の requirements.txt を使い、Windows 用の pwd と URL パス区切りの互換処理を施す。全推移依存を固定した lockfile を新設したものではない。

既存のソースは破棄・上書きせず、コミット別の管理ディレクトリを使う。既存 venv の導入先が違う場合は、セットアップで固定版へ切り替える。ユーザーの設定・エンジン選択は従来の保存先を使い続ける。起動時は完了記録と実際の依存を確認し、不足時はセットアップを実行する。

## 不変条件と責任

- 既定の取得先に master や可変タグを使わない。明示的な SEARXNG_REF も完全なコミット SHA に限定する。
- ソースの準備、設定生成、editable install、起動で参照するディレクトリを一致させる。
- ZIP 取得でもパッケージ作成に必要なルートファイルを欠落させず、実際の固定版を表すバージョン情報を供給する。
- 導入完了の記録は依存導入・互換処理・実 import が成功した後だけ保存する。
- 依存が欠けた既存環境を、パッケージの場所が見つかるという理由で完成扱いしない。

## 検証

固定 SHA の ZIP 取得、Git なしの新規導入、失敗時の非ゼロ終了、欠落依存の検出、再実行を検査する。Windows の実セットアップを隔離したソース・venv・設定で実行し、その起動スクリプトから localhost の HTTP 応答まで確認する。既存の本番サービスや設定は使わない。Linux 用は実行可能な範囲で共通処理とシェル構文・呼び出し経路を検査し、Windows の結果で Linux の実動作を確認済みとはしない。

### 実測結果 (2026-10-16)

- Python 3.13.2 / Windows PowerShell で、修正した setup_searxng.ps1 を新しい専用 venv へ実行して成功。requirements の導入、editable install、pip check、実 import / タイムゾーン取得まで通過。
- Git のない PATH と配布用 scripts だけのフォルダーでも既定保存先で成功。既存 `.searxng-src/searx/legacy-user-file.txt` を保持し、固定版は `.searxng-src/<commit>/` に導入。再実行では再取得・pip 再導入なしで成功。
- 専用環境から tzdata をアンインストールすると `check` は exit 1。PIP_NO_INDEX=1 による依存導入失敗を実 setup.ps1 経由で起こすと exit 1、成功表示なし、完了記録なし。
- その欠落状態から run_searxng_server.ps1 を実行すると依存を再導入して復旧。トップページと /config は HTTP 200、259 エンジンを登録。既存の secret_key、利用者のエンジン選択、bing の無効指定を保持。
- 新規配布フォルダーと復旧環境の両方で、HTML が参照する sxng-core.min.js / sxng-ltr.min.css / sxng-noscript.min.css と manifest.json がすべて HTTP 200。起動した専用プロセスは検査後に終了。
- 単体・既存回帰テスト `tests/test_searxng_installation.py tests/test_clean_windows_startup.py -n 0` は30件成功。変更 Python の ruff check、両シェルの bash -n、台帳検査も成功。
- Linux の実サーバー起動と外部検索サイトの検索結果は、この Windows での導入・HTTP 検証の範囲外。

ローカルの実行ログ: `tmp/searxng-pin-verification/{failed-setup,recovery-startup}.log`、`tmp/searxng-e2e/{fresh-setup,repeat-setup,launcher}.log`。専用作業領域は Git へ含めない。

## 経緯

最初の HTTP 試験で CSS が404になり、ファイルの存在を確かめる前に「ZIPに配布CSSがない」と進捗で述べた。実ファイルとHTMLのURLを照合するとCSSは存在し、Windowsの `\\` を含む資源一覧と `/` のURLの比較が不一致になることが原因だった。ファイル不足という説明は訂正し、列挙側を POSIX 形式に統一した同じ版で実HTTPの200を確認した。HTTPステータスだけを特定の原因の証拠にせず、生成したURLと配置先を照合する。

## 今回の対象外

上流 master との差を監視する仕組み・定期タスク・自動アップグレードは作らない。固定版の更新方針と検索サイト側の変更への対応は、別途検討する。
