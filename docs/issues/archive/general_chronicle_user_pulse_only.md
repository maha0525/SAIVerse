# Issue: General Chronicle が user pulse でしか生成されない (自律稼働中は欠落)

**ステータス**: ✅ 解決済み (2026-07-04, Phase 0)
**優先度**: medium
**作成日**: 2026-05-09
**関連**: `sea/runtime.py:_generate_chronicle`, [`general_chronicle_metabolism_trigger.md`](general_chronicle_metabolism_trigger.md), Track Chronicle 設計議論, `docs/intent/memory_architecture_v2.md` §6.3

## 背景

`_generate_chronicle` (`sea/runtime.py:1817-2080`) は内部で **`pulse_type == "user"` のときのみ実行**し、それ以外 (auto / schedule / meta_judgment) は早期 return する。

```python
pulse_type = getattr(persona, "_current_pulse_type", None)
if event_callback and pulse_type == "user":
    # ユーザー確認 dialog (60秒タイムアウト) 経由で実行
    ...
else:
    # auto / schedule / meta_judgment pulse は早期 return
    LOGGER.info(
        "[metabolism] Skipping Chronicle generation confirmation "
        "(pulse_type=%s, ...)"
    )
    return
```

理由は**ユーザー確認 dialog がフロントエンド経由でしか応答できない**ため。自律稼働 / 定期実行 / メタ判断 pulse では UI が紐付いていないので、確認待ちが 60 秒タイムアウトでただ詰まるだけ → スキップする実装。

## 影響

- **自律稼働中にどれだけメッセージが溜まっても Chronicle 化されない**
- 長時間自律で動いた後にユーザーが話しかけて初めて (大量の) Chronicle 生成が走る
- v0.3.0 の「自律稼働を中心軸に据える」方針 (バイオリズムによる連続稼働) と真っ向から噛み合わない
- Track Chronicle (`docs/intent/persona_cognition/track_chronicle.md`) が **独立経路**として実装されることで Track 単位の必要情報維持は救えるが、**General Chronicle (全体の流れ) の網羅性は別問題として残る**

## 確認事項

1. ユーザー確認 dialog はそもそも自律稼働では不要では? — ペルソナ自身が知らないところで自動的に Chronicle が作られても問題はない。
2. 既存 dialog はあくまでユーザー対話中の「いきなり LLM コストが発生する」体験を回避するための UI 上の配慮。自律稼働では既に LLM コストが暗黙発生している (= ペルソナが動くたびに LLM 呼び出し) ので、Chronicle 生成のコストもその一部として扱える。
3. dialog 自体を削除する/しないの判断は別議論。少なくとも自律稼働では dialog をスキップして自動承認にする変更が適切。

## 解決案候補

### 案 A: pulse_type 制限を撤廃、自律稼働では自動承認
- `_generate_chronicle` 内の `pulse_type == "user"` 条件を緩和
- 非 user pulse 時は dialog を skip して自動承認で実行
- LLM コストは 1 回の Chronicle 生成あたり数バッチ × 軽量モデルなので、自律稼働中の頻度ならコスト許容範囲

### 案 B: dialog 自体を削除 (推奨設定可能化)
- ユーザー側にも自動承認モードを提供
- ペルソナごとの設定で「Chronicle 生成は自動」を選べる
- user pulse でも dialog 不要にできる人向け

### 案 C: Chronicle 生成自体を非同期 (バックグラウンド) 化
- Pulse 完了後に別スレッドで生成
- ユーザー確認は不要 (Pulse の応答待ちを伸ばさないので)
- 生成中の Pulse は「まだ Chronicle が無い状態」で動くリスクあり

## 関連リソース

- `sea/runtime.py:_generate_chronicle` (line 1817-2080)
- `sea/runtime.py:_run_metabolism` (line 1725-)
- `sea/runtime_context.py:438-470` (anchor 期限切れ時の Chronicle 生成 trigger)
- 関連 issue: [`general_chronicle_metabolism_trigger.md`](general_chronicle_metabolism_trigger.md) — 生成 trigger を Metabolism 押し出し対象判定に変更する話。本 issue とは独立 (こちらは「いつ走るか」、本 issue は「誰の pulse で走るか」)
- 関連 Intent: `docs/intent/persona_cognition/track_chronicle.md` — Track 単位での独立経路を設けることで Track 観点の救済はできる

## 解決内容 (2026-07-04, Phase 0)

`docs/intent/memory_architecture_v2.md` §6.3 の方針（案 A: pulse_type 制限撤廃、ただしトグルで制御可能に）で解決した。

- `database/models.py` の AI テーブルに `AUTONOMOUS_CHRONICLE_ENABLED`（Boolean, デフォルト `True`）を追加
- `sea/runtime.py:_generate_chronicle` の分岐を変更: `pulse_type != "user"` かつ `AUTONOMOUS_CHRONICLE_ENABLED` が True なら確認ダイアログなしで即生成。False なら従来どおりスキップ。user pulse の確認ダイアログ挙動は変更なし
- ペルソナ設定 UI（SettingsModal）に「自律行動中も記憶整理（Chronicle生成）を行う」トグルを追加（既存の「Chronicle 自動生成」トグルの隣）
- デフォルト ON: 生成コストは軽量モデル (`memory_weave_model`) で小さく、記憶が残らない実害の方が大きいと判断

案 B（dialog 自体の削除）・案 C（非同期化）は採用せず、user pulse の確認 dialog はそのまま維持した。

## ログ

- 2026-05-09: issue 起票。Track Chronicle 設計議論で「Track 用は独立経路で実装」を決めた際、General Chronicle 側にこの欠落問題が残ることが顕在化したため別 issue として切り出し。
- 2026-07-04: `memory_architecture_v2.md` Phase 0 で解決。案 A を採用し実装。
