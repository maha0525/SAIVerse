# イベント配信口の包み紙が「配信口なし」の判定を全部すり抜けさせる

**発見**: 2026-08-31 (§1 スモーク④ sync 経路の実機検証中)
**状態**: 🔲 未解決 — 修正は 1 行だが実行時の経路選択が変わるため、リリース前に触るかはまはー裁定待ち
**深刻度**: P3 — 現状の実害は「無駄なストリーミング処理」と「設計意図と実挙動の乖離」。保存は正常 (実測 status=saved)。

## 事実 (2026-08-31 実測)

ブラウザを全部閉じた状態で sophie_city_a の oneshot スケジュール (12:00) を発火させた。
設計上は「配信口 (event_callback) が無い発言は sync (非ストリーミング) に落ちる」
はずが、実際は `Streaming check: ... event_callback=True` → ストリーミング経路を通った。
発言の保存自体は正常 (`Normal-stream finalize: msg=sophie_city_a_room:2214 status=saved`)。

## 原因

`sea/runtime_runner.py` の `wrapped_event_callback` が、包む相手 (`event_callback`) が
None でも**常に関数オブジェクトとして定義され、そのまま下流へ渡される**
(中で `if event_callback:` して無言で捨てるだけ)。下流の判定はすべて
「callback が None かどうか」で書かれているため、包み紙が全部を真にする:

- `sea/runtime_llm.py` の `use_streaming` / `use_tool_streaming`
  (`event_callback is not None`) — **sync 経路は実運用で到達不能**になり、
  唯一のスイッチは env `SAIVERSE_LLM_STREAMING=false` だけになっている。
  誰も受け取らない streaming chunk の生成・下書き行の管理が毎回走る。
- `sea/runtime.py` の Playbook 許可確認 (`if event_callback is None: 「聞く窓口が
  無いのでスキップ」`) — 窓口が無いのに「ある」と判定し、誰も見ていない画面へ
  許可ダイアログのイベントを流して応答タイムアウトまで待つ形になり得る
  (auto Pulse は `auto_mode` の判定が先に受けるが、schedule Pulse は素通り)。

## 修正の方向

包み紙を None 透過にする — `event_callback` が None のときは
`wrapped_event_callback` も None にする (1 行)。ただしこれで
「UI の開いていない定時発言が本当に sync 経路を通る」ようになるため、
実行時の挙動が変わる。リリース直前に入れるか、v0.3 は現状 (streaming が
虚空に流れるが保存は正常) のまま出して直後に直すかは、まはーの裁定で決める。

## 関連

- [`2026-08-30_release_sweep_checklist.md`](../handoff/2026-08-30_release_sweep_checklist.md) §1 スモーク④ — この検証の発端。④の実機確認は env OFF での再起動 (選択肢 b) でのみ可能
- `sea/runtime_runner.py:63` (包み紙) / `sea/runtime_llm.py:4122` (use_streaming) / `sea/runtime.py:971` (許可の窓口判定)
