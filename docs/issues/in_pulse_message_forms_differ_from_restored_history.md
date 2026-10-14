# Issue: ターン内の二回目以降の呼び出しで送る形が、次のターンで履歴から復元される形と違う

**ステータス**: 🔲 未着手（未解決 — `docs/issues/` 直下にある）
**優先度**: low（損はターン内の往復の分のキャッシュだけで、ターンの最初の呼び出しのキャッシュは効く）
**作成日**: 2026-10-14
**関連**: `docs/intent/persist_realtime_and_recall.md`（不変条件 1・§2 の実測・経緯の三巡目）、`sea/runtime_llm.py`（`_finalize_beat` の assistant 行の追記、native tool の往復）、[issue: 画像の枚数上限で先頭の一致が崩れる](image_attachment_limit_breaks_cache_prefix.md)（同じく前方一致を割る別の経路）

## 何が起きているか

一つのターン（パルス）の中で LLM を二回以上呼ぶとき、二回目以降の呼び出しでは、一回目の応答とその後の往復を送る列の後ろに積んで送る。この「ターン内で積む形」と、次のターンで同じ出来事を履歴（SAIMemory）から復元したときの形が、次の二つの場面で違う。

1. **native tool の往復**: ターン内では、tool 呼び出しの assistant 行（本文が空で `tool_calls` を持つ）と、その結果の tool 行を積む。次のターンで履歴から復元すると、assistant 行は本文が JSON の行になり、tool 結果の行は無くなる。
2. **自律パルスの通常の LLM ノードの間**: ターン内では、前のノードの発言を assistant 行のまま積む。次のターンで履歴から復元すると、自律の発言は user ロールの自律記録（`<system>[自律行動の記録]…`）になる。

形が違うと、二回目以降の呼び出しで送った列は、次のターンの最初の呼び出しの列の先頭と一致しない。どのモデルでも（`cache.persist_realtime_and_recall` の ON/OFF を問わず）、ターン内の往復の分だけ、次のターンのキャッシュが当たらない。

## 損はどこまでか（実測）

2026-10-14、Gemini 4 Argon の暗黙キャッシュで測った（`persist_realtime_and_recall.md` §2）。ターン 1 の一回目の呼び出し（24,375 トークン）のあと、ターン内の往復を足した二回目の呼び出しを挟み、ターン 2 ではその往復を別の形（ツール呼び出しを JSON 本文にし、結果の改行も変えた形）で復元して送った。ターン 2 は 24,466 トークン中 22,417 トークンがキャッシュに当たった。

一回目の呼び出しで保持された範囲は、二回目を挟んでも次のターンに効く。だから、この形の違いで失うのは、ターン内の往復の分だけになる。ターンの最初の呼び出しどうしの一致（`persist_realtime_and_recall.md` 不変条件 1）は、この違いがあっても成り立つ。

## 再現条件

Codex の敵対レビュー三巡目（`persist_realtime_and_recall` の実装、2026-10-14）の指摘。

- 自律パルスで、LLM ノードを二つ以上続けて通る Playbook を実行する。`_finalize_beat`（`sea/runtime_llm.py`）が前のノードの発言を assistant 行として `state["_messages"]` に積む。次のターンの履歴では、その発言は user ロールの自律記録になる。
- tools 付きの LLM ノードで native tool を呼ぶ。全文一括の tool 経路（`llm_client.generate(messages, tools=...)` の呼び出し。eb633917 の `sea/runtime_llm.py` 5872〜5876 行）と `_finalize_beat` の `tool_calls` 付きの assistant 行で、本文の空な assistant 行と `tool_calls`、tool 結果の行を積む。次のターンの履歴では形が変わる。
- OFF のモデルの自律パルスでスペルを繰り返す。周内の発言は assistant 行のまま積む（スペルループの `_round_message_for_next_call`。ON のモデルだけ復元形に変える — OFF の送る並びは変えない約束、`persist_realtime_and_recall.md` 不変条件 2）。

ON のモデルでは、スペルの繰り返しの周（スペルの結果の文面と、自律パルスの周内の発言）は、二巡目までの修正で次のターンの復元形に揃えてある。スペルの結果の文面は全モデル共通で揃えてある。この issue は、それ以外の往復の残り。

## 最初の呼び出しの前に足される、記録に残らない差し込み（2026-10-14 追記）

ON のモデル（`persist_realtime_and_recall`）では、ターンの最初の呼び出しに記録に残らないメッセージが載ると、次のターンの最初の呼び出しと先頭が一致しなくなる（不変条件 1 そのものの破れ）。最初の呼び出しの前に足す TOOL の結果とエラーは、Codex 五巡目の修正で記録に残る形にした。六巡目の修正で、最初の呼び出しの直前に送る列の後ろ側を記録から読み直した並びに揃えるようにし、機械検査（`sea/runtime_context.find_unrecorded_messages`、揃えた後の列と読み直した並びを比べて WARN を出す）もその比較に変えた。その検査が、まだ直していない二つを見つけている。

- トップレベルの Playbook の最初の LLM ノードに action の指示文がある場合（例: `building_move_playbook` の TOOL→LLM）。
- SUBPLAY の `report_to_parent` が、最初の呼び出しの前に親の列へ足される場合（`sea/runtime_nodes.py` の `lg_subplay_node`）。

会話の既定の Playbook（`track_user_conversation`）には当てはまらない。起きても、そのターンのキャッシュが外れるだけで、ペルソナに届く中身は変わらない。本番で起きたかどうかは、WARN（`[sea][persist_realtime_and_recall] the first send of this turn carries a message that is not kept` で始まる行）で分かる。六巡目の揃えの後は、記録に残らない差し込みはそのターンのまとまりより後ろに並ぶので、外れるのはその差し込みの分だけになっている。それも無くすなら、これらを記録に残る形で送る。

## 直し方の方向（未決）

- ターン内で積む形を、次のターンの復元形に揃える（スペルの繰り返しで行ったのと同じ方向）。native tool では、プロバイダが要求する tool 呼び出しと結果の対応（`tool_calls` と tool 行の組）を崩せないかを先に確かめる必要がある。
- 逆に、履歴からの復元をターン内の形に揃える。保存する形が変わるので、あらすじの材料や Metabolism の勘定への影響を確かめる必要がある。

どちらも、損がターン内の往復の分だけなので、急がない。

## ログ

- 2026-10-14: `persist_realtime_and_recall` の Codex 敵対レビュー三巡目で指摘された。メインが Argon で、ターンの最初の呼び出しのキャッシュは二回目を挟んでも効くことを実測し、この設計の範囲外として起票した。
