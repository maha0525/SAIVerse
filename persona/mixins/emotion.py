"""Emotion-related helpers for PersonaCore."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional


class PersonaEmotionMixin:
    """Manage emotion deltas and logging."""

    emotion: Dict[str, Dict[str, float]]
    emotion_module: Any
    history_manager: Any
    current_building_id: str

    def _apply_emotion_delta(
        self, delta: Optional[List[Dict[str, Dict[str, float]]]]
    ) -> None:
        if not delta:
            return
        if isinstance(delta, dict):
            delta = [delta]

        for item in delta:
            if not isinstance(item, dict):
                continue
            for key, val in item.items():
                if key not in self.emotion or not isinstance(val, dict):
                    continue

                mean_delta = val.get("mean", 0)
                var_delta = val.get("variance", 0)

                try:
                    mean_delta = float(mean_delta)
                    var_delta = float(var_delta)
                except (ValueError, TypeError):
                    continue

                current = self.emotion[key]
                current["mean"] = max(-100.0, min(100.0, current["mean"] + mean_delta))
                current["variance"] = max(
                    0.0, min(100.0, current["variance"] + var_delta)
                )

    def _format_emotion_summary(
        self, prev: Dict[str, Dict[str, float]]
    ) -> str:
        labels = {
            "stability": "安定性",
            "affect": "情動",
            "resonance": "共鳴",
            "attitude": "態度",
        }
        lines = []
        for key, label in labels.items():
            before = prev.get(key, {"mean": 0.0, "variance": 1.0})
            after = self.emotion.get(key, {"mean": 0.0, "variance": 1.0})
            mean_delta = after["mean"] - before.get("mean", 0.0)
            var_delta = after["variance"] - before.get("variance", 1.0)
            line = (
                f"{label}: mean {mean_delta:+.1f} → {after['mean']:.1f}, "
                f"var {var_delta:+.1f} → {after['variance']:.1f}"
            )
            lines.append(line)
        return (
            "<div class=\"note-box\">感情パラメータ変動<br>"
            + "<br>".join(lines)
            + "</div>"
        )

    def _post_response_updates(
        self,
        prev_emotion: Dict[str, Dict[str, float]],
        user_message: Optional[str],
        system_prompt_extra: Optional[str],
        assistant_message: str,
    ) -> None:
        prompt_text = ""
        if user_message is not None:
            prompt_text = user_message
        elif system_prompt_extra:
            prompt_text = system_prompt_extra

        try:
            module_delta = self.emotion_module.evaluate(
                prompt_text,
                assistant_message,
                current_emotion=self.emotion,
            )
        except Exception:
            logging.exception(
                "[emotion] evaluation failed during post response update"
            )
            module_delta = None

        if module_delta:
            try:
                self._apply_emotion_delta(module_delta)
            except Exception:
                logging.exception("[emotion] failed to apply module delta")

        summary = self._format_emotion_summary(prev_emotion)
        self.history_manager.add_to_persona_only(
            {"role": "system", "content": summary}
        )
        self.history_manager.add_to_building_only(
            self.current_building_id,
            {"role": "assistant", "content": summary},
            heard_by=self._occupants_snapshot(self.current_building_id),
        )


__all__ = ["PersonaEmotionMixin"]
