from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Union, Set

from pydantic import BaseModel, Field, model_validator
from typing_extensions import Literal


class ConditionalNext(BaseModel):
    """Conditional edge routing based on state field value."""
    field: str = Field(description="State key to evaluate (e.g., 'router.playbook'). Supports nested keys with dot notation.")
    operator: Optional[str] = Field(
        default="eq",
        description="Comparison operator: 'eq' (default, exact match), 'gte' (>=), 'gt' (>), 'lte' (<=), 'lt' (<), 'ne' (!=)"
    )
    cases: Dict[str, Optional[str]] = Field(
        description="Mapping of values to next node IDs. Use 'default' key for fallback. Value can be null to end execution. "
                    "For numeric operators (gte/gt/lte/lt), use numeric string keys like '5' and they will be compared numerically."
    )


class NodeType(str, Enum):
    LLM = "llm"
    TOOL = "tool"
    TOOL_CALL = "tool_call"
    SPEAK = "speak"
    THINK = "think"
    MEMORY = "memorize"
    SAY = "say"
    PASS = "pass"
    SUBPLAY = "subplay"
    SET = "set"
    EXEC = "exec"
    STELIS_START = "stelis_start"
    STELIS_END = "stelis_end"


class LLMNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.LLM]
    action: Optional[str] = Field(default=None, description="Prompt template. Use {variable_name} placeholders.")
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )
    response_schema: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional JSON schema to enforce structured output."
    )
    response_schema_source: Optional[str] = Field(
        default=None,
        description="Dynamic source for response_schema, resolved at node execution. "
                    "Currently supports 'spell:<spell_name>' which loads the input schema "
                    "of a registered Spell from SPELL_TOOL_SCHEMAS. The source string "
                    "supports template substitution ({state_var}). Used together with "
                    "spell_args_decider for pre_spells dynamic argument generation. "
                    "Ignored if response_schema is also specified."
    )
    output_key: Optional[str] = Field(
        default=None,
        description="Key name to store structured output for later nodes. Defaults to node id."
    )
    output_mapping: Optional[Dict[str, str]] = Field(
        default=None,
        description="Map structured output fields to state variables. "
                    "Example: {'router.playbook': 'selected_playbook', 'router.args': 'selected_args'}. "
                    "Keys are dot-notated paths in structured output, values are target state variable names."
    )
    available_tools: Optional[List[str]] = Field(
        default=None,
        description="List of tool names that LLM can call. If specified, enables tool calling for this node."
    )
    output_keys: Optional[List[Dict[str, str]]] = Field(
        default=None,
        description="Map output types to state keys. Examples: [{'text': 'speak_content'}, {'function_call': 'tool_call'}]. "
                    "Supported types: 'text', 'function_call', 'thought'. "
                    "Function calls are stored as nested keys: '<key>.name', '<key>.args.<arg_name>'."
    )
    memorize: Optional[Union[bool, Dict[str, Any]]] = Field(
        default=None,
        description="If specified, save prompt and response to SAIMemory. "
                    "``True`` で既定タグ保存、dict で詳細指定 (例: {'tags': ['conversation']})。"
                    "line_role / scope はアスペクト (§10) から導出されるため dict に書かない。"
                    "Tags will be applied to both user (prompt) and assistant (response) messages."
    )
    speak: Optional[bool] = Field(
        default=None,
        description="If True, output response to Building (UI). "
                    "When SAIVERSE_LLM_STREAMING=true (default), streams response chunks in real-time. "
                    "When false, sends complete response after generation."
    )
    metadata_key: Optional[str] = Field(
        default=None,
        description="State key containing metadata dict to attach to the speak message "
                    "(e.g., media attachments from tool execution). Only used when speak=true."
    )
    important: Optional[bool] = Field(
        default=None,
        description="If True, this node's output is considered important and will be "
                    "dual-written to both pulse_logs and messages (long-term memory)."
    )


class ToolNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.TOOL]
    action: str = Field(description="Tool name registered in tools registry.")
    args_input: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Map of argument names to state keys or literal values. Strings are treated as state keys (e.g. {'query': 'search_query.query'}). Non-string values (int/float/bool) are passed as-is to the tool."
    )
    output_key: Optional[str] = Field(
        default=None,
        description="Key name to store tool result in state for later nodes."
    )
    output_keys: Optional[list] = Field(
        default=None,
        description="List of keys to store tuple results. E.g. ['text', 'snippet', 'file_path'] for multi-value tool returns."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )
    important: Optional[bool] = Field(
        default=None,
        description="If True, this node's output is considered important and will be "
                    "dual-written to both pulse_logs and messages (long-term memory)."
    )


class ToolCallNodeDef(BaseModel):
    """Node that dynamically executes a tool chosen by an LLM node.

    Reads the tool name and arguments from state (stored by an LLM node with
    available_tools + output_keys), looks up the tool in TOOL_REGISTRY, and
    executes it.  This enables agentic loops where the LLM freely picks tools
    without per-tool branching in the playbook graph.
    """
    id: str
    type: Literal[NodeType.TOOL_CALL]
    call_source: str = Field(
        default="fc",
        description="State key prefix where the LLM stored the function call. "
                    "Reads '{call_source}.name' for the tool name and "
                    "'{call_source}.args' for the arguments dict. "
                    "Falls back to legacy state keys 'tool_name'/'tool_args' if not found."
    )
    output_key: Optional[str] = Field(
        default=None,
        description="Key name to store tool result in state for later nodes."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )
    important: Optional[bool] = Field(
        default=None,
        description="If True, this node's output is considered important and will be "
                    "dual-written to both pulse_logs and messages (long-term memory)."
    )


class SpeakNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.SPEAK]
    action: Optional[str] = Field(
        default=None, description="Optional template for final output. Defaults to last message content."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )
    important: Optional[bool] = Field(
        default=True,
        description="If True, this node's output is considered important and will be "
                    "dual-written to both pulse_logs and messages (long-term memory). "
                    "Defaults to True for speak nodes as they produce final user-facing output."
    )


class ThinkNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.THINK]
    action: Optional[str] = Field(default=None, description="Optional note to store internally.")
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )
    important: Optional[bool] = Field(
        default=None,
        description="If True, this node's output is considered important and will be "
                    "dual-written to both pulse_logs and messages (long-term memory)."
    )






class SayNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.SAY]
    action: Optional[str] = Field(
        default=None, description="Template for UI output only (no SAIMemory record). Defaults to last message content."
    )
    metadata_key: Optional[str] = Field(
        default=None, description="State key containing metadata dict to attach to the message."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )


class PassNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.PASS]
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )

class MemorizeNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.MEMORY]
    action: Optional[str] = Field(
        default=None, description="Template for the text to store. Defaults to last message content."
    )
    role: str = Field(default="assistant", description="Role name to store in SAIMemory.")
    tags: Optional[List[str]] = Field(
        default=None,
        description="Optional semantic-classification tags for SAIMemory metadata "
                    "(used for search/recall/Chronicle filtering). "
                    "Phase 3 段階 4-C 以降は context 制御用タグ (`internal` / `conversation` / "
                    "`event_message` 等) を含めず、純粋な意味分類タグのみ書く。"
                    "context 制御は line_role / scope フィールドで行う。"
    )
    line_role: Optional[str] = Field(
        default=None,
        description="Phase 3 段階 4-C: ライン階層属性。'main_line' / 'sub_line' / "
                    "'meta_judgment' / 'nested' のいずれか。指定時は _store_memory に "
                    "明示渡しされ、context 構築時に required_line_roles で参照される。"
                    "未指定時は PulseContext の現在の LineFrame から自動解決。"
    )
    scope: Optional[str] = Field(
        default=None,
        description="Phase 3 段階 4-C: メッセージの永続性。'committed' / 'discardable' / "
                    "'volatile' のいずれか。'committed' は通常の永続化、'discardable' は "
                    "メタ判断の試行錯誤ターン (continue で消える)、'volatile' は Pulse 内のみ "
                    "(サブラインの中間処理向け)。未指定時は DB の DEFAULT 'committed' に従う。"
    )
    metadata_key: Optional[str] = Field(
        default=None, description="State key containing metadata dict to attach to the message."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )
    important: Optional[bool] = Field(
        default=None,
        description="If True, this node's output is considered important and will be "
                    "dual-written to both pulse_logs and messages (long-term memory)."
    )





class SubPlayNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.SUBPLAY]
    playbook: str = Field(description="Name of the sub-playbook to execute")
    args: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Args to pass to the sub-playbook. Values are template strings "
                    "resolved against current state (e.g., '{objective}')."
    )
    propagate_output: bool = Field(default=False, description="If true, append sub-playbook outputs to parent outputs")
    execution: Optional[str] = Field(
        default="inline",
        description="Execution mode: 'inline' (default, runs in parent context) or "
                    "'subagent' (runs in a temporary thread, only result returns to parent)."
    )
    subagent_chronicle: bool = Field(
        default=True,
        description="When execution='subagent', generate a chronicle summary on completion."
    )
    isolate_pulse_context: bool = Field(
        default=False,
        description="If true, run sub-playbook with a fresh PulseContext instead of sharing the parent's. "
                    "Useful when the sub-playbook should not see prior pulse log entries (e.g., router I/O)."
    )
    line: Literal["main", "sub"] = Field(
        default="main",
        description=(
            "Which line to run the sub-playbook on. "
            "'main' (default): inherits parent state['_messages'] reference and uses parent model "
            "(continues main-line cache). "
            "'sub': forks parent state['_messages'] by COPY (not reference share) and uses persona's "
            "lightweight model. On completion, the sub-playbook's output_schema['report_to_parent'] "
            "is appended to parent state['_messages'] as a system-tagged user message. "
            "See docs/intent/persona_action_tracks.md (v0.9) for the full spec."
        ),
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )


class SetNodeDef(BaseModel):
    """Node that sets or modifies state variables."""
    id: str
    type: Literal[NodeType.SET]
    assignments: Dict[str, Any] = Field(
        description="Mapping of state keys to values. Values can be: "
                    "- Literal values (number, string, bool): {\"count\": 0, \"name\": \"test\"} "
                    "- Template strings with {var} placeholders: {\"greeting\": \"Hello {name}\"} "
                    "- Arithmetic expressions: {\"count\": \"{count} + 1\"}, {\"total\": \"{a} * {b}\"}"
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )


class ExecNodeDef(BaseModel):
    """Node that executes a dynamically selected sub-playbook."""
    id: str
    type: Literal[NodeType.EXEC]
    playbook_source: str = Field(
        default="selected_playbook",
        description="State variable name containing the playbook name to execute."
    )
    args: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Static args to pass to the sub-playbook. Values are template strings "
                    "resolved against current state (e.g., '{objective}'). "
                    "These are merged with dynamic args from args_source (args_source takes precedence)."
    )
    args_source: Optional[str] = Field(
        default="selected_args",
        description="State variable name containing args dict for the sub-playbook. "
                    "The 'input' or 'query' key from this dict is passed as sub_input."
    )
    execution: Optional[str] = Field(
        default="inline",
        description="Execution mode: 'inline' (default, runs in parent context) or "
                    "'subagent' (runs in a temporary thread, only result returns to parent)."
    )
    subagent_chronicle: bool = Field(
        default=True,
        description="When execution='subagent', generate a chronicle summary on completion."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )
    error_next: Optional[str] = Field(
        default=None,
        description="Node to transition to when sub-playbook execution fails. "
                    "If not set, normal next/conditional_next is used even on error."
    )


class StelisConfig(BaseModel):
    """Configuration for Stelis thread creation."""
    window_ratio: float = Field(
        default=0.8,
        description="Portion of parent's context window to allocate to this Stelis thread (0.0-1.0)."
    )
    max_depth: int = Field(
        default=3,
        description="Maximum allowed nesting depth for Stelis threads."
    )
    chronicle_prompt: Optional[str] = Field(
        default=None,
        description="Prompt to use when generating Chronicle summary on completion."
    )


class StelisStartNodeDef(BaseModel):
    """Node that starts a new Stelis thread for hierarchical context management."""
    id: str
    type: Literal[NodeType.STELIS_START]
    label: Optional[str] = Field(
        default=None,
        description="Human-readable label for this Stelis session (e.g., 'Coding Session')."
    )
    stelis_config: Optional[StelisConfig] = Field(
        default=None,
        description="Configuration for the Stelis thread. Uses defaults if not specified."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )


class StelisEndNodeDef(BaseModel):
    """Node that ends the current Stelis thread and returns to parent context."""
    id: str
    type: Literal[NodeType.STELIS_END]
    label: Optional[str] = Field(
        default=None,
        description="Human-readable label for logging purposes."
    )
    generate_chronicle: bool = Field(
        default=True,
        description="Whether to generate a Chronicle summary when ending the Stelis thread."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )


NodeDef = Union[
    LLMNodeDef, ToolNodeDef, ToolCallNodeDef, SpeakNodeDef, ThinkNodeDef,
    MemorizeNodeDef, SayNodeDef, PassNodeDef, SubPlayNodeDef, SetNodeDef,
    ExecNodeDef, StelisStartNodeDef, StelisEndNodeDef
]

class ParamContractError(ValueError):
    """値が ``InputParam`` の宣言 (型 / enum) に合わない。"""


def coerce_param_value(
    value: Any,
    param_type: str,
    enum_values: Optional[List[str]] = None,
    enum_source: Optional[str] = None,
) -> Any:
    """値を宣言型へ正規化して返す。合わない値は :class:`ParamContractError`。

    Playbook の入力には tool 引数と違って下流の検証が存在しないため、ここが
    契約の唯一の検査点になる。クオートされた数値/真偽値 (``"2"`` / ``"true"``)
    は宣言型へ正規化し、変換できない値と enum 外の値は暗黙値で走らせず正直に
    失敗させる。呼ばれるのは 2 箇所で、どちらも同じ判定を通す:

    - ロード時: ``InputParam.default`` (宣言そのものの検算)
    - 実行時: 呼び出しが渡した値 (``sea/runtime_graph.py``)

    ``string`` / ``object`` / 未知型は素通し。enum は静的 ``enum_values`` が
    宣言されているときだけ集合を検査する (``enum_source`` の動的 enum は
    実行時に集合を取れない)。両方が来た場合は ``enum_source`` を優先する —
    UI へ選択肢を出す API (``api/routes/config.py`` の ``resolved_options``)
    が同じ優先順位で動的側を表示するため、ここで静的集合を当てると「UI で
    選べた値が実行時に弾かれる」ずれになる。宣言としては両立を許さない
    (下の :meth:`InputParam._check_declaration_against_type` がロード時に
    弾く) ので、この分岐に来るのは validator を経ていない値だけ。
    """
    ptype = param_type or "string"

    if ptype == "number":
        if isinstance(value, bool):
            raise ParamContractError("expected a number, got a boolean")
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            try:
                return int(stripped)
            except ValueError:
                pass
            try:
                return float(stripped)
            except ValueError:
                raise ParamContractError("expected a number") from None
        raise ParamContractError("expected a number")

    if ptype == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes"):
                return True
            if low in ("false", "0", "no"):
                return False
        raise ParamContractError("expected a boolean")

    if ptype == "enum":
        if enum_source:
            return value
        # ``is None`` で判定する。空リストを「制約なし」と読む truthiness 判定は、
        # 何も許さない宣言を素通しにする fail-open だった (2026-08-16 W10 F5)。
        # 空リスト自体は下の宣言検査がロード時に弾く。
        if enum_values is None:
            return value
        if value not in enum_values:
            raise ParamContractError(f"expected one of {enum_values}")
        return value

    return value


class InputParam(BaseModel):
    name: str
    description: str

    # Type and validation
    param_type: str = Field(
        default="string",
        description="Parameter type: 'string', 'number', 'boolean', 'enum'"
    )
    required: bool = Field(
        default=True,
        description="Whether this parameter is required"
    )
    default: Optional[Any] = Field(
        default=None,
        description="Default value if not provided"
    )

    # Enum options (for param_type='enum')
    enum_values: Optional[List[str]] = Field(
        default=None,
        description="Static list of allowed values for enum type"
    )
    enum_source: Optional[str] = Field(
        default=None,
        description="Dynamic enum source in format 'collection:scope'. "
                    "Examples: 'playbooks:router_callable', 'buildings:current_city', "
                    "'items:current_building', 'personas:current_city', 'tools:available'"
    )

    # UI display control
    user_configurable: bool = Field(
        default=False,
        description="If true, this parameter is shown in UI for user input"
    )
    ui_widget: Optional[str] = Field(
        default=None,
        description="UI widget type: 'text', 'textarea', 'dropdown', 'radio'. "
                    "Defaults to 'dropdown' for enum, 'text' for string."
    )

    @model_validator(mode="after")
    def _check_declaration_against_type(self) -> "InputParam":
        """宣言そのものを型契約に照らす (ロード時 fail-closed)。

        1. enum は許す値の供給源を**ちょうど一つ**持つ (静的 ``enum_values``
           か動的 ``enum_source`` のどちらか)。
           - 供給源ゼロ・空リストは「何も許さない宣言」で、実行時に制約なし
             として素通しされていた (W10 F5)
           - 両方の宣言は禁止する。UI へ選択肢を出す API は ``enum_source``
             を優先し、実行時の検証は静的集合を見るため、両立を許すと「UI で
             選べた値が実行時に弾かれる」ずれが宣言できてしまう。優先順位を
             規則で捌くのではなく、食い違う宣言を書けなくする
        2. ``default`` は呼び出しが渡す値と同じ検証を通す。``number`` の
           default が ``"12"`` のまま state に載ると、args 経由なら正規化される
           はずの値が型の違うまま下流へ流れる (W10 F4)。変換できる形なら
           ここで正規化し、できない宣言は Playbook ごとロードを失敗させる。
        """
        if self.param_type == "enum":
            if self.enum_source and self.enum_values is not None:
                raise ValueError(
                    f"input param '{self.name}': enum_values and enum_source are "
                    f"mutually exclusive. Declare exactly one source of allowed values."
                )
            if self.enum_values is not None and not self.enum_values:
                raise ValueError(
                    f"input param '{self.name}': enum_values is an empty list. "
                    f"An enum that allows nothing can never be satisfied — "
                    f"list the allowed values or use enum_source."
                )
            if not self.enum_source and not self.enum_values:
                raise ValueError(
                    f"input param '{self.name}': param_type='enum' but neither "
                    f"enum_values (non-empty) nor enum_source is declared. "
                    f"An enum with no allowed values can never be satisfied."
                )
        if self.default is not None:
            try:
                self.default = coerce_param_value(
                    self.default, self.param_type,
                    enum_values=self.enum_values, enum_source=self.enum_source,
                )
            except ParamContractError as exc:
                raise ValueError(
                    f"input param '{self.name}': default value {self.default!r} "
                    f"does not match param_type='{self.param_type}' ({exc})"
                ) from exc
        return self


class ContextRequirements(BaseModel):
    """呼び出しごとに変えてよい文脈の指定。

    **head (人格・部屋・呪文一覧などの前置き) の中身はここで選べない。** head は
    (persona, model) の Session ごとに一つで固定するのが prefix キャッシュ共有の
    土台であり、用途やラインで章を出し入れすると同一モデルで head が変わって
    キャッシュが壊れる (``sea/runtime_context.py`` の ``PERSONA_HEAD_SECTIONS``)。

    2026-07-23 の整理で、以下のフィールドを削除した:

    - ``inventory`` / ``building_items`` / ``working_memory``
      — どこからも読まれていない残骸だった
    - ``system_prompt`` / ``available_playbooks`` / ``visual_context`` / ``memory_weave``
      — head の章を選ぶスイッチ。上記のとおり出し分けてはいけないので撤去し、
        ``PERSONA_HEAD_SECTIONS`` に固定した

    削除により「指定なし」の意味が一本化され、``_FULL_CONTEXT_REQUIREMENTS``
    (フィールド既定と別物の第二の既定) も不要になったので撤去した。既定が欲しい
    呼び出しは ``requirements`` を渡さないこと。
    """
    history_depth: Union[int, str] = Field(
        default="full",
        description="History depth: 'full' (use persona's context_length), number (character count), "
                    "'Nmessages' (e.g., '10messages' for 10 recent messages), or 0/'none' (no history)"
    )
    history_balanced: bool = Field(
        default=False,
        description="If True, balance history across conversation partners (user + other personas)"
    )
    realtime_context: bool = Field(
        default=True,
        description="Include realtime context (current time, previous AI response time, spatial info) near end of context. "
                    "Placing time-sensitive info at the end improves LLM context caching efficiency."
    )


# State keys starting with "_" are the runtime's system namespace (_messages,
# _pulse_context, _spell_enabled, _cancellation_token, ...). Playbook-declared
# names must never be able to write there — a playbook that could overwrite
# them could alter persona identity, permissions, cancellation, or Pulse
# boundaries (Spell/Playbook 監査 2026-07-15 P1). Every loaded playbook passes
# through PlaybookSchema construction, so its validator is the choke point.
RESERVED_STATE_PREFIX = "_"


class PlaybookSchema(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9_]+$")
    display_name: Optional[str] = Field(default=None, description="Human-readable display name for UI. Falls back to name if not set.")
    description: str
    input_schema: List[InputParam]
    output_schema: Optional[List[str]] = Field(
        default=None,
        description="List of state keys to propagate to parent playbook when this sub-playbook completes."
    )
    report_template: Optional[str] = Field(
        default=None,
        description=(
            "Template string for the sub-playbook's report_to_parent. Rendered after all "
            "nodes complete, with {key} / {key.subkey} placeholders resolved against the "
            "final state (top-level + dot-notation flattening for dict values). "
            "When set, the rendered text is written to parent_state['report_to_parent'] "
            "without requiring an extra LLM call. Use this for mechanical reports "
            "(image generation results, tool outputs, etc.). For dynamic summaries "
            "that need narrative reasoning, fall back to an LLM/memorize node that "
            "writes report_to_parent into the state directly."
        ),
    )
    context_requirements: Optional[ContextRequirements] = Field(
        default=None,
        description="Context requirements for this playbook. If not specified, uses full context (backward compatible)."
    )
    router_callable: bool = Field(
        default=False,
        description="If true, this playbook can be called from the router in meta playbooks."
    )
    can_run_as_child: bool = Field(
        default=False,
        description=(
            "True if this Playbook can be invoked as a child sub-playbook "
            "(via subplay node with line='sub' or /run_playbook spell). "
            "When True, the Playbook must produce report_to_parent — either via "
            "report_template or via an LLM node whose response_schema includes "
            "'report_to_parent' in properties. Enforced by a load-time validator "
            "(see _check_report_to_parent_contract)."
        ),
    )
    required_credentials: Optional[List[str]] = Field(
        default=None,
        description="List of credential types required for this playbook (e.g., ['x'], ['email']). "
                    "When set, the playbook is only available for personas that have all listed credentials configured."
    )
    user_selectable: bool = Field(
        default=False,
        description="If true, this meta playbook can be selected by user in the UI."
    )
    dev_only: bool = Field(
        default=False,
        description="If true, this playbook is only available when developer mode is enabled."
    )
    nodes: List[NodeDef]
    start_node: str

    def node_map(self):
        return {n.id: n for n in self.nodes}

    @model_validator(mode="after")
    def _check_reserved_state_namespace(self) -> "PlaybookSchema":
        """Reject playbook-declared names that would write into the ``_`` namespace.

        Write vectors covered (everything a playbook author can aim at state):

        - ``input_schema[].name``    → merged into initial state
        - ``output_schema[]``        → written back into the parent state
        - ``node.id``                → default ``output_key`` when none is given
        - ``output_key`` / ``output_keys`` / ``output_mapping`` targets
        - SET node ``assignments`` keys

        Fail-closed: a violating playbook fails to load entirely rather than
        loading with the dangerous name ignored.
        """
        def _reject(name: Any, where: str) -> None:
            if isinstance(name, str) and name.startswith(RESERVED_STATE_PREFIX):
                raise ValueError(
                    f"Playbook '{self.name}': {where} '{name}' writes into the "
                    f"reserved '_' state namespace (runtime system variables). "
                    f"Rename it without the leading underscore."
                )

        for param in self.input_schema:
            _reject(param.name, "input_schema param")
        for key in self.output_schema or []:
            _reject(key, "output_schema key")
        for node in self.nodes:
            _reject(node.id, "node id (used as default output_key)")
            _reject(getattr(node, "output_key", None), f"node '{node.id}' output_key")
            for entry in getattr(node, "output_keys", None) or []:
                if isinstance(entry, str):
                    _reject(entry, f"node '{node.id}' output_keys entry")
                elif isinstance(entry, dict):
                    for target in entry.values():
                        _reject(target, f"node '{node.id}' output_keys target")
            for target in (getattr(node, "output_mapping", None) or {}).values():
                _reject(target, f"node '{node.id}' output_mapping target")
            for key in (getattr(node, "assignments", None) or {}).keys():
                _reject(key, f"node '{node.id}' assignment key")
        return self

    @model_validator(mode="after")
    def _check_report_to_parent_contract(self) -> "PlaybookSchema":
        """Enforce: ``can_run_as_child=true`` Playbooks must emit ``report_to_parent``.

        A child Playbook (called via subplay node with ``line='sub'`` or
        ``/run_playbook`` spell) must produce ``report_to_parent`` so the
        parent line can integrate the result. Two recognized mechanisms:

        1. ``report_template`` set on the PlaybookSchema (mechanical rendering)
        2. Any LLM node whose ``response_schema`` includes ``report_to_parent``
           in its ``properties`` (LLM-generated structured output)

        Other paths (e.g. setting ``state['report_to_parent']`` directly inside
        a tool node) can't be detected statically; if a Playbook author needs
        such a path, they should still attach a ``report_template`` to satisfy
        this check.
        """
        if not self.can_run_as_child:
            return self
        if self.report_template:
            return self
        for node in self.nodes:
            if not isinstance(node, LLMNodeDef):
                continue
            schema = node.response_schema or {}
            properties = schema.get("properties") if isinstance(schema, dict) else None
            if isinstance(properties, dict) and "report_to_parent" in properties:
                return self
        raise ValueError(
            f"Playbook '{self.name}' has can_run_as_child=true but produces no "
            f"report_to_parent. Add a `report_template` to the playbook, or include "
            f"'report_to_parent' in an LLM node's response_schema.properties."
        )


class PlaybookValidationError(ValueError):
    """Raised when a Playbook graph is invalid (unreachable nodes, cycles, etc.)."""


def validate_playbook_graph(playbook: PlaybookSchema) -> None:
    node_map = playbook.node_map()
    start_id = playbook.start_node
    if start_id not in node_map:
        raise PlaybookValidationError(f"start_node '{start_id}' is not defined in nodes")

    # Collect all edges (including conditional ones)
    all_edges: Dict[str, List[Optional[str]]] = {}
    for node in playbook.nodes:
        edges: List[Optional[str]] = []

        # Check conditional_next first (takes precedence over next)
        conditional_next = getattr(node, "conditional_next", None)
        if conditional_next:
            for target in conditional_next.cases.values():
                if target is not None and target not in node_map:
                    raise PlaybookValidationError(
                        f"Node '{node.id}' conditional_next references missing target '{target}'"
                    )
                edges.append(target)
        else:
            # Use regular next
            next_id = getattr(node, "next", None)
            if next_id is not None:
                if next_id not in node_map:
                    raise PlaybookValidationError(
                        f"Node '{node.id}' references missing next '{next_id}'"
                    )
                edges.append(next_id)

        all_edges[node.id] = edges

    # BFS to find all reachable nodes (avoiding cycle check for branching graphs)
    visited: Set[str] = set()
    queue = [start_id]

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        for next_id in all_edges.get(current, []):
            if next_id is not None and next_id not in visited:
                queue.append(next_id)

    unreachable = [node_id for node_id in node_map.keys() if node_id not in visited]
    if unreachable:
        raise PlaybookValidationError("Unreachable node(s): " + ", ".join(unreachable))
