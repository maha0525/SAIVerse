# Issue: 永続 Track に complete を打とうとする / 永続であることが伝わっていない

**ステータス**: 🔲 未着手 (低優先)
**優先度**: low
**作成日**: 2026-06-29
**関連**: `saiverse/track_manager.py:816` (`complete()` は `is_persistent` なら拒否), `sea/head_pipeline/sections/autonomy_modes.py` (Track 説明), `saiverse/track_handlers/*` (永続 Track = 交流 / 対ユーザー会話 / desire_refill)

## 背景

永続 Track (`is_persistent=True`) に対してペルソナが **complete を打とうとする**ことがある。永続 Track は「ずっと続くもの」で complete / abort には遷移しない設計 (`track_manager.complete()` は永続だと `PersistentTrackError` を投げて拒否)。スペル自体は弾かれるが、ペルソナは「終わらせられる」と思って撃っており、無駄撃ち + 自己認識のズレが生じている。

永続 Track の例: 交流 Track、対ユーザー会話 Track、候補補充 Track (やりたいことを探す, desire_refill)。

## 何が問題か

- ペルソナが「この Track は永続で、完了させるものではない」という**仕様を知らない**。complete 系スペルが弾かれることを試行錯誤で学ぶしかない (対ユーザー会話 Track / task 運用と同根の「運用が伝わっていない」構造)。
- 永続 Track と非永続 Track の区別がペルソナの認知に無いと、Track の扱い方を誤る。

## 直す方向 (案)

- **恒常知識として明示**: `autonomy_modes.py` の Track 説明で「永続的にずっと続く Track (交流 / 対ユーザー会話 / やりたいことを探す 等) は完了・中止できない。続けるか、保留して離れるかのどちらか」を据える。意味ベースで「これはずっと続けるもの」と接し方を与える (memory: feedback_explain_by_reader_flow_and_meaning)。
- Track コンテキスト注入 (`build_track_context`) やメタ判断の状況テキストで、対象 Track が永続なら complete/abort を選択肢として提示しない (構造的に撃たせない)。

## ログ

- 2026-06-29: 起票。永続 Track への complete 試行を観測。永続であることをペルソナに明示する方針を記録、対処は先送り。
