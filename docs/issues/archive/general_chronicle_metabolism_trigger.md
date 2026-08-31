# Issue: General Chronicle 生成 trigger を Metabolism 押し出し対象判定に変更

**ステータス**: ✅ **解決 (2026-07-21、W4 = 体験の構造 工程(2))** — `generate_chronicle` に `evict_boundary_epoch` が入り、自動経路の編纂対象は「Metabolism で退役する新 anchor より古い未編纂」に限定された (退場時圧縮 §4-1)。コンテキスト内に残るメッセージは圧縮されない。episode digest close 時生成 (範囲指定) は W1 の (a')、押し出し時の束ねは W4 の整列チャンクが実装
**旧ステータス**: 🔲 未着手 → 解決設計は [episode.md](../../intent/episode.md) §6.1 に統合（2026-07-13）
**優先度**: low
**作成日**: 2026-05-09
**関連**: Track Chronicle 設計議論 (`docs/intent/persona_cognition/`), `sea/runtime.py:_generate_chronicle`

## 背景

現状の General Chronicle (arasuji) 生成 trigger は「未処理メッセージが (バッチサイズ × 1) 件溜まっているか」の閾値ベース (`sea/runtime.py:1907-1921` の `qualifying_batches` チェック)。

本質的には「Metabolism で押し出される予定のメッセージを Chronicle 化する」のが正しい。**今コンテキスト内に残っているメッセージのあらすじを作る必要はない** (LLM が直接読めるため)。

Track Chronicle (Track 内の必要情報を圧縮保存して再開時に呼び戻す機構) の設計議論で、書き込みの本質が「Metabolism で押し出された内容から必要情報を圧縮保存すること」であると整理された。General Chronicle も同じ本質に従うべきだが、当面は独立動作で問題ないので後回し。

## 確認事項

1. 現状の閾値ベース trigger がどこで効いているか (`_generate_chronicle` 内の `qualifying_batches == 0` 早期 return)
2. Metabolism 押し出し対象の特定ロジック (`_run_metabolism` の `evict_count = len(current_messages) - keep_count`)
3. 「押し出される予定」を判定するロジックを Chronicle 生成側に渡すインタフェース設計
4. 短時間で大量メッセージが流れた時、押し出し対象が大きくなるケースの分割戦略

## 解決案候補

### 案 A: 押し出し予定メッセージ ID を `_generate_chronicle` に渡す
- `_run_metabolism` が `evicted_message_ids` を計算
- `_generate_chronicle(persona, evicted_ids=...)` に渡す
- Chronicle 生成は渡された ID 範囲のみを対象に (まだコンテキストに残ってる分は無視)

### 案 B: anchor 移動を起点に判定
- Metabolism で anchor が動く前後の差分メッセージ群を Chronicle 化対象として扱う
- 既存の「anchor 期限切れ時にも生成」経路 (`runtime_context.py:438-464`) と整合する形

### 案 C: 当面据え置き
- Track Chronicle が先行実装される間、General は現状維持
- Track Chronicle で得られた知見 (バッチサイズ未満許容、再生成ロジック等) が安定したら General にも展開

## 関連リソース

- `sea/runtime.py:_generate_chronicle` (line 1817-2080) — Chronicle 生成本体
- `sea/runtime.py:_run_metabolism` (line 1725-1759) — Metabolism 実行
- `sea/runtime_context.py:436-470` — anchor 期限切れ時の Chronicle 生成
- `sai_memory/arasuji/generator.py` — Chronicle 生成ロジック
- Track Chronicle 設計議論: `docs/intent/persona_cognition/revisions.md` v0.31〜

## ログ

- 2026-05-09: issue 起票。Track Chronicle 設計議論 (Phase 3 中断・再開機構の検討) で「書き込みの本質は Metabolism で押し出された分の圧縮」と整理された流れで派生。Track Chronicle 実装後、知見を取り込んで再評価する想定。
