# SearXNG のセットアップと起動手順

SAIVerse から SearXNG 互換の検索を使うために、Docker なしで実際の SearXNG サーバーをローカルに立ち上げます。初回実行時にソースをクローンして専用 venv に依存をインストールし、そのまま `/search` エンドポイントを提供します。

## 前提条件
- git / Python 3 が利用できること。
- 外部ネットワークに接続できること（検索先は web）。

## サーバーの起動
`scripts/run_searxng_server.sh` を実行すると、ローカルで SearXNG が `http://localhost:8080` で立ち上がります。初回実行時は SearXNG ソースのクローンと依存インストールを自動で行います。

```bash
./scripts/run_searxng_server.sh
```

- 停止はターミナルで `Ctrl+C`。
- 2 回目以降はキャッシュされたソースと venv を再利用します（`scripts/.searxng-src`, `scripts/.searxng-venv`）。

### よく使う環境変数
- `SEARXNG_PORT` : 公開ポート（デフォルト `8080`）。
- `SEARXNG_BIND_ADDRESS` : バインドアドレス（デフォルト `0.0.0.0`）。
- `SEARXNG_REF` : 取得する SearXNG のブランチ / タグ（デフォルト `master`）。
- `SEARXNG_SETTINGS_PATH` : 設定ファイルのパス（デフォルト `scripts/searxng_settings.yml`）。初回起動時に upstream の `searx/settings.yml` をコピーし、JSON 出力を有効化したものが生成されます。
- `SEARXNG_SECRET_KEY` : SearXNG の `server.secret_key` に利用する値。未指定の場合、初回起動時にランダムな値が自動生成され、設定ファイルに保存されます。
- `SEARXNG_SRC_DIR`, `SEARXNG_VENV_DIR` : ソースと venv の保存先ディレクトリ。
- `SEARXNG_LIMITER_PATH` : レートリミット設定 (limiter.toml) の配置先。未指定なら upstream の `searx/limiter.toml` を毎回コピーして上書きします（古いテンプレートの残骸を確実に排除します）。

> ローカルで余分なエラーが出ないよう、初回起動時に `ahmia` / `torch` / `wikidata` / `radio browser` の各エンジンを無効化しています。追加エンジンを使いたい場合は、生成済みの `searxng_settings.yml` を直接編集してください。

例）ポートを 8888、bind を 127.0.0.1 にする場合:

```bash
SEARXNG_PORT=8888 SEARXNG_BIND_ADDRESS=127.0.0.1 ./scripts/run_searxng_server.sh
```

## ツールの接続設定
`tools/defs/searxng_search.py` は以下の環境変数で接続先を解決します。サーバーを別ポートで起動した場合は、`SEARXNG_URL` か `SEARXNG_BASE_URL` を合わせて設定してください。

- `SEARXNG_URL` または `SEARXNG_BASE_URL` : ベース URL（例: `http://localhost:8888`）。
- `SEARXNG_LANGUAGE` : 既定の検索言語（デフォルト `ja`）。
- `SEARXNG_SAFESEARCH` : セーフサーチレベル 0/1/2（デフォルト `1`）。
- `SEARXNG_LIMIT` : 既定の取得件数（1-20, デフォルト `5`）。

## 動作確認
1. 上記スクリプトで SearXNG を起動。
2. もう一つのターミナルで Playbook から `searxng_search` を実行するか、直接 `curl` で検索します。

```bash
curl "http://localhost:8080/search?q=hello&format=json" | head
```

結果が JSON で返れば起動は正常です。
