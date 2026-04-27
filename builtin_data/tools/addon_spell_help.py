"""addon_spell_help - アドオンが提供する非表示スペルの一覧を返す。

システムプロンプトには最小限のスペルのみ掲載されている。
このスペルを呼ぶことで、任意のアドオンが提供する追加スペルのスキーマを確認できる。

引数 ``addon`` はファジーマッチに対応している (``tools.fuzzy.resolve_fuzzy``)。
ペルソナが ``saiverse-elyth-addon__elyth`` のような周辺識別子を渡しても、
最も近いアドオン名 (``saiverse-elyth-addon``) にスナップして表示する。
情報取得系スペルなのでファジー誤マッチでも害はない。
"""
from tools.core import ToolSchema


def schema() -> ToolSchema:
    return ToolSchema(
        name="addon_spell_help",
        description=(
            "アドオンが提供する追加スペルの一覧とその使い方を返します。"
            "投稿・検索など詳細な操作を行う前に呼んでください。"
            "addon引数でアドオン名を絞り込めます（省略時は全アドオン、"
            "近い名前を渡せばファジーマッチします）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "addon": {
                    "type": "string",
                    "description": (
                        "絞り込むアドオン名（省略時は全アドオンを表示）。"
                        "正確な名前が分からなくても近い文字列でマッチします。"
                    ),
                },
            },
        },
        result_type="string",
        spell=True,
        spell_display_name="アドオンスペル一覧",
        spell_visible=True,
    )


def addon_spell_help(addon: str = "") -> str:
    from tools import SPELL_TOOL_SCHEMAS
    from tools.context import get_active_persona_id
    from tools.fuzzy import resolve_fuzzy

    try:
        from tools.mcp_client import get_mcp_manager
        mcp_mgr = get_mcp_manager()
    except Exception:
        mcp_mgr = None

    persona_id = get_active_persona_id()

    # 全スペルから addon_key 候補を抽出（表示・非表示両方）。
    # ファジーマッチの候補一覧と「利用可能なアドオン」リストの両方に使う。
    all_addon_keys: set[str] = set()
    for sname in SPELL_TOOL_SCHEMAS:
        if "__" in sname:
            all_addon_keys.add(sname.split("__", 1)[0])

    # addon が指定されていて完全一致しないなら、ファジー補完を試みる。
    fuzzy_hint: str = ""
    if addon and addon not in all_addon_keys:
        resolved, was_exact, original = resolve_fuzzy(
            addon, all_addon_keys, threshold=0.5
        )
        if not was_exact and original is not None:
            fuzzy_hint = (
                f"（'{original}' は厳密には存在しないアドオン名のため、"
                f"最も近い '{resolved}' を表示します。"
                f"次回からは '{resolved}' を直接指定してください）"
            )
            addon = resolved

    # 非表示スペルをアドオン別に収集
    by_addon: dict[str, list[tuple[str, ToolSchema]]] = {}
    for name, s in SPELL_TOOL_SCHEMAS.items():
        if getattr(s, "spell_visible", True):
            continue
        if mcp_mgr is not None and not mcp_mgr.is_tool_available_for_persona(name, persona_id):
            continue

        # ツール名の先頭セグメントをアドオン名とみなす（例: addon__server__tool → addon）
        addon_key = name.split("__", 1)[0] if "__" in name else ""
        if not addon_key:
            continue
        if addon and addon_key != addon:
            continue

        by_addon.setdefault(addon_key, []).append((name, s))

    if not by_addon:
        if addon:
            available = sorted(all_addon_keys)
            if available:
                return (
                    f"アドオン '{addon}' に使用可能な追加スペルはありません。"
                    f" 利用可能なアドオン: {', '.join(available)}"
                )
            return f"アドオン '{addon}' に使用可能な追加スペルはありません。"
        return "追加スペルを持つアドオンは現在ありません。"

    lines: list[str] = []
    if fuzzy_hint:
        lines.append(fuzzy_hint)
        lines.append("")

    multi = len(by_addon) > 1
    for addon_key, spells in by_addon.items():
        if multi:
            lines.append(f"### {addon_key}")
        for name, s in spells:
            display = s.spell_display_name or name
            lines.append(f"- **{name}** ({display}): {s.description}")
            props = s.parameters.get("properties", {})
            required_list = s.parameters.get("required", [])
            for pname, pdef in props.items():
                req_mark = "必須" if pname in required_list else "省略可"
                lines.append(f"  - {pname} ({pdef.get('type', '?')}, {req_mark}): {pdef.get('description', '')}")

    return "\n".join(lines)
