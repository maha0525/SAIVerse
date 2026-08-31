# あらすじ一覧が 500 件上限で「最新側」を切り落とす

## 状態

📋 **重複起票のため archive (2026-07-30)**。バグ自体が解決したのではなく (実装済み・実機検証待ち)、**同じバグの issue が同日に二枚立っていた**ため一本化した。

- **本体**: [arasuji_modal_500_limit_truncation](../arasuji_modal_500_limit_truncation.md) — こちらが生きている issue。症状・原因・検算・修正内容・実機検証の残りはすべてそこにある。
- 修正コミット: `a537441` (2026-07-29)

## なぜ二枚になったか

2026-07-29 の同じ夜、独立した二つの作業が同じ壁に当たり、互いを知らずに起票された。

1. エリスの記憶修復 (歪み世代 101 件削除 → 8 件再生成) の結果が UI 一覧に反映されないように見えた ← 本ファイルの発端
2. 「編纂タイミングが意図と違う」調査 (arasuji_levels §13) の過程で、Chronicle タブの最新が 7/21 で止まっていることに気づいた ← 本体 issue の発端

原因はどちらも一覧 API `/api/people/{id}/arasuji` の `ORDER BY level DESC, start_time ASC LIMIT 500` で、並びの末尾 = レベル1 の最新側から黙って欠ける形。**両方の発見経緯は本体 issue の「発見の経緯」節へ統合済み**。

## 本ファイルにあった別件の行き先

末尾に「被覆の範囲表示が歯抜けを表現できない」という、**500 件切り詰めとは別の未解決事項**が同居していた (同日の訂正記録を含む)。これは [chronicle_coverage_range_hides_gaps](../chronicle_coverage_range_hides_gaps.md) として独立させた。

移動前の原文は git 履歴 (`docs/issues/arasuji_list_limit_hides_newest.md`) に残っている。
