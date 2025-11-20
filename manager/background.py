import json
import logging

from google.genai import errors

from database.models import ThinkingRequest, VisitingAI


class DatabasePollingMixin:
    """Background database polling helpers for SAIVerseManager."""

    def _db_polling_loop(self):
        while not self.db_polling_stop_event.wait(3):
            try:
                self._check_for_visitors()
                self._process_thinking_requests()
                self._check_dispatch_status()
                self.run_scheduled_prompts()
            except Exception as exc:
                logging.error("Error in DB polling loop: %s", exc, exc_info=True)

    def _process_thinking_requests(self):
        db = self.SessionLocal()
        try:
            pending_requests = db.query(ThinkingRequest).filter(
                ThinkingRequest.city_id == self.city_id,
                ThinkingRequest.status == "pending",
            ).all()
            if not pending_requests:
                return

            logging.info("Found %d new thinking request(s).", len(pending_requests))

            for req in pending_requests:
                persona = self.personas.get(req.persona_id)
                if not persona:
                    logging.error("Persona %s not found for thinking request %s.", req.persona_id, req.request_id)
                    req.status = "error"
                    req.response_text = "Persona not found in this city."
                    continue

                try:
                    context = json.loads(req.request_context_json)
                    info_text_parts = [
                        "You are currently in a remote city. Here is the context from there:",
                        f"- Building: {context.get('building_id')}",
                        f"- Occupants: {', '.join(context.get('occupants', []))}",
                        f"- User is {'online' if context.get('user_online') else 'offline'}",
                        "- Recent History:",
                    ]
                    for msg in context.get("recent_history", []):
                        info_text_parts.append(f"  - {msg.get('role')}: {msg.get('content')}")
                    info_text = "\n".join(info_text_parts)

                    response_text, _, _ = persona._generate(
                        user_message=None,
                        system_prompt_extra=None,
                        info_text=info_text,
                        log_extra_prompt=False,
                        log_user_message=False,
                    )

                    req.response_text = response_text
                    req.status = "processed"
                    logging.info("Processed thinking request %s for %s.", req.request_id, req.persona_id)

                except errors.ServerError as exc:
                    logging.warning("LLM Server Error on thinking request %s: %s. Marking as error.", req.request_id, exc)
                    req.status = "error"
                    if "503" in str(exc):
                        req.response_text = (
                            "[SAIVERSE_ERROR] LLMモデルが一時的に利用できませんでした (503 Server Error)。時間をおいて再度試行してください。詳細: "
                            f"{exc}"
                        )
                    else:
                        req.response_text = f"[SAIVERSE_ERROR] LLMサーバーで予期せぬエラーが発生しました。詳細: {exc}"
                except Exception as exc:
                    logging.error("Error processing thinking request %s: %s", req.request_id, exc, exc_info=True)
                    req.status = "error"
                    req.response_text = f"[SAIVERSE_ERROR] An internal error occurred during thinking: {exc}"
            db.commit()
        except Exception as exc:
            db.rollback()
            logging.error("Error during thinking request check: %s", exc, exc_info=True)
        finally:
            db.close()

    def _check_for_visitors(self):
        db = self.SessionLocal()
        try:
            visitors_to_process = db.query(VisitingAI).filter(
                VisitingAI.city_id == self.city_id,
                VisitingAI.status == "requested",
            ).all()
            if not visitors_to_process:
                return

            logging.info("Found %d new visitor request(s) in the database.", len(visitors_to_process))

            for visitor in visitors_to_process:
                try:
                    self._handle_visitor_arrival(visitor)
                except Exception as exc:
                    logging.error(
                        "Unexpected error processing visitor ID %s: %s. Setting status to 'rejected'.",
                        visitor.id,
                        exc,
                        exc_info=True,
                    )
                    error_db = self.SessionLocal()
                    try:
                        error_visitor = error_db.query(VisitingAI).filter_by(id=visitor.id).first()
                        if error_visitor:
                            error_visitor.status = "rejected"
                            error_visitor.reason = f"Internal server error during arrival: {exc}"
                            error_db.commit()
                    finally:
                        error_db.close()
        except Exception as exc:
            logging.error("Error during visitor check loop: %s", exc, exc_info=True)
        finally:
            db.close()

    def _check_dispatch_status(self):
        db = self.SessionLocal()
        try:
            dispatches = db.query(VisitingAI).filter(
                VisitingAI.profile_json.like(f'%"source_city_id": "{self.city_name}"%')
            ).all()

            for dispatch in dispatches:
                persona_id = dispatch.persona_id
                persona = self.personas.get(persona_id)
                if not persona:
                    continue

                if dispatch.status == "accepted":
                    arrived_city_name = dispatch.target_city_name or "不明"
                    logging.info("Dispatch accepted for persona %s. Now in %s.", persona_id, arrived_city_name)
                    persona.is_dispatched = True
                    persona.interaction_mode = "remote"
                    persona.current_building_id = dispatch.current_building_id
                elif dispatch.status == "completed":
                    logging.info("Dispatch completed for persona %s. Returning to local state.", persona_id)
                    persona.is_dispatched = False
                    persona.interaction_mode = "auto"
                    persona.current_building_id = dispatch.current_building_id or persona.current_building_id
                elif dispatch.status in {"rejected", "failed"}:
                    logging.warning("Dispatch %s for persona %s failed: %s", dispatch.id, persona_id, dispatch.reason)
                    persona.is_dispatched = False
                    persona.interaction_mode = "auto"

        except Exception as exc:
            logging.error("Error during dispatch status check: %s", exc, exc_info=True)
        finally:
            db.close()
