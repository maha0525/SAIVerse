# スペルが走った回のストリーム切断は、検知そのものが無い

**発見**: 2026-09-14 (サーバー切断時の中断通告の追加 (`server_cut_stream_writes_no_interruption_notice.md`) への代行レビュー F2)
**状態**: 未解決
**深刻度**: P3 — 発生条件は「サーバーが切った部分文が、たまたま完全なスペル行を含んでいる」または「スペルループの継続ストリームが切られる」。頻度は測っていない

## 症状

サーバーがストリームを途中で切ったとき、「言い切っていない」印・画面への知らせ・中断の通告を出す後始末は **スペルが一つも走らなかった回の完了パスにしか無い**。切られた部分文がスペル行を含んでいた回はスペル分岐へ進み、後始末を一つも通らない:

- スペルループの途中の切断は、スペル実行後の継続ストリームが発話を続けるので実質自己修復される (実害小)。
- **継続ストリーム自体が切られた回**が問題で、途切れた本文がそのまま「言い切った発言」として確定する。印が立たないので「続きの生成」ボタンも出ず、通告も書かれない。プリフィル不可の Gemini 3.x では、その発言の続きを起こす手段が無いまま会話が進む。

「サーバーが切った」の申告 (`state["_stream_error"]`) は最初のストリーム消費の直後に立つ一箇所だけで、消費するのはスペル無し完了パスの一箇所だけ。スペルループの再ストリームは `consume_stream_error` を呼ばない。2026-09-14 の修正で、残留した申告が次の Beat の言い切った発言に偽の印を乗せる漏れは Beat 入り口の掃除で塞いだが、**これは蓋であって、供給源 (後始末が片側の分岐にしか無いこと) は残っている**。

## 直し方 (レビュアーの提案、未裁定)

申告の消費を、ストリーム消費の直後に Beat ローカルの変数へ引き取り、スペル分岐・スペル無し分岐の両方の出口で同じ後始末 (印 + 知らせ + 通告) を通す形にする。スペルループの各周の再ストリームにも `consume_stream_error` の検査を足し、途中の周の切断 (自己修復される側) と最終周の切断 (発言が途切れたまま確定する側) を区別する。

あわせて、現在の回帰テスト `test_a_stale_stream_error_from_an_earlier_beat_does_not_leak` は残留を手で state に仕込んで検証しており、実際に残留を作る分岐 (スペル分岐) は通していない。この issue を直すときに、スペル分岐を実際に通す回帰へ差し替える。

## 解消 (2026-09-25、`docs/intent/reply_stop_exit.md` の作り直しの一部として)

供給源 (後始末がスペル無しの完了パスにしか無いこと) を、後始末の一本化で塞いだ。検収・隔離環境の確認済みで archive へ移動。

- **申告を state に置かない**: 最初のストリームの切断の申告は Beat のローカル (`_initial_stream_error`) に引き取り、スペルループへ `initial_stream_error` として渡す。周ごとの再ストリームの申告はループ自身が `consume_stream_error` で消費する。Beat 入り口の掃除 (`state.pop("_stream_error")`) は不要になったので消した。
- **途中の周と締めの周を見分ける**: 切られた本文がスペル行を含む回は、次の周の生成が発話を続けるので (自己回復) 申告を捨てる。スペル行を含まない本文 = 締めの発言として途切れたまま確定する回だけ、`SpellLoopResult.final_stream_error` で呼び出し元へ返す。一文字も来ないうちに切られた締めの周も同じ申告で返る。
- **印と通告は返事の後始末が一回だけ**: 締めの発言を保存した側は「最後に保存した発言」の記録に「途中で切れた本文 + この後で話が止まった + 切断の申告」を書き足すだけ (`sea/runtime_llm.py` の `_note_stream_cut`)。返事の一番外側 (`run_meta_user`) の後始末 (`sea/reply_stop_exit.py` の `settle_reply_stop`) がそれを見て、印 (`metadata["_interrupted"]`) と中断の通告 (①) を置き、情報の知らせ (info) に「続きの生成」を出す発言の id (別の部屋ならその部屋) を載せる。一文字も来なかった回は、話が止まった直前の発言 (スペルの結果で終わる周の本文) に ③ の通告が付く。
- **回帰テストの差し替え**: 残留を手で state に仕込んでいた `test_a_stale_stream_error_from_an_earlier_beat_does_not_leak` を外し、スペル分岐を実際に通す 3 本に置き換えた (`tests/test_streaming_placeholder_salvage.py` の `test_a_cut_on_a_round_with_a_spell_recovers_without_a_mark` / `test_a_cut_on_the_closing_round_marks_the_cut_utterance` / `test_a_closing_round_cut_before_any_word_marks_the_spell_utterance`)。

## 関連

- [server_cut_stream_writes_no_interruption_notice.md](server_cut_stream_writes_no_interruption_notice.md) (スペル無し側の後始末 — 2026-09-14 実装)
- `sea/runtime_llm.py` の `_record_interruption_notice` (通告の共通の書き手。2026-09-25 以降、呼ぶのは `sea/reply_stop_exit.py` の後始末だけ)
