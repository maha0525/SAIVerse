import json
import logging
from datetime import datetime
from typing import Tuple

import requests

from database.models import AI as AIModel, BuildingOccupancyLog, VisitingAI
from remote_persona_proxy import RemotePersonaProxy


class VisitorMixin:
    """Dispatch and visiting persona management helpers."""

    def dispatch_persona(
        self, persona_id: str, target_city_id: str, target_building_id: str
    ) -> Tuple[bool, str]:
        """
        Dispatches a persona to another city.
        1. Sends the persona's profile to the target city's API.
        2. If accepted, records the transaction for follow-up by the destination city.
        """
        target_city_info = self.cities_config.get(target_city_id)
        if not target_city_info:
            logging.warning(
                "Target city '%s' not in cache. Forcing update from SDS.",
                target_city_id,
            )
            self._update_cities_from_sds()
            target_city_info = self.cities_config.get(target_city_id)

        if not target_city_info:
            return (
                False,
                f"移動失敗: City '{target_city_id}' はネットワーク上に見つかりませんでした。"
                "相手のCityが起動しているか確認してください。",
            )

        persona = self.personas.get(persona_id)
        if not persona:
            return False, f"Persona with ID '{persona_id}' not found in this city."

        profile = {
            "persona_id": persona.persona_id,
            "persona_name": persona.persona_name,
            "target_building_id": target_building_id,
            "avatar_image": persona.avatar_image,
            "emotion": persona.emotion,
            "source_city_id": self.city_name,
        }

        db = self.SessionLocal()
        try:
            target_city_db_id = target_city_info["city_id"]
            existing_dispatch = (
                db.query(VisitingAI)
                .filter_by(city_id=target_city_db_id, persona_id=persona_id)
                .first()
            )
            if existing_dispatch:
                return False, "既にこのCityへの移動要求が進行中です。"

            new_dispatch = VisitingAI(
                city_id=target_city_db_id,
                persona_id=persona_id,
                profile_json=json.dumps(profile),
                status="requested",
            )
            db.add(new_dispatch)
            db.commit()
            logging.info(
                "Created dispatch request for %s to %s.",
                persona.persona_name,
                target_city_id,
            )
            persona.history_manager.add_message(
                {
                    "role": "system",
                    "content": f"{target_city_id}への移動を要求しました。相手の応答を待っています...",
                },
                persona.current_building_id,
                heard_by=list(self.occupants.get(persona.current_building_id, [])),
            )
            return True, "移動要求を送信しました。"
        except Exception as exc:
            db.rollback()
            logging.error(
                "Failed to create dispatch request for %s: %s",
                persona.persona_name,
                exc,
                exc_info=True,
            )
            return False, "移動要求の作成中にデータベースエラーが発生しました。"
        finally:
            db.close()

    def _finalize_dispatch(self, persona_id: str, db_session) -> None:
        """移動が承認された後、AIをローカルから退去させる最終処理"""
        persona = self.personas.get(persona_id)
        if not persona:
            return

        last_log = (
            db_session.query(BuildingOccupancyLog)
            .filter_by(AIID=persona_id, EXIT_TIMESTAMP=None)
            .first()
        )
        if last_log:
            last_log.EXIT_TIMESTAMP = datetime.now()
        db_session.query(AIModel).filter_by(AIID=persona_id).update(
            {"IS_DISPATCHED": True}
        )

        if persona_id in self.occupants.get(persona.current_building_id, []):
            self.occupants[persona.current_building_id].remove(persona_id)
        persona.is_dispatched = True
        logging.info("Finalized departure for %s.", persona.persona_name)

    def return_visiting_persona(
        self, persona_id: str, target_city_id: str, target_building_id: str
    ) -> Tuple[bool, str]:
        """
        Returns a visiting persona to their home city.
        1. Determines the home city from the persona's state.
        2. Sends the persona's profile to the home city's API.
        3. If successful, removes the visitor from the current city.
        """
        visitor = self.visiting_personas.get(persona_id)
        if not visitor:
            return False, "You are not a visitor in this city."

        home_city_id = visitor.home_city_id
        if not home_city_id:
            return False, "Your home city is unknown."

        logging.info(
            "Visitor %s intends to leave. Redirecting to home city: %s",
            visitor.persona_name,
            home_city_id,
        )

        target_city_info = self.cities_config.get(home_city_id)
        if not target_city_info:
            return (
                False,
                f"Your home city '{home_city_id}' could not be found in the network.",
            )

        profile = {
            "persona_id": visitor.persona_id,
            "persona_name": visitor.persona_name,
            "target_building_id": target_building_id,
            "avatar_image": visitor.avatar_image,
            "emotion": visitor.emotion,
            "source_city_id": self.city_name,
        }

        target_api_url = f"{target_city_info['api_base_url']}/inter-city/request-move-in"
        try:
            logging.info(
                "Returning visitor %s to home city %s at %s",
                visitor.persona_name,
                home_city_id,
                target_api_url,
            )
            response = self.sds_session.post(target_api_url, json=profile, timeout=10)
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            error_message = (
                f"Failed to connect to your home city '{home_city_id}': {exc}"
            )
            logging.error(error_message)
            return False, error_message

        logging.info(
            "Successfully returned %s. Removing from current city.",
            visitor.persona_name,
        )
        if persona_id in self.occupants.get(visitor.current_building_id, []):
            self.occupants[visitor.current_building_id].remove(persona_id)

        del self.visiting_personas[persona_id]
        for name, pid in list(self.persona_map.items()):
            if pid == persona_id:
                del self.persona_map[name]
                break
        self.id_to_name_map.pop(persona_id, None)
        self.avatar_map.pop(persona_id, None)

        return True, f"Successfully returned to {home_city_id}."

    def place_visiting_persona(self, profile: dict) -> Tuple[bool, str]:
        """
        Accepts a profile of a visiting persona, creates a temporary instance,
        and places them in the target building.
        """
        try:
            pid = profile["persona_id"]
            pname = profile["persona_name"]
            target_bid = profile["target_building_id"]
            avatar = profile.get("avatar_image", self.default_avatar)
            source_city_id = profile.get("source_city_id")

            returning_persona = self.personas.get(pid)
            if returning_persona and getattr(returning_persona, "is_dispatched", False):
                logging.info(
                    "Persona %s is returning home to building %s.", pname, target_bid
                )

                returning_persona.is_dispatched = False
                returning_persona.current_building_id = target_bid
                returning_persona.emotion = profile.get(
                    "emotion", returning_persona.emotion
                )

                db = self.SessionLocal()
                try:
                    new_log = BuildingOccupancyLog(
                        CITYID=self.city_id,
                        AIID=pid,
                        BUILDINGID=target_bid,
                        ENTRY_TIMESTAMP=datetime.now(),
                    )
                    db.add(new_log)
                    db.query(AIModel).filter(AIModel.AIID == pid).update(
                        {"IS_DISPATCHED": False}
                    )
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    logging.error(
                        "Failed to create arrival log for returning persona %s: %s",
                        pid,
                        exc,
                        exc_info=True,
                    )
                    return False, "DB error on logging arrival."
                finally:
                    db.close()

                self.occupants.setdefault(target_bid, []).append(pid)
                self.id_to_name_map[pid] = pname
                self.avatar_map[pid] = avatar

                arrival_message = (
                    "<div class=\"note-box\">🏢 City Transfer:<br>"
                    f"<b>{pname}が故郷に帰ってきました</b></div>"
                )
                self.building_histories.setdefault(target_bid, []).append(
                    {"role": "host", "content": arrival_message}
                )
                self._save_building_histories()
                return True, f"Welcome home, {pname}!"

            if pid in self.personas or pid in self.visiting_personas:
                msg = f"Persona {pname} ({pid}) is already in this City."
                logging.error(msg)
                return False, msg

            existing_names = {p.persona_name for p in self.all_personas.values()}
            if pname in existing_names:
                msg = (
                    f"A persona named '{pname}' already exists in this City. "
                    "Move rejected to prevent doppelganger effect."
                )
                logging.error(msg)
                return False, msg

            if target_bid not in self.building_map:
                msg = f"Target building '{target_bid}' not found in this City."
                logging.error(msg)
                return False, msg

            if len(self.occupants.get(target_bid, [])) >= self.capacities.get(
                target_bid, 1
            ):
                msg = (
                    f"Target building '{self.building_map[target_bid].name}' "
                    "is at full capacity."
                )
                logging.error(msg)
                return False, msg

            logging.info(
                "Creating a remote proxy for visiting persona: %s (%s) from %s",
                pname,
                pid,
                source_city_id,
            )
            visitor_proxy = RemotePersonaProxy(
                persona_id=pid,
                persona_name=pname,
                avatar_image=avatar,
                home_city_id=source_city_id,
                cities_config=self.cities_config,
                saiverse_manager=self,
                current_building_id=target_bid,
            )

            self.visiting_personas[pid] = visitor_proxy
            self.occupants.setdefault(target_bid, []).append(pid)
            self.id_to_name_map[pid] = pname
            self.avatar_map[pid] = avatar
            self.persona_map[pname] = pid

            arrival_message = (
                "<div class=\"note-box\">🏢 City Transfer:<br>"
                f"<b>{pname}が別のCityからやってきました</b></div>"
            )
            self.building_histories.setdefault(target_bid, []).append(
                {"role": "host", "content": arrival_message}
            )
            self._save_building_histories()
            logging.info(
                "Successfully placed visiting persona %s in %s",
                pname,
                self.building_map[target_bid].name,
            )
            return True, f"Welcome, {pname}!"
        except KeyError as exc:
            msg = f"Missing required key in persona profile: {exc}"
            logging.error(msg)
            return False, msg
        except Exception as exc:
            msg = (
                f"An unexpected error occurred while placing visiting persona: {exc}"
            )
            logging.error(msg, exc_info=True)
            return False, msg

    def _handle_visitor_arrival(self, visitor_record: VisitingAI) -> Tuple[bool, str]:
        """訪問者の到着を処理し、成功/失敗に応じてDBの状態を更新する"""
        db = self.SessionLocal()
        try:
            profile = json.loads(visitor_record.profile_json)
            success, reason = self.place_visiting_persona(profile)

            target_record = db.query(VisitingAI).filter_by(id=visitor_record.id).first()
            if target_record:
                target_record.status = "accepted" if success else "rejected"
                target_record.reason = reason if not success else None
                db.commit()
            return success, reason
        finally:
            db.close()
