import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Callable, TYPE_CHECKING

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
        manager_ref: Optional[Any] = None,
    ):
        self.SessionLocal = session_factory
        self.city_id = city_id
        self.occupants = occupants
        self.capacities = capacities
        self.building_map = building_map
        self.building_histories = building_histories
        self.id_to_name_map = id_to_name_map
        self.user_entity_id = str(user_id)
        self._manager_ref = manager_ref

    def _check_game_region_gate(self, entity_id: str, to_id: str) -> Optional[str]:
        """game Region の入場ゲート。拒否理由を返す (入場可なら None)。

        ゲーム進行中 (phase が playing / paused) の Region 内 Building は、
        参加者 (state.participants) と Ruler 以外の入場を拒否する。退出方向は
        制限しない (退出はポーズで対応)。設計: temp/region_rpg_intent.md §D (不変条件 4)
        """
        get_top_region = getattr(self._manager_ref, "get_top_region_of_building", None)
        if get_top_region is None:
            return None
        region = get_top_region(to_id)
        if region is None or not region.is_game_region:
            return None
        phase = region.state.get("phase")
        if phase not in ("playing", "paused"):
            return None
        participants = [str(p) for p in region.state.get("participants", [])]
        if str(entity_id) in participants or str(entity_id) == str(region.ruler_id):
            return None
        building_name = self.building_map[to_id].name if to_id in self.building_map else to_id
        return (
            f"移動失敗: '{building_name}' では現在ゲームが進行中のため、"
            "参加者以外は入場できません。"
        )

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
        # Quarantine block: refuse entry to buildings whose log.json was
        # detected as corrupted/zero-byte at startup. The user must resolve
        # via the UI (restore from backup / reset / handle manually) before
        # the building accepts new entries.
        quarantined = getattr(self._manager_ref, "quarantined_buildings", None)
        if quarantined and to_id in quarantined:
            logging.warning(
                "move_entity blocked: destination %s is quarantined (corrupted log.json)",
                to_id,
            )
            return False, (
                f"移動失敗: 建物 '{self.building_map[to_id].name}' は会話履歴ファイルが"
                "破損しているため一時的に隔離されています。アラートバナーから対応してください。"
            )

        game_gate_denial = self._check_game_region_gate(entity_id, to_id)
        if game_gate_denial:
            logging.info(
                "move_entity blocked by game region gate: %s -> %s (%s)",
                entity_id, to_id, game_gate_denial,
            )
            return False, game_gate_denial

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
            event_key = f"occupancy:{entity_id}:{from_id}:{to_id}:{int(now.timestamp())}"
            # entity_name / building_name も event に含める (intent §E 視点別
            # レンダリング用)。 これで history_manager 側が manager_ref なし
            # で entity_id == self.persona_id 判定 + 自然な文言生成できる。
            left_metadata = {
                "event": {
                    "type": "occupancy",
                    "action": "leave",
                    "entity_id": entity_id,
                    "entity_name": entity_name,
                    "entity_type": entity_type,
                    "from_building_id": from_id,
                    "from_building_name": from_building_name,
                    "to_building_id": to_id,
                    "to_building_name": to_building_name,
                    "event_key": event_key,
                }
            }
            enter_metadata = {
                "event": {
                    "type": "occupancy",
                    "action": "enter",
                    "entity_id": entity_id,
                    "entity_name": entity_name,
                    "entity_type": entity_type,
                    "from_building_id": from_id,
                    "from_building_name": from_building_name,
                    "to_building_id": to_id,
                    "to_building_name": to_building_name,
                    "event_key": event_key,
                    "recalled_by": [],
                    # auto_ingest 側がペルソナの context に Building 情報を流し込むための
                    # ペイロード。visual_context のキャッシュに頼らず、移動の瞬間に
                    # SYSTEM_PROMPT や physical_vessel_id を episodic memory へ
                    # 直接届ける経路。詳細: docs/intent/stackchan_vessel.md A-3-a
                    "building_info": self._build_building_info(to_id),
                }
            }
            left_message = f'<div class="note-box" data-entity-id="{entity_id}">🚶 {action_type}:<br><b>{entity_name}が{to_building_name}へ移動しました</b></div>'
            entered_message = f'<div class="note-box" data-entity-id="{entity_id}">🚶 {action_type}:<br><b>{entity_name}が{from_building_name}から入室しました</b></div>'
            # Add events through manager's add_building_event so they get proper
            # seq / message_id / heard_by — without this, auto_ingest's
            # ``persona_id in heard_by`` filter excludes them and personas have
            # no episodic memory of moving (and seq corruption breaks new
            # message numbering — see manager/history.py:add_building_event).
            #
            # heard_by:
            #  - LEFT in FROM building: occupants AFTER move (= remaining ones who
            #    "witnessed the leave")
            #  - ENTER in TO building: occupants AFTER move (= including the moving
            #    entity, so they have a record of arriving)
            from_occupants = list(self.occupants.get(from_id, []))
            to_occupants = list(self.occupants.get(to_id, []))
            mgr = self._manager_ref
            if mgr is not None and hasattr(mgr, "add_building_event"):
                mgr.add_building_event(
                    from_id,
                    {"role": "host", "content": left_message, "metadata": left_metadata},
                    heard_by=from_occupants,
                )
                mgr.add_building_event(
                    to_id,
                    {"role": "host", "content": entered_message, "metadata": enter_metadata},
                    heard_by=to_occupants,
                )
            else:
                # Fallback: manager_ref が無い場合 (= 主にテスト経路)。 Phase 2+3 以降は
                # DB 経由でしか書けないので、 何もしない (テスト側で manager_ref 必須に)。
                logging.warning(
                    "occupancy event ignored: manager_ref unavailable for %s -> %s",
                    from_id, to_id,
                )

            logging.info(f"Moved {entity_type} '{entity_id}' from {from_id} to {to_id}.")

            # Dynamic State Sync: AIペルソナ入室時のスナップショット初期化
            if entity_type == "ai":
                try:
                    from saiverse.dynamic_state import DynamicStateManager
                    manager = self._manager_ref
                    if manager:
                        persona = getattr(manager, "personas", {}).get(entity_id)
                        if persona:
                            DynamicStateManager.on_building_entered(persona, to_id, manager)
                except Exception:
                    logging.exception("[dynamic_state] on_building_entered failed for %s -> %s", entity_id, to_id)

                # Addon hooks: ペルソナの建物移動を addon に通知する。
                # Vessel Building に紐付くアドオンが avatar セット等の物理リソースを
                # 切り替え / 退室時に身体を非表示にするためのフックポイント。
                # 詳細: docs/intent/stackchan_avatar_pipeline.md
                try:
                    from saiverse.addon_hooks import dispatch_hook
                    # 退室イベントを先に発火 (= addon が「ペルソナ A が出た」
                    # を処理した後に「ペルソナ B が入った」を処理できるよう)。
                    dispatch_hook(
                        "persona_exited_building",
                        persona_id=entity_id,
                        building_id=from_id,
                        from_building_id=from_id,
                        to_building_id=to_id,
                    )
                    dispatch_hook(
                        "persona_entered_building",
                        persona_id=entity_id,
                        building_id=to_id,
                        from_building_id=from_id,
                    )
                except Exception:
                    logging.exception(
                        "[addon_hooks] persona move hook dispatch failed "
                        "for %s -> %s", entity_id, to_id,
                    )

            # game Region ライフサイクル: 参加者の退出/帰還による自動ポーズ/再開。
            # フック内部で例外を吸収するので移動本体には影響しない。
            lifecycle = getattr(self._manager_ref, "game_lifecycle", None)
            if lifecycle is not None:
                lifecycle.on_entity_moved(entity_id, from_id, to_id)

            return True, None
        except Exception as e:
            if manage_session_locally: db.rollback()
            logging.error(f"Failed to move {entity_type} '{entity_id}' in DB: {e}", exc_info=True)
            return False, "データベースの更新中にエラーが発生しました。"
        finally:
            if manage_session_locally: db.close()

    def _is_user(self, entity_id: str) -> bool:
        return entity_id == self.user_entity_id

    def _build_building_info(self, building_id: str) -> Dict[str, Any]:
        """auto_ingest がペルソナの context へ流し込むための Building 情報を組み立てる。

        ``available_tools`` は A-3-c (mcp_client の Building 単位 visibility) 実装後に
        spell_tools 経由で填まる想定。それまでは空リスト。

        詳細: docs/intent/stackchan_vessel.md A-3-a
        """
        building = self.building_map.get(building_id)
        if building is None:
            return {}
        return {
            "name": getattr(building, "name", "") or building_id,
            "system_prompt": getattr(building, "base_system_instruction", "") or "",
            "physical_vessel_id": getattr(building, "physical_vessel_id", None),
            "available_tools": [],
        }
