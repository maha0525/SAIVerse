# Stack-chan 複数機体 (Phase 7') — 実機検証で見つかった事項の追跡

作成: 2026-07-01。実機テスト中に発見したバグ・完了作業・残タスクを一元管理する。
セッションを跨いでも「何が済んで何が残ってるか」をここだけ見れば分かる状態にする。
関連: `docs/intent/stackchan_vessel.md` 設計 K / Phase 7'、`docs/intent/mcp_addon_integration.md`。

---

## ✅ 完了（2026-07-01 セッション）

- **`vessel_building_id` 残骸掃除**: addon.json params から撤去、dead `_vessel_building_id()` 4ファイル削除、ペアリング時書き込み削除。
- **デバイス操作スペル 9 個を native wrapper 化** (`tools/device_controls.py` 新規): `set_avatar`/`set_mouth`/`set_mouth_sequence`/`set_led`/`set_all_leds`/`set_leds`/`clear_leds`/`set_brightness`/`set_volume`。`resolve_vessel_connection` で現在機体へ振り分け。生 MCP は `mcp_servers.json` で `visible:false`。引数スキーマは gateway `stdio_server.py` と 1:1。**実機で音量・タッチ制御が貫通、9ツール Gemini spec OK を確認済み**。
- **UI デバイス操作を機体別ルーティング化** (`api_routes.py` `_call_device_mcp_tool` + 4 endpoint、`ui/Panel.tsx` DeviceSection に機体セレクタ): 旧 `:global` 固定 → `:instance:{vessel_id}`。**実機で音量・タッチが選択機体に届くこと確認済み**。
- **DeviceSection の vessel 一覧追従** (`refreshKey`): ペアリング追加/解除で機体セレクタが即更新。
- **既存機体のポート backfill**: `set_ports(076797f8…, 18765, 8766)` 実行済み。NVS の 18765 に一致。
- **`mcp_servers.json` コメントの `${}` 地雷除去**: `_comment_building_ids` の `${addon...vessel_building_id}` / `_comment_env` の `${runtime.lan_ip}` を素の表記に。本体 MCP client がコメント欄も placeholder 走査するため、コメントに構文記号を書くと gateway 起動が `missing_config` で abort していた（実機で踏んだ、修正後クリーン起動を確認）。

---

## 🟢 実装済み（2026-07-01、要実機検証）

### P0 — エアの顔が出ない（avatar 入室ロードのレース + reconcile 空回り）→ 実装済み
**原因** (`avatar_loader.py`、18:19 ログで確定):
1. **レース**: 入室で avatar ロード(load_avatar_set/set_avatar/set_blink)が撃たれるが、gateway subprocess 起動完了より数秒早く発火して全部 `gateway が接続されていません` で空振り(18:19:39 avatar 試行 → 18:19:45 gateway ready)。
2. **リカバリ不能**: gateway ready 後に再ロードすべき `_periodic_reconcile_loop` が `_fetch_device_session_id()` を building_id 無し(context 経路)で呼ぶため、背景スレッドに persona 文脈が無く機体を特定できず毎回失敗(30秒おきの `現在のペルソナまたは Building` warning はこれ)→ reconcile 本体に到達せず、空振りした avatar が再試行されなかった。
**修正** (実装済み): `_periodic_reconcile_loop` を per-vessel 化。`list_vessel_building_ids()` を回し、各 building で occupant を DB から引き、`_fetch_device_session_id(building_id)` (= `_for_building` 経路)で device 接続を検知したらその occupant の avatar を building 文脈で `on_persona_entered_building` 経由ロード。dead 化した `_reconcile_vessel_state` は除去。**要実機検証**: 再起動後、数秒で顔が出る / `現在のペルソナまたは Building` warning が消える。

### P1 — 再起動を跨ぐと在室ペルソナの gateway が自動起動しない → 実装済み
**原因** (`vessel_gateways.py`): gateway 起動は `persona_entered_building` (移動イベント)のみが契機で、起動時に既に在室しているペルソナには発火しない。単一機体時代の global 常時起動からの退行。
**修正** (実装済み): `vessel_gateways.py` に起動時 reconcile daemon thread (`_reconcile_gateways_on_startup`) 追加。MCP manager ready を待ってから、occupant の居る全 Vessel の gateway を冪等起動。誰も居ない Vessel は起動しない(入室時 lazy)。**要実機検証**: 再起動だけで(出し入れ不要で)在室機体の gateway が起動する。

### P2 — avatar の device-reboot 検知が multi-vessel で機能しない → P0 に内包して実装済み
P0 の per-vessel ループが `last_sid_by_building` で機体ごとに session_id を監視し、変化(=reboot)を検知したらその機体を再 reconcile する。**残る精緻化**(未対応・低優先): `AvatarLoader.reconcile_session` の last-session cache が global のままなので、2 機体が別 session を持つと初回 reconcile 時に一度だけ cache が振れる(視覚バグではない)。per-vessel cache 化は将来。

---

### ✅ ペアリング URL の host が gateway 実 IP とズレる（2026-07-01 実機発見・修正済）
**症状**: 2 台目 device がペアリング画面表示の `ws://192.168.0.9:8767` に設定したが繋がらない。gateway は実際には `.10` で動いていた。
**原因**: ペアリング URL (`_build_gateway_ws_url`) は AddonConfig の手動 `vision_host`(=.9、古い固定値) を host に使う一方、gateway の実 IP は `${runtime.lan_ip}`(= `saiverse.lan_ip.get_local_ip()` の socket probe = .10) で別ソース。Wi-Fi の IP 変動で両者がズレ、画面表示 URL と実 gateway が食い違った。gateway は vision_host を一切使わない。
**修正**: `_build_gateway_ws_url` を `get_local_ip()` (gateway と同一ソース) 第一 + auto 失敗時のみ vision_host フォールバックに変更。`get_local_ip()`=.10 で gateway 一致を確認。要バックエンド再起動で UI 反映。

### ✅ PaHub が古い global インスタンス経路のまま（ENV III「Port A 接続エラー」の主因、2026-07-01 実機発見・修正済）
**症状**: エア(stackchan_room, ENV III を PaHub 経由で搭載)の温湿度・気圧スペルが「Port A 接続エラー」(`ESP_ERR_INVALID_STATE`)。物理接続は正常。
**原因**: `tools/hubs/pahub.py` の `_get_mcp_connection` が `_make_instance_key(..., persona_id=None)` = global インスタンスを引いていた。複数機体(instance_template)では global は存在せず None → `open_all_channels: MCP connection not available` → ハブの channel が開けず、その先の 0x44/0x70 に届かない。env3.py は複数機体対応で `resolve_vessel_connection` に直っていたが、**PaHub が取り残されていた**（device エンドポイントと同じ「global 経路の取り残し」バグの 3 例目）。
**修正**: `pahub._get_mcp_connection` を `vessel_dispatch.resolve_vessel_connection`(現在機体・per-vessel)に変更。addon 全体を grep して他に global 経路の残りが無いことも確認。要バックエンド再起動。

### ✅ 複合アクション（うなずき等）が global インスタンス経路 + 生 MCP 直呼びで失敗（2026-07-01 実機発見・修正済）
**症状**: 「うなずき」複合アクションが `MCP サーバー '...__stackchan' に接続されていません` で失敗。
**原因**: 本体 `saiverse/composite_actions.py` が (1) `_get_mcp_connection` で `:global` インスタンスを引く（複数機体では存在せず即失敗）、(2) `_make_call_awaitable` が「MCP ツール優先」で `conn.call_tool`（単一 global 接続前提）に流し、per-vessel の native wrapper を使わない。加えて (3) native wrapper に切り替えても、`run_in_executor` は contextvars を伝播しないため wrapper 内の `resolve_vessel_connection` が現在ペルソナを解決できない（直の spell は `_run_spell_tool_async._run` が persona_context を張るので動く）。「global 経路の取り残し」バグの 4 例目（本体側）。
**修正**: (a) `_make_call_awaitable` を **native tool 優先**に反転（native wrapper が per-vessel 解決を内包）、(b) global conn 取得を非致命化（native のみのアクションは conn 不在でも実行）、(c) `contextvars.copy_context()` を捕捉し sync native を `ctx.run` 経由で executor に流して persona_context を張り直す、(d) UI のツール一覧取得(`get_available_tool_schemas`)も global 無し時に任意の稼働インスタンスを使う。ruff + py_compile OK。要バックエンド再起動で実機確認。

## 🔴 未修正（優先度順）

### 🚨 BLOCKER — stackchan-mcp gateway の ownership lock が machine-global singleton（A-2 の根本障害）
**症状**: 2 機体目の gateway 起動が `unhandled errors in a TaskGroup (1 sub-exception)` で失敗。1 機体目は正常。
**原因** (fork `stackchan_mcp/ownership.py` + `cli.py`、実機 18:53 + lock ファイルで確定):
- `LOCK_PATH = ~/.stackchan-mcp/owner.lock` = **machine-global の単一ロック（port/device 別ではない）**。`owner.lock` の中身 = pid 34516（1 機体目 stackchan_room の gateway）。
- `acquire_lock` は既存ロックが生きた pid に握られていれば `OwnershipError("device already owned by ... pid 34516")` を raise。`cli.py` は起動時に無条件で `acquire_lock(owner_id)` を呼ぶ。回避フラグ/env なし。
- → 2 機体目 gateway が起動時にグローバルロックを取れず即死 → stdio 接続失敗 → TaskGroup 例外。
- intent §K-1 の「各 gateway は**無改修で**機体ごとに別ポートで listen」前提がこのロックで崩れる。「device を 2 つの gateway が奪い合わない」ための安全機構だが、複数機体=複数 device を想定していない。
**方針決定 (まはー、2026-07-01)**: A-1（1 ポートで複数 device 多重化）は gateway が構造的に単一 device 設計（`ESP32Manager._connection` 単一スロット・新接続が旧を evict・単一トークン認証・全 tool が単一接続対象）で大改修が必要と確認したため不採用。A-2 継続 + per-port lock で進める。詳細比較は本セッションのやり取り参照。

**修正 (fork checkout に実装済み・未コミット、要デプロイ)**: `temp/stackchan-mcp/gateway/stackchan_mcp/cli.py` に per-WS_PORT ロック化を実装。`_ws_port_lock_path()` ヘルパ追加（`LOCK_DIR / f"owner-{ws_port}.lock"`）、`_acquire_startup_lock` の acquire・stdio/http 両経路の release・`_run_ownership_check`（`--check` 診断）を全て per-port path に統一（`--check` が旧 global lock を読んで誤報告する既存ユーザー影響も解消）。各 device の gateway が自分のポートのロックを持ち N gateway 共存、同一ポート二重起動は従来通り拒否。py_compile OK。まはーの firmware WIP (`stackchan.cc`) には非接触。
**残: デプロイ（まはー fork パイプライン）**: uvx が取る場所へ反映が必要。(a) fork integration ブランチに commit + push → uvx キャッシュ更新（`--from git+...@branch` はブランチ ref をキャッシュするので `uv cache clean` か `--refresh` が要る）、または (b) テスト用に addon `mcp_servers.json` の `--from` をローカル checkout パスへ一時的に向ける。**上流 PR 候補**（ロックは machine-global でなく device/port スコープが正しい）。
**回避 (暫定)**: この修正をデプロイするまで 1 機体しか同時起動できず、A-2 の 2 機体テストはブロックされる。

### 別軸 — 複数ペルソナ同時発話で後発ペルソナの音声が物理機体に届かない
voice-tts が GPU 1 個で TTS 生成を順次処理するため、一方が長く喋ると他方の音声生成が speak_hook の 60 秒 first-chunk timeout に間に合わず、物理機体が無音になる。複数機体のルーティングバグではなく voice-tts のアーキテクチャ課題。詳細と方向性は独立 issue [`voice_tts_multi_persona_concurrent_speech.md`](voice_tts_multi_persona_concurrent_speech.md)。暫定方針: 既知制約として受容し Phase 7' は "発話並行性以外は完成" と区切る。

### P3 — 本体 MCP client がコメント欄の `${}` を placeholder として解決しようとする（latent）

### P4 — 本体 MCP client の subprocess errlog が server 名共有（instance 別でない）
`_open_subprocess_errlog` (`tools/mcp_client.py:430`) が `mcp_subprocess_{server_name}.log` を使い、名前付きインスタンス間で errlog を共有。append モードなので致命ではないが、複数機体の subprocess ログが 1 ファイルに混ざり診断しづらい。instance_id を含めた per-instance パスにするのが望ましい（本体側、低優先）。
**症状**: `mcp_servers.json` の `_comment_*` フィールドに `${...}` リテラルを書くと、resolver が本物の placeholder と誤認して解決を試み、不正キーだと `missing_config` で subprocess 起動を abort する。
**原因**: `tools/mcp_config.py` の `resolve_config_placeholders` が config 全 string を走査し、`_comment_*` 等のドキュメント用フィールドを除外していない。
**修正方針**(本体側、別スコープ): resolver が `_` 始まりのキー(慣習的にコメント)や既知の非解決フィールドをスキップする。当面は「コメントに構文記号を書かない」で回避(2026-07-01 実施済み)。

---

## ✅ 実機で検証済み（OK）

- デバイス制御(音量スライダ / タッチ有効無効)が gateway 経由で選択機体に貫通。
- `mcp_servers.json` コメント地雷除去後、gateway がクリーン起動(実 instance_key、startup error なし)。
- device_controls.py の 9 ツールが to_gemini で 1 つも落ちない(set_leds/set_mouth_sequence のネスト配列含め Gemini spec OK)。
- ポート自動割当が衝突回避(既存 18765/8766、新規 8767/8768)。

---

## ⬜ 残テスト（未実施、§D-1 = Phase 7' 完了条件）

2 機体(片方 ENV III あり/なし)同時接続で:
- (a) 別ペルソナが各機体で同時に首振り・発話、混線しない
- (b) ENV III なし機体で温湿度スペルが出ない / あり機体で出る
- (c) 持ち替えで同じ `move_head` が別機体を動かす
- (d) set_volume がスペル/UI 両経路で選択機体だけに効く
- (e) 表情が機体ごとに出る（← P0 修正が前提）

---

## 📄 doc / memory 更新 TODO（実機テスト完了後）

- `docs/intent/stackchan_vessel.md`: Phase 7' を「実装完了(生スペル wrapper 化・UI 機体振り分け含む)」に更新、上記 P0-P2 を既知課題として反映。
- `docs/issues/stackchan_multi_vessel_verification_handoff.md`: 本 doc で置き換え/統合(旧 handoff は前セッションのハルシネーション文脈で古い)。
- memory `project_stackchan_multi_vessel`: 実装完了 + P0-P2 の存在を反映。
