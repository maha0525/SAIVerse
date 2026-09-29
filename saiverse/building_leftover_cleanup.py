"""消えた建物を指したまま残っているアイテムの置き場所・設置物・リアルタイムスペルを、起動時に片付ける。

2026-09-29 までの建物の削除 (manager/admin.py::AdminService.delete_building) は、
建物の行と入退室の記録しか消さなかった。そのため利用者の DB には、消した建物の
ID を指したままのアイテムの置き場所・設置物 (とそれにぶら下がる記録)・建物に
結びつけたリアルタイムスペルが残っている。建物の ID は使い回される (日本語名の
建物は空いている最小の番号を使う) ので、放っておくと無関係な新しい部屋の中身と
して戻ってくる。docs/issues/building_delete_leaves_contents.md の「5. 既にある残骸」。

片付け方:

- アイテム: 置き場所の行だけを消して「どこにも置かれていない」状態にする。
  アイテム本体は消さない — 消す前に利用者へ訊くことができないので、取り返しの
  つく方に倒す (ワールドエディタから置き直せる)。
- 設置物: ぶら下がる行ごと消す (建物の削除と同じ実装
  :func:`saiverse.observer_manager.delete_fixture_rows`)。設置物には
  「どこにも置かれていない」状態が無い。
- 建物に結びつけたリアルタイムスペル: 建物自身の設定なので消す。

会話の記録 (building_messages) とペルソナ側の表には触らない — 残す会話の扱いは
別の段階 (特殊な ID への付け替え) で決める。

何度走らせても結果は同じ (残骸が無ければ何もしない)。同じ DB を使う別の
SAIVerse が動いている間は、部屋 ID の付け替え (saiverse/building_id_repair.py)
と同じ関所で見送る — 相手のプロセスが建物を作っている最中の行を、建物の無い
残骸と取り違えないため。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from database.models import (
    Building,
    Fixture,
    ItemLocation,
    RealtimeSpellBinding,
)

LOGGER = logging.getLogger(__name__)

_LOG_PREFIX = "[building-leftover-cleanup]"


def cleanup_deleted_building_leftovers(*, session_factory, db_path) -> Optional[Dict[str, int]]:
    """消えた建物を指したまま残っている行を片付け、片付けた数を返す。

    Args:
        session_factory: saiverse.db のセッションを作る呼び出し可能オブジェクト
        db_path: saiverse.db のパス (同じ DB を使う別プロセスの確認に使う)

    Returns:
        ``{"unplaced_items", "deleted_fixtures", "deleted_realtime_spells"}`` の数。
        別のプロセスが同じ DB を使っていて見送ったときは None。
    """
    from saiverse.observer_manager import delete_fixture_rows
    from saiverse.runtime_marker import another_running_process_owns_db

    owned, owner = another_running_process_owns_db(db_path)
    if owned:
        LOGGER.warning(
            "%s 同じ DB を使う別の SAIVerse が動いているので、消えた建物の残骸の片付けを見送ります: %s",
            _LOG_PREFIX, owner,
        )
        return None

    db = session_factory()
    try:
        existing_buildings = db.query(Building.BUILDINGID)

        orphan_item_locations = (
            db.query(ItemLocation)
            .filter(
                ItemLocation.OWNER_KIND == "building",
                ItemLocation.OWNER_ID.notin_(existing_buildings),
            )
            .delete(synchronize_session=False)
        )

        orphan_fixture_ids: List[str] = [
            row[0]
            for row in db.query(Fixture.FIXTURE_ID)
            .filter(Fixture.BUILDING_ID.notin_(existing_buildings))
            .all()
        ]
        # 起動の途中 (定期観測の予約より前) に走るので、予約の取り消しは要らない
        delete_fixture_rows(db, orphan_fixture_ids)

        orphan_spells = (
            db.query(RealtimeSpellBinding)
            .filter(
                RealtimeSpellBinding.OWNER_KIND == "building",
                RealtimeSpellBinding.OWNER_ID.notin_(existing_buildings),
            )
            .delete(synchronize_session=False)
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    counts = {
        "unplaced_items": int(orphan_item_locations or 0),
        "deleted_fixtures": len(orphan_fixture_ids),
        "deleted_realtime_spells": int(orphan_spells or 0),
    }
    if any(counts.values()):
        LOGGER.info(
            "%s 消えた建物の残骸を片付けました: どこにも置かれていない状態へ移したアイテム %d 件、"
            "消した設置物 %d 件、消した建物のリアルタイムスペル %d 件",
            _LOG_PREFIX,
            counts["unplaced_items"],
            counts["deleted_fixtures"],
            counts["deleted_realtime_spells"],
        )
    else:
        LOGGER.info("%s 消えた建物の残骸はありませんでした", _LOG_PREFIX)
    return counts
