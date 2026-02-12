#!/usr/bin/env python
"""Playbook Dry-Run Analysis Tool.

Statically analyze SAIVerse playbook execution flow WITHOUT calling LLMs.
Simulates state propagation through the node graph, tracking variables,
context profiles, intermediate messages, and memorize operations to detect
potential issues at design time.

Usage:
  python scripts/playbook_dry_run.py deep_research
  python scripts/playbook_dry_run.py builtin_data/playbooks/public/novel_writing.json
  python scripts/playbook_dry_run.py --all
  python scripts/playbook_dry_run.py deep_research --verbose
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Bootstrap: add project root to sys.path
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sea.playbook_models import (  # noqa: E402
    CONTEXT_PROFILES,
    ConditionalNext,
    ExecNodeDef,
    LLMNodeDef,
    MemorizeNodeDef,
    NodeType,
    PassNodeDef,
    PlaybookSchema,
    PlaybookValidationError,
    SayNodeDef,
    SetNodeDef,
    SpeakNodeDef,
    StelisEndNodeDef,
    StelisStartNodeDef,
    SubPlayNodeDef,
    ThinkNodeDef,
    ToolCallNodeDef,
    ToolNodeDef,
    validate_playbook_graph,
)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

# Full-context profile names (history_depth="full")
_FULL_CONTEXT_PROFILES = frozenset(
    name for name, prof in CONTEXT_PROFILES.items()
    if prof["requirements"].history_depth == "full"
)


@dataclass
class VarInfo:
    """Provenance information for a simulated state variable."""
    name: str
    source_node: str          # Node ID that set this
    source_type: str          # e.g. "set", "llm_output_key", "tool_output_key", ...
    is_structured: bool = False
    children: List[str] = field(default_factory=list)


@dataclass
class Diagnostic:
    """A warning or informational message produced during analysis."""
    level: str          # "WARN" or "INFO"
    code: str           # e.g. "UNDEF_VAR"
    message: str
    node_id: str
    playbook_name: str = ""


@dataclass
class ContextState:
    """Tracks LLM context visibility within a single playbook execution."""
    cached_profiles: Dict[str, str] = field(default_factory=dict)
    intermediate_msg_sources: List[str] = field(default_factory=list)
    # output_key set by each LLM node in _intermediate_msgs
    intermediate_output_keys: Dict[str, str] = field(default_factory=dict)  # output_key -> node_id
    memorize_since_cache: Dict[str, List[str]] = field(default_factory=dict)

    def deep_copy(self) -> "ContextState":
        return ContextState(
            cached_profiles=dict(self.cached_profiles),
            intermediate_msg_sources=list(self.intermediate_msg_sources),
            intermediate_output_keys=dict(self.intermediate_output_keys),
            memorize_since_cache={k: list(v) for k, v in self.memorize_since_cache.items()},
        )


@dataclass
class NodeReport:
    """Analysis result for one node in the execution trace."""
    step: int
    node_id: str
    node_type: str
    details: List[str] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)
    next_label: str = ""
    indent: int = 0          # for sub-playbook nesting
    path_label: str = ""     # branch identifier


# ---------------------------------------------------------------------------
# Template variable extraction
# ---------------------------------------------------------------------------

_TEMPLATE_RE = re.compile(r"\{([\w.]+)\}")


def _extract_refs(template: Optional[str]) -> List[str]:
    """Extract {variable_name} references from a template string."""
    if not template:
        return []
    return _TEMPLATE_RE.findall(template)


# ---------------------------------------------------------------------------
# JSON Schema key extraction
# ---------------------------------------------------------------------------

def _schema_keys(schema: Optional[Dict[str, Any]], prefix: str = "") -> List[str]:
    """Extract property key paths from a JSON Schema (for response_schema)."""
    if not schema:
        return []
    keys: List[str] = []
    if schema.get("type") == "object" and "properties" in schema:
        for prop, sub in schema["properties"].items():
            full = f"{prefix}.{prop}" if prefix else prop
            keys.append(full)
            keys.extend(_schema_keys(sub, full))
    return keys


# ---------------------------------------------------------------------------
# Builtin state variables (always present)
# ---------------------------------------------------------------------------

BUILTIN_VARS = {"input", "last", "persona_id", "persona_name", "pulse_id", "pulse_type"}


# ---------------------------------------------------------------------------
# Main analyser
# ---------------------------------------------------------------------------

class PlaybookAnalyzer:
    def __init__(
        self,
        playbook_dirs: List[Path],
        *,
        recursive: bool = True,
        max_loop: int = 2,
        verbose: bool = False,
    ):
        self.playbook_dirs = playbook_dirs
        self.recursive = recursive
        self.max_loop = max_loop
        self.verbose = verbose
        self._cache: Dict[str, PlaybookSchema] = {}
        self._analysis_stack: List[str] = []
        self._analyzed_subplaybooks: Set[str] = set()  # dedup sub-playbook diagnostics

    # -- Loading -----------------------------------------------------------

    def load_playbook(self, name_or_path: str) -> Optional[PlaybookSchema]:
        """Load a playbook by name or file path.  Returns None if not found."""
        # Already cached?
        if name_or_path in self._cache:
            return self._cache[name_or_path]

        path = Path(name_or_path)
        if path.is_file():
            return self._load_from_file(path, name_or_path)

        # Search directories by name
        for d in self.playbook_dirs:
            for candidate in [
                d / f"{name_or_path}.json",
                d / f"{name_or_path}_playbook.json",
            ]:
                if candidate.is_file():
                    return self._load_from_file(candidate, name_or_path)
        return None

    def _load_from_file(self, path: Path, cache_key: str) -> Optional[PlaybookSchema]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pb = PlaybookSchema(**data)
            validate_playbook_graph(pb)
            self._cache[cache_key] = pb
            self._cache[pb.name] = pb
            return pb
        except (json.JSONDecodeError, PlaybookValidationError, Exception) as exc:
            print(f"  ERROR loading {path}: {exc}", file=sys.stderr)
            return None

    # -- Analysis entry point -----------------------------------------------

    def analyze(
        self,
        playbook: PlaybookSchema,
        *,
        parent_vars: Optional[Dict[str, VarInfo]] = None,
        indent: int = 0,
    ) -> Tuple[List[NodeReport], List[Diagnostic]]:
        """Analyze a playbook.  Returns (reports, all_diagnostics)."""
        # Reset sub-playbook dedup tracker on each top-level analysis
        if parent_vars is None:
            self._analyzed_subplaybooks = set()

        # Build initial known_vars
        known: Dict[str, VarInfo] = {}
        for name in BUILTIN_VARS:
            known[name] = VarInfo(name, "(builtin)", "initial_state")

        # input_schema vars
        for param in playbook.input_schema:
            vi = VarInfo(param.name, "(input_schema)", "input_schema")
            known[param.name] = vi
            if parent_vars and param.name in parent_vars:
                known[param.name] = parent_vars[param.name]

        # Parent-provided vars
        if parent_vars:
            for k, v in parent_vars.items():
                if k not in known:
                    known[k] = v

        ctx = ContextState()
        reports: List[NodeReport] = []
        diags: List[Diagnostic] = []
        # Collect all vars set across ALL paths (for output_schema check)
        all_vars_seen: Set[str] = set()
        self._walk(
            playbook, playbook.start_node, known, ctx,
            reports=reports, diags=diags,
            indent=indent, path_label="", step_counter=[0],
            visit_counts={}, all_vars_seen=all_vars_seen,
        )

        # Check output_schema: are declared keys set in ANY path?
        if playbook.output_schema:
            for key in playbook.output_schema:
                if key not in known and key not in all_vars_seen:
                    d = Diagnostic("WARN", "OUTPUT_SCHEMA_UNSET",
                                   f"output_schema key '{key}' is never set by any node in this playbook",
                                   "(playbook)", playbook.name)
                    diags.append(d)

        return reports, diags

    # -- Graph walking ------------------------------------------------------

    def _walk(
        self,
        playbook: PlaybookSchema,
        node_id: Optional[str],
        known: Dict[str, VarInfo],
        ctx: ContextState,
        *,
        reports: List[NodeReport],
        diags: List[Diagnostic],
        indent: int,
        path_label: str,
        step_counter: List[int],
        visit_counts: Dict[str, int],
        all_vars_seen: Set[str],
    ):
        node_map = playbook.node_map()

        while node_id is not None:
            if node_id not in node_map:
                break

            # Loop detection
            vc = visit_counts.get(node_id, 0)
            if vc >= self.max_loop:
                step_counter[0] += 1
                reports.append(NodeReport(
                    step=step_counter[0], node_id=node_id, node_type="(loop)",
                    details=[f"[LOOP LIMIT: node '{node_id}' visited {vc} times, stopping]"],
                    indent=indent, path_label=path_label,
                ))
                return
            visit_counts[node_id] = vc + 1

            node_def = node_map[node_id]
            before_keys = set(known.keys())
            step_counter[0] += 1
            report = self._analyze_node(
                node_def, playbook, known, ctx,
                step=step_counter[0], indent=indent, path_label=path_label,
            )
            reports.append(report)
            diags.extend(report.diagnostics)

            # Track all vars set across all paths
            new_keys = set(known.keys()) - before_keys
            all_vars_seen.update(new_keys)

            # Handle branching
            cond_next = getattr(node_def, "conditional_next", None)
            error_next = getattr(node_def, "error_next", None) if isinstance(node_def, ExecNodeDef) else None

            if cond_next and isinstance(cond_next, ConditionalNext):
                for case_val, target in cond_next.cases.items():
                    if target is None:
                        step_counter[0] += 1
                        reports.append(NodeReport(
                            step=step_counter[0], node_id=f"(END via {node_id}[{case_val}])",
                            node_type="END", indent=indent, path_label=path_label,
                        ))
                        continue
                    branch_label = f"{path_label}/{node_id}[{case_val}]" if path_label else f"{node_id}[{case_val}]"
                    self._walk(
                        playbook, target,
                        copy.deepcopy(known), ctx.deep_copy(),
                        reports=reports, diags=diags,
                        indent=indent, path_label=branch_label,
                        step_counter=step_counter,
                        visit_counts=dict(visit_counts),
                        all_vars_seen=all_vars_seen,
                    )
                return

            if error_next:
                # Fork: success path and error path
                success_next = getattr(node_def, "next", None)
                if success_next:
                    success_label = f"{path_label}/{node_id}[success]" if path_label else f"{node_id}[success]"
                    self._walk(
                        playbook, success_next,
                        copy.deepcopy(known), ctx.deep_copy(),
                        reports=reports, diags=diags,
                        indent=indent, path_label=success_label,
                        step_counter=step_counter,
                        visit_counts=dict(visit_counts),
                        all_vars_seen=all_vars_seen,
                    )
                error_label = f"{path_label}/{node_id}[error]" if path_label else f"{node_id}[error]"
                self._walk(
                    playbook, error_next,
                    copy.deepcopy(known), ctx.deep_copy(),
                    reports=reports, diags=diags,
                    indent=indent, path_label=error_label,
                    step_counter=step_counter,
                    visit_counts=dict(visit_counts),
                    all_vars_seen=all_vars_seen,
                )
                return

            # Normal next
            next_id = getattr(node_def, "next", None)
            report.next_label = next_id or "END"
            node_id = next_id

    # -- Per-node analysis --------------------------------------------------

    def _analyze_node(
        self,
        node_def: Any,
        playbook: PlaybookSchema,
        known: Dict[str, VarInfo],
        ctx: ContextState,
        *,
        step: int,
        indent: int,
        path_label: str,
    ) -> NodeReport:
        node_id = node_def.id
        node_type_str = node_def.type.value if hasattr(node_def.type, "value") else str(node_def.type)
        report = NodeReport(
            step=step, node_id=node_id, node_type=node_type_str,
            indent=indent, path_label=path_label,
        )

        ntype = node_def.type

        if ntype == NodeType.SET:
            self._sim_set(node_def, known, report, playbook)
        elif ntype == NodeType.LLM:
            self._sim_llm(node_def, known, ctx, report, playbook)
        elif ntype == NodeType.TOOL:
            self._sim_tool(node_def, known, report, playbook)
        elif ntype == NodeType.TOOL_CALL:
            self._sim_tool_call(node_def, known, report, playbook)
        elif ntype == NodeType.MEMORY:
            self._sim_memorize(node_def, known, ctx, report, playbook)
        elif ntype == NodeType.SUBPLAY:
            self._sim_subplay(node_def, known, ctx, report, playbook, indent)
        elif ntype == NodeType.EXEC:
            self._sim_exec(node_def, known, ctx, report, playbook)
        elif ntype in (NodeType.SPEAK, NodeType.SAY, NodeType.THINK):
            self._sim_output(node_def, known, report, playbook)
        elif ntype == NodeType.PASS:
            self._sim_pass(node_def, known, report, playbook)
        elif ntype == NodeType.STELIS_START:
            report.details.append("Stelis thread start")
        elif ntype == NodeType.STELIS_END:
            report.details.append("Stelis thread end")
            known["_subagent_chronicle"] = VarInfo("_subagent_chronicle", node_id, "stelis_end")

        # Report next
        cond = getattr(node_def, "conditional_next", None)
        nxt = getattr(node_def, "next", None)
        if cond and isinstance(cond, ConditionalNext):
            cases_str = ", ".join(f"{k}->{v}" for k, v in cond.cases.items())
            report.next_label = f"BRANCH on {cond.field}: {cases_str}"
            # Check conditional field is defined
            self._check_var_defined(cond.field, known, report, playbook, "UNDEF_CONDITIONAL",
                                    f"conditional_next.field '{cond.field}'")
        elif nxt:
            report.next_label = nxt
        else:
            report.next_label = "END"

        return report

    # -- SET ----------------------------------------------------------------

    def _sim_set(self, node_def: SetNodeDef, known: Dict[str, VarInfo],
                 report: NodeReport, playbook: PlaybookSchema):
        assignments = node_def.assignments or {}
        sets: List[str] = []
        for key, val in assignments.items():
            if isinstance(val, str):
                refs = _extract_refs(val)
                for ref in refs:
                    self._check_var_defined(ref, known, report, playbook, "UNDEF_VAR",
                                            f"SET assignment '{key}' references '{{{ref}}}'")
            known[key] = VarInfo(key, node_def.id, "set")
            val_repr = json.dumps(val, ensure_ascii=False) if not isinstance(val, str) else f'"{val}"'
            sets.append(f"{key} <- {val_repr}")
        report.details.append(f"Sets: {', '.join(sets)}" if sets else "Sets: (none)")

    # -- LLM ----------------------------------------------------------------

    def _sim_llm(self, node_def: LLMNodeDef, known: Dict[str, VarInfo],
                 ctx: ContextState, report: NodeReport, playbook: PlaybookSchema):
        node_id = node_def.id
        profile = getattr(node_def, "context_profile", None)
        action = getattr(node_def, "action", None)
        response_schema = getattr(node_def, "response_schema", None)
        output_key = getattr(node_def, "output_key", None) or node_id
        memorize_cfg = getattr(node_def, "memorize", None)
        model_type_field = getattr(node_def, "model_type", None)

        # Context profile
        if profile:
            prof_info = CONTEXT_PROFILES.get(profile, {})
            mt = prof_info.get("model_type", "normal") if prof_info else "normal"
            if profile in ctx.cached_profiles:
                report.details.append(f"Profile: {profile} (CACHED from {ctx.cached_profiles[profile]}), model={mt}")
                report.diagnostics.append(Diagnostic(
                    "INFO", "PROFILE_REUSE",
                    f"Context profile '{profile}' reused from cache (built at '{ctx.cached_profiles[profile]}'). "
                    f"_intermediate_msgs has {len(ctx.intermediate_msg_sources)} prior LLM output(s).",
                    node_id, playbook.name,
                ))
                # Check STALE_MEMORIZE
                stale = ctx.memorize_since_cache.get(profile, [])
                if stale:
                    report.diagnostics.append(Diagnostic(
                        "WARN", "STALE_MEMORIZE",
                        f"Standalone MEMORIZE node(s) {stale} occurred since profile '{profile}' was cached at "
                        f"'{ctx.cached_profiles[profile]}'. Their content is NOT in base context or _intermediate_msgs.",
                        node_id, playbook.name,
                    ))
            else:
                report.details.append(f"Profile: {profile} (FRESH), model={mt}")
                ctx.cached_profiles[profile] = node_id
                ctx.memorize_since_cache[profile] = []
        else:
            report.details.append("Profile: (none) - uses state['messages']")
            if model_type_field:
                report.details.append(f"Model type: {model_type_field}")
            report.diagnostics.append(Diagnostic(
                "WARN", "NO_CONTEXT_PROFILE",
                "LLM node has no context_profile. Uses playbook-level context_requirements or state['messages'].",
                node_id, playbook.name,
            ))

        # Action template
        if action:
            truncated = action[:80].replace("\n", "\\n") + ("..." if len(action) > 80 else "")
            report.details.append(f"Action: \"{truncated}\"")
            refs = _extract_refs(action)
            if refs:
                ref_status = []
                for ref in refs:
                    ok = self._is_var_defined(ref, known)
                    ref_status.append(f"{ref} [{'OK' if ok else 'UNDEF'}]")
                    if not ok:
                        report.diagnostics.append(Diagnostic(
                            "WARN", "UNDEF_VAR",
                            f"Template reference '{{{ref}}}' is not set by any prior node",
                            node_id, playbook.name,
                        ))
                report.details.append(f"  Template refs: {', '.join(ref_status)}")

                # REDUNDANT_INPUT check
                if profile in _FULL_CONTEXT_PROFILES and "input" in refs:
                    report.diagnostics.append(Diagnostic(
                        "WARN", "REDUNDANT_INPUT",
                        f"Action uses '{{input}}' but profile '{profile}' already includes full conversation "
                        f"history (user's input is already present as the latest message).",
                        node_id, playbook.name,
                    ))

                # REDUNDANT_INTERMEDIATE check
                if profile in _FULL_CONTEXT_PROFILES:
                    for ref in refs:
                        base_key = ref.split(".")[0]
                        if base_key in ctx.intermediate_output_keys:
                            source_node = ctx.intermediate_output_keys[base_key]
                            report.diagnostics.append(Diagnostic(
                                "INFO", "REDUNDANT_INTERMEDIATE",
                                f"Action references '{{{ref}}}' (from {source_node}'s output_key). "
                                f"That LLM's response is already in _intermediate_msgs.",
                                node_id, playbook.name,
                            ))
        else:
            report.details.append("Action: (none)")

        # Response schema
        if response_schema:
            props = list(response_schema.get("properties", {}).keys())
            report.details.append(f"Response Schema: {{{', '.join(props)}}}")

        # Output key & variables produced
        report.details.append(f"Output Key: {output_key}")
        known[output_key] = VarInfo(output_key, node_id, "llm_output_key", is_structured=bool(response_schema))
        known["last"] = VarInfo("last", node_id, "llm")

        if response_schema:
            child_keys = _schema_keys(response_schema, output_key)
            for ck in child_keys:
                known[ck] = VarInfo(ck, node_id, "llm_response_schema")
            if child_keys:
                report.details.append(f"  Schema keys: {', '.join(child_keys)}")

        # output_keys (text/function_call mapping)
        output_keys_spec = getattr(node_def, "output_keys", None)
        if output_keys_spec:
            for mapping in output_keys_spec:
                if isinstance(mapping, dict):
                    for _type, var_name in mapping.items():
                        known[var_name] = VarInfo(var_name, node_id, f"llm_output_keys.{_type}")
                        report.details.append(f"  output_keys: {_type} -> {var_name}")

        # output_mapping: copies structured output fields to top-level state vars
        output_mapping = getattr(node_def, "output_mapping", None)
        if output_mapping and isinstance(output_mapping, dict):
            for source_path, target_key in output_mapping.items():
                known[target_key] = VarInfo(target_key, node_id, f"output_mapping({output_key}.{source_path})")

        # _intermediate_msgs tracking
        ctx.intermediate_msg_sources.append(node_id)
        ctx.intermediate_output_keys[output_key] = node_id
        report.details.append(f"_intermediate_msgs: [{', '.join(ctx.intermediate_msg_sources)}]")

        # Memorize
        if memorize_cfg:
            tags = memorize_cfg.get("tags", []) if isinstance(memorize_cfg, dict) else []
            report.details.append(f"Memorize: SAIMemory {tags}")

    # -- TOOL ---------------------------------------------------------------

    def _sim_tool(self, node_def: ToolNodeDef, known: Dict[str, VarInfo],
                  report: NodeReport, playbook: PlaybookSchema):
        node_id = node_def.id
        tool_name = node_def.action
        report.details.append(f"Tool: {tool_name}")

        args_input = getattr(node_def, "args_input", None) or {}
        if args_input:
            for arg_name, source in args_input.items():
                if isinstance(source, str):
                    ok = self._is_var_defined(source, known)
                    report.details.append(f"  args_input: {arg_name} <- state[\"{source}\"] [{'OK' if ok else 'UNDEF'}]")
                    if not ok:
                        report.diagnostics.append(Diagnostic(
                            "WARN", "UNDEF_ARGS_INPUT",
                            f"args_input['{arg_name}'] references state['{source}'] which is not set by any prior node",
                            node_id, playbook.name,
                        ))
                else:
                    report.details.append(f"  args_input: {arg_name} <- (literal) {source}")

        output_key = getattr(node_def, "output_key", None)
        output_keys = getattr(node_def, "output_keys", None)
        sets: List[str] = []
        if output_keys:
            for k in output_keys:
                if isinstance(k, str):
                    known[k] = VarInfo(k, node_id, "tool_output_keys")
                    sets.append(k)
        if output_key:
            known[output_key] = VarInfo(output_key, node_id, "tool_output_key")
            sets.append(output_key)
        known["last"] = VarInfo("last", node_id, "tool")
        sets.append("last")
        report.details.append(f"Sets: {', '.join(sets)}")

    # -- TOOL_CALL ----------------------------------------------------------

    def _sim_tool_call(self, node_def: ToolCallNodeDef, known: Dict[str, VarInfo],
                       report: NodeReport, playbook: PlaybookSchema):
        node_id = node_def.id
        call_source = getattr(node_def, "call_source", "fc")
        report.details.append(f"Call source: {call_source}")
        # Check that call_source.name and call_source.args exist
        for suffix in [f"{call_source}.name", f"{call_source}.args"]:
            self._check_var_defined(suffix, known, report, playbook, "UNDEF_VAR",
                                    f"tool_call requires '{suffix}'")

        output_key = getattr(node_def, "output_key", None)
        if output_key:
            known[output_key] = VarInfo(output_key, node_id, "tool_call_output_key")
            report.details.append(f"Output Key: {output_key}")
        known["last"] = VarInfo("last", node_id, "tool_call")

    # -- MEMORIZE -----------------------------------------------------------

    def _sim_memorize(self, node_def: MemorizeNodeDef, known: Dict[str, VarInfo],
                      ctx: ContextState, report: NodeReport, playbook: PlaybookSchema):
        node_id = node_def.id
        action = getattr(node_def, "action", None) or "{last}"
        tags = getattr(node_def, "tags", None) or []
        role = getattr(node_def, "role", "assistant")

        report.details.append(f"Role: {role}, Tags: {tags}")

        refs = _extract_refs(action)
        if refs:
            ref_status = []
            for ref in refs:
                ok = self._is_var_defined(ref, known)
                ref_status.append(f"{ref} [{'OK' if ok else 'UNDEF'}]")
                if not ok:
                    report.diagnostics.append(Diagnostic(
                        "WARN", "UNDEF_VAR",
                        f"MEMORIZE template reference '{{{ref}}}' is not set by any prior node",
                        node_id, playbook.name,
                    ))
            truncated = action[:60].replace("\n", "\\n") + ("..." if len(action) > 60 else "")
            report.details.append(f"Action: \"{truncated}\"")
            report.details.append(f"  Template refs: {', '.join(ref_status)}")

        report.details.append("-> SAIMemory (standalone MEMORIZE, NOT added to _intermediate_msgs)")

        # Track standalone memorize for STALE_MEMORIZE detection
        for profile_name in ctx.cached_profiles:
            ctx.memorize_since_cache.setdefault(profile_name, []).append(node_id)

        known["last"] = VarInfo("last", node_id, "memorize")

    # -- SUBPLAY ------------------------------------------------------------

    def _sim_subplay(self, node_def: SubPlayNodeDef, known: Dict[str, VarInfo],
                     ctx: ContextState, report: NodeReport, playbook: PlaybookSchema,
                     parent_indent: int):
        node_id = node_def.id
        sub_name = node_def.playbook
        execution = getattr(node_def, "execution", "inline") or "inline"
        propagate = getattr(node_def, "propagate_output", False)
        template = getattr(node_def, "input_template", "{input}") or "{input}"

        report.details.append(f"Sub-playbook: {sub_name}")
        report.details.append(f"Execution: {execution}")
        if propagate:
            report.details.append("propagate_output: true")

        # Check input_template refs
        refs = _extract_refs(template)
        if refs:
            ref_status = []
            for ref in refs:
                ok = self._is_var_defined(ref, known)
                ref_status.append(f"{ref} [{'OK' if ok else 'UNDEF'}]")
                if not ok:
                    report.diagnostics.append(Diagnostic(
                        "WARN", "UNDEF_VAR",
                        f"input_template reference '{{{ref}}}' is not set by any prior node",
                        node_id, playbook.name,
                    ))
            report.details.append(f"  Input refs: {', '.join(ref_status)}")

        report.details.append("INFO: Sub-playbook executes with fresh state (new _intermediate_msgs, no cached profiles)")

        # Recursive analysis
        if self.recursive:
            sub_pb = self.load_playbook(sub_name)
            if sub_pb is None:
                report.diagnostics.append(Diagnostic(
                    "WARN", "SUBPLAY_NOT_FOUND",
                    f"Sub-playbook '{sub_name}' not found in any playbook directory",
                    node_id, playbook.name,
                ))
            elif sub_name in self._analysis_stack:
                report.details.append(f"  [RECURSIVE: '{sub_name}' already in analysis stack, skipping]")
            elif sub_name in self._analyzed_subplaybooks:
                # Already analyzed in this session; propagate output vars without re-emitting diagnostics
                report.details.append(f"  [ALREADY ANALYZED: '{sub_name}' — diagnostics reported on first encounter]")
                if sub_pb.output_schema:
                    for key in sub_pb.output_schema:
                        known[key] = VarInfo(key, node_id, f"subplay_output({sub_name})")
                    report.details.append(f"  Output schema: [{', '.join(sub_pb.output_schema)}]")
            else:
                self._analysis_stack.append(sub_name)
                self._analyzed_subplaybooks.add(sub_name)
                sub_reports, sub_diags = self.analyze(sub_pb, parent_vars=known, indent=parent_indent + 1)
                report.details.append(f"  --- Sub-playbook: {sub_name} ({len(sub_reports)} nodes) ---")
                # Embed sub-reports
                for sr in sub_reports:
                    sr.indent = parent_indent + 1
                report.details.extend(
                    [f"    [{sr.step}] {sr.node_id} ({sr.node_type})" for sr in sub_reports[:5]]
                )
                if len(sub_reports) > 5:
                    report.details.append(f"    ... ({len(sub_reports) - 5} more nodes)")

                # Propagate sub-playbook diagnostics
                report.diagnostics.extend(sub_diags)

                # Propagate output_schema
                if sub_pb.output_schema:
                    for key in sub_pb.output_schema:
                        known[key] = VarInfo(key, node_id, f"subplay_output({sub_name})")
                    report.details.append(f"  Output schema: [{', '.join(sub_pb.output_schema)}]")
                else:
                    report.details.append("  Output schema: (none)")

                self._analysis_stack.pop()

        known["last"] = VarInfo("last", node_id, "subplay")

        # Runtime sets _subagent_chronicle when execution == "subagent"
        if execution == "subagent":
            known["_subagent_chronicle"] = VarInfo("_subagent_chronicle", node_id, "subagent_end")

    # -- EXEC ---------------------------------------------------------------

    def _sim_exec(self, node_def: ExecNodeDef, known: Dict[str, VarInfo],
                  ctx: ContextState, report: NodeReport, playbook: PlaybookSchema):
        node_id = node_def.id
        pb_source = getattr(node_def, "playbook_source", "selected_playbook") or "selected_playbook"
        args_source = getattr(node_def, "args_source", "selected_args") or "selected_args"
        error_next = getattr(node_def, "error_next", None)

        report.details.append(f"Playbook source: state[\"{pb_source}\"] [DYNAMIC]")
        report.details.append(f"Args source: state[\"{args_source}\"]")
        if error_next:
            report.details.append(f"Error next: {error_next}")
        report.details.append("INFO: Executed playbook has its own state (new _intermediate_msgs, no cached profiles)")
        report.details.append("INFO: Output schema depends on which playbook is executed at runtime")

        self._check_var_defined(pb_source, known, report, playbook, "UNDEF_VAR",
                                f"EXEC playbook_source '{pb_source}'")

        known["last"] = VarInfo("last", node_id, "exec")
        known["_exec_error"] = VarInfo("_exec_error", node_id, "exec")

        # Runtime sets _subagent_chronicle when execution == "subagent"
        exec_mode = getattr(node_def, "execution", "inline") or "inline"
        if exec_mode == "subagent":
            known["_subagent_chronicle"] = VarInfo("_subagent_chronicle", node_id, "subagent_end")

    # -- SPEAK / SAY / THINK ------------------------------------------------

    def _sim_output(self, node_def: Any, known: Dict[str, VarInfo],
                    report: NodeReport, playbook: PlaybookSchema):
        action = getattr(node_def, "action", None)
        if action:
            refs = _extract_refs(action)
            for ref in refs:
                if not self._is_var_defined(ref, known):
                    report.diagnostics.append(Diagnostic(
                        "WARN", "UNDEF_VAR",
                        f"Template reference '{{{ref}}}' is not set by any prior node",
                        node_def.id, playbook.name,
                    ))
            truncated = action[:60].replace("\n", "\\n") + ("..." if len(action) > 60 else "")
            report.details.append(f"Action: \"{truncated}\"")
        else:
            report.details.append("Action: (uses last)")

    # -- PASS ---------------------------------------------------------------

    def _sim_pass(self, node_def: PassNodeDef, known: Dict[str, VarInfo],
                  report: NodeReport, playbook: PlaybookSchema):
        report.details.append("(pass-through)")

    # -- Helpers ------------------------------------------------------------

    def _is_var_defined(self, ref: str, known: Dict[str, VarInfo]) -> bool:
        """Check if a variable reference (possibly with dot notation) is resolvable."""
        if ref in known:
            return True
        if ref in BUILTIN_VARS:
            return True
        # Check if base key exists (for dot notation like foo.bar)
        parts = ref.split(".")
        if len(parts) > 1 and parts[0] in known:
            base_var = known[parts[0]]
            # If the base is structured, children are expected
            if base_var.is_structured:
                return True
            # Even if not marked structured, accept it (runtime resolves dynamically)
            return True
        return False

    def _check_var_defined(self, ref: str, known: Dict[str, VarInfo],
                           report: NodeReport, playbook: PlaybookSchema,
                           code: str, context: str):
        if not self._is_var_defined(ref, known):
            report.diagnostics.append(Diagnostic(
                "WARN", code,
                f"{context} is not set by any prior node",
                report.node_id, playbook.name,
            ))


# ---------------------------------------------------------------------------
# Report formatter
# ---------------------------------------------------------------------------

def format_report(
    playbook: PlaybookSchema,
    source_path: str,
    reports: List[NodeReport],
    diags: List[Diagnostic],
    *,
    verbose: bool = False,
) -> str:
    lines: List[str] = []
    sep = "=" * 70
    lines.append(sep)
    lines.append(f"Playbook Analysis: {playbook.name}")
    lines.append(sep)
    lines.append(f"Source: {source_path}")
    desc = playbook.description[:100] + ("..." if len(playbook.description) > 100 else "")
    lines.append(f"Description: {desc}")
    inputs = ", ".join(p.name for p in playbook.input_schema) or "(none)"
    lines.append(f"Input Schema: {inputs}")
    outputs = ", ".join(playbook.output_schema) if playbook.output_schema else "(none)"
    lines.append(f"Output Schema: {outputs}")
    lines.append("")
    lines.append("--- Execution Trace ---")
    lines.append("")

    for r in reports:
        prefix = "  " * r.indent
        path_str = f" [{r.path_label}]" if r.path_label else ""
        lines.append(f"{prefix}[{r.step}] {r.node_id} ({r.node_type}){path_str}")
        if verbose or r.node_type == "(loop)":
            for d in r.details:
                lines.append(f"{prefix}    {d}")
        else:
            # Show condensed details
            for d in r.details:
                if d.startswith("  "):  # sub-details
                    continue
                lines.append(f"{prefix}    {d}")

        # Show inline diagnostics for this node
        for diag in r.diagnostics:
            if diag.playbook_name == (playbook.name if hasattr(playbook, 'name') else ""):
                marker = "WARN" if diag.level == "WARN" else "INFO"
                lines.append(f"{prefix}    {marker} {diag.code}: {diag.message}")

        if r.next_label:
            lines.append(f"{prefix}    -> {r.next_label}")
        lines.append("")

    # Warnings summary
    lines.append("--- Diagnostics Summary ---")
    warns = [d for d in diags if d.level == "WARN"]
    infos = [d for d in diags if d.level == "INFO"]
    if not warns and not infos:
        lines.append("No issues found.")
    else:
        for i, d in enumerate(warns + infos, 1):
            lines.append(f"  {i}. {d.level} {d.code} [{d.playbook_name}/{d.node_id}]: {d.message}")
    lines.append(f"\nTotal: {len(warns)} warning(s), {len(infos)} info(s)")
    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    # Ensure UTF-8 output on Windows (cp932 can't handle some characters)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Dry-run analysis of SAIVerse playbook execution flow.",
    )
    parser.add_argument(
        "target", nargs="?",
        help="Playbook name or JSON file path",
    )
    parser.add_argument(
        "--playbook-dir", action="append", dest="playbook_dirs",
        help="Directory to search for playbook JSONs (can be specified multiple times)",
    )
    parser.add_argument(
        "--no-recursive", action="store_true",
        help="Don't recursively analyze sub-playbooks",
    )
    parser.add_argument(
        "--max-loop", type=int, default=2,
        help="Max iterations to unroll loops (default: 2)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show detailed state at each node",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Analyze all playbooks in builtin_data/playbooks/public/",
    )

    args = parser.parse_args()

    if not args.target and not args.all:
        parser.error("Either specify a playbook target or use --all")

    # Resolve playbook directories
    default_dir = ROOT / "builtin_data" / "playbooks" / "public"
    if args.playbook_dirs:
        dirs = [Path(d) for d in args.playbook_dirs]
    else:
        dirs = [default_dir]

    analyzer = PlaybookAnalyzer(
        dirs,
        recursive=not args.no_recursive,
        max_loop=args.max_loop,
        verbose=args.verbose,
    )

    if args.all:
        _run_all(analyzer, default_dir, args.verbose)
    else:
        _run_single(analyzer, args.target, args.verbose)


def _run_single(analyzer: PlaybookAnalyzer, target: str, verbose: bool):
    pb = analyzer.load_playbook(target)
    if pb is None:
        print(f"ERROR: Could not load playbook '{target}'", file=sys.stderr)
        sys.exit(1)

    reports, diags = analyzer.analyze(pb)
    output = format_report(pb, target, reports, diags, verbose=verbose)
    print(output)

    warns = sum(1 for d in diags if d.level == "WARN")
    sys.exit(1 if warns > 0 else 0)


def _run_all(analyzer: PlaybookAnalyzer, directory: Path, verbose: bool):
    files = sorted(directory.glob("*.json"))
    if not files:
        print(f"No playbook files found in {directory}", file=sys.stderr)
        sys.exit(1)

    total_warns = 0
    total_infos = 0
    summary_lines: List[str] = []

    for f in files:
        pb = analyzer.load_playbook(str(f))
        if pb is None:
            summary_lines.append(f"  SKIP {f.name}: failed to load")
            continue

        reports, diags = analyzer.analyze(pb)
        warns = [d for d in diags if d.level == "WARN"]
        infos = [d for d in diags if d.level == "INFO"]
        total_warns += len(warns)
        total_infos += len(infos)

        status = "OK" if not warns else f"{len(warns)} WARN"
        if infos:
            status += f", {len(infos)} INFO"
        summary_lines.append(f"  {pb.name:40s} {status}")

        if warns and verbose:
            for d in warns:
                summary_lines.append(f"    WARN {d.code} [{d.node_id}]: {d.message}")

    sep = "=" * 70
    print(sep)
    print("Playbook Dry-Run: All Playbooks")
    print(sep)
    print(f"Directory: {directory}")
    print(f"Playbooks: {len(files)}")
    print()
    for line in summary_lines:
        print(line)
    print()
    print(f"Total: {total_warns} warning(s), {total_infos} info(s) across {len(files)} playbooks")
    print(sep)

    sys.exit(1 if total_warns > 0 else 0)


if __name__ == "__main__":
    main()
