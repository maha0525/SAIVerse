# 補修 UI の表示磨き — 吸収が進捗カウンタに乗らない / 確認モーダルが横長すぎる

**発見**: 2026-08-31〜09-01 (被覆補修の実機検証中、まはー観察 2 件)
**状態**: 🟣 一部検証待ち — §2 の表示修正候補を準備。§1 の進捗表示は本変更の対象外。実視認・レビュー未完了、リリース非遮断
**深刻度**: P3

## 1. 吸収の合体が進捗カウンタに乗らない

補修ジョブの進捗表示「Chronicle を生成しています (N/M)」は従来の通常チャンク (executor) のコミットでしか進まない。吸収 (run_absorption の合体・上位語り直し) が処理の大半を占める回では、実際は 5〜8 秒に 1 本のペースで進んでいるのに **0/M のまま**に見える (2026-09-01 の aifi 再補修で実測 — まはーが「全然進まない」と不安になった)。合体 1 件ごとに run のメッセージ数ぶん進捗イベントを流す配線を足す。

## 2. 実行前の確認モーダルが横長すぎる

「過去の会話をあらすじにする」モーダルがワイド画面で間延びする (2026-08-31 まはー観察)。max-width を入れて中央寄せに。

## 関連

- `sai_memory/arasuji/absorption.py` (進捗イベントの発行元候補) / `api/routes/people/arasuji.py` (ジョブ進捗の中継) / `frontend/src/components/memory/ArasujiViewer.tsx` (モーダルとカウンタ表示)
- [arasuji_tiny_run_absorption (archive)](archive/arasuji_tiny_run_absorption.md) — 本体

## §2 の修正候補と検証 (2026-10-02)

`ArasujiViewer.module.css` の確認窓は `min-width: 400px; max-width: 90vw` で、広い画面では説明文に合わせて伸び、狭い画面では最小幅と padding がはみ出しを作っていた。

- 確認窓の外寸を `width: 100%; max-width: 36rem; box-sizing: border-box` にし、最小幅を解除。画面の余白と中央寄せを保つ。
- 長いモデル名は折り返し、狭幅の見積もり行と操作列も折り返す。縦スクロールは既存 `ModalOverlay` に任せ、safe center で上端が画面外に隠れるのを避ける。
- 同じクラスを使う「手動の畳み」の確認窓にも同じ幅上限が適用される。進捗経路、ジョブ開始/取消/再接続、テーマ色、文言は変更しない。
- `cd frontend && node scripts/test-arasuji-modal-layout.cjs`: CSS 契約と実コンポーネントの合成データで、繰り返し開く→取消→再表示→背景から閉じる、内側から外側へのドラッグで閉じない既存ガードを検証。ジョブは起動しない。
- 実視認は未完了: dot クラウドブラウザーが隔離 localhost を `net::ERR_BLOCKED_BY_CLIENT` で拒否。次は両テーマ・360/960/1600px・低い画面で、長文、上下スクロール、閉じる/再表示を確認する。issue は未解決に残す。

### §1 の扱い

起票時の「通常チャンクのコミットでしか進まない」は現行コードと別途突き合わせが必要。本変更では既存の吸収・進捗イベント経路を追加/削除せず、§1 の解決を主張しない。

検証記録: `npm test`、`node scripts/test-arasuji-modal-layout.cjs`、`npx tsc --noEmit`、`npm run build`、`python scripts/check_in_flight.py`、`git diff --check` は通過。全体 `npm run lint` は既存 CJS にも再現する設定エラー (`react-hooks/set-state-in-effect` に plugin が適用されない) で停止。実視認の代替合格にはしない。
