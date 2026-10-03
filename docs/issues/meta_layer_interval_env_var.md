# Issue: `SAIVERSE_META_LAYER_INTERVAL_SECONDS` env で interval 上書きが効くか確認

**ステータス**: 保留（旧 v1 定期メタ判断向けの提案は失効。後継の実機検証を完了扱いにしない）
**優先度**: low
**作成日**: 2026-05-08

## 現行仕様（2026-10-02 確認）

**旧メタ判断の interval 読み出しを `MetaLayer` に追加する作業は不要**。
[Track 撤廃計画 §7.3–7.4](../intent/track_retirement.md) で v1 メタ判断の退役が裁定・実装され、
[`MetaLayer`](../../saiverse/meta_layer.py) に `on_periodic_tick` / `on_track_alert` /
`_classify_situation` は存在しない。残るのは判断系の共有基盤（ロック・設定・ログ）。
同計画にある「実機検証は v0.4 送り」という留保は維持する。

一方、**`SAIVERSE_META_LAYER_INTERVAL_SECONDS` という env 自体は廃止されていない**。
現在の読み手は [`AutonomyManager.__init__`](../../saiverse/autonomy_manager.py) で、
定期メタ判断から縮退した watchdog の間隔を決めるフォールバックとして残っている。

- 優先順位はコンストラクタ引数 → `MetaLayer._load_judgment_config` が返す
  `periodic_interval_minutes` → env（秒を分へ換算）→ 既定 50 分。
- 設定読み出しは欠けた値を既定値で埋めるため、DB の個別指定が無くても通常は
  `periodic_interval_minutes=50` が返り、env より先に採用される。env は常に
  全ペルソナを上書きする設定ではない。
- env を読む経路では下限 0.5 分、`float` に変換できない文字列は警告して 50 分へ戻る。
- v0.3 の自律駆動は [`AUTONOMOUS_DRIVING_SHIPPED=False`](../../saiverse/autonomy_wiring.py)
  で止まっている。env の変更だけで判断点や watchdog が動き出すわけではない。
  出荷範囲と後続設計は [自律行動 v3 §11](../intent/autonomous_behavior_v3.md) を参照する。

**検証範囲（2026-10-02 の実行記録）**:

- **リポジトリの既存自動テスト**: 隔離環境・LLM なしで
  [`test_meta_layer.py`](../../tests/test_meta_layer.py)、
  [`test_autonomy_manager.py`](../../tests/test_autonomy_manager.py)、
  [`test_v03_autonomy_gate.py`](../../tests/test_v03_autonomy_gate.py) を実行した。
  これらは MetaLayer の共有基盤、watchdog、v0.3 の停止を扱うが、
  **env の読み出しと上記の優先順位を検証するテストではない**。
- **その場の手動・合成確認（リポジトリに未収録）**: 合成 manager と、起動しない
  `AutonomyManager` コンストラクタの7ケースで、引数・設定・env の優先順位、
  設定の既定値が env に優先すること、秒→分換算・下限・不正値・未設定時を確認した。
  退役メソッド3件の不存在も手動で確認した。これは今回の確認記録であり、
  将来の変更を継続監視する自動回帰テストがあるという意味ではない。
- **未検証**: 実ペルソナの自律起動と interval の実時間検証。v0.4 の留保は残る。

## 経緯: 起票時の記録（以下の確認・追加実装案は現行手順ではない）

2026-10-02: 旧 v1 判断の退役と、名前を残した env の現存を分けて記録した。
2026-10-03: 関連6テスト（MetaLayer / AutonomyManager / v0.3 gate / Pulse 復帰 / Spell 不使用 / SpellList）を隔離 HOME・LLM なしで再実行し、120 passed（既存 warning 5件）。この再実行にも env 優先順位の自動テストは含まれない。
2026-10-03: [PR #360 のレビュー](https://github.com/maha0525/SAIVerse/pull/360#issuecomment-5964735859) を受け、自動テストとその場の手動・合成確認を区別した。
以下は 2026-05-08 時点の未確認事項と提案であり、退役した判断経路や新しい DB 列を
復活・追加するための指示ではない。

**起票時のステータス**: 🔲 未着手
**優先度**: low
**作成日**: 2026-05-08
**関連**: `saiverse/meta_layer.py`, [docs/intent/persona_cognition/README.md](../intent/persona_cognition/README.md) Phase 4 進捗表

### 背景

Phase 4 進捗表の項目に「env `SAIVERSE_META_LAYER_INTERVAL_SECONDS` で interval 上書き」が 🟡 (コード上 `DEFAULT_INTERVAL_MINUTES = 50` のみ、env 連動は未確認) と記載されている。

メタレイヤーの定期 tick interval をデバッグや特殊運用で短くしたい場合に env で上書きできる仕組みが想定されているが、実装が現状動いているかコードで確認していない。

### 確認事項

1. `saiverse/meta_layer.py` で `SAIVERSE_META_LAYER_INTERVAL_SECONDS` の読み出しがあるか
2. 読み出しがあれば実機で env 設定して挙動確認
3. 読み出しが無ければ追加実装

### 解決案候補

#### 案 A: env 変数読み出しを追加 (シンプル)

```python
import os
DEFAULT_INTERVAL_SECONDS = int(os.environ.get("SAIVERSE_META_LAYER_INTERVAL_SECONDS", 50 * 60))
```

メタレイヤー初期化時に env を見て、無ければデフォルト (50 分)。

#### 案 B: ペルソナ単位で interval をカスタマイズ

- AI テーブルに `META_LAYER_INTERVAL_SECONDS` カラム追加
- ペルソナごとに違う interval を設定可能
- env はグローバルデフォルトに

これは「特定ペルソナだけ頻繁にメタ判断したい」需要があれば。普段は不要。

### 関連リソース

- `saiverse/meta_layer.py` — メタレイヤー実装
- `saiverse/autonomy_manager.py` — メタレイヤー定期 tick タイマー (現状の interval 管理場所候補)
- [docs/intent/persona_cognition/README.md](../intent/persona_cognition/README.md) Phase 4 進捗表

### ログ

- 2026-05-08: issue 起票。実機で問題が出ていない (デフォルト 50 分で動いている) ので低優先度。デバッグ時に必要になったら着手。
