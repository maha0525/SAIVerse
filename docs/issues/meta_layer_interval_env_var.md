# Issue: `SAIVERSE_META_LAYER_INTERVAL_SECONDS` env で interval 上書きが効くか確認

**ステータス**: 🔲 未着手
**優先度**: low
**作成日**: 2026-05-08
**関連**: `saiverse/meta_layer.py`, [docs/intent/persona_cognition/README.md](../intent/persona_cognition/README.md) Phase 4 進捗表

## 背景

Phase 4 進捗表の項目に「env `SAIVERSE_META_LAYER_INTERVAL_SECONDS` で interval 上書き」が 🟡 (コード上 `DEFAULT_INTERVAL_MINUTES = 50` のみ、env 連動は未確認) と記載されている。

メタレイヤーの定期 tick interval をデバッグや特殊運用で短くしたい場合に env で上書きできる仕組みが想定されているが、実装が現状動いているかコードで確認していない。

## 確認事項

1. `saiverse/meta_layer.py` で `SAIVERSE_META_LAYER_INTERVAL_SECONDS` の読み出しがあるか
2. 読み出しがあれば実機で env 設定して挙動確認
3. 読み出しが無ければ追加実装

## 解決案候補

### 案 A: env 変数読み出しを追加 (シンプル)

```python
import os
DEFAULT_INTERVAL_SECONDS = int(os.environ.get("SAIVERSE_META_LAYER_INTERVAL_SECONDS", 50 * 60))
```

メタレイヤー初期化時に env を見て、無ければデフォルト (50 分)。

### 案 B: ペルソナ単位で interval をカスタマイズ

- AI テーブルに `META_LAYER_INTERVAL_SECONDS` カラム追加
- ペルソナごとに違う interval を設定可能
- env はグローバルデフォルトに

これは「特定ペルソナだけ頻繁にメタ判断したい」需要があれば。普段は不要。

## 関連リソース

- `saiverse/meta_layer.py` — メタレイヤー実装
- `saiverse/autonomy_manager.py` — メタレイヤー定期 tick タイマー (現状の interval 管理場所候補)
- [docs/intent/persona_cognition/README.md](../intent/persona_cognition/README.md) Phase 4 進捗表

## ログ

- 2026-05-08: issue 起票。実機で問題が出ていない (デフォルト 50 分で動いている) ので低優先度。デバッグ時に必要になったら着手。
