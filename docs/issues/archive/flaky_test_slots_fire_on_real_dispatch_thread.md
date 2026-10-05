# Issue: test_slots_fire_on_real_dispatch_thread がフルスイート実行で間欠 fail

**ステータス**: ✅ 完了 (2026-10-05、原因特定・テスト修正)
**優先度**: low
**作成日**: 2026-07-13
**関連**: `tests/test_autonomy_wiring.py::test_slots_fire_on_real_dispatch_thread`

## 結論

締切 (20 秒) を負荷で割っていたのではなかった。**テスト自身のポーリングが、発火を壊していた。**

このテストの DB (`session_factory` フィクスチャ) は、メモリ上の SQLite を
`StaticPool` で作っている。`StaticPool` は全スレッドに**同じ一本の接続**を渡す。
SQLAlchemy は Session を閉じて接続をプールへ返すたびに、その接続へ ROLLBACK を
発行する (reset-on-return)。接続が一本しかないので、

1. dispatch スレッド (EventScheduler) がコマ発火の予約 tx を書いている途中で、
2. 主スレッドのポーリング (`load_day_plan`) が読み、Session を閉じると、
3. その ROLLBACK が dispatch スレッドの**未確定の書き込みを巻き戻す**。
4. dispatch スレッドの後続の commit は空振りし、コマは done に届かない
   (状態の食い違いから例外になる回もある)。

一度巻き戻されたらコマはもう進まないので、締切を延ばしても直らない。並列の
フルスイートで頻度が上がるのは、負荷でスレッドの切り替わりがばらけ、ポーリングが
書き込みの途中に割り込む確率が上がるため。2026-07-07 に観測した「`load_day_plan`
が一瞬 None を返す」も、同じ共有接続の同時使用から来ていた可能性が高い
(こちらは再現で確かめていない)。

**本番には無い現象**: 本番の DB (`database/session.py`) はファイルの SQLite を
通常のプールで開くので、スレッドごとに別の接続を持つ。ある接続の ROLLBACK が
他の接続の tx を巻き戻すことはない。

## 修正

テストは発火の完了を DB を覗かずに知る形へ変えた。`day_plan._fire_slot_by_id`
(EventScheduler が呼ぶコールバックの本体) を包み、終わったら `threading.Event`
を立てる。主スレッドはその合図を待ち (上限 60 秒は「発火しない故障」を待ち続け
ないためのもの)、dispatch スレッドを止めてから一度だけ読んで status を確かめる。
発火そのものは従来どおり実時刻の dispatch スレッドで本物の `_fire_slot` が走る。

## 検証

- 共有接続の巻き戻しの機序: 二つのスレッドで同じ `StaticPool` エンジンを使い、
  書き手の flush 後・commit 前に読み手が読んで閉じると、書き手の commit 後の値が
  元のまま (pending) になることを最小の再現で確かめた。
- 旧形と新形の比較 (同じ準備で各 40 回、ポーリング間隔を 1ms に詰めて衝突を
  起きやすくした): 旧形のポーリングは 37/40 で落ち、新形の完了合図は 40/40 で通った。
- 並列のフルスイート (`pytest -n auto`): 直す前のコードで回した 1 回目でこのテストが
  落ちることを再現した。直した後に 3 回回し、3 回ともこのテストは通った
  (残る失敗は、この作業ツリーに frontend の node_modules が無いことによる
  `test_localization.py` の 1 件だけで、この件とは無関係)。

## 同じ形のテストの点検

`StaticPool` を使い、かつ別スレッドを起こすテストを洗った。別スレッドの書き込み中に
主スレッドが DB を覗くポーリングはこのテストだけだった。他の並行テスト
(`test_user_conversation.py` / `test_metabolism_global_defaults.py` /
`test_feed_intake.py` 等) は、被検コード側のロックで DB 操作が直列化されること
自体を確かめるテストで、ポーリングで DB を読む形ではない。
`test_session_anchor_rows.py` の待ち合わせはメモリ上のフラグを見ているだけ。

## 経緯

- 2026-07-07: 本文コメントに「共有 in-memory SQLite の癖で load_day_plan が一瞬
  None を返すことがある」と記録し、「まだ読めない」をポーリング継続扱いにする
  対策を入れた (症状への手当てで、原因は未特定のまま)。
- 2026-07-13: ライフ Phase 4 完了後の全体スイート (`pytest tests -q
  --ignore=tests/test_avatar_pipeline.py`、6 分弱) で 1 回 fail。単独・ファイル単位
  (50 件) は passed。当時の推測は「高負荷時に 20 秒 deadline を割るタイミング
  flaky」で、次に落ちたら assert 位置を記録し、deadline 延長か負荷再現を検討する
  方針だった。
- 2026-10-05: 並列のフルスイートで 2 回連続 fail (単独は毎回 passed)。上の機序を
  特定してテストを修正し、完了。
