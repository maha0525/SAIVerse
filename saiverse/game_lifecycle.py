"""game Region のライフサイクル状態機械。

phase: setup → playing ⇄ paused → (end_game: アーカイブ →) setup に戻る。

- 参加者 (participants) は start_game で確定し、Region.STATE_JSON に保存される。
  入場ゲート (occupancy_manager._check_game_region_gate) はこの state を参照する。
- 参加者が Region 外へ出ると自動ポーズ、全員が戻ると自動再開 (on_entity_moved)。
- パーティー移動の統一: ゲーム中のパーティー (参加者一行 + Ruler) は常に同じ
  Building にいる (state.party_location)。ユーザー参加者の移動に全員が追従し、
  Ruler の game_move_party spell で一行ごと移動できる。これにより auto_ingest
  (現在地 Building の履歴のみ取り込む) が特別扱いなしで全員に機能する。
- 終了時はセッションアーカイブを必ず保存してから状態をリセットする
  (動的生成された世界を消さない — NPC ペルソナ昇格などの原資になる)。
  終了後はパーティー全員が控室へ自動帰還する。
- phase 遷移とシーン変更は host イベントとしてパーティーの現在 Building に
  流す (auto_ingest 経由で参加ペルソナ + Ruler の SAIMemory に届く)。

設計意図: temp/region_rpg_intent.md §C-5, §D (リポジトリ外管理)
"""
import json
import logging
import uuid
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from database.models import Region as RegionModel
from saiverse.regions import Region

LOGGER = logging.getLogger(__name__)

PHASE_SETUP = "setup"
PHASE_PLAYING = "playing"
PHASE_PAUSED = "paused"

VALID_OUTCOMES = ("clear", "gameover", "aborted")


class GameLifecycleService:
    def __init__(self, manager):
        self.manager = manager

    def _system_move_context(self):
        """ライフサイクル主導の移動 (パーティー追従・帰還・復帰) は system 移動
        として入口トポロジーをバイパスする (docs/intent/region.md §2.4)。
        """
        om = getattr(self.manager, "occupancy_manager", None)
        bypass = getattr(om, "topology_bypass", None)
        return bypass() if bypass else nullcontext()

    # ------------------------------------------------------------------
    # phase 遷移 API
    # ------------------------------------------------------------------

    def start_game(self, region_id: str, participant_ids: List[str]) -> str:
        region, err = self._resolve_game_region(region_id)
        if err:
            return err
        if not region.ruler_id:
            return "Error: This region has no Ruler. Create a Ruler first."
        phase = region.state.get("phase")
        if phase in (PHASE_PLAYING, PHASE_PAUSED):
            return "Error: A game is already in progress in this region."

        participants = [str(p) for p in (participant_ids or [])]
        if not participants:
            return "Error: At least one participant is required."
        user_id = str(self.manager.user_id)
        unknown = [
            p for p in participants
            if p != user_id and p not in self.manager.personas
        ]
        if unknown:
            return f"Error: Unknown participants: {', '.join(unknown)}"
        if str(region.ruler_id) in participants:
            return "Error: The Ruler cannot be a participant."

        new_state = dict(region.state)
        new_state.update({
            "phase": PHASE_PLAYING,
            "participants": participants,
            "session_id": uuid.uuid4().hex,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "scene": new_state.get("scene") or "exploration",
        })
        err = self._write_state(region_id, new_state)
        if err:
            return err
        self._on_game_started(region_id, participants)

        # ユーザーが既に Region 内にいる場合はその場にパーティーを集合させる。
        # (通常は控室から開始 → ユーザーの最初の移動で追従が発火する)
        event_building = self.manager.state.user_current_building_id
        region_building_ids = {
            b.building_id for b in self.manager.get_region_buildings(region_id)
        }
        if event_building in region_building_ids:
            fresh = self.manager.regions.get(region_id)
            if fresh is not None:
                self._move_party_to(fresh, event_building, anchor_entity_id=user_id)
        self._emit_game_event(
            region_id,
            event_building or region.entrance_building_id,
            f"RPG セッションを開始しました (世界: {region.name} / scene: {new_state['scene']})",
            action="start",
        )
        LOGGER.info(
            "[game_lifecycle] Game started in region '%s' (session=%s, participants=%s)",
            region_id, new_state["session_id"], participants,
        )
        return (
            f"Game started in region '{region.name}' "
            f"(session {new_state['session_id'][:8]}, {len(participants)} participants)."
        )

    def pause_game(self, region_id: str, reason: str = "") -> str:
        region, err = self._resolve_game_region(region_id)
        if err:
            return err
        if region.state.get("phase") != PHASE_PLAYING:
            return "Error: No game is currently playing in this region."
        new_state = dict(region.state)
        new_state["phase"] = PHASE_PAUSED
        new_state["paused_at"] = datetime.now().isoformat(timespec="seconds")
        if reason:
            new_state["pause_reason"] = reason
        err = self._write_state(region_id, new_state)
        if err:
            return err
        self._emit_game_event(
            region_id,
            new_state.get("party_location") or region.entrance_building_id,
            f"ゲームを一時中断しました ({reason or 'manual'})",
            action="pause",
        )
        LOGGER.info("[game_lifecycle] Game paused in region '%s' (%s)", region_id, reason or "manual")
        return f"Game paused in region '{region.name}'."

    def resume_game(
        self, region_id: str, *,
        location_overrides: Optional[Dict[str, str]] = None,
    ) -> str:
        """ゲームを再開する。

        location_overrides は on_entity_moved (移動フック) 専用: 移動した本人の
        state 更新がフックより後になるため、所在を to_id で上書きして判定する。
        手動 API (POST /game/resume) からは渡さない。
        """
        region, err = self._resolve_game_region(region_id)
        if err:
            return err
        if region.state.get("phase") != PHASE_PAUSED:
            return "Error: No paused game in this region."
        if not self._all_participants_inside(region, overrides=location_overrides):
            return "Error: Cannot resume until all participants are inside the region."
        new_state = dict(region.state)
        new_state["phase"] = PHASE_PLAYING
        new_state.pop("paused_at", None)
        new_state.pop("pause_reason", None)
        err = self._write_state(region_id, new_state)
        if err:
            return err
        self._emit_game_event(
            region_id,
            new_state.get("party_location") or region.entrance_building_id,
            "ゲームを再開しました",
            action="resume",
        )
        LOGGER.info("[game_lifecycle] Game resumed in region '%s'", region_id)
        return f"Game resumed in region '{region.name}'."

    def end_game(self, region_id: str, outcome: str = "clear") -> str:
        if outcome not in VALID_OUTCOMES:
            return f"Error: Invalid outcome '{outcome}'. Must be one of {VALID_OUTCOMES}."
        region, err = self._resolve_game_region(region_id)
        if err:
            return err
        if region.state.get("phase") not in (PHASE_PLAYING, PHASE_PAUSED):
            return "Error: No game in progress in this region."

        # アーカイブは必須: 失敗したらゲームを閉じない (世界を消さない)
        archive_path, archive_err = self._archive_session(region, outcome)
        if archive_err:
            return archive_err

        participants = list(region.state.get("participants", []))
        new_state = dict(region.state)
        session_id = new_state.get("session_id")
        new_state.update({
            "phase": PHASE_SETUP,
            "participants": [],
            "last_session_id": session_id,
            "last_outcome": outcome,
        })
        for key in ("session_id", "started_at", "paused_at", "pause_reason", "scene",
                    "party_location"):
            new_state.pop(key, None)
        err = self._write_state(region_id, new_state)
        if err:
            return err
        self._on_game_ended(region_id, participants)

        # パーティー全員 (参加者 + Ruler) を控室へ帰還させる。state リセット後に
        # 行うことで、移動フック (auto-pause) と入場ゲートが反応しない。
        lobby = region.entrance_building_id
        if lobby:
            targets = list(participants)
            if region.ruler_id:
                targets.append(str(region.ruler_id))
            self._move_entities_to(region.region_id, targets, lobby, only_from_region=True)
            self._emit_game_event(
                region_id, lobby,
                f"ゲームが終了しました (結果: {outcome})。控室に戻りました",
                action="end",
                participants_override=participants,
            )
        LOGGER.info(
            "[game_lifecycle] Game ended in region '%s' (outcome=%s, archive=%s)",
            region_id, outcome, archive_path,
        )
        return (
            f"Game ended in region '{region.name}' (outcome: {outcome}). "
            f"Session archived to {archive_path}."
        )

    def set_scene(self, region_id: str, scene: str) -> str:
        """ゲーム中のシーン状態 (exploration / conversation / battle / shopping / rest 等) を変更する。

        シーンは自由文字列 (列挙は推奨値)。ゲーム進行中のみ変更できる。
        """
        region, err = self._resolve_game_region(region_id)
        if err:
            return err
        if region.state.get("phase") not in (PHASE_PLAYING, PHASE_PAUSED):
            return "Error: No game in progress in this region."
        scene = (scene or "").strip()
        if not scene:
            return "Error: scene is required."
        new_state = dict(region.state)
        new_state["scene"] = scene
        err = self._write_state(region_id, new_state)
        if err:
            return err
        self._emit_game_event(
            region_id,
            new_state.get("party_location") or region.entrance_building_id,
            f"シーンが「{scene}」に変わりました",
            action="scene_change",
        )
        LOGGER.info("[game_lifecycle] Scene changed to '%s' in region '%s'", scene, region_id)
        return f"Scene changed to '{scene}'."

    # ------------------------------------------------------------------
    # 移動フック (自動ポーズ / 自動再開)
    # ------------------------------------------------------------------

    def on_entity_moved(self, entity_id: str, from_id: str, to_id: str) -> bool:
        """OccupancyManager.move_entity 成功後に呼ばれる。

        - playing 中に参加者が Region 外へ出たら自動ポーズ
        - ユーザー参加者が Region 内 Building へ移動したらパーティー全員が追従
          (追従発火はユーザーの移動のみ。ペルソナの移動では発火しないため
          追従移動が再帰的に連鎖することはない)
        - paused 中に参加者が戻って全員揃ったら自動再開
        移動本体を巻き込まないため、例外はここで吸収してログする。

        Returns:
            成功したか (2026-07-21 Codex レビュー P2)。outbox 配送経路
            (move.post_game_lifecycle ハンドラ) がこの戻り値を見て失敗を
            再試行対象にする。直接呼び出し元 (縮退経路・既存テスト) は
            戻り値を無視してよい。
        """
        try:
            entity_id = str(entity_id)
            from_region = self._game_region_of(from_id)
            to_region = self._game_region_of(to_id)

            # 退出: playing 中の Region から外へ (Region 内移動は除く)
            if (
                from_region is not None
                and (to_region is None or to_region.region_id != from_region.region_id)
                and from_region.state.get("phase") == PHASE_PLAYING
                and entity_id in [str(p) for p in from_region.state.get("participants", [])]
            ):
                result = self.pause_game(
                    from_region.region_id,
                    reason=f"participant '{entity_id}' left the region",
                )
                LOGGER.info("[game_lifecycle] auto-pause: %s", result)

            # パーティー追従: ユーザー参加者が Region 内 Building へ移動したら
            # 参加ペルソナ + Ruler を同じ Building に集める (再開時の再集結も
            # この経路: 追従で全員が揃った後、下の resume 判定が成立する)
            if (
                to_region is not None
                and to_region.state.get("phase") in (PHASE_PLAYING, PHASE_PAUSED)
                and entity_id == str(self.manager.user_id)
                and entity_id in [str(p) for p in to_region.state.get("participants", [])]
            ):
                self._move_party_to(to_region, to_id, anchor_entity_id=entity_id)

            # 入場: paused 中の Region へ参加者が戻った
            # 移動した本人の所在は to_id で上書きして判定する: ユーザー移動では
            # state.user_current_building_id の更新が move_entity (= このフック) の
            # 後に行われるため、state を読むと旧所在地のままで判定が空振りする
            if (
                to_region is not None
                and (from_region is None or from_region.region_id != to_region.region_id)
                and to_region.state.get("phase") == PHASE_PAUSED
                and entity_id in [str(p) for p in to_region.state.get("participants", [])]
                and self._all_participants_inside(to_region, overrides={entity_id: to_id})
            ):
                result = self.resume_game(
                    to_region.region_id,
                    location_overrides={entity_id: to_id},
                )
                LOGGER.info("[game_lifecycle] auto-resume: %s", result)
            return True
        except Exception:
            LOGGER.exception(
                "[game_lifecycle] on_entity_moved failed (entity=%s, %s -> %s)",
                entity_id, from_id, to_id,
            )
            return False

    # ------------------------------------------------------------------
    # パーティー移動
    # ------------------------------------------------------------------

    def move_party(self, region_id: str, to_building_id: str) -> str:
        """パーティー (参加者一行 + Ruler) を Region 内の Building へ一括移動する。

        Ruler の game_move_party spell から呼ばれる (= GM の語りによる場面転換)。
        ユーザーも含めて移動する。
        """
        region, err = self._resolve_game_region(region_id)
        if err:
            return err
        if region.state.get("phase") != PHASE_PLAYING:
            return "Error: No game is currently playing in this region."
        building_ids = {
            b.building_id for b in self.manager.get_region_buildings(region_id)
        }
        if to_building_id not in building_ids:
            return f"Error: Building '{to_building_id}' is not part of this region."
        failed = self._move_party_to(region, to_building_id, include_user=True)
        building = self.manager.building_map.get(to_building_id)
        name = getattr(building, "name", to_building_id) if building else to_building_id
        if failed:
            return (
                f"Party moved to '{name}', but some members could not move: "
                f"{', '.join(failed)}"
            )
        return f"Party moved to '{name}'."

    def rejoin_party(self, region_id: str) -> str:
        """ユーザーをパーティーの現在地へ復帰させる (入口の「復帰」ボタン経路)。

        参加者資格が認可そのものなので system 移動として入口トポロジーを
        バイパスする (party_location が SubRegion 内部でも直接戻れる)。
        移動に伴い on_entity_moved がパーティー再集結 + 自動再開を発火する。
        """
        region, err = self._resolve_game_region(region_id)
        if err:
            return err
        phase = region.state.get("phase")
        if phase not in (PHASE_PLAYING, PHASE_PAUSED):
            return "Error: No game in progress in this region."
        user_id = str(self.manager.user_id)
        if user_id not in [str(p) for p in region.state.get("participants", [])]:
            return "Error: You are not a participant of this game."
        target = region.state.get("party_location") or region.entrance_building_id
        if not target:
            return "Error: The party location is unknown."
        if self.manager.state.user_current_building_id == target:
            return "Already with the party."
        with self._system_move_context():
            ok, msg = self.manager.move_user(target)
        if not ok:
            return f"Error: Failed to rejoin the party: {msg}"
        building = self.manager.building_map.get(target)
        name = getattr(building, "name", target) if building else target
        return f"Rejoined the party at '{name}'."

    def _move_party_to(
        self,
        region: Region,
        to_building_id: str,
        *,
        anchor_entity_id: Optional[str] = None,
        include_user: bool = False,
    ) -> List[str]:
        """参加ペルソナ + Ruler を to_building_id へ移動し、party_location を更新する。

        anchor_entity_id (= 自力で移動済みのエンティティ) はスキップする。
        ユーザーは include_user=True のとき (= game_move_party spell 経路) のみ動かす。
        戻り値は移動に失敗したエンティティ ID のリスト (失敗してもゲームは止めない)。
        """
        user_id = str(self.manager.user_id)
        targets = [str(p) for p in region.state.get("participants", [])]
        if region.ruler_id:
            targets.append(str(region.ruler_id))

        failed: List[str] = []
        with self._system_move_context():
            for eid in targets:
                if anchor_entity_id is not None and eid == str(anchor_entity_id):
                    continue
                if eid == user_id:
                    if not include_user:
                        continue
                    ok, msg = self.manager.move_user(to_building_id)
                else:
                    if eid not in self.manager.personas:
                        continue
                    ok, msg = self.manager.summon_persona(eid, to_building_id)
                if not ok:
                    failed.append(eid)
                    LOGGER.warning(
                        "[game_lifecycle] party member '%s' failed to move to '%s': %s",
                        eid, to_building_id, msg,
                    )

        # party_location を保存 (途中の自動再開等で state が動いている可能性が
        # あるので、reload 済みの最新 state に対して書く)
        fresh = self.manager.regions.get(region.region_id)
        if fresh is not None and fresh.state.get("party_location") != to_building_id:
            new_state = dict(fresh.state)
            new_state["party_location"] = to_building_id
            err = self._write_state(region.region_id, new_state)
            if err:
                LOGGER.error(
                    "[game_lifecycle] failed to persist party_location for '%s': %s",
                    region.region_id, err,
                )
        return failed

    def _move_entities_to(
        self,
        region_id: str,
        entity_ids: List[str],
        to_building_id: str,
        *,
        only_from_region: bool = False,
    ) -> None:
        """指定エンティティ群を to_building_id へ移動する (end_game の控室帰還用)。

        only_from_region=True なら、現在 Region 内にいるエンティティだけを動かす
        (= 中断中に既に外へ出ているユーザーをテレポートさせない)。
        """
        region_building_ids = {
            b.building_id for b in self.manager.get_region_buildings(region_id)
        }
        user_id = str(self.manager.user_id)
        with self._system_move_context():
            for eid in entity_ids:
                eid = str(eid)
                if eid == user_id:
                    loc = self.manager.state.user_current_building_id
                else:
                    persona = self.manager.personas.get(eid)
                    if persona is None:
                        continue
                    loc = getattr(persona, "current_building_id", None)
                if loc == to_building_id:
                    continue
                if only_from_region and loc not in region_building_ids:
                    continue
                if eid == user_id:
                    ok, msg = self.manager.move_user(to_building_id)
                else:
                    ok, msg = self.manager.summon_persona(eid, to_building_id)
                if not ok:
                    LOGGER.warning(
                        "[game_lifecycle] failed to return '%s' to '%s': %s",
                        eid, to_building_id, msg,
                    )

    # ------------------------------------------------------------------
    # ライフサイクルイベント通知
    # ------------------------------------------------------------------

    def _emit_game_event(
        self,
        region_id: str,
        building_id: Optional[str],
        text: str,
        *,
        action: str,
        participants_override: Optional[List[str]] = None,
    ) -> None:
        """phase 遷移 / シーン変更を host イベントとして Building に流す。

        auto_ingest の host 経路を通って参加ペルソナ + Ruler の SAIMemory に
        event_message タグ付きで届く。通知失敗でゲーム進行は止めない。
        """
        if not building_id:
            return
        try:
            region = self.manager.regions.get(region_id)
            if participants_override is not None:
                heard_by = list(participants_override)
            else:
                heard_by = (
                    [str(p) for p in region.state.get("participants", [])]
                    if region else []
                )
            if region and region.ruler_id:
                heard_by.append(str(region.ruler_id))
            self.manager.add_building_event(
                building_id,
                {
                    "role": "host",
                    "content": (
                        f'<div class="note-box">🎲 Game:<br><b>{text}</b></div>'
                    ),
                    "metadata": {
                        "event": {
                            "type": "game_lifecycle",
                            "action": action,
                            "region_id": region_id,
                        }
                    },
                },
                heard_by=heard_by,
            )
        except Exception:
            LOGGER.exception(
                "[game_lifecycle] failed to emit game event (%s in %s)",
                action, building_id,
            )

    # ------------------------------------------------------------------
    # セッションアーカイブ
    # ------------------------------------------------------------------

    def _archive_session(self, region: Region, outcome: str) -> Tuple[Optional[str], Optional[str]]:
        """セッションの全状態を JSON として保存する。

        Returns:
            (archive_path, None) on success / (None, error message) on failure
        """
        try:
            from saiverse.data_paths import USER_DATA_DIR
            session_id = region.state.get("session_id") or f"unknown_{uuid.uuid4().hex[:8]}"
            archive_dir = USER_DATA_DIR / "game_sessions" / session_id
            archive_dir.mkdir(parents=True, exist_ok=True)

            subregions = self.manager.get_subregions(region.region_id)
            buildings = self.manager.get_region_buildings(region.region_id)
            items_by_building: Dict[str, List[Dict[str, Any]]] = {}
            for b in buildings:
                item_ids = self.manager.items_by_building.get(b.building_id, [])
                items_by_building[b.building_id] = [
                    dict(self.manager.items[iid]) for iid in item_ids if iid in self.manager.items
                ]

            snapshot = {
                "schema_version": 1,
                "session_id": session_id,
                "outcome": outcome,
                "ended_at": datetime.now().isoformat(timespec="seconds"),
                "region": self._region_to_dict(region),
                "subregions": [self._region_to_dict(r) for r in subregions],
                "buildings": [
                    {
                        "building_id": b.building_id,
                        "name": b.name,
                        "description": b.description,
                        "system_instruction": b.base_system_instruction,
                        "region_id": b.region_id,
                    }
                    for b in buildings
                ],
                "items_by_building": items_by_building,
                "state": dict(region.state),
                # キャラシート (game_character_sheet) は Phase 2 実装時にここへ追加する
            }
            archive_file = archive_dir / "session.json"
            archive_file.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=1, default=str),
                encoding="utf-8",
            )
            LOGGER.info("[game_lifecycle] Session archived: %s", archive_file)
            return str(archive_file), None
        except Exception as exc:
            LOGGER.error(
                "[game_lifecycle] Failed to archive session for region '%s': %s",
                region.region_id, exc, exc_info=True,
            )
            return None, f"Error: Failed to archive session; game not closed. ({exc})"

    @staticmethod
    def _region_to_dict(region: Region) -> Dict[str, Any]:
        return {
            "region_id": region.region_id,
            "parent_region_id": region.parent_region_id,
            "name": region.name,
            "description": region.description,
            "region_type": region.region_type,
            "ruler_id": region.ruler_id,
            "entrance_building_id": region.entrance_building_id,
            "config": dict(region.config),
        }

    # ------------------------------------------------------------------
    # Track 拘束 (専用 Track `game_session`)
    # ------------------------------------------------------------------
    # ゲーム開始時に参加ペルソナへ game_session Track を initial_status=running で
    # 作成する。TrackManager が既存 running Track を pending に押し出すため、
    # 進行中の行動はここで自然に退避される。連続 auto-pulse 機構は存在しない
    # (旧 SubLineScheduler は自律行動 v2 で廃止) ので、この Track が勝手に回る
    # ことはない。メタ判断の停止は MetaLayer.on_periodic_tick 側の
    # is_participating ゲートで行う。

    GAME_TRACK_TYPE = "game_session"

    def active_game_for_user(self) -> Optional[Dict[str, Any]]:
        """ユーザーが参加中 (playing/paused) のゲームがあれば、現在地に関わらず
        UI のゲームモード判定情報を返す。それ以外は None。

        場所要件を課さない理由: 復帰 (rejoin) の認可は参加者資格そのものであり、
        UI 上のユーザー移動は発言を伴う (= 復帰のために控室で一言喋らせるのは
        無意味な手順になる)。物理位置は inside / at_entrance で通知し、表示の
        切り替えはフロント側が行う。

        /api/user/status に載せて LocationSync ポーリングに相乗りさせる。
        - inside=True: セッションログ表示 + 発話可
        - inside=False (入口・ゲーム外を問わず): 通常チャット + セッションログの
          read-only 閲覧トグル + 「復帰」ボタン
        """
        user_id = str(self.manager.user_id)
        user_bid = self.manager.state.user_current_building_id
        for region in self.manager.regions.values():
            if not region.is_game_region:
                continue
            state = region.state
            if state.get("phase") not in (PHASE_PLAYING, PHASE_PAUSED):
                continue
            if user_id not in [str(p) for p in state.get("participants", [])]:
                continue
            inside_region = self._game_region_of(user_bid)
            return {
                "region_id": region.region_id,
                "region_name": region.name,
                "phase": state.get("phase"),
                "scene": state.get("scene"),
                "party_location": state.get("party_location"),
                "inside": inside_region is not None
                and inside_region.region_id == region.region_id,
                "at_entrance": user_bid is not None
                and user_bid == region.entrance_building_id,
            }
        return None

    def is_participating(self, persona_id: str) -> bool:
        """ペルソナが進行中 (playing / paused) のゲームの参加者かを返す。"""
        pid = str(persona_id)
        for region in self.manager.regions.values():
            if not region.is_game_region:
                continue
            if region.state.get("phase") not in (PHASE_PLAYING, PHASE_PAUSED):
                continue
            if pid in [str(p) for p in region.state.get("participants", [])]:
                return True
        return False

    def _on_game_started(self, region_id: str, participants: List[str]) -> None:
        tm = getattr(self.manager, "track_manager", None)
        if tm is None:
            return
        region = self.manager.regions.get(region_id)
        region_name = region.name if region else region_id
        session_id = region.state.get("session_id") if region else None
        from saiverse.track_manager import STATUS_RUNNING
        for pid in participants:
            if pid not in self.manager.personas:
                continue  # user はスキップ (Track はペルソナのもの)
            try:
                track_id = tm.create(
                    persona_id=pid,
                    track_type=self.GAME_TRACK_TYPE,
                    title=f"ゲームセッション: {region_name}",
                    intent=(
                        f"Region『{region_name}』で GM (Ruler) の進行する RPG セッションに"
                        "参加している。ゲームが終了するまでこの活動に専念する。"
                    ),
                    metadata=json.dumps(
                        {"region_id": region_id, "session_id": session_id},
                        ensure_ascii=False,
                    ),
                    initial_status=STATUS_RUNNING,
                )
                LOGGER.info(
                    "[game_lifecycle] game_session track %s created for persona %s",
                    track_id, pid,
                )
            except Exception:
                LOGGER.exception(
                    "[game_lifecycle] Failed to create game_session track for persona %s", pid
                )

    def _on_game_ended(self, region_id: str, participants: List[str]) -> None:
        tm = getattr(self.manager, "track_manager", None)
        if tm is None:
            return
        from saiverse.track_manager import STATUS_PENDING, STATUS_RUNNING
        for pid in participants:
            if pid not in self.manager.personas:
                continue
            try:
                tracks = tm.list_for_persona(
                    pid, statuses=[STATUS_RUNNING, STATUS_PENDING]
                )
                for track in tracks:
                    if track.track_type != self.GAME_TRACK_TYPE:
                        continue
                    tm.complete(track.track_id)
                    LOGGER.info(
                        "[game_lifecycle] game_session track %s completed for persona %s",
                        track.track_id, pid,
                    )
            except Exception:
                LOGGER.exception(
                    "[game_lifecycle] Failed to complete game_session track for persona %s", pid
                )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _resolve_game_region(self, region_id: str) -> Tuple[Optional[Region], Optional[str]]:
        region = self.manager.regions.get(region_id)
        if region is None:
            return None, "Error: Region not found."
        if not region.is_game_region:
            return None, "Error: Not a game region."
        return region, None

    def _game_region_of(self, building_id: Optional[str]) -> Optional[Region]:
        if not building_id:
            return None
        region = self.manager.get_top_region_of_building(building_id)
        if region is not None and region.is_game_region:
            return region
        return None

    def _all_participants_inside(
        self, region: Region, overrides: Optional[Dict[str, str]] = None
    ) -> bool:
        """参加者全員が Region 内に居るかを返す。

        overrides: {entity_id: building_id}。移動フックから呼ぶ場合、移動した
        本人の state 更新がまだ済んでいないことがある (ユーザー移動の
        user_current_building_id は move_entity の後に更新される) ため、
        フックの引数 to_id をここで上書きとして渡す。
        """
        building_ids = {
            b.building_id for b in self.manager.get_region_buildings(region.region_id)
        }
        user_id = str(self.manager.user_id)
        for pid in [str(p) for p in region.state.get("participants", [])]:
            if overrides and pid in overrides:
                loc = overrides[pid]
            elif pid == user_id:
                loc = self.manager.state.user_current_building_id
            else:
                persona = self.manager.personas.get(pid)
                loc = getattr(persona, "current_building_id", None) if persona else None
            if loc not in building_ids:
                return False
        return True

    def _write_state(self, region_id: str, state: Dict[str, Any]) -> Optional[str]:
        db = self.manager.SessionLocal()
        try:
            db_region = db.query(RegionModel).filter_by(REGION_ID=region_id).first()
            if db_region is None:
                return "Error: Region not found in DB."
            db_region.STATE_JSON = json.dumps(state, ensure_ascii=False)
            db.commit()
        except Exception as exc:
            db.rollback()
            LOGGER.error(
                "[game_lifecycle] Failed to write state for region '%s': %s",
                region_id, exc, exc_info=True,
            )
            return f"Error: {exc}"
        finally:
            db.close()
        self.manager._reload_regions()
        return None
