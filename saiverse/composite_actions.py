"""ユーザー定義複合アクション (Composite Actions)

複数の MCP ツール呼び出しを組み合わせた「動作」を定義し、
ペルソナの spell として動的に登録する仕組み。

設計: docs/intent/user_defined_composite_actions.md
"""
from __future__ import annotations

import ast
import asyncio
import contextvars
import inspect
import json
import logging
import operator as _op
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from saiverse.addon_paths import get_addon_data_dir

LOGGER = logging.getLogger(__name__)

MAX_STEPS = 20
MAX_WAIT_MS = 10_000
MAX_PARAMS = 8
_ACTION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# steps の値に書ける "${param_name}" プレースホルダ (= parameters で宣言した
# パラメータ名の参照)。 文字列全体がこの形のときは「型付き置換」、 式の一部に
# 埋め込まれているときは「式評価 / 文字列補間」 (_resolve_placeholder 参照)。
_PLACEHOLDER_RE = re.compile(r"^\$\{([a-z][a-z0-9_]*)\}$")
# 式・文字列中に複数現れる ${name} を全て拾うための非アンカー版。
_PLACEHOLDER_FIND_RE = re.compile(r"\$\{([a-z][a-z0-9_]*)\}")
_PARAM_TYPES = ("number", "integer", "string", "boolean")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StepDef:
    type: str  # "parallel" | "wait"
    calls: List[ToolCall] = field(default_factory=list)
    # 数値リテラル、 または "${param}" プレースホルダ文字列を取りうる
    duration_ms: Any = 0


@dataclass
class ParamDef:
    """呼び出し時に指定できるアクションのパラメータ定義 (intent doc §A)。"""
    name: str
    type: str = "number"  # number | integer | string | boolean
    default: Any = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"type": self.type}
        if self.default is not None:
            d["default"] = self.default
        if self.minimum is not None:
            d["min"] = self.minimum
        if self.maximum is not None:
            d["max"] = self.maximum
        if self.description:
            d["description"] = self.description
        return d


@dataclass
class ActionDefinition:
    id: str
    display_name: str
    description: str
    steps: List[StepDef] = field(default_factory=list)
    parameters: List[ParamDef] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "steps": [
                {
                    "type": s.type,
                    **({"calls": [{"tool": c.tool, "args": c.args} for c in s.calls]}
                       if s.type == "parallel" else {}),
                    **({"duration_ms": s.duration_ms}
                       if s.type == "wait" else {}),
                }
                for s in self.steps
            ],
        }
        if self.parameters:
            d["parameters"] = {p.name: p.to_dict() for p in self.parameters}
        return d


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_action(
    data: Dict[str, Any],
    allowed_tools: Optional[List[str]] = None,
) -> ActionDefinition:
    action_id = data.get("id", "")
    if not _ACTION_ID_RE.match(action_id):
        raise ValueError(
            f"id は英小文字で始まり英小文字・数字・アンダースコアのみ (1-64文字): {action_id!r}"
        )

    display_name = str(data.get("display_name", "")).strip()
    if not display_name:
        raise ValueError("display_name は必須です")

    description = str(data.get("description", "")).strip()

    # --- parameters (呼び出し時引数の宣言) ---
    raw_params = data.get("parameters") or {}
    if not isinstance(raw_params, dict):
        raise ValueError("parameters はオブジェクト型である必要があります")
    if len(raw_params) > MAX_PARAMS:
        raise ValueError(f"parameters は最大 {MAX_PARAMS} 個です (現在 {len(raw_params)})")
    parameters: List[ParamDef] = []
    param_names: set = set()
    for pname, spec in raw_params.items():
        if not _PARAM_NAME_RE.match(pname):
            raise ValueError(
                f"parameters のキー '{pname}' は英小文字で始まり英小文字・数字・"
                "アンダースコアのみ (1-32文字) です"
            )
        if not isinstance(spec, dict):
            raise ValueError(f"parameters['{pname}'] はオブジェクト型である必要があります")
        ptype = spec.get("type", "number")
        if ptype not in _PARAM_TYPES:
            raise ValueError(
                f"parameters['{pname}'].type は {_PARAM_TYPES} のいずれか (got {ptype!r})"
            )
        parameters.append(ParamDef(
            name=pname,
            type=ptype,
            default=spec.get("default"),
            minimum=spec.get("min", spec.get("minimum")),
            maximum=spec.get("max", spec.get("maximum")),
            description=str(spec.get("description", "")).strip(),
        ))
        param_names.add(pname)

    def _check_placeholder(value: Any, ctx: str) -> None:
        """値に含まれる全 ``${param}`` が宣言済みパラメータを参照しているか検証。

        式 (``50*${speed}``) や文字列補間 (``ch${index}``) も対象にするため、
        まるごと一致だけでなく文字列中の全トークンを走査する。
        """
        if not isinstance(value, str):
            return
        for name in _PLACEHOLDER_FIND_RE.findall(value):
            if name not in param_names:
                raise ValueError(f"{ctx}: 未定義のパラメータ '{name}' を参照しています")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or len(raw_steps) == 0:
        raise ValueError("steps は1個以上必要です")
    if len(raw_steps) > MAX_STEPS:
        raise ValueError(f"steps は最大 {MAX_STEPS} 個です (現在 {len(raw_steps)})")

    steps: List[StepDef] = []
    for i, raw in enumerate(raw_steps):
        step_type = raw.get("type")
        if step_type == "parallel":
            raw_calls = raw.get("calls")
            if not isinstance(raw_calls, list) or len(raw_calls) == 0:
                raise ValueError(f"steps[{i}]: parallel ステップには calls が1個以上必要です")
            calls: List[ToolCall] = []
            for j, rc in enumerate(raw_calls):
                tool_name = rc.get("tool", "")
                if not tool_name:
                    raise ValueError(f"steps[{i}].calls[{j}]: tool は必須です")
                if allowed_tools is not None and tool_name not in allowed_tools:
                    raise ValueError(
                        f"steps[{i}].calls[{j}]: ツール '{tool_name}' はこのアドオンで使用できません"
                    )
                args = rc.get("args", {})
                if not isinstance(args, dict):
                    raise ValueError(f"steps[{i}].calls[{j}]: args は辞書型である必要があります")
                for k, v in args.items():
                    _check_placeholder(v, f"steps[{i}].calls[{j}].args['{k}']")
                calls.append(ToolCall(tool=tool_name, args=args))
            steps.append(StepDef(type="parallel", calls=calls))

        elif step_type == "wait":
            duration = raw.get("duration_ms", 0)
            if isinstance(duration, str):
                # ${param} を含む式 / 文字列であること (実時間はパラメータ由来)。
                tokens = _PLACEHOLDER_FIND_RE.findall(duration)
                if not tokens:
                    raise ValueError(
                        f"steps[{i}]: wait の duration_ms 文字列は ${{param}} を含む"
                        "式である必要があります (例: ${duration_ms} や ${seconds}*1000)"
                    )
                for name in tokens:
                    if name not in param_names:
                        raise ValueError(
                            f"steps[{i}]: duration_ms が未定義のパラメータ '{name}' を参照しています"
                        )
                steps.append(StepDef(type="wait", duration_ms=duration))
            else:
                if not isinstance(duration, (int, float)) or duration <= 0:
                    raise ValueError(f"steps[{i}]: wait の duration_ms は正の数が必要です")
                duration = int(duration)
                if duration > MAX_WAIT_MS:
                    raise ValueError(
                        f"steps[{i}]: wait の duration_ms は最大 {MAX_WAIT_MS}ms です (現在 {duration}ms)"
                    )
                steps.append(StepDef(type="wait", duration_ms=duration))

        else:
            raise ValueError(f"steps[{i}]: type は 'parallel' または 'wait' のみ (got {step_type!r})")

    return ActionDefinition(
        id=action_id,
        display_name=display_name,
        description=description,
        steps=steps,
        parameters=parameters,
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _actions_dir(addon_name: str) -> Path:
    return get_addon_data_dir(addon_name) / "actions"


def _builtin_actions_dir(addon_name: str) -> Optional[Path]:
    """expansion_data 内の builtin_actions ディレクトリを返す (存在しなければ None)。"""
    from saiverse.data_paths import EXPANSION_DATA_DIR
    addon_dir = EXPANSION_DATA_DIR / addon_name
    if not addon_dir.is_dir():
        return None
    builtin = addon_dir / "builtin_actions"
    return builtin if builtin.is_dir() else None


def _seed_builtin_actions(addon_name: str) -> None:
    """builtin_actions/ のサンプルを user actions dir にコピー (初回のみ)。"""
    builtin_dir = _builtin_actions_dir(addon_name)
    if builtin_dir is None:
        return
    dest_dir = _actions_dir(addon_name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in builtin_dir.glob("*.json"):
        dest = dest_dir / src.name
        if not dest.exists():
            shutil.copy2(src, dest)
            LOGGER.info("composite_actions: seeded builtin action %s -> %s", src.name, dest)


def load_actions(addon_name: str) -> List[ActionDefinition]:
    _seed_builtin_actions(addon_name)
    actions_dir = _actions_dir(addon_name)
    if not actions_dir.is_dir():
        return []
    results: List[ActionDefinition] = []
    for p in sorted(actions_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            action = validate_action(data)
            results.append(action)
        except Exception as exc:
            LOGGER.warning("composite_actions: skipping invalid action %s: %s", p, exc)
    return results


def save_action(addon_name: str, action: ActionDefinition) -> Path:
    actions_dir = _actions_dir(addon_name)
    actions_dir.mkdir(parents=True, exist_ok=True)
    path = actions_dir / f"{action.id}.json"
    path.write_text(
        json.dumps(action.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    LOGGER.info("composite_actions: saved action %s to %s", action.id, path)
    return path


def delete_action(addon_name: str, action_id: str) -> bool:
    path = _actions_dir(addon_name) / f"{action_id}.json"
    if path.exists():
        path.unlink()
        LOGGER.info("composite_actions: deleted action %s", path)
        return True
    return False


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _coerce_param_value(p: ParamDef, raw: Any) -> Any:
    """パラメータ値を宣言された型に変換し、 数値は min/max でクランプする。"""
    if raw is None:
        return None
    try:
        if p.type == "integer":
            val: Any = int(raw)
        elif p.type == "number":
            val = float(raw)
        elif p.type == "boolean":
            val = raw.lower() in ("true", "1", "yes", "on") if isinstance(raw, str) else bool(raw)
        else:
            val = str(raw)
    except (TypeError, ValueError):
        return p.default
    if p.type in ("integer", "number"):
        if p.minimum is not None and val < p.minimum:
            val = type(val)(p.minimum)
        if p.maximum is not None and val > p.maximum:
            val = type(val)(p.maximum)
    return val


def _resolve_param_values(
    action: ActionDefinition, provided: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """宣言済みパラメータについて、 呼び出し値 (なければ default) を型変換して返す。"""
    provided = provided or {}
    values: Dict[str, Any] = {}
    for p in action.parameters:
        raw = provided.get(p.name)
        if raw is None:
            raw = p.default
        values[p.name] = _coerce_param_value(p, raw)
    return values


# 式評価で許可する演算子 (算術のみ)。 Name / Call / 属性アクセス等は一切評価
# しないので、 文字列パラメータを差し込まれても任意コード実行にはならない。
_ARITH_BINOPS = {
    ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul,
    ast.Div: _op.truediv, ast.FloorDiv: _op.floordiv,
    ast.Mod: _op.mod, ast.Pow: _op.pow,
}
_ARITH_UNARY = {ast.UAdd: _op.pos, ast.USub: _op.neg}


def _arith_eval_node(node: ast.AST) -> float:
    """算術 AST ノードのみを評価する (数値リテラル + 四則 + 累乗 + 単項符号)。"""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("数値以外の定数は式に使えません")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ARITH_BINOPS:
        left = _arith_eval_node(node.left)
        right = _arith_eval_node(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 64 or abs(left) > 1e6):
            raise ValueError("累乗が大きすぎます")
        return _ARITH_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ARITH_UNARY:
        return _ARITH_UNARY[type(node.op)](_arith_eval_node(node.operand))
    raise ValueError("式に使えない要素が含まれています")


def _safe_eval_arith(expr: str) -> float:
    """算術式文字列を安全に評価する。 失敗時は例外を送出。"""
    tree = ast.parse(expr, mode="eval")
    return _arith_eval_node(tree.body)


def _resolve_placeholder(value: Any, param_values: Dict[str, Any]) -> Any:
    """steps の値に書かれた ``${param}`` を実値に解決する。

    挙動は 3 種類:

    * **値まるごと** ``"${speed}"`` → そのパラメータの型付き値 (number/string/
      boolean をそのまま保持)。
    * **式の一部** ``"50*${speed}"`` → ``${name}`` を数値で差し込み、 算術式
      (``+ - * / // % **`` と括弧・単項符号) として評価。 整数になる場合は int。
    * **文字列補間** ``"ch${index}"`` のように算術として評価できないものは、
      ``${name}`` を文字列値で置換した文字列を返す。

    式評価は :func:`_safe_eval_arith` が算術ノードしか通さないため、 文字列
    パラメータを差し込まれても任意コード実行にはならない。
    """
    if not isinstance(value, str):
        return value

    # 値まるごと ${name} → 型付き置換 (従来挙動を維持)
    m = _PLACEHOLDER_RE.match(value)
    if m:
        return param_values.get(m.group(1))

    if not _PLACEHOLDER_FIND_RE.search(value):
        return value

    # 式モード: 各 ${name} を repr() で差し込む。 数値は数値リテラルに、
    # 文字列はクオート付きになるので算術評価は自然に失敗 → 補間にフォールバック。
    substituted = _PLACEHOLDER_FIND_RE.sub(
        lambda mo: repr(param_values.get(mo.group(1))), value
    )
    try:
        result = _safe_eval_arith(substituted)
    except (ValueError, SyntaxError, ZeroDivisionError, TypeError, OverflowError):
        # 文字列補間にフォールバック (未定義パラメータは空文字)
        return _PLACEHOLDER_FIND_RE.sub(
            lambda mo: "" if param_values.get(mo.group(1)) is None
            else str(param_values.get(mo.group(1))),
            value,
        )
    if isinstance(result, float) and result.is_integer():
        return int(result)
    return result


def _get_mcp_connection(addon_name: str):
    """Resolve the MCP connection for the addon's first MCP server."""
    from tools.mcp_client import get_mcp_manager, _make_instance_key

    manager = get_mcp_manager()
    if manager is None:
        raise RuntimeError("MCP クライアントが初期化されていません")

    addon_servers = [
        qname for qname, meta in manager._server_meta.items()
        if meta.get("addon_name") == addon_name
    ]
    if not addon_servers:
        raise RuntimeError(f"アドオン '{addon_name}' に MCP サーバーが見つかりません")

    qualified_name = addon_servers[0]
    instance_key = _make_instance_key(qualified_name, persona_id=None)
    conn = manager._connections.get(instance_key)
    if conn is None:
        raise RuntimeError(
            f"MCP サーバー '{qualified_name}' に接続されていません。"
            "デバイス / gateway が起動しているか確認してください。"
        )
    return conn


def _get_mcp_instance_connection(addon_name: str, instance_id: str):
    """指定機体 (MCP 名前付きインスタンス) の gateway 接続を返す (無ければ None)。

    複数機体アドオン (instance_template scope) は global インスタンスを持たず、
    生 MCP ツール (native wrapper の無い ``get_device_info`` 等) は
    ``{server}:instance:{instance_id}`` の接続へ流す必要がある。 テスト実行では
    対象機体が明示されているので、 その機体の instance 接続を直接引く
    (native tool は resolve_vessel_connection が同じ機体へ解決するので、 生 MCP と
    native で同一機体に揃う)。 接続が無い (機体未起動) 場合は None。
    """
    from tools.mcp_client import get_mcp_manager, _make_instance_key

    manager = get_mcp_manager()
    if manager is None:
        return None
    for qname, meta in manager._server_meta.items():
        if meta.get("addon_name") != addon_name:
            continue
        conn = manager._connections.get(
            _make_instance_key(qname, instance_id=instance_id)
        )
        if conn is not None:
            return conn
    return None


def _mcp_tool_names(conn) -> set:
    """gateway 接続が公開している MCP ツール名の集合。"""
    names: set = set()
    for tool_def in getattr(conn, "tools", []) or []:
        n = getattr(tool_def, "name", None)
        if n:
            names.add(n)
    return names


def _make_call_awaitable(
    conn, mcp_names: set, tool: str, args: Dict[str, Any],
):
    """1 件のツール呼び出しを awaitable にして返す (native 優先 → MCP fallback)。

    **native tool を優先する**。 native wrapper (move_head / set_avatar 等) は
    現在ペルソナが降りている機体の gateway インスタンスへの per-vessel 解決を
    内包するため、 複数機体では生 MCP ツール (``conn.call_tool``、 単一の global
    接続前提) では正しい機体に振り分けられない。 native tool が存在しない名前
    のみ、 gateway 接続上の生 MCP ツールとして ``conn.call_tool`` に流す
    (= global / per_persona scope の addon 向けの従来経路)。 conn が無い
    (= instance_template で global インスタンスが無い) 場合は native のみで動く。

    native tool は同期 callable で、 内部で MCP イベントループに
    ``run_coroutine_threadsafe(...).result()`` でブロックする (env3 / move_head
    と同形)。 ``_execute_action_async`` 自体がその同じループ上で動くため、 ここで
    inline 呼び出しするとデッドロックする。 よって同期 native tool は
    ``run_in_executor`` でワーカースレッドに逃がす。 building-gate の印が付いた
    ラッパだけを外す: このステップを保持するアクション spell 自体が
    既に vessel building gate 済みで、 ワーカースレッドには persona context が
    伝播しないため、 内側で再度ゲートを通すと誤判定で弾かれる。
    """
    from tools import TOOL_REGISTRY

    func = TOOL_REGISTRY.get(tool)
    if func is not None:
        target = (
            getattr(func, "__wrapped__", func)
            if getattr(func, "_saiverse_building_gate", False)
            else func
        )
        if inspect.iscoroutinefunction(target):
            # async native は _execute_action_async の Task 文脈 (persona_context
            # 伝播済み) で await されるので、 そのまま返せば文脈が乗る。
            return target(**args)
        # sync native は executor スレッドに逃がすが、 run_in_executor は
        # contextvars を伝播しないため、 **この呼び出しごとに** 現在の Task 文脈を
        # copy して ``call_ctx.run`` で張り直す。 これで native wrapper 内の
        # resolve_vessel / resolve_vessel_connection が現在ペルソナ (または
        # テスト実行の対象機体上書き) を解決でき、 per-vessel に正しく振り分く
        # (= 直の spell が _run_spell_tool_async の ``_run`` で persona_context を
        # 張るのと同形)。 1 つの parallel ステップに複数の sync native call がある
        # とき、 同じ Context を共有すると ``ctx.run`` が並列で二重に入り
        # "Context is already entered" になるため、 call ごとに独立 copy にする。
        # 本関数は _execute_action_async の Task 文脈内で呼ばれるので、 ここでの
        # copy_context() は上書き済みの instance_id 等を正しく捕捉する。
        call_ctx = contextvars.copy_context()
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(None, lambda: call_ctx.run(target, **args))

    if conn is not None and tool in mcp_names:
        return conn.call_tool(tool, args)

    async def _missing():
        raise RuntimeError(
            f"ツール '{tool}' は native tool にも MCP サーバーにも見つかりません"
            " (複数機体では native wrapper 経由が必要です)"
        )
    return _missing()


async def _execute_action_async(
    addon_name: str,
    action: ActionDefinition,
    param_values: Optional[Dict[str, Any]] = None,
    target_instance_id: Optional[str] = None,
) -> str:
    # テスト実行 (addon 管理 UI 発) は persona 文脈が無いので、 native wrapper の
    # per-vessel 解決 (現在ペルソナが降りている機体) が効かない。 UI で選んだ機体
    # を狙えるよう、 ここで「対象インスタンス上書き」を張る。 本コルーチンは MCP
    # ループスレッド上の Task として走るため、 ここで set した contextvar は下の
    # ``copy_context()`` に取り込まれ、 sync native は ``ctx.run`` 経由の executor
    # で、 async native は本 Task の await でそれぞれ同じ値を読む (他 Task には
    # 漏れない = Task ごとに独立 context)。 target_instance_id が None のスペル経路
    # では上書きせず、 従来の persona 文脈解決に委ねる。
    if target_instance_id:
        from tools.context import set_tool_target_instance_id
        set_tool_target_instance_id(target_instance_id)

    # 生 MCP 接続 (native wrapper の無い ``get_device_info`` 等の直呼び用) を引く。
    #  - target_instance_id あり (テスト実行で機体を明示): その機体の instance
    #    接続。 複数機体は global を持たないので、 生 MCP ツールも native tool と
    #    同じ選択機体へ揃える。
    #  - target_instance_id なし (スペル経路): 従来通り global 接続 (global /
    #    per_persona scope の addon 向け)。 instance_template では global が無いので
    #    conn=None になるが、 native wrapper が per-vessel 解決するため native のみ
    #    のアクションは実行できる (生 MCP ツールだけは conn 不在時に
    #    _make_call_awaitable が明示エラーにする)。
    if target_instance_id:
        conn = _get_mcp_instance_connection(addon_name, target_instance_id)
        if conn is None:
            LOGGER.info(
                "composite_actions: no instance MCP conn for %s/%s; "
                "native-wrapper-only mode", addon_name, target_instance_id,
            )
    else:
        try:
            conn = _get_mcp_connection(addon_name)
        except RuntimeError as exc:
            LOGGER.info(
                "composite_actions: no global MCP conn for %s (%s); "
                "native-wrapper-only mode", addon_name, exc,
            )
            conn = None
    mcp_names = _mcp_tool_names(conn) if conn is not None else set()
    param_values = param_values or {}
    results: List[str] = []

    # native tool を executor に逃がす際の contextvars 張り直しは、
    # _make_call_awaitable が **call ごとに** copy_context() する (同一 Context を
    # 共有すると parallel step で "Context is already entered" になるため)。

    for i, step in enumerate(action.steps):
        if step.type == "wait":
            raw_dur = _resolve_placeholder(step.duration_ms, param_values)
            try:
                dur_ms = float(raw_dur)
            except (TypeError, ValueError):
                dur_ms = 0.0
            # 実行時にも上限でクランプ (パラメータ経由の極端値を防ぐ)
            dur_ms = max(0.0, min(dur_ms, float(MAX_WAIT_MS)))
            if dur_ms > 0:
                await asyncio.sleep(dur_ms / 1000.0)
            continue

        if step.type == "parallel":
            resolved = [
                (call.tool, {k: _resolve_placeholder(v, param_values) for k, v in call.args.items()})
                for call in step.calls
            ]
            coros = [
                _make_call_awaitable(conn, mcp_names, tool, args)
                for tool, args in resolved
            ]
            step_results = await asyncio.gather(*coros, return_exceptions=True)
            for j, ((tool, _args), res) in enumerate(zip(resolved, step_results)):
                if isinstance(res, Exception):
                    error_msg = f"steps[{i}].calls[{j}] ({tool}) 失敗: {res}"
                    LOGGER.warning("composite_actions: %s", error_msg)
                    results.append(error_msg)
                    return "\n".join(results)
                results.append(f"{tool}: {res}")

    if not results:
        return f"アクション '{action.display_name}' を実行しました。"
    return "\n".join(results)


def execute_action(
    addon_name: str,
    action: ActionDefinition,
    param_values: Optional[Dict[str, Any]] = None,
    timeout_sec: float = 30.0,
    target_instance_id: Optional[str] = None,
) -> str:
    """アクションを実行する。

    ``target_instance_id`` を渡すと、 native wrapper の per-vessel 解決を persona
    文脈ではなく指定の MCP インスタンス (= 機体) に固定する。 addon 管理 UI の
    テスト実行が「どの機体で試すか」をユーザーに選ばせるために使う。 スペル経路
    (ペルソナ駆動) では None のまま呼び、 従来通り現在機体へ解決する。
    """
    from saiverse.addon_config import is_addon_enabled

    if not is_addon_enabled(addon_name):
        raise RuntimeError(
            f"アドオン '{addon_name}' は無効化されているためアクションを実行できません"
        )

    # Stored JSON and API test payloads both pass through the same allowlist
    # immediately before execution.  Validation at save time alone is not a
    # security boundary because files can be edited while SAIVerse is stopped.
    allowed_tools = get_available_tools(addon_name)
    validate_action(action.to_dict(), allowed_tools=allowed_tools)

    import tools.mcp_client as _mcp

    loop = _mcp._loop
    if loop is None:
        raise RuntimeError("MCP イベントループが初期化されていません")

    resolved = _resolve_param_values(action, param_values)
    future = asyncio.run_coroutine_threadsafe(
        _execute_action_async(
            addon_name, action, resolved,
            target_instance_id=target_instance_id,
        ),
        loop,
    )
    return future.result(timeout=timeout_sec)


# ---------------------------------------------------------------------------
# Spell registration
# ---------------------------------------------------------------------------

def _spell_name(addon_name: str, action_id: str) -> str:
    return f"{addon_name}__action__{action_id}"


def _make_action_spell_func(addon_name: str, action: ActionDefinition):
    """Create the sync callable registered as a spell in TOOL_REGISTRY.

    ペルソナがスペル呼び出し時に渡した引数 (``kwargs``) を ``execute_action``
    の ``param_values`` に流し、 steps 内の ``${param}`` を実値に解決する。
    宣言済みパラメータ以外の kwargs は無視される。
    """
    def _invoke(**kwargs) -> str:
        try:
            return execute_action(addon_name, action, param_values=kwargs)
        except Exception as exc:
            LOGGER.exception("composite_actions: spell execution failed for %s", action.id)
            return f"アクション '{action.display_name}' の実行に失敗しました: {exc}"
    return _invoke


def _action_param_schema(action: ActionDefinition) -> Dict[str, Any]:
    """アクションの parameters から spell 用 JSON schema (properties) を組み立てる。

    全パラメータは default を前提に optional 扱い (= ``required`` を立てない)。
    ペルソナが省略すれば default が使われる。
    """
    _JSON_TYPE = {
        "number": "number", "integer": "integer",
        "string": "string", "boolean": "boolean",
    }
    properties: Dict[str, Any] = {}
    for p in action.parameters:
        prop: Dict[str, Any] = {"type": _JSON_TYPE.get(p.type, "string")}
        if p.description:
            prop["description"] = p.description
        if p.minimum is not None:
            prop["minimum"] = p.minimum
        if p.maximum is not None:
            prop["maximum"] = p.maximum
        if p.default is not None:
            prop["default"] = p.default
        properties[p.name] = prop
    return {"type": "object", "properties": properties}


def _vessel_building_id(addon_name: str) -> Optional[str]:
    from saiverse.addon_config import get_params
    params = get_params(addon_name)
    return params.get("vessel_building_id") if params else None


def register_action_spells(addon_name: str) -> int:
    from tools import register_external_tool
    from tools.core import ToolSchema

    actions = load_actions(addon_name)
    if not actions:
        return 0

    vbid = _vessel_building_id(addon_name)
    building_ids = [vbid] if vbid else None
    count = 0

    for action in actions:
        name = _spell_name(addon_name, action.id)
        schema = ToolSchema(
            name=name,
            description=action.description or action.display_name,
            parameters=_action_param_schema(action),
            result_type="string",
            spell=True,
            spell_display_name=action.display_name,
            spell_visible=True,
            addon_name=addon_name,
            building_ids=building_ids,
        )
        func = _make_action_spell_func(addon_name, action)
        if register_external_tool(name, schema, func, allow_replace=True):
            count += 1
            LOGGER.info("composite_actions: registered spell '%s' (%s)", name, action.display_name)

    return count


def unregister_action_spells(addon_name: str) -> int:
    from tools import _remove_registered_tool, SPELL_TOOL_NAMES

    prefix = f"{addon_name}__action__"
    to_remove = [n for n in list(SPELL_TOOL_NAMES) if n.startswith(prefix)]
    for name in to_remove:
        _remove_registered_tool(name)
        LOGGER.info("composite_actions: unregistered spell '%s'", name)
    return len(to_remove)


def register_all_addon_action_spells() -> int:
    """全有効アドオンのアクション spell を登録する (起動時用)。"""
    from saiverse.addon_config import is_addon_enabled
    from saiverse.data_paths import EXPANSION_DATA_DIR

    if not EXPANSION_DATA_DIR.exists():
        return 0

    total = 0
    for addon_dir in sorted(EXPANSION_DATA_DIR.iterdir()):
        if not addon_dir.is_dir():
            continue
        addon_name = addon_dir.name
        if not is_addon_enabled(addon_name):
            continue
        try:
            count = register_action_spells(addon_name)
            if count:
                total += count
        except Exception as exc:
            LOGGER.warning(
                "composite_actions: failed to register actions for %s: %s",
                addon_name, exc,
            )
    return total


# ---------------------------------------------------------------------------
# Available tools query
# ---------------------------------------------------------------------------

def get_available_tool_schemas(addon_name: str) -> List[Dict[str, Any]]:
    """アクションのステップで呼べるツールの一覧を、 引数スキーマ付きで返す。

    各要素は ``{"name", "description", "parameters"}``。 ``parameters`` は JSON
    Schema (``{"type": "object", "properties": {...}, "required": [...]}``)。
    UI はこれを使って args をキー単位のフォームで編集する (JSON 直書きの解消)。

    収集元は :func:`get_available_tools` と同じ (アドオンの MCP ツール + 同じ
    アドオンが登録した native tool、 アクション spell 自身は除外)。
    """
    result: Dict[str, Dict[str, Any]] = {}

    from tools.mcp_client import get_mcp_manager

    manager = get_mcp_manager()
    if manager is not None:
        for qname, meta in manager._server_meta.items():
            if meta.get("addon_name") != addon_name:
                continue
            conn = manager._connections.get(f"{qname}:global")
            if conn is None:
                # 複数機体 (instance_template): global が無いので、 tool 一覧の
                # 取得元として任意の稼働インスタンスを 1 つ使う (どの機体でも
                # gateway が公開する MCP ツール名は同じ)。
                prefix = f"{qname}:instance:"
                conn = next(
                    (c for k, c in manager._connections.items()
                     if k.startswith(prefix)),
                    None,
                )
            if conn is None:
                continue
            for tool_def in getattr(conn, "tools", []):
                name = getattr(tool_def, "name", None)
                if not name:
                    continue
                result[name] = {
                    "name": name,
                    "description": getattr(tool_def, "description", "") or "",
                    "parameters": getattr(tool_def, "inputSchema", None) or {},
                }

    try:
        from tools import TOOL_SCHEMAS

        action_prefix = f"{addon_name}__action__"
        for schema in TOOL_SCHEMAS:
            if getattr(schema, "addon_name", None) != addon_name:
                continue
            if not getattr(schema, "spell", False):
                continue
            if schema.name.startswith(action_prefix):
                continue
            result[schema.name] = {
                "name": schema.name,
                "description": getattr(schema, "description", "") or "",
                "parameters": getattr(schema, "parameters", None) or {},
            }
    except Exception:
        LOGGER.exception("composite_actions: failed to enumerate native addon tool schemas")

    return [result[k] for k in sorted(result)]


def get_available_tools(addon_name: str) -> List[str]:
    """アクション定義のステップで呼べるツール名一覧を返す。

    アドオンの gateway が公開する MCP ツール名に加えて、 同じアドオンが登録した
    SAIVerse native tool 名 (``move_head`` / ``see`` / ``servo8_set_angle`` 等)
    も含める。 これにより複合アクションのステップで native tool を選べる
    (intent doc §C「アクション定義のステップで i2c_write の代わりに native tool
    を使えるようにする」)。 アクション spell 自身 (``<addon>__action__*``) は
    アクションがアクションを呼ぶ再帰を避けるため除外する。
    """
    return [t["name"] for t in get_available_tool_schemas(addon_name)]
