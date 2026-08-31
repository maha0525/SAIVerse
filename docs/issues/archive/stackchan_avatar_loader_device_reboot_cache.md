# Issue: avatar_loader が device reboot を検知せず stale cache で skip transfer する

**ステータス**: ✅ 実装 + 実機検証完了 (2026-05-19、 event driven 化 + burst window)
**優先度**: medium
**作成日**: 2026-05-18
**関連**:
- `expansion_data/saiverse-stackchan-addon/avatar_loader.py` (= `_last_loaded` dict)
- 関連 issue: [stackchan_avatar_psram_peak.md](stackchan_avatar_psram_peak.md) (firmware 側 PSRAM peak、こちらは別軸)
- 関連 commit: `_last_loaded` mark を ok==True に厳格化した修正 (本 issue とは別件)

## 背景

`avatar_loader.py` は SAIVerse 側で `_last_loaded: dict[persona_id, checksum]` を in-memory に持っていて、同じ checksum なら `is_load_required == False` で **skip transfer** に分岐する。これによって短時間の再入室や重複 hook で 3.4MB matrix transfer を都度発行しないようにしている。

問題: **device 側 (ESP32) が reboot すると PSRAM 上の AvatarSet 状態は完全にクリアされる**。しかし SAIVerse 側の `_last_loaded` dict は SAIVerse プロセスが生きてる限り維持されるため、「device に load 済み」と誤認した状態が続く。

→ device reboot 後にペルソナを再入室させても、SAIVerse は skip transfer に分岐 → device の LCD は no avatar (= boot 後 初期状態) のまま、永久に新しい avatar が届かない。

回復には **SAIVerse プロセス再起動** が必須。これは負担が大きい。

## 観測 (2026-05-18)

シナリオ:
1. SAIVerse 起動、エア入室で avatar matrix transfer 成功 (`ok: true, bytes_transferred: 3456000`)
2. `_last_loaded[air_city_a] = sha256:dc6f...` 記録
3. しばらく経って ESP32 が何かのきっかけで reboot (= stroke 後の reset 等、本 issue とは別の原因)
4. device 再起動完了、gateway に再接続 (= WS reconnect)
5. SAIVerse からエアを再入室
6. `is_load_required(air_city_a, sha256:dc6f...)` → False → **skip transfer**
7. LCD に avatar が表示されない

backend.log の決定的な行:
```
22:52:25 avatar_loader: persona=air_city_a already loaded
         (checksum=sha256:dc6f...), skip transfer
```

しかしこの直前 (22:27:48) に ESP32 は USB_UART_CHIP_RESET で reboot していた。

## 解決案候補

### 案 A: WS 接続イベントを subscribe して cache クリア

`tools.mcp_client` の WS 切断/再接続イベントを `avatar_loader.py` で subscribe する。device 切断/再接続を観測したら `_last_loaded.clear()` する。

**メリット**: device reboot は必ず WS 切断を伴うので、確実に検知できる。ロジックも単純。

**デメリット**: WS の一時的な瞬断 (= ping timeout 等の network 揺れ) でも cache がクリアされる → 余分な transfer 発生。ただし重複 transfer は副作用としては許容範囲 (= 3.4MB を稀に重複転送するだけ、致命的ではない)。

### 案 B: device の session_id / boot time を都度確認

`get_status` MCP tool で device の `session_id` か `uptime` を取得し、SAIVerse 側で「前回見た値」と比較して変化してたら cache クリア。

**メリット**: WS 切断/再接続を直接観測しなくても判定できる、誤検知が少ない。

**デメリット**: `get_status` 呼び出しが毎回必要 (= 1 ラウンドトリップ追加)、入退室時に小さなレイテンシ。device 側に session_id / uptime / boot_id を返す仕様が必要 (= 既存ツールで取れるか要確認、なければ firmware 側にも改修必要)。

### 案 C: cache を完全に廃止して常に load を試みる

`_last_loaded` を廃止、毎回 `load_avatar_set` を呼ぶ。

**メリット**: 最も単純、device 状態と完全に独立 (= reboot 含めて常に整合)。

**デメリット**: 同ペルソナ連続入室で都度 3.4MB transfer が走る (= 帯域・時間・PSRAM peak 負荷)。

### 推奨

**案 A**。WS 切断検知は SAIVerse が既に持ってる情報経路 (`tools.mcp_client` の connection lifecycle)、device 改修も不要、検知精度も十分。瞬断による余分な transfer は許容できる範囲。

具体実装案:
1. `tools.mcp_client` の WS disconnect / reconnect hook を `avatar_loader.py` の AvatarSetLoader が register する
2. disconnect / reconnect 観測時に `_last_loaded.clear()`
3. 既存の入退室 hook 経路はそのまま (= load 時に `is_load_required` で判定する流れは維持)

## 関連リソース

- 該当コード: `expansion_data/saiverse-stackchan-addon/avatar_loader.py` (`AvatarSetLoader._last_loaded` / `is_load_required` / `mark_loaded`)
- WS lifecycle 観測経路: `tools/mcp_client.py` (= 接続管理側)
- 観測ログ: `~/.saiverse/user_data/logs/20260518_213859/backend.log` の 22:27:48 (reboot) と 22:52:25 (skip transfer) 周辺

## ログ

- 2026-05-18: stackchan stuck 再現実験中に発見、Issue 化
- 2026-05-19: **案 B (= device の boot_session_id 取得) で実装完了**。再考の結果、案 A (WS hook で cache クリア) は subprocess (= stackchan-mcp gateway) が SAIVerse 起動中ずっと live で device reboot を SAIVerse に通知しない構造的制約から SAIVerse 単独実装不可と判明、案 B に切替。
  - **firmware 側**: `Board::boot_session_id_` member を追加、 ctor で `esp_random` ベースの UUID v4 を生成 (NVS 非永続)、 `WifiBoard::GetDeviceStatusJson()` の出力 JSON に `boot_session_id` フィールド追加。 boot ごとに新 UUID が生成されるので host 側で boot 識別可能 (`board.h`, `board.cc`, `wifi_board.cc`)
  - **SAIVerse 側**: `AvatarSetLoader` に `_last_seen_session_id` member + `reconcile_session()` method 追加、 `_fetch_device_session_id()` で MCP `get_device_status` 経由 boot_session_id 取得、 `on_persona_entered_building` の load 試行前に `reconcile_session` を呼ぶ。 session_id 変化検知時に cache invalidate
  - 古い firmware (= `boot_session_id` フィールドなし) との互換: `_fetch_device_session_id` は `None` を返し、 `reconcile_session(None)` は何もしない (= 既存挙動を維持)
- 2026-05-19: 初回実機検証で **より根本的なバグを追加発見**。エア入室 → エリス入室 → エア再入室で「エリスの顔のまま」(エアに戻らない) という現象。 既存の `_last_loaded: dict[persona_id, checksum]` は **device が同時に保持できる avatar set は 1 つだけ** (= adopt 経路で新 set が旧 set を上書きして消す) という実機モデルと合っていない設計だった。 per-persona に「load 済み checksum」 を覚えても、 実際は古い set はすでに device から消えているので、 skip transfer に分岐すると LCD は古い set の avatar を出し続ける (= 物理的にはそうではなく、 上書きで消された 1 つ前の set の face)。
  - **追加修正**: `_last_loaded: dict[str, str]` を `_device_current_checksum: Optional[str]` に置換。 device が現在保持してる 1 つの checksum のみを覚える。 `is_load_required(persona_id, checksum)` は `_device_current_checksum != checksum` で判定 (persona 別比較は不要)、 `mark_loaded(persona_id, checksum)` は上書き挙動、 `clear_cache` は `None` に戻すのみ。 `persona_id` 引数は API 互換のため残すが内部判定で使わない (`del persona_id`)
  - これで session_id 変化検知 (= reboot) + device 1 set モデル (= 同一 session 内の swap) の両方が正しく invalidate される
- 実機検証: firmware build + flash 済 (= app partition only、 NVS / coredump partition 保持)、 SAIVerse 再起動後の swap 動作を確認予定
- 2026-05-19 (続き): エア → エリス → エア 再入室で「2 度目のエア入室時に LCD がエリスのまま」を確認、 上記データ構造変更で解決を確認 (3 度目のエア入室で正しく transfer 走り LCD がエアに切替)。
- 2026-05-19: **能動的 state 同期機構を追加実装**。 入室時の lazy check では拾えないシナリオ (= SAIVerse 起動中の device 単独 reboot / device blank + vessel 入室済 SAIVerse 起動) に対応する daemon thread を ``avatar_loader.py`` 末尾に追加。
  - module ロード時に thread 起動 (= daemon、 SAIVerse 停止で自動終了)
  - 起動 8 秒後に **初期 reconcile**: ``BuildingOccupancyLog`` の ``EXIT_TIMESTAMP IS NULL`` で「現在 vessel に居る persona」を取得し、 ``on_persona_entered_building`` を直接呼んで avatar transfer 経路に乗せる
  - 以降 30 秒間隔で **session_id polling**: ``get_device_status`` 経由で ``boot_session_id`` 取得、 ``reconcile_session`` に ``on_invalidate`` callback を渡して、 変化検知時のみ vessel 居住者の avatar を再 transfer
  - ``reconcile_session`` に ``on_invalidate: Optional[Callable]`` 引数を追加 (既存呼び出しは callback なしで挙動不変)
  - 古い firmware (= boot_session_id フィールド未対応) は ``_fetch_device_session_id`` が None を返すので polling 経路は noop で進む、 既存挙動を破壊しない
  - 実機検証: SAIVerse 再起動して 8 秒後に initial reconcile ログ + avatar load が走ること、 device 物理 reboot 後 30 秒以内に reboot 検知 + 再 transfer が走ることを確認予定
- 2026-05-19: 初期 reconcile 観測で **8 秒固定 sleep + polling tick で待つ設計が遅すぎる** ことが判明。 実機ログ (`20260519_013421`) で「8 秒経過 → polling 1 回目で device 未接続 → 30 秒後の polling 2 回目で device ready 検知 → initial reconcile」となり、 SAIVerse 起動から avatar 表示まで ~46 秒。 まはー指摘「gateway subprocess の起動完了タイミングでやればいい」が正論なので **event driven 化** に切替。
  - **本体 ``tools/mcp_client.py`` 側拡張**: ``MCPClientManager`` に ``on_server_ready(qualified_name, callback)`` / ``_fire_server_ready(qualified_name)`` / ``_ready_servers: Set[str]`` を追加。 ``_start_global_instance`` 成功時 + ``reconnect_server`` 成功時に fire。 既に ready な状態で register された callback は即 fire (= 後発 subscriber も取り逃さない)。
  - **``avatar_loader.py`` 側変更**: ``_INITIAL_DELAY_SEC`` (= 8 秒固定 sleep) 廃止、 代わりに ``_device_ready_event: threading.Event`` を追加。 registrar thread で ``get_mcp_manager()`` 利用可能を待って ``on_server_ready`` 登録 → callback で event set → reconcile thread が event 受け取って initial reconcile 実行、 という流れ。
  - **fallback**: hook 機構が壊れた / 本体が古くて hook 非対応 / gateway 起動失敗、 等の最終保険として ``_READY_EVENT_FALLBACK_SEC = 120`` 秒の上限 wait を入れる。 timeout 後は polling-only mode に降りるが session_id 取得失敗を許容するので無害。
  - **polling の役割整理**: 起動時の急ぎは event 側担当、 polling は **device 単独 reboot 検知** (= シナリオ A) のみに専念。 30 秒間隔は維持。
- 2026-05-19: 上記実装で **MCP server ready != device available** が判明。 gateway subprocess は数百 ms で起動するが、 ESP32 が WS server に接続完了するまで別途数秒〜十数秒 (= ESP32 boot + WiFi assoc + WS handshake) のラグがある。 server ready event 即時で session_id 取得すると "No ESP32 device connected" で失敗するため、 追加修正:
  - **burst window 導入**: server ready event 後、 ``_INITIAL_BURST_SEC = 60`` 秒間は ``_INITIAL_BURST_INTERVAL_SEC = 2`` 秒間隔で session_id 取得を retry (= ESP32 接続を即時検出)、 window 経過後は通常 ``_POLL_INTERVAL_SEC = 30`` 秒間隔に落とす (= ESP32 永久未接続時の polling 負荷削減)。
  - 実機検証済 (2026-05-19): SAIVerse 起動から数秒以内に initial reconcile + avatar transfer 完了を確認。
