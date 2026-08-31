from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any, Dict, Optional

from .playbook_models import RESERVED_STATE_PREFIX
from .runtime_utils import _format


LOGGER = logging.getLogger(__name__)


def effective_auto_mode(state: Dict[str, Any], fallback: bool = False) -> bool:
    """「この Pulse の応答ループにユーザーが居ないか」を state から読む。

    ノードを組み立てる factory は compile 時の生の ``auto_mode`` 引数を capture
    できるが、実効値は state の ``_auto_mode`` 側にある (親 Pulse からの単調 OR
    継承が入るのはそちらだけ)。真実の置き場を一つにするため、ノードの実行時は
    必ずこちらを読む。

    ``fallback`` は state に ``_auto_mode`` が無いとき (LangGraph を経ない直接
    呼び出し・テスト) の値で、呼び出し側が capture した引数を渡す。
    """
    value = state.get("_auto_mode")
    if value is None:
        return bool(fallback)
    return bool(value)


def playbook_write_key(name: Any, where: str) -> Optional[str]:
    """Playbook 宣言由来の名前を state の書き込み先として検査する。

    ``_`` で始まる名前は runtime のシステム変数 (``_messages`` /
    ``_pulse_context`` / ``_spell_enabled`` / ``_cancellation_token`` …) の
    領域で、Playbook 側の宣言から書けてはならない。ロード時の
    ``PlaybookSchema`` validator が fail-closed で弾くが、不変条件を「全ての
    ロード経路が validator を通ること」だけに預けないため、値が実際に効く
    書き込み点でも同じ判定を通す。

    ``{key}.{sub}`` のような派生キーを組む場合も、基点の名前をここで通して
    から組み立てること (基点が安全なら派生も安全になる)。

    Returns:
        書いてよい名前。禁止なら ``None`` (WARNING を残す)。
    """
    if not isinstance(name, str) or not name:
        return None
    if name.startswith(RESERVED_STATE_PREFIX):
        LOGGER.warning(
            "[sea][state] Refusing playbook write to reserved '_' namespace: "
            "%s = '%s'", where, name,
        )
        return None
    return name


def set_playbook_var(state: Dict[str, Any], name: Any, value: Any, where: str) -> bool:
    """Playbook が宣言した名前で state へ書く唯一の口 (判定は
    :func:`playbook_write_key` 一箇所)。

    Returns:
        書いたら True、拒否したら False。
    """
    key = playbook_write_key(name, where)
    if key is None:
        return False
    state[key] = value
    return True


def process_structured_output(node_def: Any, text: Any, state: Dict[str, Any]) -> bool:
    schema = getattr(node_def, "response_schema", None)
    if not schema:
        return False

    node_id = getattr(node_def, "id", "?")
    LOGGER.debug("[sea] _process_structured_output: node=%s, text type=%s, len=%s, repr=%r", node_id, type(text).__name__, len(text) if isinstance(text, str) else "(not str)", text if isinstance(text, str) and len(text) < 200 else "(truncated)" if isinstance(text, str) else text)

    parsed: Optional[Dict[str, Any]]
    if isinstance(text, dict):
        parsed = text
        LOGGER.debug(
            "[sea] _process_structured_output: text is already a dict, keys=%s",
            list(parsed.keys()) if isinstance(parsed, dict) else "(not a dict)",
        )
    else:
        parsed = extract_structured_json(text)
        LOGGER.debug("[sea] _process_structured_output: extracted JSON, parsed=%s", parsed is not None)

    if parsed is None:
        LOGGER.warning("[sea] structured output parse failed for node %s", node_id)
        return False

    key = getattr(node_def, "output_key", None) or getattr(node_def, "id", "") or "node"
    LOGGER.debug("[sea] _process_structured_output: storing to state['%s']", key)
    store_structured_result(state, key, parsed)

    output_mapping = getattr(node_def, "output_mapping", None)
    if output_mapping:
        LOGGER.debug("[sea] _process_structured_output: applying output_mapping: %s", output_mapping)
        apply_output_mapping(state, key, output_mapping)

    return True


def apply_output_mapping(state: Dict[str, Any], output_key: str, mapping: Dict[str, str]) -> None:
    output_data = state.get(output_key)
    if output_data is None:
        LOGGER.warning(
            "[sea] output_mapping: output_key %s not found in state (available keys: %s)",
            output_key,
            list(state.keys())[:20],
        )
        return

    LOGGER.debug(
        "[sea] output_mapping: output_data type=%s, keys=%s",
        type(output_data).__name__,
        list(output_data.keys()) if isinstance(output_data, dict) else "(not a dict)",
    )

    for source_path, target_key in mapping.items():
        if source_path.startswith(f"{output_key}."):
            relative_path = source_path[len(output_key) + 1 :]
            value = resolve_nested_value(output_data, relative_path)
        else:
            value = resolve_nested_value(output_data, source_path)

        if value is not None:
            if set_playbook_var(state, target_key, value, where="output_mapping target"):
                LOGGER.debug("[sea] output_mapping: %s -> %s = %s", source_path, target_key, str(value))
        else:
            LOGGER.warning(
                "[sea] output_mapping: failed to resolve %s from %s (keys: %s)",
                source_path,
                output_key,
                list(output_data.keys()) if isinstance(output_data, dict) else "(not a dict)",
            )


def resolve_nested_value(data: Any, path: str) -> Any:
    if path == "":
        return data
    keys = path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and key.isdigit():
            idx = int(key)
            if idx < len(current):
                current = current[idx]
            else:
                return None
        else:
            return None
        if current is None:
            return None
    return current


def store_structured_result(state: Dict[str, Any], key: str, data: Any) -> None:
    # 基点の名前だけ検査すれば、派生する "{key}.{path}" も同じ判定に乗る。
    safe_key = playbook_write_key(key, where="structured output key")
    if safe_key is None:
        return
    state[safe_key] = data
    for path, value in flatten_dict(data).items():
        state[f"{safe_key}.{path}"] = value


def flatten_dict(value: Any, prefix: str = "") -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            new_prefix = f"{prefix}.{k}" if prefix else str(k)
            result.update(flatten_dict(v, new_prefix))
    elif isinstance(value, list):
        if prefix:
            result[prefix] = json.dumps(value, ensure_ascii=False)
        for idx, item in enumerate(value):
            new_prefix = f"{prefix}.{idx}" if prefix else str(idx)
            result.update(flatten_dict(item, new_prefix))
    else:
        result[prefix or "value"] = value
    return result


def resolve_state_value(state: Dict[str, Any], key: str) -> Any:
    if "." not in key:
        return state.get(key, "")

    parts = key.split(".")
    value: Any = state
    for part in parts:
        if value is None:
            break
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list):
            if part.isdigit():
                idx = int(part)
                value = value[idx] if idx < len(value) else None
            else:
                value = None
                break
        else:
            value = None
            break

    if value is not None:
        return value

    if key in state:
        return state[key]

    return ""


def extract_structured_json(text: str) -> Optional[Dict[str, Any]]:
    LOGGER.debug("[sea] extract_structured_json: CALLED with text type=%s, len=%d", type(text).__name__, len(text))
    candidate = text.strip()
    if not candidate:
        LOGGER.debug("[sea] extract_structured_json: candidate is empty after strip")
        return None
    if candidate.startswith("```"):
        for seg in candidate.split("```"):
            seg = seg.strip()
            if seg.startswith("{") and seg.endswith("}"):
                candidate = seg
                break
    if not candidate.startswith("{"):
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if match:
            candidate = match.group(0)
    LOGGER.debug("[sea] extract_structured_json: attempting to parse JSON, candidate (first 200 chars): %s", candidate[:200])
    try:
        parsed = json.loads(candidate)
        LOGGER.debug("[sea] extract_structured_json: json.loads succeeded, type(parsed)=%s", type(parsed).__name__)
    except Exception as e:
        LOGGER.warning("[sea] extract_structured_json: JSON parse failed: %s", e, exc_info=True)
        LOGGER.debug("[sea] extract_structured_json: candidate text (first 500 chars): %s", candidate[:500])
        return None
    if not isinstance(parsed, dict):
        LOGGER.warning("[sea] extract_structured_json: parsed is not a dict, type=%s, value=%s", type(parsed).__name__, str(parsed)[:200])
        return None
    return parsed


def resolve_set_value(value_template: Any, state: Dict[str, Any]) -> Any:
    if isinstance(value_template, (int, float, bool, type(None))):
        return value_template

    if not isinstance(value_template, str):
        return value_template

    if value_template.startswith("="):
        return eval_arithmetic_expression(value_template[1:], state)

    try:
        result = _format(value_template, state)
        if result == value_template and "{" in value_template:
            LOGGER.debug("[sea][set] Template not expanded. Keys in state: %s", list(state.keys()))
        return result
    except Exception as exc:
        LOGGER.warning("[sea][set] _format failed: %s", exc)
        return value_template


def eval_arithmetic_expression(expr: str, state: Dict[str, Any]) -> Any:
    expanded = expr
    placeholder_pattern = re.compile(r"\{(\w+)\}")
    for match in placeholder_pattern.finditer(expr):
        var_name = match.group(1)
        var_value: Any = state.get(var_name, 0)
        try:
            if isinstance(var_value, str):
                var_value = float(var_value) if "." in var_value else int(var_value)
        except (ValueError, TypeError):
            var_value = 0
        expanded = expanded.replace(match.group(0), str(var_value))

    try:
        tree = ast.parse(expanded, mode="eval")
        allowed_node_types = (
            ast.Expression,
            ast.BinOp,
            ast.UnaryOp,
            ast.Constant,
            ast.Num,
            ast.Add,
            ast.Sub,
            ast.Mult,
            ast.Div,
            ast.Mod,
            ast.FloorDiv,
            ast.UAdd,
            ast.USub,
        )
        for node in ast.walk(tree):
            if not isinstance(node, allowed_node_types):
                raise ValueError(f"Unsupported node type: {type(node).__name__}")

        result = eval(compile(tree, "<string>", "eval"))
        if isinstance(result, float) and result.is_integer():
            return int(result)
        return result
    except Exception as exc:
        LOGGER.warning("[sea][set] Failed to evaluate expression '%s': %s", expr, exc)
        return 0
