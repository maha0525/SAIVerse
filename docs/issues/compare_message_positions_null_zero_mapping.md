# compare_message_positions が NULL created_at を 0 に写像する (正典順序の二枚目)

**発見**: 2026-08-31 (被覆補修 §16 の Codex 消し込み中、実装エージェントの同族走査)
**状態**: 🟣 検証待ち — 実装・隔離回帰テスト済み、PR レビュー待ち。未マージのため archive へ移さない。
**深刻度**: P3 — created_at が NULL の行と 0 (1970 epoch) または負の epoch の行が同一 DB に混在すると順序が逆転しうる

## 事実

メッセージの正典順序の正は「NULL created_at は全ての実時刻より前」
(`sai_memory/memory/storage.py` の `_canonical_before_clause` 族)。
被覆補修 (§16) はこの共有述語に一本化済みだが、発見時の
`sai_memory/arasuji/storage.py` の `compare_message_positions` は NULL→0 の
写像で比較しており、規則の二枚目として残っていた。

## 修正の全体と責任

インポートで時刻が欠けた生ログも、履歴表示・Chronicle・提示窓で同じ歴史位置に
並ばなければならない。保存値はそのまま読み、比較器が `memory.storage` の
`canonical_position_key` を使う。NULL は負の epoch を含む全実時刻より前、
NULL 同士・同秒同士は rowid 順。この規則は W8 の既存仕様であり、新しい裁定ではない。

修正箇所は呼び手ごとの境界調整ではなく、全呼び手が使う比較器。
SQL の並び・境界句や永続データの移行は変更しない。既存データに対する次の位置判断が揃い、
既存の起点・fold を一括で書き換える処理は加えない。

### 呼び手の確認

全呼び手は `sea/session_lifecycle.py` にある。

- `_is_ahead_of` → 冷えた起点の前進の可否。比較不能なら実在を調べ、現在起点だけが不在の場合に前進する。
- `_advance_anchor_preserving_folds` → 新起点以降に残る fold の保持。比較不能な fold は保持する。
- `resolve_metabolism_anchor` → 新モデルの起点を最前線と他モデル行から選ぶ。比較不能なら最前線に倒す。
- `_plan_marker_crossing_record` → スルースのパンマーカーを越えるかの判定。比較不能なら前進しない。
- `_plan_window_refill` → 重なるあらすじに、既に開いた範囲より古い材料があるかの判定。比較不能なら拡張しない。

前四者は `_compare_positions` 経由、読み戻しは直接呼ぶ。
同一 ID の早期 return も除き、同じ不在 ID 同士は公開契約どおり `None` とする。
どの呼び手も「不在でも同一 ID なら比較成立」を必要としていない。
照会エラーは従来どおり例外で伝え、呼び手の strict / 縮退の選択に任せる。

## 検証

- 追加回帰は `tests/test_session_anchor_rows.py`。NULL / 負の epoch / 0 / 正の epoch と同値の rowid 順を混在させ、全組合せの比較と SQL の提示窓境界が一致することを確認。
- 不在 ID (片方・両方・同じ ID) は `None`、同一の実在 NULL 行は `0`。
- 合成データの実 SQLite から起点解決まで通し、NULL 起点の前進・fold 保持・未通過範囲の記録・再解決の冪等・他モデルの借用を確認。
- 修正前は追加回帰を含む絞り込み実行で 6 failed / 5 passed。修正後は `test_session_anchor_rows.py` / `test_coverage_repair.py` / `test_time_order_canonical_w8.py` / `test_window_refill.py` / `test_window_floor.py` の計 263 passed。
- 変更 Python の `ruff check` 合格。テストは一時 `SAIVERSE_HOME` と一時 DB だけを使い、本番ペルソナ・永続履歴・LLM は使っていない。
- 残る境界: 全スイート、実機の会話・LLM 生成は未実施。読み戻しの既存スイートは通したが、NULL 混在での読み戻し全体を通す新規テストは本変更に含まない。

## 経緯

- 2026-08-31: §16 の消し込み中に発見。旧状態は「未解決 — 影響先が §14 の anchor 前進系のため、v0.3 リリース前には触らない (まはー裁定を経ず既存機構の挙動を変えない)」。当時は NULL と 0 の混在だけを影響条件としていた。
- 2026-10-02: v0.3.20 発行後のバックログ修正として着手。W8 の既存仕様と全呼び手を再確認し、負の epoch にも同じ逆転があることを隔離回帰で確認。共有キーへ統一し、PR レビュー待ちにした。

## 関連

- `tests/test_coverage_repair.py` — 一本化済み側の回帰
- [W8 の正典順序と NULL の裁定記録](../handoff/2026-07-22_w8_time_order_handoff.md)
- [intent: あらすじのレベル制](../intent/arasuji_levels.md) §14 / §16
