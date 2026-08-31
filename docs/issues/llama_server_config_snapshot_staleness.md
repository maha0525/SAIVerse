# llama_server 設定の編集が、作成済みクライアントの自動起動に反映されない

**発見**: 2026-08-31 (busy 判定の実機検証中。idle_timeout=60 を保存したのに、直後の自動起動が既定 600 秒で登録された)
**状態**: 🔲 未解決 — 原因特定済み・対処未着手
**深刻度**: P3 — 回避は簡単 (チャットのモデルを選び直す / 再起動)。ただし「設定を保存したのに効かない」は説明なしだと不信を生む

## 事象と原因

モデル編集 UI の保存は `reload_configs()` でメモリ上の設定台帳を即リロードする — ここは正しい。しかし llama.cpp 自動起動の設定 (`llama_server` ブロック: idle_timeout / busy_deadline 等) は、**LLM クライアント生成時に `bind_llama_server` へスナップショットとして渡され、以後の毎リクエスト前 `ensure_running` はそのスナップショットを使い回す** (`llm_clients/factory.py` の `client.bind_llama_server(llama_server_base, config)`)。

そのため、クライアントが既に作られているペルソナでは、設定を保存しても次の起動・停止判定は**古い設定**で動く。クライアントの作り直し (チャット UI でモデルを選び直す / ペルソナのモデル変更 / バックエンド再起動) まで反映されない。

実測 (2026-08-31): 19:54 クライアント生成 (idle_timeout 未設定) → 20:00:44 に idle_timeout=60 を UI 保存 (リロード済み) → 20:03:49 の自動起動はスナップショット由来の既定 600 で登録 → 停止は `idle for 620s (timeout=600s)` で発火した。

## 対処の候補

1. `ensure_running` を通すとき、config を bind 時のスナップショットではなく `get_model_config(model_name)` で毎回引き直す (台帳は既にリロード済みなので鮮度が出る)。呼び出し頻度は毎リクエスト 1 回なので辞書引きのコストは無視できる。
2. または `reload_configs()` が既存クライアントの bind 済み設定を無効化する。

案 1 が素直。

## 関連

- `llm_clients/factory.py` (bind_llama_server) / `llm_clients/openai.py:333-346` / `llm_clients/llama_server.py` (ensure_running)
- [intent: llama_server_auto_launch.md](../intent/llama_server_auto_launch.md) — 「受け入れた限界」の一覧に載せるか、本 issue で直すかは裁定待ちの 8 項と一緒に判断
