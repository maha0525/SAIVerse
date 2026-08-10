# MCP 接続の復旧経路の穴（remote 化で顕在化しやすい）

**ステータス**: 未着手（Elyth Remote MCP 移行のスコープから分離）

> **2026-08-10 追記**: per_persona ツール検出の再設計 (`docs/intent/mcp_addon_integration.md` §I — 起動時 discovery を廃止し、Pulse 頭でペルソナ自身の接続を張り Beat 頭で一覧を聞き直す) が確定した。実装されれば **2 と 3 は工程ごと消滅**する (「一回きりの検出」が無くなり、鍵保存の次の Pulse から自然に使える / 接続失敗は既存の失敗記録に載る)。**1 も接続ライフサイクルが Pulse 単位になることで形が変わる** — 「切れた接続を掴み続ける」は「Pulse 頭の接続確立が毎回やり直す」に置き換わる見込み。§I の実装と検証が済んだ時点で、本 issue の残りを再評価して閉じるか書き直す。
>
> **2026-08-10 追記 2 (§I 実装後)**: §I は実装され、per_persona については **2 と 3 が実際に消滅**した (per_persona 経路に「検出」という工程が無くなった。`_start_instance` 経由の missing_config 記録・UI 表示は従来どおり生きている)。**1 は残る** — ただし per_persona に限り、被害範囲が「再起動まで」から「次の Pulse 頭まで」に縮んだ:
> - Pulse 頭の取得は `_connections` の在否ではなく `connection.connected` を見て、死んでいれば張り直す (遅延起動の wrapper 側の判定は未変更 = 「表にあるか」のまま)。
> - Beat 頭は死んだ接続を見つけたら提示 (所属) を空集合へ倒す (§I 消し込み)。
> - `reconnect_server` は未解決 config で畳む / 解決不能なら触らない、の 2 分岐を持つようになったが、**依然として `_start_instance` の共通経路には載っていない** (成功時の failure clear や backoff 記録が付かない)。
>
> 残る修正方向は「遅延起動の判定を実接続で行う」と「`reconnect_server` を `_start_instance` の共通経路へ寄せる」の 2 本。加えて、**接続断で所属 (`_persona_tool_names`) を空集合化する無効化コールバックを一元化する**と、Beat 頭の結論待ちに頼らず接続が死んだ瞬間に提示が畳める (§I の「派生状態の無効化は元の値を書き換える側に置く」と同じ原則)。

## 症状

どちらも「一度つまずいたら再起動まで戻らない」形の穴。族が同じなのでまとめる。

### 1. 接続が切れた後も切れた接続を掴み続ける

`MCPServerConnection.call_tool()` は失敗時に `disconnect()` するが、`MCPClientManager._connections` からは除去しない。per_persona のツール wrapper は

```python
if instance_key not in manager._connections:   # ← 「表にあるか」しか見ない
```

で遅延起動を判定するため、切れた接続が表に残っている限り再起動されず、以降の呼び出しは `ConnectionError` を返し続ける。

`reconnect_server()` も `_start_instance()` / `_record_failure()` を経由しないので、再接続に失敗しても stale な接続が残り、失敗カテゴリの記録も backoff も付かない（UI の失敗表示にも出ない）。

### 2. パラメータ保存後に per_persona のツール検出が再実行されない

初回の `_discover_per_persona_tools()` は未解決 config ならプローブをスキップする（2026-08-09 追加、外部サーバーへ placeholder の literal を送らないため）。

その後ユーザーが API キーを保存しても、`reconnect_server()` は実接続が `_connections` に無いと即 False を返して何もしない。**ツール自体が `TOOL_REGISTRY` に未登録なので、SAIVerse の再起動かアドオンの再 toggle までペルソナから使えない。**

### 3. キー未設定が UI のどこにも出ない

remote の per_persona で全ペルソナのキーが未設定のとき、起動時のツール検出はプレースホルダーを送らないようスキップする（2026-08-09 追加）。この経路は `_record_failure()` を通らないため、`get_failed_instances()` にも UI の失敗一覧にも出ない。**ユーザーから見ると「そのアドオンのツールが理由もなく存在しない」状態**で、原因を知る手段がログしかない（ログは歯止めにも案内にもならない）。

`_start_instance` 側の missing_config は失敗として記録・表示されるので、**同じ「キー未設定」が経路によって見え方が違う**。

- 対処の方向: discovery のスキップにも pending / missing_config 相当の状態を持たせ、UI の失敗一覧（またはアドオン管理画面のツール欄）に「キー未設定でツール未登録」を出す。上の 2 と同じ画面で解決できるはず。

## なぜ範囲外にしたか

Elyth の Remote MCP 移行（2026-08-09）で Codex のレビューが検出した。**どちらも stdio 時代から同じ構造で、remote 対応が作った欠陥ではない**（同じセッションで検出された「認証ヘッダーが redirect 先へ漏れる」「未解決 placeholder が外部へ送信される」は remote 対応が作った経路なので、そちらは同じ変更で塞いだ）。

ただし remote では子プロセスと違い、ネットワーク瞬断・サーバー側の再起動・セッション期限切れが起きる。**構造は同じでも踏む確率が上がる。**

修正は接続ライフサイクル・参照カウント・backoff・reconnect の相互作用に踏み込むため、移行のスコープから分離した。

## 実機で見ること

- Elyth に繋がった状態からネットワークを遮断してツールを呼び、回復後にもう一度呼ぶ → 自力で復旧するか、それとも `ConnectionError` を返し続けるか
- API キー未設定で起動し、後からアドオン設定で保存する → 再起動なしでツールが現れるか

## 修正の方向（未着手）

- 遅延起動の判定を「管理表にあるか」から「実際に繋がっているか」へ変える
- `reconnect_server()` を `_start_instance()` / `_record_failure()` の共通経路に載せ、失敗を backoff と UI 表示へ流す
- パラメータ更新時に per_persona のツール検出を再実行する経路を用意する（実接続の有無に依存しない）

いずれも参照カウントとの相互作用を壊さないことが条件。関連: `docs/intent/mcp_addon_integration.md` §A / §F。
