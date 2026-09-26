# 正常終了なのに確定できなかった下書き行を、誰も後始末しない

**発見**: 2026-09-14 (中断通告の追加への代行レビュー二巡目、指摘 2 と参考 5)
**状態**: 未解決
**深刻度**: P3 — どちらも「確定の失敗」「空応答」という異常系の中の残債で、通常運転では踏まない

ストリーミングの発言は、先に空の下書き行を建物の記録に作り、本文が揃ったら確定して中身を入れる。Beat が例外で死んだ回の後始末 (`_settle_placeholder_on_beat_death` — 2026-09-25 の作り直しで後始末は `sea/reply_stop_exit.py` の `settle_reply_stop` に一本化、保存は `_save_cut_utterance` / `_save_draft_on_beat_death`) は整備済みだが、**例外を出さずに正常終了へ抜ける経路に、確定できなかった下書き行の受け皿が二つ欠けている** (sea/runtime_llm.py の no-spell 完了パス)。

1. **確定が「保存失敗」を返した回**: エラーログは残るが再試行は無く、本文の入らない下書き行がそのまま残る。これを後から掃く機構も現状無い (`orphaned_streaming_placeholder_cleanup.md` の候補 3「起動時に掃く」は不採用のまま)。画面はこの行を描かないので、発言は本人の記憶にしか残らない。
2. **リトライしても本文が空だった回**: `text=""` のまま確定を打つ。空文字の取り下げ判定 (`_withdraw_if_nothing_was_said`) は「ストリーム中の停止」「締めの Beat (H-1)」「Beat 死亡 (N-1)」の三箇所にはあるのに、この正常完了だけ通っていない — 同族の四箇所目。本文の無い記録が建物とペルソナのログに永続する。

## 直し方 (案)

2 は H-1/N-1 と同じ取り下げ判定をこの完了パスにも通すだけで揃う。1 は「その場で再試行するか、起動時に未確定の下書き行を掃くか」の設計判断が要る (候補 3 の再検討)。

## 関連

- `docs/issues/orphaned_streaming_placeholder_cleanup.md` (親問題 — Beat 死亡側は解決済み)
- [server_cut_stream_writes_no_interruption_notice.md](server_cut_stream_writes_no_interruption_notice.md) (この残債が見つかったレビューの対象)
