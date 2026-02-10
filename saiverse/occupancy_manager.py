import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Callable, TYPE_CHECKING

from sqlalchemy.orm import Session

from database.models import BuildingOccupancyLog, User as UserModel

if TYPE_CHECKING:
    from .buildings import Building


class OccupancyManager:
    """
    エンティティ（AI、ユーザーなど）の移動と占有状態の管理を専門に行うクラス。
    """
    def __init__(
        self,
        session_factory: Callable[[], Session],
        city_id: int,
        occupants: Dict[str, List[str]],
        capacities: Dict[str, int],
        building_map: Dict[str, 'Building'],
        building_histories: Dict[str, List[Dict[str, str]]],
        id_to_name_map: Dict[str, str],
        user_id: int,
    ):
        self.SessionLocal = session_factory
        self.city_id = city_id
        self.occupants = occupants
        self.capacities = capacities
        self.building_map = building_map
        self.building_histories = building_histories
        self.id_to_name_map = id_to_name_map
        self.user_entity_id = str(user_id)

    def move_entity(
        self,
        entity_id: str,
        entity_type: str,  # 'ai' or 'user'
        from_id: str,
        to_id: str,
        db_session: Optional[Session] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        エンティティを建物間で移動させる。移動に関するすべてのロジックをここに集約する。
        """
        entity_id = str(entity_id)

        # 1. 移動前のチェック
        if to_id not in self.building_map:
            logging.warning("move_entity aborted: destination %s unknown", to_id)
            return False, f"移動失敗: 建物 '{to_id}' が見つかりません。"
        if from_id == to_id:
            return True, "同じ場所にいます。"

        if entity_type == 'ai':
            capacity_limit = self.capacities.get(to_id, 1)
            current_ai = sum(
                1 for occ in self.occupants.get(to_id, []) if not self._is_user(occ)
            )
            if current_ai >= capacity_limit and entity_id not in self.occupants.get(to_id, []):
                logging.info(
                    "move_entity denied: %s -> %s capacity reached (current=%d, limit=%d)",
                    from_id,
                    to_id,
                    current_ai,
                    capacity_limit,
                )
                return False, f"{self.building_map[to_id].name}は定員オーバーです"

        # 2. DBとメモリの操作
        db = db_session if db_session else self.SessionLocal()
        manage_session_locally = not db_session

        try:
            now = datetime.now()
            if entity_type == 'ai':
                last_log = db.query(BuildingOccupancyLog).filter_by(AIID=entity_id, BUILDINGID=from_id, EXIT_TIMESTAMP=None).order_by(BuildingOccupancyLog.ENTRY_TIMESTAMP.desc()).first()
                if last_log:
                    last_log.EXIT_TIMESTAMP = now
                new_log = BuildingOccupancyLog(CITYID=self.city_id, AIID=entity_id, BUILDINGID=to_id, ENTRY_TIMESTAMP=now)
                db.add(new_log)
                entity_name = self.id_to_name_map.get(entity_id, entity_id)
            elif entity_type == 'user':
                user = db.query(UserModel).filter_by(USERID=int(entity_id)).first()
                if not user: return False, "移動失敗: ユーザーが見つかりません。"
                user.CURRENT_BUILDINGID = to_id
                entity_name = user.USERNAME or "ユーザー"
            else:
                logging.warning("move_entity aborted: unknown entity type %s", entity_type)
                return False, f"不明なエンティティタイプ: {entity_type}"

            if manage_session_locally: db.commit()

            if entity_id in self.occupants.get(from_id, []): self.occupants[from_id].remove(entity_id)
            self.occupants.setdefault(to_id, []).append(entity_id)

            # 3. ログメッセージの生成
            from_building_name = self.building_map[from_id].name
            to_building_name = self.building_map[to_id].name
            action_type = "AI Action" if entity_type == 'ai' else "User Action"
            left_message = f'<div class="note-box">🚶 {action_type}:<br><b>{entity_name}が{to_building_name}へ移動しました</b></div>'
            self.building_histories.setdefault(from_id, []).append({"role": "host", "content": left_message})
            entered_message = f'<div class="note-box">🚶 {action_type}:<br><b>{entity_name}が{from_building_name}から入室しました</b></div>'
            self.building_histories.setdefault(to_id, []).append({"role": "host", "content": entered_message})

            logging.info(f"Moved {entity_type} '{entity_id}' from {from_id} to {to_id}.")
            return True, None
        except Exception as e:
            if manage_session_locally: db.rollback()
            logging.error(f"Failed to move {entity_type} '{entity_id}' in DB: {e}", exc_info=True)
            return False, "データベースの更新中にエラーが発生しました。"
        finally:
            if manage_session_locally: db.close()

    def _is_user(self, entity_id: str) -> bool:
        return entity_id == self.user_entity_id
