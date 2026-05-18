"""SpellListSection — 利用可能なスペル一覧の head section。

`sea/runtime_context.py` 旧 system prompt の 6. Spell section を移植したもの。
capture 時点で per-persona の visibility / availability_check / addon manifest を
解決して snapshot に焼き込み、以降は render が snapshot のみから描画する
(= live SPELL_TOOL_SCHEMAS や addon_loader を render 時に参照しない)。

詳細: docs/intent/cached_head_architecture.md §5.3 / persona_cognition 不変条件 7
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Optional

from sea.head_pipeline.types import (
    EventType,
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpellEntry:
    name: str
    display_name: str
    description: str
    parameters_json: str            # JSON Schema を文字列化 (frozen + hashable に保つ)
    addon_key: Optional[str]        # None = builtin
    visible: bool                   # True なら一覧に表示、False なら hidden_count に寄与


@dataclass(frozen=True)
class AddonManifest:
    addon_key: str
    display_name: str
    description: str


@dataclass(frozen=True)
class SpellListSnapshot:
    enabled: bool
    entries: tuple[SpellEntry, ...]                  # visible / hidden 含む
    addon_manifests: tuple[AddonManifest, ...]       # addon_key -> manifest (visible >0 or hidden >0 のみ)


class SpellListSection:
    """Spell 一覧の section 実装。

    capture が live state にアクセスする経路:
      - SPELL_TOOL_SCHEMAS (global registry)
      - tools.mcp_client.get_mcp_manager().is_tool_available_for_persona
      - ToolSchema.availability_check
      - expansion_data/<addon_key>/addon.json
      - runtime._is_spell_enabled_for_persona (= AI.SPELL_ENABLED DB)

    これらは render では一切触らない (= cache 安定性保証)。
    """

    name = "spell_list"
    order = 600
    refresh_on_events = frozenset({EventType.ADDON_LOADED, EventType.ADDON_UNLOADED})

    # ---- capture ----

    def capture(self, ctx: LineHeadInput) -> SpellListSnapshot:
        from tools import SPELL_TOOL_SCHEMAS

        enabled = self._resolve_enabled(ctx)
        if not enabled:
            return SpellListSnapshot(enabled=False, entries=(), addon_manifests=())

        try:
            from tools.mcp_client import get_mcp_manager
            mcp_mgr = get_mcp_manager()
        except Exception:
            mcp_mgr = None

        persona_id = ctx.persona_id
        building_id_for_filter = ctx.current_building_id

        entries: list[SpellEntry] = []
        addon_keys_seen: set[str] = set()
        for sname, sschema in SPELL_TOOL_SCHEMAS.items():
            # MCP-backed availability (= per-persona placeholder 解決可否 +
            # Phase 4 で復活した Building 単位 visibility)。snapshot は Metabolism /
            # flush_diffs タイミングで再キャプチャされるので、head は Metabolism 時
            # の current_building 基準で固定、移動時の diff_to_notifications で
            # 「使えるようになった/使えなくなった」を末尾通知する形になる。
            if mcp_mgr is not None and not mcp_mgr.is_tool_available_for_persona(
                sname, persona_id, building_id_for_filter,
            ):
                continue
            availability_check = getattr(sschema, "availability_check", None)
            if availability_check is not None:
                try:
                    if not availability_check(persona_id):
                        continue
                except Exception:
                    LOGGER.warning(
                        "spell_list: availability_check raised for %s persona=%s",
                        sname, persona_id, exc_info=True,
                    )
                    continue

            addon_key = getattr(sschema, "addon_name", None)
            if not addon_key and "__" in sname:
                addon_key = sname.split("__", 1)[0]
            if addon_key:
                addon_keys_seen.add(addon_key)

            parameters_json = json.dumps(
                sschema.parameters or {}, ensure_ascii=False, sort_keys=True,
            )
            entries.append(SpellEntry(
                name=sname,
                display_name=sschema.spell_display_name or "",
                description=sschema.description or "",
                parameters_json=parameters_json,
                addon_key=addon_key,
                visible=bool(getattr(sschema, "spell_visible", True)),
            ))

        entries.sort(key=lambda e: (e.addon_key or "", e.name))
        manifests = self._collect_manifests(addon_keys_seen)
        return SpellListSnapshot(
            enabled=True,
            entries=tuple(entries),
            addon_manifests=tuple(manifests),
        )

    # ---- render ----

    def render(self, snapshot: SpellListSnapshot) -> Optional[RenderedSection]:
        if snapshot is None:
            return None
        if not snapshot.enabled:
            return RenderedSection(text=(
                "## スペル\nスペルは現在使用できません。/spell コマンドを使用しないでください。"
            ))

        lines: list[str] = [
            "## スペル",
            "発言中にスペルを唱えると、情報を取得できます。スペルを唱えると、結果が返ってくるのでそれを踏まえて発言を続けてください。",
            "構造化出力（JSON応答）内ではスペルは使用できません。",
            "",
            "### 使い方",
            "/spell name='ツール名' args={'引数名': '値'}",
            "",
            "### 複数スペルの同時使用",
            "1回の発言に複数の /spell 行を書くと、すべて並列実行されます。結果はまとめて返ってきます。",
            "互いに独立したスペルは同じ発言にまとめて書いてください（LLM呼び出しが節約されます）。",
            "あるスペルの結果を次のスペルの引数に使いたい場合は、別の発言で順番に使用してください。",
            "",
            "### 利用可能なスペル",
        ]

        builtin_visible: list[SpellEntry] = []
        addon_groups: dict[str, dict[str, Any]] = {}  # addon_key -> {"visible": [], "hidden_count": int}
        for entry in snapshot.entries:
            if entry.addon_key:
                group = addon_groups.setdefault(
                    entry.addon_key, {"visible": [], "hidden_count": 0},
                )
                if entry.visible:
                    group["visible"].append(entry)
                else:
                    group["hidden_count"] += 1
            else:
                if entry.visible:
                    builtin_visible.append(entry)

        for entry in builtin_visible:
            self._render_entry(lines, entry)

        manifests_by_key = {m.addon_key: m for m in snapshot.addon_manifests}
        for addon_key, group in addon_groups.items():
            if not group["visible"] and group["hidden_count"] == 0:
                continue
            manifest = manifests_by_key.get(addon_key)
            display = (manifest.display_name if manifest and manifest.display_name else addon_key)
            desc = manifest.description if manifest else ""
            hidden = group["hidden_count"]

            header = f"**{display}**"
            if display != addon_key:
                header += f" (`{addon_key}`)"
            if desc:
                header += f" — {desc}"
            if hidden > 0:
                header += f"（追加スペル{hidden}個あり、`addon_spell_help(addon=\"{addon_key}\")`で確認）"
            lines.append("")
            lines.append(header)
            for entry in group["visible"]:
                self._render_entry(lines, entry)

        return RenderedSection(text="\n".join(lines))

    # ---- diff ----

    def diff_to_notifications(
        self, old: Optional[SpellListSnapshot], new: Optional[SpellListSnapshot],
    ) -> list[NotificationLabel]:
        if old is None or new is None:
            return []

        labels: list[NotificationLabel] = []
        if old.enabled != new.enabled:
            if new.enabled:
                labels.append(NotificationLabel(
                    kind="spell_system_enabled",
                    label="スペル機構が有効になりました",
                ))
            else:
                labels.append(NotificationLabel(
                    kind="spell_system_disabled",
                    label="スペル機構が無効になりました",
                ))
            return labels  # 切替時はリスト diff より先にこれを通知

        old_visible = {e.name: e for e in old.entries if e.visible}
        new_visible = {e.name: e for e in new.entries if e.visible}
        for name in sorted(new_visible.keys() - old_visible.keys()):
            entry = new_visible[name]
            display = entry.display_name or entry.name
            labels.append(NotificationLabel(
                kind="spell_added",
                label=f"スペル {display} ({name}) が使えるようになりました",
            ))
        for name in sorted(old_visible.keys() - new_visible.keys()):
            entry = old_visible[name]
            display = entry.display_name or entry.name
            labels.append(NotificationLabel(
                kind="spell_removed",
                label=f"スペル {display} ({name}) が使えなくなりました",
            ))
        return labels

    # ---- serialize / deserialize ----

    def serialize_snapshot(self, snapshot: SpellListSnapshot) -> str:
        payload = {
            "enabled": snapshot.enabled,
            "entries": [asdict(e) for e in snapshot.entries],
            "addon_manifests": [asdict(m) for m in snapshot.addon_manifests],
        }
        return json.dumps(payload, ensure_ascii=False)

    def deserialize_snapshot(self, data: str) -> SpellListSnapshot:
        payload = json.loads(data)
        entries = tuple(SpellEntry(**e) for e in payload.get("entries", []))
        manifests = tuple(
            AddonManifest(**m) for m in payload.get("addon_manifests", [])
        )
        return SpellListSnapshot(
            enabled=bool(payload.get("enabled", False)),
            entries=entries,
            addon_manifests=manifests,
        )

    # ---- 内部ヘルパー ----

    def _resolve_enabled(self, ctx: LineHeadInput) -> bool:
        """AI.SPELL_ENABLED の解決。manager 経由で DB を引く。"""
        manager = ctx.manager
        persona_id = ctx.persona_id
        if not manager or not persona_id:
            return False
        session_factory = getattr(manager, "SessionLocal", None)
        if not session_factory:
            return False
        db = session_factory()
        try:
            from database.models import AI as AIModel
            ai = db.query(AIModel).filter_by(AIID=persona_id).first()
            return bool(ai.SPELL_ENABLED) if ai else False
        except Exception:
            LOGGER.warning(
                "spell_list: failed to resolve SPELL_ENABLED for persona=%s",
                persona_id, exc_info=True,
            )
            return False
        finally:
            db.close()

    def _collect_manifests(self, addon_keys: set[str]) -> list[AddonManifest]:
        """expansion_data/<addon_key>/addon.json から display_name / description を集める。"""
        if not addon_keys:
            return []
        try:
            from saiverse.data_paths import EXPANSION_DATA_DIR as _EXP_DIR
        except Exception:
            return []

        manifests: list[AddonManifest] = []
        for addon_key in sorted(addon_keys):
            manifest_path = _EXP_DIR / addon_key / "addon.json"
            display_name = ""
            description = ""
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    display_name = raw.get("display_name") or ""
                    description = raw.get("spell_description") or raw.get("description") or ""
            except FileNotFoundError:
                pass
            except Exception:
                LOGGER.debug(
                    "spell_list: failed to load manifest for addon=%s",
                    addon_key, exc_info=True,
                )
            manifests.append(AddonManifest(
                addon_key=addon_key,
                display_name=display_name,
                description=description,
            ))
        return manifests

    def _render_entry(self, lines: list[str], entry: SpellEntry) -> None:
        display = entry.display_name or entry.name
        lines.append(f"- **{entry.name}** ({display}): {entry.description}")
        try:
            parameters = json.loads(entry.parameters_json) if entry.parameters_json else {}
        except json.JSONDecodeError:
            parameters = {}
        props = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
        required_list = parameters.get("required", []) if isinstance(parameters, dict) else []
        for pname, pdef in props.items():
            req_mark = "必須" if pname in required_list else "省略可"
            if not isinstance(pdef, dict):
                continue
            lines.append(
                f"  - {pname} ({pdef.get('type', '?')}, {req_mark}): {pdef.get('description', '')}"
            )
