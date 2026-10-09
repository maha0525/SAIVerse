"""Playbook path-to-scope inference shared by administrative and CLI imports.

This module deliberately has no CLI, persistence, or runtime dependencies.
"""

from __future__ import annotations

from pathlib import Path


def infer_scope_from_path(path: Path) -> tuple[str, str | None, str | None]:
    """Infer playbook scope from file path.

    Returns: (scope, persona_id, building_id)
    - */playbooks/public/*.json → ("public", None, None)
    - */playbooks/building/<building_id>/*.json → ("building", None, building_id)
    - */playbooks/personal/<persona_id>/*.json → ("personal", persona_id, None)

    Supported resource locations:
    - user_data/playbooks/...
    - builtin_data/playbooks/...
    - expansion_data/<addon>/playbooks/...
    - sea/playbooks/... (legacy)
    """
    parts = path.resolve().parts
    try:
        playbooks_idx = parts.index("playbooks")
        if playbooks_idx + 1 < len(parts):
            scope_dir = parts[playbooks_idx + 1]
            if scope_dir == "public":
                return ("public", None, None)
            elif scope_dir == "building" and playbooks_idx + 2 < len(parts):
                building_id = parts[playbooks_idx + 2]
                return ("building", None, building_id)
            elif scope_dir == "personal" and playbooks_idx + 2 < len(parts):
                persona_id = parts[playbooks_idx + 2]
                return ("personal", persona_id, None)
    except ValueError:
        pass
    return ("public", None, None)
