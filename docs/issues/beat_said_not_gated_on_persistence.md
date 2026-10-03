# 「建物へ喋った」の印が、書き込みの成否を見ずに立つ

**発見**: 2026-09-14 (中断通告の追加への代行レビュー二巡目、指摘 3)
**状態**: 検証待ち (採番による成功判定は develop に取り込み済み、実地の異常経路の確認待ち。下記の既存の未補填2経路は未解決)
**深刻度**: P3 — 発火には「建物への書き込みが DB で失敗し、かつ直後に Beat が例外で死ぬ」の重なりが要る

## 症状

sea/runtime_llm.py で、建物へ本文を直接書く経路 (`_emit_say_and_capture` を呼ぶ側) の複数箇所 (2026-09-14 時点で 4580 / 4629 / 5484 / 5647 付近) が、書き込みの成否を見ずに `beat_said = True` を立てる。書けたかどうかの判定は戻り値の `message_id` の有無 (DB 採番) で行うのが repo の裁定 (builtin_data/tools/tell.py) で、対照的に `_emit_beat_segments` 経由の枝は戻り値を見ている。

`beat_said` は「建物には本文があるのに本人の記憶に無い」形を防ぐ補填 (`_backfill_memory_on_beat_death`) の発火条件なので、書き込みが失敗した回にこの印が立ったまま Beat が例外で死ぬと、**建物に存在しない本文が本人の記憶にだけ書かれる** — 補填が防ごうとした食い違いの裏返しが起きる。

## 直し方 (案)

各呼び出し箇所で戻り値を受け、`isinstance(bmsg, dict) and bmsg.get("message_id")` のときだけ `beat_said` を立てる。中断通告の追加 (2026-09-14) で同じ判定を `_partial_landed_bid` に入れた実装がその場 (5484 の 3 行下) にあるので、形はそれに揃える。4 箇所それぞれの文脈 (ループ側の別判定 4758 / 5239 との棲み分け) を確認しながら一括で。

## 関連

- [server_cut_stream_writes_no_interruption_notice.md](archive/server_cut_stream_writes_no_interruption_notice.md) (同じ判定を通告側に入れた実装)
- [unfinalized_placeholder_on_clean_exit.md](unfinalized_placeholder_on_clean_exit.md) (同じレビューで出た隣の残債)


## 全体で守ることと今回の境界

生成 → 建物の履歴への保存 → 本人の記憶への保存、の間で例外が起きても、建物にある本文だけを記憶へ補填する。建物の保存成功の真実は DB 採番の `message_id` にあり、UI への送信や dict の戻り値だけでは代用できない。`HistoryManager.add_to_building_only` は insert が失敗すると未採番の `for_insert` を返し、`RuntimeEmitters.emit_say` はそれを透過する。

今回の変更箇所は、成功の証拠を受け取って補填用の状態を更新する境界に限る。

- tool streaming の `both` / `text`、placeholder を作れなかった streaming fallback、同期の直接発話の4経路で、`message_id` がある回だけ `beat_said` と `beat_said_text` を更新する。後続の失敗で前の成功の印や本文を消さない。
- 当初の「対照的に segment 経由は戻り値を見ている」は不十分だった。`_emit_beat_segments` 自身が dict だけで成功扱いしていたため、同じ採番条件へ揃える。
- segment の bool は「どれか1件の保存成功」のまま保つ。ただしその bool だけでは締めの本文の証拠にならない。各 `BeatSegment.saved_message_id` に直接保存の採番を残し、3 caller は締めの segment 自身の採番を確かめてから `final_continuation` を補填対象にする。先行成功・締め失敗の組合せで、未保存の締めを記憶へ作らない。
- スペル途中の既存退避 `_write_beat_body_directly` も、採番がある回だけ `BEAT_BODY_UNMEMORIZED_KEY` を更新して `True` を返す。後続の退避失敗は前の保存済み本文を上書きしない。確定失敗を受けた caller は保存成功時だけ `finalized=True` にし、失敗時は既存の Beat 出口の最終保存を妨げない。
- 既存の `emitted` は重複処理を防ぐ印で、退避書き込みの試行前にも立つ。保存証拠と混同せず、今回その意味や `announce` の画面イベントは変えない。

## 検証と経緯

- 2026-10-02: 実 `lg_llm_node` を fake LLM / synthetic persona で通し、修正前に4経路 × `None` / 空 dict / ID の無い dict / 空 ID の16例で未保存本文の補填を再現。成功した直接発話の後に未採番 segment が来る2例で補填本文の上書きを再現。
- 同日: 先行 segment 成功・締め segment 失敗を同期 / streaming fallback / tool の3経路で再現。締めが成功した対照群も通す。
- 同日: 実 `RuntimeEmitters.emit_say` が履歴の未採番 dict を返す境界を通し、`_emit_beat_segments` の戻り値・segment の採番・保存完了イベントが一致することを検証。`tests/test_streaming_placeholder_salvage.py` の末尾へ回帰を追加し、既存の隔離設定削除とは行を分けた。
- 同日: `test_runtime_llm_helpers` / `test_pipeline_stream_spell_voicing` / `test_tell_spell` / `test_streaming_placeholder_salvage` / `test_beat_segment_records` / `test_beat_finalize` の隔離実行は **285 passed + 6 subtests passed**。変更 Python の `ruff check`、`git diff --check`、`scripts/check_in_flight.py` も合格 (台帳は既存 RSS 行の経過措置警告のみ)。フルスイートは未実行。
- 本番ペルソナ、実 LLM、実データの書き込みは実施していない。作成時はレビューと実地の異常経路の確認待ちとして archive へ動かさなかった。後日のレビュー結果と残る穴は下記に記録する。

## スペル途中の既存退避の確認と、対象外の救済拡張

追加監査で、`_close_streaming_beat` 内の `_write_beat_body_directly` も未採番 dict で補填キーを置き `True` を返していた。これは「建物へ書いたか」という既存の戻り値契約に反するため、同じ採番の門を既存の位置に入れた。`no-placeholder` / `finalize-failure` の2経路を実 `_run_spell_loop` で通し、修正前に未採番4種 × 2経路の8例と、先行成功・後続失敗3種 × 2経路の6例を再現。修正後は成功の対照群とともに16例が合格。さらに実 node の出口で、退避成功時だけ補填が走り、退避失敗時は既存の最終確定が呼ばれる2例を固定した。

[finalize_failure_rescue_expansion.md](finalize_failure_rescue_expansion.md) で裁定待ちなのは、直接退避を締めくくり・普段の返事・中断時の確定にも新設することと、二重配信防止の仕組み。本変更は新しい退避先・再試行ループ・配信機構を作らない。通常の memorize や、中断した言いかけの保存 `_save_cut_utterance` が本人の記憶を残す方針も変えず、既存の直接保存を根拠とした「補填」の成功判定だけを揃える。

## 残る境界: 先行 segment が建物だけに残る2経路（未解決）

[PR #357 のレビュー](https://github.com/maha0525/SAIVerse/pull/357#issuecomment-5964632697) を受け、
現行と修正前の呼び出し側を照合した。次の2件は**この PR より前からの穴**であり、
今回の「建物に保存できた本文だけを補填する」判定修正では解決していない。
どちらも前提は、その周の SAIMemory 書き込みに失敗していること。

1. **非ストリーミングの混在結果**: 先行 segment A の記憶書き込みが失敗し、
   建物への保存は成功する。末尾 B は建物への保存に失敗し、その後 Beat が例外で終わると、
   A は建物にはあるが記憶に無いまま残り得る。
   [tool 側の呼び出し](https://github.com/maha0525/SAIVerse/blob/1157a4793a1c897b4b33d5a8126f2ae897988285/sea/runtime_llm.py#L5463-L5485) と
   [同期側](https://github.com/maha0525/SAIVerse/blob/1157a4793a1c897b4b33d5a8126f2ae897988285/sea/runtime_llm.py#L6467-L6494)
   は末尾自身の採番を確かめて `final_continuation` を補填対象にするが、
   先行 segment ごとの未記憶本文は出口へ渡していない。
   旧実装も補填候補は末尾 B で、A の欠落は直していなかった。今回止めたのは、
   A の保存成功を根拠に**建物に無い B を記憶へ書く**誤り。
2. **ストリーミングの placeholder 確定**: 周の記憶書き込みが失敗して
   `_round_memorized=False` でも、[`_close_streaming_beat` の確定成功](https://github.com/maha0525/SAIVerse/blob/1157a4793a1c897b4b33d5a8126f2ae897988285/sea/runtime_llm.py#L3171-L3189)
   は `segment.emitted=True` にして戻る。直接退避の枝と違って、
   `BEAT_BODY_UNMEMORIZED_KEY` にその周の本文を残さない。
   後続 continuation で Beat が失敗したとき、保存済みの先行 segment は再 emit されず、
   記憶に欠けた本文も補填へ届かない。この枝は今回の修正前も同じだった。

segment ごとの「建物に保存済み・本人の記憶には未記入」を出口まで運び、
一度だけ補填する修正は別途設計・実装する。保存済みの事実と記憶済みの事実の所有者、
HTML を含まない記憶用本文、重複補填の防止を一緒に確認する必要がある。
この追記は方式の採用や runtime 拡張の承認ではなく、未解決範囲の記録である。

将来の隔離回帰は、A の記憶失敗＋建物成功 → B の建物失敗 → Beat 終了と、
A の記憶失敗＋placeholder 確定成功 → continuation 失敗 → Beat 終了を通し、
A だけが一度補填されることを確かめる。今回これらを修正済み・回帰で保護済みとはしない。

### レビュー結果と台帳の旧記録

- 2026-10-03: 上記レビューは採番で門番する変更にコード修正を求めず、既存の2件を記録するよう指摘した。Windows の統合フルスイートについても当該 PR 起因の失敗なしとの報告があるが、この文書追記で独自に同じ実機検証をしたわけではない。実地の異常経路の確認は未実施。
- 台帳の更新前の行（2026-10-02）: 状態「検証待ち」、次アクション「直接書き込み・締め segment・スペル途中の既存退避の採番判定は隔離回帰の確認を終え、レビュー待ち。次は差分レビューを受け、既存の成功経路と失敗時の出口の扱いを確認する。」、誰待ち「私 (レビュー対応)」。

- 2026-10-03: PR #357 を develop へマージした (レビューはメティス、マージの判断はまはー)。台帳から移送した旧次アクション: 「採番による成功判定はマージ判断待ち。次は既存の未補填2経路を残す境界を確認し、取り込みを判断する。segment ごとの補填拡張は別途。」(誰待ち: まはー (マージ判断))
