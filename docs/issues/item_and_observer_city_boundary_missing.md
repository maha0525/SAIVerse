# Issue: アイテムの操作と観測値の送り込みが「この街のものか」を確かめていない

**ステータス**: 🔴 未解決 (未着手)
**優先度**: low (一つの DB を一つの街で使う普段の形では起きない。一つの DB を複数の街で共有する形でだけ効く)
**作成日**: 2026-09-29
**きっかけ**: フィードスタンドの削除 (docs/issues/archive/feed_fixture_no_delete_or_edit.md) の Codex 敵対レビュー 1 巡目。まはーの判断で、その変更の範囲外として issue に残す。

## 症状

SAIVerse の DB には複数の街 (City) の行が同居できる。フィードまわり (saiverse/feed_manager.py の `city_feed_fixture_ids` を使う経路) と、2026-09-29 に足した設置物の編集・削除 (saiverse/observer_manager.py の `city_fixture_ids`) は、「対象が現在の街の建物に属するか」を書き込みの SQL 文自身の条件で確かめる。ところが、次の経路はこの確認をしていない。別の街の ID を渡されると、別の街のものを書き換えたり消したりできる。

## 確認した事実 (2026-09-29、コードを読んで確認)

- **アイテムの削除**: `manager/admin.py::delete_item` と `delete_bag_contents` は `ITEM_ID` だけで対象を引く。街の確認は無い。
- **アイテムの編集・移動**: `admin.update_item` は、移し先の建物・ペルソナ・バッグが現在のプロセスの手元の一覧 (`building_map` / `personas` / `item_service.items`) にあるかは確かめる。ただし、動かすアイテム自身がどの街のものかは確かめない。`manager/items.py` には街に関する条件が一つも無い。
- **観測値の送り込み**: `POST /api/observer/{observer_id}/push` (api/routes/observer.py) は `get_observer(observer_id)` を DB 全体から引く。`ObserverManager.record_metrics` も観測設定を `OBSERVER_ID` だけで引き、設置物の `STATE_JSON` の更新条件も `FIXTURE_ID` だけ。
- **設置物の読み出し**: `GET /api/observer/fixture/{id}` と `GET /api/observer/building/{id}/fixtures` も街を見ない (読み出しだけ)。
- **外部アプリからの登録**: `ObserverManager.create_fixture` (同じ ID なら上書き) と `create_observer` は、建物や設置物が現在の街にあるか、そもそも存在するかを確かめない。`create_observer` は、無い設置物に紐づく観測設定を作れる。

## 直す方向 (未裁定)

アイテムと観測の系に、フィードと同じ形の「現在の街の建物に属するか」の条件を、書き込みの SQL 文そのものに載せる。アイテムは置き場所 (部屋 / ペルソナの持ち物 / バッグの中 / どこにも置かない) によって街への辿り方が違う。とくに「どこにも置かない」アイテムがどの街に属するのかは、今の作りでは決まっていない。なので、直す前に「アイテムはどの街のものか」の定義を決める必要がある。

## ログ

- 2026-09-29: 起票 (Codex の指摘をコードで裏取りし、今回の変更の範囲外の既存欠陥として記録)。
