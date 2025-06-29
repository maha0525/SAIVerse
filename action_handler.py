import json
import logging
import re
from typing import List, Dict, Optional, Tuple

class ActionHandler:
    def __init__(self, action_priority: Dict[str, int]):
        self.action_priority = action_priority

    def parse_response(self, content: str) -> Tuple[str, List[Dict[str, object]]]:
        """Extracts action blocks from the AI's response."""
        pattern = re.compile(r"::act\s*(.*?)\s*::end", re.DOTALL)
        actions: List[Dict[str, object]] = []
        for match in pattern.finditer(content):
            snippet = match.group(1).strip()
            try:
                data = json.loads(snippet)
                if isinstance(data, dict):
                    actions.append(data)
                elif isinstance(data, list):
                    actions.extend(data)
            except json.JSONDecodeError:
                logging.warning("Failed to parse act section: %s", snippet)
        say = pattern.sub("", content).strip()
        return say, actions

    def execute_actions(
        self, actions: List[Dict[str, object]]
    ) -> Tuple[Optional[str], Optional[str], Optional[List[Dict[str, int]]]]:
        """Executes sorted actions and returns the results."""
        sorted_actions = sorted(
            actions,
            key=lambda a: self.action_priority.get(str(a.get("action", "")), 100),
        )
        next_id, think, delta = None, None, None
        for act in sorted_actions:
            action = act.get("action")
            if action == "think":
                think = act.get("inner_words")
            elif action == "emotion_shift":
                delta = act.get("delta")
            elif action == "move":
                next_id = act.get("target")
        return next_id, think, delta
