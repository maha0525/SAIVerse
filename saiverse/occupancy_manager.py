import logging
import threading
from contextlib import contextmanager
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
        self._topology_bypass = threading.local()

    # ------------------------------------------------------------------
    # Region 入口トポロジー (docs/intent/region.md §2.4, §3)
    # ------------------------------------------------------------------

    @contextmanager
    def topology_bypass(self):
        """system 移動 (lifecycle のパーティー追従・帰還等) 用の入口トポロジー
        バイパス。呼び出しスレッド内でのみ有効 (threading.local の深度カウンタ)。
        """
        depth = getattr(self._topology_bypass, "depth", 0)
        self._topology_bypass.depth = depth + 1
        try:
            yield
        finally:
            self._topology_bypass.depth -= 1

    def _topology_bypassed(self) -> bool:
        return getattr(self._topology_bypass, "depth", 0) > 0

    def _scope_chain(self, building_id: str) -> List[str]:
        """Building の所属スコープを内側から外側へ並べたリストを返す。

        例: SubRegion 内部 → [sub_id, region_id]、Region 直属 → [region_id]、
        Region 無所属 (City 直属) → []。
        """
        chain: List[str] = []
        building = self.building_map.get(building_id)
        get_region = getattr(self._manager_ref, "get_region", None)
        rid = getattr(building, "region_id", None)
        while rid and get_region:
            if rid in chain:  # 自己参照の破損データで無限ループしない
                break
            chain.append(rid)
            region = get_region(rid)
            rid = getattr(region, "parent_region_id", None) if region else None
        return chain

    def _check_entrance_topology(
        self, entity_id: str, from_id: str, to_id: str
    ) -> Optional[str]:
        """Region 入口経由の不変条件を執行する。拒否理由を返す (移動可なら None)。

        移動先のスコープチェーンに「移動元のチェーンに無いスコープ」が現れたら
        境界越え。新規スコープがちょうど 1 つ、かつ移動元がその入口 Building の
        ときだけ通過を許し、その境界点で entry policy を執行する。
        退出方向 (新規スコープなし) は制限しない。
        """
        if self._topology_bypassed():
            return None
        get_region = getattr(self._manager_ref, "get_region", None)
        if get_region is None:
            return None
        from_scopes = set(self._scope_chain(from_id))
        to_chain = self._scope_chain(to_id)
        new_scopes = [s for s in to_chain if s not in from_scopes]
        if not new_scopes:
            return None

        if len(new_scopes) == 1:
            region = get_region(new_scopes[0])
            if region is not None and getattr(region, "entrance_building_id", None) == from_id:
                # 入口→内部の正規の通過。境界点で entry policy を執行する
                return self._check_entry_policy(entity_id, region)

        # 直行は拒否し、最外殻の新規スコープの入口を案内する
        outer = get_region(new_scopes[-1])
        dest_name = self.building_map[to_id].name if to_id in self.building_map else to_id
        if outer is None:
            return f"移動失敗: '{dest_name}' の所属 Region 情報が見つかりません。"
        entrance_id = getattr(outer, "entrance_building_id", None)
        if entrance_id:
            entrance = self.building_map.get(entrance_id)
            entrance_name = getattr(entrance, "name", entrance_id) if entrance else entrance_id
            return (
                f"移動失敗: '{dest_name}' は『{outer.name}』の内部です。"
                f"入口 '{entrance_name}' (ID: {entrance_id}) から入ってください。"
            )
        return (
            f"移動失敗: '{dest_name}' は『{outer.name}』の内部ですが、"
            "入口が設定されていないため外部から入れません。"
        )

    def _check_entry_policy(self, entity_id: str, region: Any) -> Optional[str]:
        """入口→内部の境界点で entry policy を執行する。

        config.entry_policy: 'open' (デフォルト) | 'locked' | 'whitelist'。
        locked / whitelist は config.entry_allowed (ID リスト) に載っていれば通す。
        鍵の開閉操作 (entry_policy の書き換え) は住人ペルソナの tool として別途実装する。
        """
        config = getattr(region, "config", None) or {}
        policy = config.get("entry_policy", "open")
        if policy == "open":
            return None
        allowed = [str(x) for x in (config.get("entry_allowed") or [])]
        if str(entity_id) in allowed:
            return None
        if policy == "locked":
            return f"移動失敗: 『{region.name}』には鍵がかかっています。"
        return f"移動失敗: 『{region.name}』へは関係者以外入れません。"

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

        topology_denial = self._check_entrance_topology(entity_id, from_id, to_id)
        if topology_denial:
            logging.info(
                "move_entity blocked by entrance topology: %s (%s -> %s): %s",
                entity_id, from_id, to_id, topology_denial,
            )
            return False, topology_denial

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
