# Issue: あらすじ実行前の概算費用に金額が表示されない

**ステータス**: 🔲 未着手（原因確認済み。文言修正・回帰追加は未実施）
**優先度**: high（実行前に費用を判断する表示）
**作成日**: 2026-10-03
**関連**: [補修 UI の表示磨き](repair_ui_progress_and_modal_polish.md) / [あらすじレベル制](../intent/arasuji_levels.md)

## 症状

あらすじの確認モーダルで有料扱いのモデルを選んだとき、概算費用が
日本語では `約 ${p1} USD`、英語では `Approx. ${p1} USD` のように表示される。
通貨は入るが金額が入らず、利用者が費用を読んでから実行を判断できない。
無料表示とは別の分岐であり、見積もり計算自体の誤りはこの調査では確認していない。

## 原因の経路（調査版 `370ebbc5b15b95dd7d8bd8d6beadc9632cd8fb8b`）

1. [`ArasujiViewer`](https://github.com/maha0525/SAIVerse/blob/370ebbc5b15b95dd7d8bd8d6beadc9632cd8fb8b/frontend/src/components/memory/ArasujiViewer.tsx#L1313-L1323)
   は整形済み金額を `p1`、通貨を `p2` として渡す。
2. [`messages.json` の `components.memory.ArasujiViewer.text117`](https://github.com/maha0525/SAIVerse/blob/370ebbc5b15b95dd7d8bd8d6beadc9632cd8fb8b/frontend/src/i18n/messages.json#L3278-L3281)
   が `約 ${p1} {p2}` / `Approx. ${p1} {p2}` になっている。
3. [`translate`](https://github.com/maha0525/SAIVerse/blob/370ebbc5b15b95dd7d8bd8d6beadc9632cd8fb8b/frontend/src/i18n/core.ts#L27-L32)
   の正規表現 `(?<!\$)\{(\w+)\}` は `$` の直後の `{p1}` を補間しない。
   したがって `p2` だけが置換され、金額のプレースホルダーが残る。

PR #359 の CSS 変更より前からある不具合であり、モーダル幅の後退ではない。
メッセージ外出しの `04f59154` に同じ文言と補間条件が存在する。

## 修正時に守ることと検証

まずこの文言と引数の組み立てを補間契約に合わせる。ほかのメッセージで意図して残す
`${...}` を壊さないため、この1キーのために全体の正規表現を無条件に緩めない。
画面が所有する「表示する金額」と、見積もり API の計算・実行ジョブは分ける。

隔離データで、ja/en の金額と通貨、0.01未満の4桁、通常の2桁、無料分岐を確認する。
実行ボタンを押さず、実際の翻訳関数 → コンポーネント → 確認画面まで通し、
未置換プレースホルダーが無いことを自動テストにも残す。

## 記録

- 2026-10-03: [PR #359 のレビュー](https://github.com/maha0525/SAIVerse/pull/359#issuecomment-5964787763) による実ブラウザの指摘をソース照合し、独立した未解決 issue として記録。今回の変更は docs のみで、費用表示の修正・実行・課金はしていない。
