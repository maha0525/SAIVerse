# メタ判断レガシー経路 `_run_judgment` が lossy かつ到達不能になった

## 状態
未解決（クリーンアップ待ち）。緊急ではない（本番のバグ自体は別途修正済み）。

## 背景: 元のバグ（修正済み）
2026-06-29、air_city_a が 14:34 に共創小説 Track を `track_create`＋`track_activate` で
開始したが、その判断が **SAIMemory（`line_role='meta_judgment'` メッセージ）に保存されず**、
Life View / Pulse タイムラインにも出ず、ペルソナ自身が「なぜこの Track を始めたか」を
記憶できていなかった。

### 真因（確定）
`SAIVerseManager.__init__` で **自律 tick スレッド（AutonomyManager）をペルソナ登録
（`_run_persona_post_registration`）で起動した後、139 行ほど後で `pulse_controller` を
代入していた**。再起動直後、ペルソナ読み込みでこの隙間が広がり、最初の periodic tick
（14:33:59）がそこに刺さった。`getattr(manager, "pulse_controller", None)` が None を返し、
正規の playbook 経路（`submit_meta_judgment` → `meta_judgment_finalize`）に行けず、
**レガシー `_run_judgment` にフォールバック**した。

レガシー `_run_judgment`（`saiverse/meta_layer.py`）は:
- `_record_judgment_log` で `meta_judgment_log` テーブルには書く
- しかし **`append_persona_message(line_role='meta_judgment')` に相当する SAIMemory 保存を
  一切行わない**（最終応答テキストは `logging.info` するだけで messages に永続化しない）
- スペル結果もタプル丸ごと文字列化される（`("Created track '…`）など finalize と挙動が乖離

→ `meta_judgment_log` にはあるが SAIMemory に無い、という観測の正体。

### 適用済み修正（恒久対策）
`saiverse/saiverse_manager.py:__init__`: `sea_runtime` + `pulse_controller` の初期化を
ペルソナ登録（`_run_persona_post_registration`, Step 5）より**前**に移動。tick スレッドが
起動する時点で pulse_controller が必ず存在するため、本番のレースは閉じた。

## 残課題（このドキュメントの本題）
上記の構造修正により、`_run_judgment` への唯一の到達経路（`_run_judgment_via_playbook`
内の `pulse_controller is None` フォールバック, `meta_layer.py`）は**本番で到達不能**に
なった。しかし:

1. `_run_judgment` とその専用ヘルパ（`_get_heavyweight_client` / `_build_system_prompt` /
   `_extract_spells` / `_format_spell_results`）はコード上に残っている。
2. テストスイート（`tests/test_meta_layer.py`）は依然としてこの legacy 経路を「生きた経路」
   として広範に検証している（`test_legacy_path_*`、および pulse_controller 未設定で
   public 入口から入りフォールバックを期待する一連のテスト ≈ 11 件）。
3. legacy 経路は **lossy**（SAIMemory 保存欠落）であり、万一再び到達した場合は
   サイレントに記憶が失われる。

## あるべき対応
- `_run_judgment` と legacy 専用ヘルパを除去する（[[feedback_no_dead_code_via_flags]]
  「新実装で旧 path を残さない」）。
- `pulse_controller is None` 分岐は、lossy フォールバックではなく **tick スキップ＋次周期
  再評価**に置き換える（判断機会は失われない。構造修正済みなので通常は到達しない多重防御）。
- 関連テスト（`test_legacy_path_*` ほか）は、playbook 経路（`_FakePulseController` を立てる
  既存パターン, `test_meta_layer.py` 内）へ移行するか削除する。

## 関連
- `saiverse/meta_layer.py` — `_run_judgment_via_playbook` のフォールバック分岐、`_run_judgment`
- `saiverse/saiverse_manager.py` — `__init__` の初期化順序（修正済み）
- `builtin_data/tools/meta_judgment_finalize.py` — 正規経路の SAIMemory 保存（`append_persona_message`）
