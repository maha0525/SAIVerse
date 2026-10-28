# SearXNG の導入版を固定する

ステータス: 完了 (2026-10-16 実装・Windows 隔離検証、PR #403 を 2026-10-15 ごろ develop へマージ。既存環境の挙動は変えない — 効くのは次のまっさらな導入。2026-10-22 の依存の下限の上書きは `installer_revision` を上げたので、既存の導入も次の setup・起動で入れ直す)

## 目的と合意

2026-10-16、初回導入で SearXNG 用 Python 環境の tzdata が欠ける報告を調査した。まはーは、検証した SearXNG の版を固定し、master との乖離への対策は別途検討する方針を選んだ。調査: [tzdata の導入漏れ](../issues/windows_searxng_missing_tzdata.md)。

利用者が導入日によって異なる未検証のソースを受け取らず、Windows / Linux と Git の有無によらず同じコミットを使えることを目標にする。本番ペルソナ・世界のデータには触れない。

## 導入から利用まで

固定コミットの正典を scripts に置く。セットアップはそのコミットのソースと依存定義を取得し、SearXNG 専用 venv に導入する。Windows の環境差を補う tzdata もここへ導入し、外部コマンドの失敗を成功扱いしない。設定マージと起動側も同じソースを参照する。

正典は `scripts/searxng_pin.json`。取得は Git の有無によらず、完全な SHA を指定した公式 ZIP に一本化した。初回採用コミットは `ff8f5ddcc9f2c58d8b0276ec030f6a3adfa17525`、追加する tzdata は本体と同じ `2025.3`。上流の requirements.txt を使い、Windows 用の pwd と URL パス区切りの互換処理を施す。全推移依存を固定した lockfile を新設したものではない。

既存のソースは破棄・上書きせず、コミット別の管理ディレクトリを使う。既存 venv の導入先が違う場合は、セットアップで固定版へ切り替える。ユーザーの設定・エンジン選択は従来の保存先を使い続ける。起動時は完了記録と実際の依存を確認し、不足時はセットアップを実行する。SAIVerse の更新のときも、`update_engine.py` の `update_dependencies` が同じ確認とセットアップを行う。画面の「Update」ボタンの更新では、その前に SearXNG のサーバーを止め、終わったら起動し直す (2026-10-23、[update_completes_with_new_code.md](update_completes_with_new_code.md))。別の Python の系列で作られた venv は退避して作り直し、失敗したら退避した venv を元に戻す。セットアップの失敗は SAIVerse のフォルダの `.searxng_setup_failed.json` に書き、本体の画面のバナーに出す (次の成功で消える。[dependency_management.md](dependency_management.md) §2-7)。退避の直後に窓を閉じるなどで止まり、退避先の記録 (`.setup_in_progress/searxng-venv.json`) だけが残ったときも、起動時のバナーに出し、次の更新・start.bat での起動が本体の Python で入れ直しを引き継ぐ (2026-10-23)。

### 依存の下限の上書き (2026-10-22)

上流の requirements.txt は、pip install の前に `searxng_install.py` の `prepare_requirements()` が書き換える。`REQUIREMENT_FLOORS` の各パッケージについて、その行を `名前>=下限` に置き換え、行が無ければ末尾に足す。二回適用しても結果は変わらず、書き込みは一時ファイルから `os.replace` で差し替える。SearXNG の setup.py は requirements.txt から install_requires を作るので、`pip install -e` のメタデータにも同じ下限が入り、最後の `pip check` と食い違わない。

- certifi は `>=2026.10.19`。コミット `ff8f5ddcc` は `certifi==2026.7.22` を固定していて、鍵漏えいで外された emSign のルート CA (GHSA-6q48-6c7w-9gw2) をまだ信頼する。SSL_CERT_FILE などの指定が無いとき、curl_cffi は外部の検索エンジンへの TLS 接続を certifi の CA で検証する。
- anyio は `>=4.14.2` (CVE-2026-63374、TLS の証明書なりすまし)。コミット `ff8f5ddcc` は anyio を要求しないが、古い SearXNG ソースから最初に導入した venv には httpx と anyio が残る (pip install -r は既存のパッケージを消さない) ので、その残りを引き上げる。

この上書きは `installer_revision` を `2` に上げて入れた。完了記録の `installer_revision` が一致しない既存の導入は、次の setup・起動で依存を入れ直す。`searxng_pin.json` のコミット自身が修正済みの版を要求するようになったら、その項目を `REQUIREMENT_FLOORS` から外す (2026-10-22 時点で上流 master も certifi 2026.7.22 のまま)。

### ダウンロード時の証明書の切り替え (2026-10-26)

OS の証明書ストアが空の macOS (python.org 版 Python の既知の挙動) では、固定版の ZIP の取得 (`download_searxng_source.py` の urllib) が `CERTIFICATE_VERIFY_FAILED` で失敗する。`searxng_install.py` の `main()` の先頭で `saiverse/tls_trust.py` の `ensure_default_https_trust()` を呼び、その環境でだけ、検証に使う証明書を同梱の certifi へ切り替える。経緯と残りの検証は [../issues/mac_setup_processes_lack_tls_trust_and_homebrew_path.md](../issues/mac_setup_processes_lack_tls_trust_and_homebrew_path.md)。SearXNG のサーバー本体が TLS で検証に使う証明書 (上の certifi の下限の話) は、この変更では変わらない — サーバーは `run_searxng_server.ps1` / `.sh` から直接起動され、インストーラーで設定した環境変数を引き継がない。画面の Update ボタン経由の再起動が本体 (`main.py`) の `SSL_CERT_FILE` を引き継ぐのは 2026-09-02 からの挙動で、requirements.lock の certifi は上の下限と同じ版。

## 不変条件と責任

- 既定の取得先に master や可変タグを使わない。明示的な SEARXNG_REF も完全なコミット SHA に限定する。
- ソースの準備、設定生成、editable install、起動で参照するディレクトリを一致させる。
- ZIP 取得でもパッケージ作成に必要なルートファイルを欠落させず、実際の固定版を表すバージョン情報を供給する。
- 導入完了の記録は依存導入・互換処理・実 import が成功した後だけ保存する。
- 依存が欠けた既存環境を、パッケージの場所が見つかるという理由で完成扱いしない。
- `REQUIREMENT_FLOORS` の書き換えは pip install より前に済ませ、requirements.txt・editable install のメタデータ・`pip check` が同じ下限を見る。

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

### 実測結果 (2026-10-22、依存の下限の上書き)

- 隔離した worktree の `scripts/.searxng-src` / `scripts/.searxng-venv` へ、Windows・Python 3.13 で新規の setup が成功。certifi 2026.10.19 と anyio 4.15.1 が入り、`pip check` は "No broken requirements found."、searxng のメタデータの要求に `certifi>=2026.10.19` と `anyio>=4.14.2` が入った。
- 同じ venv を certifi 2026.7.22 / anyio 4.12.0 と `installer_revision` 1 の完了記録に戻し、requirements.txt も上流の文面に戻すと、`check` は exit 1。続く setup で両者が引き上がり、`pip check` も成功。もう一度 setup すると pip install は走らず、requirements.txt の中身は変わらない。
- 導入した venv の curl_cffi は certifi の cacert.pem を既定の CA として使い、外部サイトへの HTTPS が 200 を返した。SearXNG の HTTP 起動は今回は確かめていない。

ローカルの実行ログ: `tmp/searxng-floor-verification/{setup,upgrade-setup,repeat-setup}.log`。

## 経緯

最初の HTTP 試験で CSS が404になり、ファイルの存在を確かめる前に「ZIPに配布CSSがない」と進捗で述べた。実ファイルとHTMLのURLを照合するとCSSは存在し、Windowsの `\\` を含む資源一覧と `/` のURLの比較が不一致になることが原因だった。ファイル不足という説明は訂正し、列挙側を POSIX 形式に統一した同じ版で実HTTPの200を確認した。HTTPステータスだけを特定の原因の証拠にせず、生成したURLと配置先を照合する。

## 今回の対象外

上流 master との差を監視する仕組み・定期タスク・自動アップグレードは作らない。固定版の更新方針と検索サイト側の変更への対応は、別途検討する。
