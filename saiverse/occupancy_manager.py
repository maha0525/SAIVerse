import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
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
    ) -> Tuple[bool, Optional[str]]:
        """エンティティを建物間で移動させる。移動に関するすべてのロジックをここに集約する。

        W5/B1 (分離監査「移動 DB を先に commit し、後処理失敗で失敗結果と実世界が
        分裂する」) の構造:

        1. 事前チェック — False を返してよいのはここまで (まだ何も起きていない)。
        2. **単一 tx**: 位置遷移 (occupancy log / User.CURRENT_BUILDINGID) +
           leave/enter の building イベント + 台帳 applied + 後処理 outbox を
           1 commit で確定。tx が転べば全て巻き戻り、False を正直に返せる。
        3. commit 後: in-memory occupants を確定遷移から更新し、後処理
           (dynamic state / addon hooks / game lifecycle) を outbox 配送で実行。
           **commit 後は False を返さない** — 後処理の失敗は pending/dead に
           残る再配送状態であり、移動の失敗ではない。

        persona.current_building_id / user state cache の更新は従来どおり
        呼び出し側の責務 (完全な集約は柱5 = W7 の canonical 化と同時に行う —
        本変更で「失敗を返したのに DB は移動済み」の分裂は消えるため、呼び出し
        側の成功時更新は常に確定済みの遷移を映す)。

        旧 ``db_session`` パラメータは廃止 (実利用者ゼロの死んだ口。tx の所有は
        本メソッドに一本化)。
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
        quarantined = getattr(self._manager_ref, "quarantined_buildings", None) or {}
        if to_id in quarantined:
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
        elif entity_type != 'user':
            logging.warning("move_entity aborted: unknown entity type %s", entity_type)
            return False, f"不明なエンティティタイプ: {entity_type}"

        ledger = getattr(self._manager_ref, "execution_ledger", None)
        if ledger is None:
            return self._move_entity_legacy(entity_id, entity_type, from_id, to_id)

        persona_queue_id = entity_id if entity_type == 'ai' else None
        execution_id, _created = ledger.begin_execution(
            "move.entity",
            persona_id=persona_queue_id,
            payload={
                "entity_id": entity_id, "entity_type": entity_type,
                "from_id": from_id, "to_id": to_id,
            },
        )
        try:
            ledger.mark_running(execution_id)
        except Exception:
            logging.error(
                "move_entity: failed to mark running (execution=%s)",
                execution_id, exc_info=True,
            )
            try:
                # prepared 孤児を残さない (副作用ゼロ確定なので failed が正直)
                ledger.abandon_prepared(execution_id, "mark_running failed")
            except Exception:
                logging.error(
                    "move_entity: failed to abandon prepared (execution=%s)",
                    execution_id, exc_info=True,
                )
            return False, "データベースの更新中にエラーが発生しました。"

        # 2. 単一 tx: 位置遷移 + leave/enter イベント + applied + 後処理 outbox
        now = datetime.now()
        db = self.SessionLocal()
        try:
            if entity_type == 'ai':
                last_log = db.query(BuildingOccupancyLog).filter_by(
                    AIID=entity_id, BUILDINGID=from_id, EXIT_TIMESTAMP=None
                ).order_by(BuildingOccupancyLog.ENTRY_TIMESTAMP.desc()).first()
                if last_log:
                    last_log.EXIT_TIMESTAMP = now
                new_log = BuildingOccupancyLog(
                    CITYID=self.city_id, AIID=entity_id,
                    BUILDINGID=to_id, ENTRY_TIMESTAMP=now,
                )
                db.add(new_log)
                entity_name = self.id_to_name_map.get(entity_id, entity_id)
            else:
                user = db.query(UserModel).filter_by(USERID=int(entity_id)).first()
                if not user:
                    db.rollback()
                    ledger.mark_failed(execution_id, "user not found")
                    return False, "移動失敗: ユーザーが見つかりません。"
                user.CURRENT_BUILDINGID = to_id
                entity_name = user.USERNAME or "ユーザー"
            # 先行の位置遷移を flush して write ロックを取る — 以降の
            # max(seq) 読みが SQLite の単一書き手直列化に入る (採番レース防止)
            db.flush()

            from database.building_messages import (
                insert_building_message_in_session,
            )
            for event_building_id, event_msg in self._build_occupancy_events(
                entity_id, entity_type, entity_name, from_id, to_id, now,
            ):
                if event_building_id in quarantined:
                    logging.warning(
                        "move_entity: building %s is quarantined — occupancy "
                        "event skipped", event_building_id,
                    )
                    continue
                insert_building_message_in_session(
                    db, event_building_id, event_msg
                )

            outbox_items = self._build_move_outbox_items(
                entity_id, entity_type, from_id, to_id, persona_queue_id
            )
            ledger.mark_applied(
                execution_id,
                result={"from": from_id, "to": to_id},
                outbox_items=outbox_items,
                session=db,
            )
            db.commit()
        except Exception as e:
            db.rollback()
            logging.error(
                f"Failed to move {entity_type} '{entity_id}' in DB: {e}",
                exc_info=True,
            )
            try:
                ledger.mark_failed(execution_id, str(e) or type(e).__name__)
            except Exception:
                logging.error(
                    "move_entity: failed to record move failure (execution=%s)",
                    execution_id, exc_info=True,
                )
            return False, "データベースの更新中にエラーが発生しました。"
        finally:
            db.close()

        # 3. commit 済み — ここから先は False を返さない (B1)。
        #    in-memory は確定した遷移をそのまま映す。
        if entity_id in self.occupants.get(from_id, []):
            self.occupants[from_id].remove(entity_id)
        self.occupants.setdefault(to_id, []).append(entity_id)
        logging.info(f"Moved {entity_type} '{entity_id}' from {from_id} to {to_id}.")

        # 後処理 (dynamic state / addon hooks / game lifecycle) の即時配送試行。
        # 失敗しても pending に残り、Beat 関所 / 回復 tick が引き継ぐ。
        try:
            ledger.flush_pending_for_persona(persona_queue_id)
        except Exception:
            logging.warning(
                "move_entity: post-move delivery deferred (%s -> %s); outbox "
                "remains pending for the gate / recovery tick",
                from_id, to_id, exc_info=True,
            )
        return True, None

    def _build_occupancy_events(
        self,
        entity_id: str,
        entity_type: str,
        entity_name: str,
        from_id: str,
        to_id: str,
        now: datetime,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """leave/enter の building イベント (host message) を組み立てる。

        heard_by は**移動後**の占有 (leave = 残った目撃者 / enter = 移動者含む
        到着記録) — in-memory occupants は commit 後まで触らないため、ここでは
        無変異で導出する。
        """
        from_building_name = self.building_map[from_id].name if from_id in self.building_map else from_id
        to_building_name = self.building_map[to_id].name
        action_type = "AI Action" if entity_type == 'ai' else "User Action"
        event_key = f"occupancy:{entity_id}:{from_id}:{to_id}:{int(now.timestamp())}"
        timestamp = now.astimezone(timezone.utc).isoformat()
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
        from_occupants_after = sorted({
            str(eid) for eid in self.occupants.get(from_id, [])
            if eid and str(eid) != entity_id
        })
        to_occupants_after = sorted({
            str(eid) for eid in ([*self.occupants.get(to_id, []), entity_id])
            if eid
        })
        return [
            (from_id, {
                "role": "host", "content": left_message,
                "metadata": left_metadata, "timestamp": timestamp,
                "heard_by": from_occupants_after, "ingested_by": [],
            }),
            (to_id, {
                "role": "host", "content": entered_message,
                "metadata": enter_metadata, "timestamp": timestamp,
                "heard_by": to_occupants_after, "ingested_by": [],
            }),
        ]

    def _build_move_outbox_items(
        self,
        entity_id: str,
        entity_type: str,
        from_id: str,
        to_id: str,
        persona_queue_id: Optional[str],
    ) -> List[Dict[str, Any]]:
        """移動後処理の outbox item 列 (dynamic state / addon hooks / game lifecycle)。

        payload は移動時点の事実を凍結する。配送順は persona キュー内 FIFO
        (OUTBOX_ID 昇順) で従来の呼び出し順 (dynamic state → hooks → lifecycle)
        を保つ。ハンドラは execution_ledger_wiring 側。
        """
        payload = {
            "entity_id": entity_id, "entity_type": entity_type,
            "from_id": from_id, "to_id": to_id,
        }
        items: List[Dict[str, Any]] = []
        if entity_type == 'ai':
            items.append({
                "target": "move.post_dynamic_state",
                "payload": payload, "persona_id": persona_queue_id,
            })
            items.append({
                "target": "move.post_addon_hooks",
                "payload": payload, "persona_id": persona_queue_id,
            })
        items.append({
            "target": "move.post_game_lifecycle",
            "payload": payload, "persona_id": persona_queue_id,
        })
        return items

    def _move_entity_legacy(
        self,
        entity_id: str,
        entity_type: str,
        from_id: str,
        to_id: str,
    ) -> Tuple[bool, Optional[str]]:
        """execution_ledger の無い環境 (旧テストスタブ等) の縮退経路。

        従来実装のまま: DB commit 後の後処理 (イベント・hook) が裸で走る。
        本番 manager は常に台帳を持つため、この経路は縮退時のみ。
        """
        logging.warning(
            "move_entity: manager has no execution_ledger; running in legacy "
            "mode (%s -> %s)", from_id, to_id,
        )
        db = self.SessionLocal()
        try:
            now = datetime.now()
            if entity_type == 'ai':
                last_log = db.query(BuildingOccupancyLog).filter_by(AIID=entity_id, BUILDINGID=from_id, EXIT_TIMESTAMP=None).order_by(BuildingOccupancyLog.ENTRY_TIMESTAMP.desc()).first()
                if last_log:
                    last_log.EXIT_TIMESTAMP = now
                new_log = BuildingOccupancyLog(CITYID=self.city_id, AIID=entity_id, BUILDINGID=to_id, ENTRY_TIMESTAMP=now)
                db.add(new_log)
                entity_name = self.id_to_name_map.get(entity_id, entity_id)
            else:
                user = db.query(UserModel).filter_by(USERID=int(entity_id)).first()
                if not user:
                    return False, "移動失敗: ユーザーが見つかりません。"
                user.CURRENT_BUILDINGID = to_id
                entity_name = user.USERNAME or "ユーザー"

            db.commit()

            if entity_id in self.occupants.get(from_id, []):
                self.occupants[from_id].remove(entity_id)
            self.occupants.setdefault(to_id, []).append(entity_id)

            mgr = self._manager_ref
            if mgr is not None and hasattr(mgr, "add_building_event"):
                for event_building_id, event_msg in self._build_occupancy_events(
                    entity_id, entity_type, entity_name, from_id, to_id, now,
                ):
                    heard_by = event_msg.pop("heard_by", [])
                    event_msg.pop("ingested_by", None)
                    # 上の occupants 更新後なので heard_by を再算出せずそのまま使う
                    mgr.add_building_event(event_building_id, event_msg, heard_by=heard_by)
            else:
                logging.warning(
                    "occupancy event ignored: manager_ref unavailable for %s -> %s",
                    from_id, to_id,
                )

            logging.info(f"Moved {entity_type} '{entity_id}' from {from_id} to {to_id}.")

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

                try:
                    from saiverse.addon_hooks import dispatch_hook
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

            lifecycle = getattr(self._manager_ref, "game_lifecycle", None)
            if lifecycle is not None:
                lifecycle.on_entity_moved(entity_id, from_id, to_id)

            return True, None
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to move {entity_type} '{entity_id}' in DB: {e}", exc_info=True)
            return False, "データベースの更新中にエラーが発生しました。"
        finally:
            db.close()

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
