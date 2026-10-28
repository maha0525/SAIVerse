# macOS で update / setup の別プロセスに証明書の切り替えが効かず、Homebrew のコマンドがビルド時に見つからない

**状態**: 検証待ち (2026-10-26 実装。Windows の既存機能のテストは全件成功。macOS の実機は、次のリリースを Mac の利用者が更新するときに確かめる)
**関連**: `saiverse/tls_trust.py`、`scripts/searxng_install.py`、`scripts/rebuild_addon_envs.py`、`saiverse/addon_installer.py`、[archive/urllib_https_fails_when_os_trust_store_empty.md](archive/urllib_https_fails_when_os_trust_store_empty.md)、[../intent/dependency_management.md](../intent/dependency_management.md) §2-8

## 報告 (2026-10-26、のろけゆきさん)

macOS で v0.3.26 / voice-tts v0.6.1 へ更新したとき、起動時の再構築が二つ失敗した。

1. SearXNG の再インストールが `SSL: CERTIFICATE_VERIFY_FAILED (unable to get local issuer certificate)` で失敗する。
2. voice-tts の GPT-SoVITS 用の Python 環境の再構築 (`pip install -r envs/gpt_sovits-macos.txt`) が、python_mecab_ko のビルド時の `RuntimeError: mecab-config not found` で失敗する。mecab-ko は Homebrew で入っていて、`/opt/homebrew/bin/mecab-config` は存在していた。

報告者は、Python の `Install Certificates.command` の実行と、`/opt/homebrew/bin` を PATH に足した上での手動の pip 実行で自力で復旧した。

## 原因

1. **証明書**: macOS の python.org 版 Python は OS のキーチェーンから CA 証明書を読めず、`ssl.create_default_context()` が証明書を 1 枚も持たないことがある ([archive/urllib_https_fails_when_os_trust_store_empty.md](archive/urllib_https_fails_when_os_trust_store_empty.md) で 2026-09-02 に実測済み)。その対策である `saiverse/tls_trust.py` の `ensure_default_https_trust()` (OS のストアが空のときだけ、urllib が標準で検証に使う証明書を同梱の certifi へ切り替える) は `main.py` でしか呼ばれていなかった。SearXNG の再インストールは setup / 更新から**別プロセス**の `scripts/searxng_install.py` として実行され、`scripts/download_searxng_source.py` の `urlretrieve` で、初期設定のまま GitHub から固定版の ZIP を取得しようとして失敗する。v0.3.26 では SearXNG の依存パッケージの下限を上げて `installer_revision` を上げた ([../intent/searxng_installation.md](../intent/searxng_installation.md)) ため、全 Mac 利用環境でこのダウンロードが実行される。
2. **PATH**: Homebrew のコマンド置き場 (Apple Silicon は `/opt/homebrew/bin`) は macOS の標準の PATH に無く、shell の設定で `brew shellenv` を通した shell から起動したときだけ PATH に入る。`saiverse/addon_installer.py` の `_run_subprocess` では親プロセスの PATH をそのまま子プロセス (pip など) へ引き継ぐため、起動のされ方によっては、pip でソースからビルドするときに `mecab-config` が見つからない。

## 対処 (2026-10-26)

- `scripts/searxng_install.py` と `scripts/rebuild_addon_envs.py` のそれぞれの `main()` の先頭で `ensure_default_https_trust()` を呼ぶようにした。そのプロセスの urllib と、`SSL_CERT_FILE` を引き継ぐ子プロセスに効く。OS の証明書ストアが読める環境 (Windows など) では何もしない。certifi の証明書へ切り替えたときは、その旨と証明書ファイルの場所を画面に一行出す (`searxng_install.py` は stderr に出力する。stdout は `source` などの結果を取り込む呼び出し元があるため)。
- `ensure_default_https_trust()` 自体は、切り替えに失敗しても (certifi のファイルが壊れている等) 例外を外へ出さず、理由をログに残して何もしないようにした。`searxng_install.py` は HTTPS 通信を行わない action (`source` / `python` / `settings`) の前にもこの関数を呼ぶので、ここで例外により処理が止まると SearXNG の起動まで失敗するため (コードレビューの指摘)。
- `saiverse/addon_installer.py` の `_run_subprocess` で、macOS の場合のみ、`/opt/homebrew/bin` と `/usr/local/bin` のうち PATH に無く実在するものを PATH の**末尾**に足すようにした (`_path_with_homebrew_bins`)。末尾に足すため、専用環境の bin や利用者のツールを同名の Homebrew のコマンドで上書きしない。

SearXNG のサーバー本体が TLS で検証に使う証明書は、この変更では変わらない: start.bat / start.sh からのサーバーは `run_searxng_server.sh` / `.ps1` から直接起動され、インストーラー (子プロセス) で設定した環境変数は親の shell には反映されない。なお、画面の Update ボタン経由の再起動では、サーバーは本体 (`main.py`) が設定した `SSL_CERT_FILE` を以前から引き継ぐ (2026-09-02 からの挙動で、今回は変えていない。requirements.lock の certifi は SearXNG 側の下限と同じ 2026.10.19 なので、中身の差は無い)。

## 今回は変更していない他の実行経路 (コードレビューで洗った記録)

- `update_engine.py` 自身の pip と、`searxng_install.py` の中の pip には Homebrew の PATH の補完を入れていない。配布済みのビルド (wheel) が無いパッケージをソースコードからビルドするときにだけ関係し、本体と SearXNG が使うパッケージのインストールでは発生していない。必要になったら同じ形で追加する。
- `scripts/download_searxng_source.py` を単体で直接実行した場合は、証明書の切り替えを行わない (現在の呼び出し元は `searxng_install.py` 経由だけで、そちらでは切り替えが有効になる)。
- `ensure_default_https_trust()` の「OS の証明書ストアが空か」の判定は、cafile (証明書ファイル) が無く capath (証明書のディレクトリ) だけがある Linux 環境を空と誤判定しうる (`ssl.SSLContext.get_ca_certs()` が capath の証明書を返さないため。未検証)。これは 2026-09-02 の `main.py` の呼び出しから同じで、今回その対象範囲が setup / 更新のプロセスにも広がった。

## こちらでは直せないもの

mecab-ko 自体が入っていない Mac では、GPT-SoVITS の環境の構築に `brew install mecab-ko` が要る。導入の案内とエラーメッセージは voice-tts 側のリポジトリの役割であり、本件の範囲外。

## 検証

- Windows の既存機能のテスト: `tests/test_searxng_installation.py` / `tests/test_update_addon_env_rebuild.py` / `tests/test_tls_trust_fallback.py` / `tests/test_addon_env_rebuild.py` / `tests/test_update_dependencies_reinstall.py` / `tests/test_update_engine_searxng.py` / `tests/test_environment_alerts.py` / `tests/test_addon_installer_homebrew_path.py` (新設) の 215 件がすべて成功。PATH の補正が `sys.platform == "darwin"` 以外では一切行われないことと、certifi のファイルが壊れていても例外が外へ出ないことをテストで確認している。
- macOS の実機: 次のリリースを Mac の利用者 (報告者ののろけゆきさんの環境が最有力) が更新したとき、`Install Certificates.command` も PATH の設定も無しで SearXNG の再インストールと voice-tts の環境の再構築が通ることを確かめる。

## 経緯

- 2026-10-26: のろけゆきさんの Discord の報告を受けて起案し、同日に実装した。
