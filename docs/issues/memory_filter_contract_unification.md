# SAIMemory 絞り込み契約の三重実装 — 実 adapter と2つのフェイクの意味ズレ

**発見**: 2026-07-29（判断プロンプト混入修理 c77bc40 の Codex 再レビュー medium2）
**状態**: 未解決（低リスクだが放置すると「テストは通るのに本番で逆」の隠れ回帰を作る）
**関連**: `saiverse_memory/adapter.py` `_payload_passes_context_filter` / `tests/test_day_scenario.py` RecordingAdapter / `scripts/run_day_sim.py` の ListMemoryAdapter

## 事実

メッセージ絞り込み（required_tags / line_role / scope / pulse / strict_tags）の実装が3箇所にあり、**意味が揃っていない**:

| 観点 | 実 adapter | test_day_scenario フェイク | run_day_sim フェイク |
|---|---|---|---|
| required_tags の一致 | **any**（どれか一つ） | **all**（全部） | all |
| タグ無し行の legacy 素通し | あり（'conversation' 要求時を除く） | あり | **なし**（常に除外） |
| line_role / scope / pulse | あり | なし | なし |
| paired_action 展開 | あり | あり | **なし** |

今夜の呼び出し（単一タグ + strict_tags=True）では3実装の結果が偶然一致するが、複数タグの要求や strict 指定漏れの回帰では、フェイクと本番が**逆の結果**を返しうる。この夜に実際、絞り込み系の意味ズレ（親保持機構の完全一致 / タグ救済の緩さ）が本番バグ2件の根だった——同族をテスト側にも抱えている状態。

## 修正方針（Codex 提案ベース）

- 判定ロジック（タグ any 一致・legacy 素通し・pulse override・strict）と paired_action 展開を**共通ヘルパーに一本化**し、実 adapter と両フェイクで共有する。
- 同じ契約テストを3実装すべてに通す（strict / non-strict / 複数タグ / conversation 要求 / タグ無し行）。

「二つ目の実装は一つ目を読んでから書く」——挙動の知識は正典1枚に集約する族の仕事。
