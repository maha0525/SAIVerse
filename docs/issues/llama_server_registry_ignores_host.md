# llama-server 管理の帳簿がポート番号だけを鍵にしていて、別ホスト同ポートを区別できない

**発見**: 2026-08-03 (自動起動の busy 判定改修の「受け入れた限界」4 項として記録) → **2026-08-31 まはー裁定で issue 化** — 「別ホストで同ポートは普通にある運用」。他の限界 7 項は同日受理で確定。
**状態**: 🟣 検証待ち — `(host, port)` への統一は develop に取り込み済み、実機確認待ち (未解決に残す)
**深刻度**: P3 — 現行運用 (ローカル自動起動は単発の実験、常用は NEBULA の llama-swap) では実害未発生。多マシン運用が広がると踏む

## 事象 (修正前)

`LlamaServerManager` (llm_clients/llama_server.py) の管理帳簿は**ポート番号だけ**を鍵にしている:

- 管理下サーバーの台帳 `_servers: Dict[int, ManagedServer]`
- 貸出札 (実行中リクエストの印) のカウンタ `_inflight` もポート鍵

そのため `localhost:8080` と `NEBULA:8080` のような**別マシンの同じポート番号**が、帳簿の上では同一視される。現行の防御は「host 不一致で endpoint が応答していたら、壊さず・上書き起動もせず、警告して現状を使う」(受け入れた限界 4 の挙動) — 保守側だが、区別はできていない。

具体的な混線の形 (机上、未実測):

1. 遠隔サーバー (例: NEBULA の 8089) へのリクエストの貸出札が、**ローカルで自動起動した同番号のサーバー**の idle 停止を止め続ける (方向は保守側 = 止めない、だが帳簿として誤り)。
2. host 不一致の警告分岐では、ローカル管理下プロセスの活動時刻が遠隔リクエストで更新される — idle 停止がさらに遠のく。

## 直し方の方向 (起票時)

管理台帳・貸出札・警告抑止表の鍵を `port` から `(host, port)` に揃える (外部確認の `_external_ok` は既に (host, port) 鍵 — 内側だけが揃っていない)。「同一ポートの多 host はサポート外」の警告分岐は、鍵が揃えば分岐ごと不要になる可能性が高い — 撤去できるかを実装時に問う (不変条件を破る構造は補修より先に撤去を問う)。

## 関連

- [intent: llama_server_auto_launch.md](../intent/llama_server_auto_launch.md) — 「受け入れた限界」4 項 (本 issue へ移管)。他 7 項は 2026-08-31 受理で確定
- `llm_clients/llama_server.py` — `_servers` / `_inflight` / `request_lease` / ensure_running の host 不一致分岐


## 実装と検証 (2026-10-02)

- `_servers` / `_inflight` / `_external_ok` は `(host, port)`、`_slots_warned` は `((host, port), generation)` に揃えた。停止・再起動・終了時回収・起動失敗時の後片付けも同じ鍵を使う。
- host 不一致の救済分岐を撤去し、遠隔接続先の健康確認や lease 返却がローカルの activity / busy 時計を変更しないようにした。運用ログにも host を含め、同ポートの別接続先を区別できるようにした。
- 変更前に上記の時計混線を 2 件の赤テストで再現。変更後は `tests/test_llama_server_manager.py` の既存契約と同ポート別 host の回帰テストで、ensure → lease → 通常応答 / stream close、idle 判定、世代交代、警告、shutdown までを検証する。
- 呼び出し元の factory / `OpenAIClient` / `LlamaCachedClient` は引き続き同じ `base_url` を manager に渡す。外部の正常応答を再利用する経路、30 秒キャッシュと観測順序判定、unknown を止めない契約、busy_deadline の非常停止は維持する。

### 検証結果

- `SAIVERSE_HOME=<一時ディレクトリ> python -m pytest tests/test_llama_server_manager.py tests/test_in_flight_check.py -n 0 -q`: **105 passed, 6 skipped** (manager 74 件を含む)。skip は既存台帳の経過措置テストで、対象行の削除/免除失効による正常系。既存の Google SDK の型警告 1 件あり。
- `ruff check llm_clients/llama_server.py tests/test_llama_server_manager.py`: 合格。
- `python scripts/check_in_flight.py`: 合格 (既存 RSS 行の経過措置警告のみ)。`git diff --check`: 合格。
- 全テストスイートは実行していない。

### 残る境界

- 本番ペルソナ・実推論・実サーバー起動・実ネットワーク接続は行っていない。テストは独立した `SAIVERSE_HOME` と合成プロセス / HTTP / SDK で隔離した。
- この変更は起動可否の仕様を変えず、遠隔プロセス起動も追加しない。既存の起動経路はローカル `Popen` であり、非 loopback の LAN bind や command 設定も従来どおり。遠隔 endpoint 不通時の起動方針を変えるには別判断が要る。
- ローカルの既知の別名 (localhost / localhost.localdomain / 127.0.0.0/8 / ::1 / wildcard) は帳簿だけを正規化する。DNS 別名の解決・統合は行わない。接続・起動の host は元のまま。
- `llama_server_{port}.log` のファイル名は互換性のため維持する。同ポート別 host の管理プロセスは同じログファイルへ追記しうる。

## 経緯

- 2026-08-31: まはー裁定で、同ポート別 host を受け入れた限界から外し、未着手 issue にした。
- 2026-10-02: 帳簿の接続先単位化を実装。レビューと実機確認を次工程とし、archive には移さない。


### 追加レビュー検証 (2026-10-03)

- 別名の lease が idle 停止を阻止できないケース 6 件と、二スレッドの別名同時起動による重複 launch を修正前に再現した。
- 修正後: manager と台帳テスト **114 passed, 6 skipped**。ruff 合格。合成プロセス / HTTP 置換による検証であり、Windows・実サーバーは未検証。

- 2026-10-03: PR #348 を develop へマージした (レビューはメティス、マージの判断はまはー)。台帳から移送した旧次アクション: 「接続先ごとの台帳・貸出札・停止処理の修正を draft PR レビュー待ち。次は差分レビューと、承認された隔離環境での実機確認を行う。」(誰待ち: まはー (PR レビュー・実機確認))
