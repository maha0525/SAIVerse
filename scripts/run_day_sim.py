#!/usr/bin/env python
"""一日シミュレータ CLI — シナリオを回して一日レポート (一日新聞) を出す (自律行動 v2 §12)。

Usage:
  python scripts/run_day_sim.py --scenario <file.json> [--db-file <path>]
  python scripts/run_day_sim.py --scenario <file.json> --real --city city_a --db-file <path>
  python scripts/run_day_sim.py --scenario <file.json> --report-only --db-file <path>

モード (二段モード、intent §12):

- **mock (既定、安全側)**: LLM を一切呼ばない配線テスト。判断点はルールベースの
  mock 裁定 (day_open は動的 enum から時間割を機械編成、post_session は成果物が
  あれば done)、作業セッションは mock LLM (document_create スペル発動 → mock
  スペルが Item を実際に DB へ作る) で運転する。DB は --db-file (省略時
  in-memory) に最小スキーマを作る — **本番 DB を渡さないこと** (シナリオの
  ペルソナ行等を書き込む)。
- **--real (実 LLM モード)**: SAIVerseManager を実 DB で構築し (``start()`` は
  呼ばない — 背景スレッドなしの DES 単一スレッド)、判断点は実 Playbook + 実 LLM、
  セッションは実スペルで運転する。**環境の API キー (OPENAI_API_KEY /
  GEMINI_API_KEY / CLAUDE_API_KEY 等) を使い、実コストが発生する。** 一巡の
  コストはおおよそ日次予算ぶんの LLM コールになる。テスト環境
  (test_fixtures/setup_test_env.py で作った test_data/) の DB を --db-file で
  渡し、SAIVERSE_HOME / SAIVERSE_USER_DATA_DIR も test_fixtures 流儀で
  切り替えて使うこと (本番 DB / 本番ペルソナには向けない)。

- **--report-only**: シナリオは回さず、シナリオの persona_id / plan_date に
  対する一日レポートだけを既存 DB から生成する。SAIMemory 由来の節
  (セッションダイジェスト等) はペルソナ adapter が無い構成では「(なし)」になる。

レポートは stdout に出力し、--out (省略時 ~/.saiverse/personas/<id>/day_reports/
<date>.md) へも書き出す。

シナリオ JSON の形式は saiverse/day_scenario.py のモジュール docstring を参照。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import uuid
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

# プロジェクトルートを追加
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# --real で API キーが要る。main.py と同じく .env を読む (既存の環境変数は上書きしない)
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

LOGGER = logging.getLogger("scripts.run_day_sim")


# ---------------------------------------------------------------------------
# mock モードの部品 (配線テスト: LLM コストゼロ)
# ---------------------------------------------------------------------------


class ListMemoryAdapter:
    """SAIMemory adapter の最小 mock (append + tag フィルタ付き読み出し)。"""

    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []

    def get_current_thread(self) -> str:
        return "mock_thread"

    def append_persona_message(self, payload: Dict[str, Any]) -> None:
        self.messages.append(dict(payload))

    def recent_persona_messages_by_count(self, max_messages, *, required_tags=None,
                                         required_line_roles=None,
                                         required_scopes=None, pulse_id=None):
        selected = []
        for payload in self.messages:
            tags = (payload.get("metadata") or {}).get("tags") or []
            if required_tags and not all(t in tags for t in required_tags):
                continue
            selected.append(payload)
        return selected[-max_messages:]


class MockSessionLLMClient:
    """作業セッション用のルールベース mock LLM。

    直近のメッセージを見て応答を決める:
    - 指示書 (【指示書】) が来たら: 指示書に document_create の語があれば
      document_create スペルを 1 発、なければスペルなしの短い作業報告
    - ダイジェスト指示が来たら: 短い要約
    - それ以外 (スペル実行結果の確認) は: スペルなしの締めの言葉
    """

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, messages, tools=None, temperature=None, **kwargs):
        self.calls += 1
        last = str((messages or [{}])[-1].get("content") or "")
        if "作業セッションはここで終了" in last:
            return "指示書のとおりに作業した。実際に行ったことだけをここに残す。"
        if "【指示書】" in last:
            if "document_create" in last:
                title = f"mock成果物-{self.calls}"
                return (
                    "作業に取りかかる。\n"
                    f"/spell name='document_create' args={{\"title\": \"{title}\"}}"
                )
            return "資料を読み、要点を頭の中で整理した。今回はここまでにする。"
        return "結果を確認した。これで一区切りにする。"

    def consume_usage(self):
        return None


class MockWorkRuntime:
    """run_work_session が触る SEARuntime 最小 mock (tests/test_work_session.py と同型)。

    _store_memory は committed / volatile を解決しつつ、ペルソナの
    ListMemoryAdapter へ転送する (一日レポートがダイジェストを読めるように)。
    """

    def __init__(self, llm_client: MockSessionLLMClient):
        self.llm_client = llm_client
        self.session_lifecycle = SimpleNamespace(
            touch_anchor_after_llm_call=lambda persona, usage, anchor_id=None: None,
        )

    def _prepare_context(self, persona, building_id, user_input, requirements=None,
                         pulse_id=None, **kwargs):
        return [{"role": "system", "content": "HEAD (mock)"}]

    def _select_llm_client(self, node_def, persona, needs_structured_output=False,
                           state=None):
        return self.llm_client

    def select_llm_client(self, node_def, persona, execution_context=None,
                          needs_structured_output=False, state=None):
        model = execution_context.model_key if execution_context is not None else "mock-model"
        return self.llm_client, model

    def _default_temperature(self, persona):
        return None

    def _get_cache_kwargs(self, persona_id=None):
        return {}

    def _is_spell_enabled_for_persona(self, persona):
        return True

    def _dump_llm_io(self, playbook_name, node_id, persona, messages, output_text):
        return None

    def _accumulate_usage(self, state, model, input_tokens, output_tokens,
                          cost_usd, cached_tokens=0, cache_write_tokens=0):
        return None

    def _get_or_create_pulse_context(self, pulse_id):
        from sea.pulse_context import PulseContext
        return PulseContext(pulse_id=pulse_id)

    def _flush_pulse_logs(self, persona, pulse_context):
        return None

    def _store_memory(self, persona, text, *, role="assistant", tags=None,
                      pulse_id=None, metadata=None, playbook_name=None,
                      pulse_context=None, line_role=None, line_id=None,
                      origin_track_id=None, scope=None, paired_action_text=None,
                      thought_signature=None, spell_origin_id=None, spell_seq=None,
                      return_message_id=False):
        from saiverse import clock

        resolved_scope = scope
        if pulse_context is not None and resolved_scope is None:
            resolved_scope = pulse_context.current_line_metadata().get("scope")
        adapter = getattr(persona, "sai_memory", None)
        if adapter is not None and hasattr(adapter, "append_persona_message"):
            payload_metadata = dict(metadata or {})
            payload_metadata["tags"] = list(tags or [])
            adapter.append_persona_message({
                "role": role,
                "content": text,
                "created_at": clock.now().isoformat(timespec="seconds"),
                "metadata": payload_metadata,
                "scope": resolved_scope,
                "line_role": line_role,
            })
        return "mock-msg-id" if return_message_id else True


def _make_mock_spell_executor(session_factory):
    """document_create スペルの mock 実行器 (Item を実際に DB へ作る)。"""

    async def _fake_spell(tool_name, tool_args, persona, state, playbook_name,
                          event_callback, messages=None):
        if tool_name == "document_create":
            from database.models import Item

            item_id = str(uuid.uuid4())
            title = str(tool_args.get("title") or tool_args.get("name") or "mock文書")
            now = datetime.utcnow()
            db = session_factory()
            try:
                db.add(Item(
                    ITEM_ID=item_id, NAME=title, TYPE="document",
                    DESCRIPTION="mock day sim artifact",
                    CREATOR_ID=persona.persona_id, CREATED_AT=now, UPDATED_AT=now,
                ))
                db.commit()
            finally:
                db.close()
            return (f"文書「{title}」を作成しました。アイテムID: {item_id}", None)
        return (f"スペル {tool_name} を実行しました (mock)。", None)

    return _fake_spell


def _default_mock_judge(kind: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """判断点のルールベース mock 裁定 (配線テストの既定挙動)。

    day_open は response_schema の動的 enum (実在 ref / facility) から時間割を
    機械編成する。post_session は成果物があれば done、なければ continue。
    """
    ctx = json.loads(args.get("judgment_context") or "{}")
    schema = args.get("response_schema") or {}

    if kind == "day_open":
        slot_props = (
            schema.get("properties", {}).get("timetable", {})
            .get("items", {}).get("properties", {})
        )
        ref_enum = [r for r in slot_props.get("ref", {}).get("enum", []) if r != "none"]
        facility_enum = slot_props.get("facility", {}).get("enum", ["own_room"])

        def _pick_facility(kind_name: str) -> str:
            preferred = {"知る": "library", "作る": "workshop"}.get(kind_name)
            if preferred and preferred in facility_enum:
                return preferred
            return facility_enum[-1]  # own_room が末尾

        timetable: List[Dict[str, Any]] = []
        hour = 10
        for i, ref in enumerate(ref_enum[:3]):
            slot_kind = "作る" if i % 2 else "知る"
            verb = "作業を進める" if slot_kind == "作る" else "調べ物をする"
            timetable.append({
                "start": f"{hour:02d}:00", "kind": slot_kind,
                "title": f"{ref} の{verb} (mock)", "ref": ref,
                "facility": _pick_facility(slot_kind), "budget_rounds": 4,
                "note": f"{ref} に取り組む (mock)",
            })
            hour += 2
        timetable.append({
            "start": f"{hour:02d}:00", "kind": "暮らし", "ref": "none",
            "title": "静かに過ごす (mock)",
            "facility": "own_room", "budget_rounds": 0, "note": "静かな時間 (mock)",
        })
        timetable.append({
            "start": "20:00", "kind": "休む", "ref": "none", "title": "休む (mock)",
            "facility": "own_room", "budget_rounds": 0, "note": "",
        })
        return {
            "monologue": "今日の時間割を組んだ (mock 裁定)。",
            "timetable": timetable,
        }
    if kind == "post_session":
        artifacts = ctx.get("artifacts") or []
        output: Dict[str, Any] = {
            "monologue": "セッションを終えた (mock 裁定)。",
            # digest 統合 (W1 Chunk C / D9): post_session 自身が実績要約を書く
            # (response_schema で required)。
            "digest": "指示書のとおりに作業した (mock 裁定の実績要約)。",
            "new_desires": [],
            "remaining_timetable": None,
        }
        if ctx.get("task_ref"):
            if artifacts:
                output["task_verdict"] = {
                    "status": "done", "artifact_ref": artifacts[0],
                    "desk_memo": "成果物を作り終えた (mock)",
                }
            else:
                output["task_verdict"] = {
                    "status": "continue", "desk_memo": "続きは次のコマで (mock)",
                }
        return output
    if kind == "post_conversation":
        return {
            "monologue": "会話が終わった (mock 裁定)。",
            "picked_tasks": [], "new_desires": [], "remaining_timetable": None,
        }
    if kind == "on_event":
        if ctx.get("is_alert"):
            return {"monologue": "すぐに応対する (mock 裁定)。",
                    "reaction": {"type": "engage_now"}}
        return {"monologue": "覚えておく (mock 裁定)。",
                "reaction": {"type": "note_only", "memo": "イベントがあった (mock)"}}
    if kind == "day_close":
        return {
            "monologue": "一日を終える (mock 裁定)。予定と実績を見比べた。",
            "tomorrow_memo": "今日の続きから始める (mock)",
            "day_theme": "配線テストの一日",
            "user_report_seeds": [],
        }
    raise ValueError(f"unknown judgment kind: {kind!r}")


def _build_mock_manager(db_file: Optional[str], scenario) -> Any:
    """配線テスト用の最小スタブ manager を組み立てる。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database.models import AI, Base, City, User
    from saiverse.day_scenario import MockJudgmentPulseController
    from saiverse.event_scheduler import EventScheduler
    from saiverse.track_manager import TrackManager

    if db_file:
        engine = create_engine(f"sqlite:///{db_file}",
                               connect_args={"check_same_thread": False})
    else:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    persona_id = scenario.persona_id
    db = session_factory()
    try:
        if db.query(User).count() == 0:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="sim"))
            db.flush()
        city = db.query(City).first()
        if city is None:
            city = City(USERID=1, CITYNAME="sim_city", UI_PORT=3001, API_PORT=8001)
            db.add(city)
            db.flush()
        if db.query(AI).filter(AI.AIID == persona_id).first() is None:
            db.add(AI(AIID=persona_id, HOME_CITYID=city.CITYID, AINAME=persona_id))
        db.commit()
    finally:
        db.close()

    persona = SimpleNamespace(
        persona_id=persona_id,
        persona_name=persona_id,
        current_building_id="own_room",
        private_room_id="own_room",
        sai_memory=ListMemoryAdapter(),
    )

    class _StubOccupancy:
        # 本物の OccupancyManager と同じ契約 (W7 柱5): 成功時に
        # persona.current_building_id を service 側で更新する。
        def move_entity(self, entity_id, entity_type, from_id, to_id, db_session=None):
            if entity_id == persona.persona_id:
                persona.current_building_id = to_id
            return True, "ok (mock)"

    manager = SimpleNamespace(
        SessionLocal=session_factory,
        personas={persona_id: persona},
        buildings=[
            SimpleNamespace(building_id="library", name="図書館"),
            SimpleNamespace(building_id="workshop", name="工房"),
        ],
        event_scheduler=EventScheduler(),  # start() しない (DES 前提)
        track_manager=TrackManager(session_factory=session_factory),
        occupancy_manager=_StubOccupancy(),
        sea_runtime=MockWorkRuntime(MockSessionLLMClient()),
        _engine=engine,
    )
    persona_path = Path(tempfile.mkdtemp(prefix="day_sim_"))
    manager.pulse_controller = MockJudgmentPulseController(
        manager, _default_mock_judge, persona_path,
    )
    return manager


def _build_real_manager(city_name: str, db_file: Optional[str], sds_url: str) -> Any:
    """実 LLM モード: SAIVerseManager を構築する (start() は呼ばない)。"""
    from database.paths import default_db_path
    from saiverse.saiverse_manager import SAIVerseManager

    db_path = Path(db_file) if db_file else default_db_path()
    manager = SAIVerseManager(
        city_name=city_name,
        db_path=str(db_path),
        sds_url=sds_url,
    )
    return manager


def run_mock_scenario(scenario: Any, db_file: Optional[str] = None):
    """mock 一日シムを端から端まで回す (CLI と回帰テストの共用経路)。

    LLM を一切呼ばない。スペル実行は mock に差し替える (document_create →
    実 Item 作成)。呼び出し側は終了後に ``clock.disable_virtual()`` を呼ぶこと。

    Returns:
        (manager, result, markers) — markers は generate_raw_log 用の実行前高水位。
    """
    from saiverse.day_scenario import ScenarioPlayer

    manager = _build_mock_manager(db_file, scenario)
    markers = _capture_world_markers(manager)
    with ExitStack() as stack:
        stack.enter_context(patch(
            "sea.runtime_llm.SPELL_TOOL_NAMES",
            {"document_create", "searxng_search", "memory_recall"},
        ))
        stack.enter_context(patch(
            "sea.runtime_llm._run_spell_tool_async",
            new=_make_mock_spell_executor(manager.SessionLocal),
        ))
        result = ScenarioPlayer().run(manager, scenario)
    return manager, result, markers


# ---------------------------------------------------------------------------
# 生データ抽出 (raw log)
# ---------------------------------------------------------------------------
# 一日新聞は決定論の要約であり、レビューには「実際に何が起きたか」の生データが
# 対で必要 (2026-07-05 の実 LLM 3 回サイクルで確立した運用: 新聞と生データは
# セットでレビューに出す)。以前はアドホックに sqlite / sea_trace を掘っていた
# のを、シム実行の直後に同じプロセスから決定論で抽出する。


def _capture_world_markers(manager: Any) -> Dict[str, int]:
    """シナリオ実行前の連番高水位を記録する (この走で増えた行だけ抽出するため)。

    building_messages.timestamp 等は実時刻系が混ざる (タイムスタンプ3系統問題)
    ため、日付では絞れない。連番 (autoincrement id) の前後比較が唯一確実。
    """
    from sqlalchemy import func as sqla_func

    from database.models import BuildingMessage, BuildingOccupancyLog

    db = manager.SessionLocal()
    try:
        return {
            "building_messages": db.query(
                sqla_func.coalesce(sqla_func.max(BuildingMessage.id), 0)).scalar(),
            "occupancy": db.query(
                sqla_func.coalesce(sqla_func.max(BuildingOccupancyLog.ID), 0)).scalar(),
        }
    finally:
        db.close()


def _fmt_memory_created_at(value: Any) -> str:
    """SAIMemory payload の created_at (epoch int / ISO 文字列) を表示用に。"""
    if isinstance(value, (int, float)) or (isinstance(value, str) and str(value).isdigit()):
        try:
            return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)[:19] if value else "-"


def generate_raw_log(
    manager: Any,
    scenario: Any,
    result: Any,
    markers: Dict[str, int],
    *,
    mode: str,
) -> str:
    """一日シムの生データを markdown で組み立てる (LLM 不使用・実在記録のみ)。"""
    from sqlalchemy import func as sqla_func

    from database.models import (
        BuildingMessage, BuildingOccupancyLog, PersonaDayPlan, PersonaTask,
    )

    persona_id = scenario.persona_id
    lines: List[str] = []
    lines.append(f"# 一日シム生データ — {persona_id} {scenario.plan_date} (mode={mode})")
    lines.append("")
    lines.append(f"- 実行統計: events={result.executed_events} / judgments={len(result.judgments)}")
    lines.append(f"- 種まき: tasks={result.seeded_task_refs} desires={result.seeded_desire_refs}")

    # ログセッションへの参照 (フル LLM I/O は llm_io.log / sea_trace.log にある)
    try:
        from saiverse import logging_config
        if logging_config._initialized:
            lines.append(f"- ログセッション: {logging_config.get_session_log_dir()}")
    except Exception:
        pass
    lines.append("")

    # --- 判断点 ---
    lines.append("## 判断点")
    lines.append("")
    if result.judgments:
        for j in result.judgments:
            status = "OK" if j.get("submitted") else f"SKIP ({j.get('reason')})"
            lines.append(f"- [{j.get('at')}] {j.get('kind')}: {status}")
    else:
        lines.append("(なし)")
    lines.append("")

    # --- ペルソナ記憶 (plan_date の全行・全文) ---
    lines.append("## ペルソナ記憶 (SAIMemory)")
    lines.append("")
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    if adapter is None:
        lines.append("(sai_memory adapter なし)")
    else:
        payloads = adapter.recent_persona_messages_by_count(2000)
        plan_date = str(scenario.plan_date)
        shown = 0
        for p in payloads:
            ts = _fmt_memory_created_at(p.get("created_at"))
            if not ts.startswith(plan_date):
                continue
            shown += 1
            meta = p.get("metadata") or {}
            tags = ",".join(meta.get("tags") or []) or "-"
            attrs = [f"tags={tags}"]
            if p.get("scope"):
                attrs.append(f"scope={p['scope']}")
            if p.get("line_role"):
                attrs.append(f"line={p['line_role']}")
            lines.append(f"### [{ts}] {p.get('role')}  ({'  '.join(attrs)})")
            lines.append("")
            if p.get("paired_action_text"):
                lines.append(f"> action: {p['paired_action_text']}")
                lines.append("")
            lines.append(str(p.get("content") or ""))
            lines.append("")
        if shown == 0:
            lines.append(f"(plan_date={plan_date} のメッセージなし — "
                         "タイムスタンプが実時刻系の場合は全件が対象外になる)")
    lines.append("")

    db = manager.SessionLocal()
    try:
        # --- 建物メッセージ (この走で増えた分) ---
        lines.append("## 建物メッセージ (building_messages)")
        lines.append("")
        bm_rows = (
            db.query(BuildingMessage)
            .filter(BuildingMessage.id > markers["building_messages"])
            .order_by(BuildingMessage.id)
            .all()
        )
        if bm_rows:
            for m in bm_rows:
                who = m.persona_id or m.role
                event = f"  event={m.event_type}" if m.event_type else ""
                lines.append(f"- [{m.timestamp}] {m.building_id} seq={m.seq} {m.role}({who}){event}")
                lines.append(f"  {m.content}")
        else:
            lines.append("(なし)")
        lines.append("")

        # --- 移動 (この走で増えた occupancy 行) ---
        lines.append("## 移動 (building_occupancy_log)")
        lines.append("")
        occ_rows = (
            db.query(BuildingOccupancyLog)
            .filter(BuildingOccupancyLog.ID > markers["occupancy"])
            .order_by(BuildingOccupancyLog.ID)
            .all()
        )
        if occ_rows:
            for o in occ_rows:
                exit_ts = o.EXIT_TIMESTAMP or "(滞在中)"
                lines.append(f"- {o.AIID}: {o.BUILDINGID}  {o.ENTRY_TIMESTAMP} → {exit_ts}")
        else:
            lines.append("(なし)")
        lines.append("")

        # --- タスク / 欲求のスナップショット (全 status) ---
        lines.append("## タスク / 欲求スナップショット (persona_task)")
        lines.append("")
        task_rows = (
            db.query(PersonaTask)
            .filter(PersonaTask.persona_id == persona_id)
            .order_by(PersonaTask.short_id)
            .all()
        )
        if task_rows:
            for t in task_rows:
                kind = "desire" if t.parent_kind == "note" else (t.parent_kind or "standalone")
                desire = ""
                if t.desire_type or t.desire_state:
                    desire = (f"  desire_type={t.desire_type} state={t.desire_state}"
                              f" source={t.desire_source!r}")
                artifacts = f"  artifacts={t.artifact_refs}" if t.artifact_refs else ""
                lines.append(
                    f"- task:{t.short_id} [{t.status}] ({kind}) {t.title}"
                    f"  goal={t.goal!r}{desire}{artifacts}"
                    f"  created={t.created_at} updated={t.updated_at} completed={t.completed_at}"
                )
        else:
            lines.append("(なし)")
        lines.append("")

        # --- 時間割の最終状態 ---
        lines.append("## 時間割 (persona_day_plan)")
        lines.append("")
        plan_row = (
            db.query(PersonaDayPlan)
            .filter(PersonaDayPlan.persona_id == persona_id)
            .filter(PersonaDayPlan.plan_date == str(scenario.plan_date))
            .first()
        )
        if plan_row is not None:
            lines.append("```json")
            lines.append(json.dumps(
                {"slots": json.loads(plan_row.slots_json),
                 "meta": json.loads(plan_row.meta_json) if plan_row.meta_json else None},
                ensure_ascii=False, indent=2))
            lines.append("```")
        else:
            lines.append("(なし)")

        # --- この走で作られた Item (成果物の突合用) ---
        from database.models import Item
        lines.append("")
        lines.append("## アイテム (直近作成順・上位 20)")
        lines.append("")
        item_rows = (
            db.query(Item).order_by(Item.CREATED_AT.desc()).limit(20).all()
        )
        for it in item_rows:
            lines.append(
                f"- item:{it.SHORT_ID} [{it.TYPE}] {it.NAME}"
                f"  creator={it.CREATOR_ID}  file={it.FILE_PATH or '-'}  created={it.CREATED_AT}"
            )
        if not item_rows:
            lines.append("(なし)")
    finally:
        db.close()

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--scenario", required=True, help="シナリオ JSON ファイル")
    parser.add_argument("--db-file", default=None, help="SQLite DB パス (mock 既定: in-memory)")
    parser.add_argument("--real", action="store_true",
                        help="実 LLM モード (API キー必須・実コスト発生)。既定は mock")
    parser.add_argument("--city", default="city_a",
                        help="--real 時の City 名 (default: city_a)")
    parser.add_argument("--sds-url", default="http://127.0.0.1:8080",
                        help="--real 時の SDS URL")
    parser.add_argument("--report-only", action="store_true",
                        help="シナリオを回さず一日レポートだけ生成する")
    parser.add_argument("--out", default=None,
                        help="レポートの出力先ファイル (省略時: ~/.saiverse/personas/<id>/day_reports/<date>.md)")
    parser.add_argument("--no-raw-log", action="store_true",
                        help="生データ抽出を出力しない (既定はレポートと対で <date>_raw.md を出す)")
    parser.add_argument("--raw-log-out", default=None,
                        help="生データの出力先ファイル (省略時: レポートと同じ場所の <date>_raw.md)")
    args = parser.parse_args()

    # Windows コンソール (cp932) では日本語レポートの一部文字が出力できないため
    # stdout を UTF-8 に固定する (リダイレクト先ファイルも UTF-8 になる)。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from saiverse import clock
    from saiverse.day_report import generate_day_report, save_day_report
    from saiverse.day_scenario import (
        RealConversationUserEventDriver,
        ScenarioPlayer,
        SyncJudgmentDispatcher,
        load_scenario,
    )

    scenario = load_scenario(args.scenario)
    result = None
    markers = None

    try:
        if args.report_only:
            if args.real:
                manager = _build_real_manager(args.city, args.db_file, args.sds_url)
            else:
                manager = _build_mock_manager(args.db_file, scenario)
        elif args.real:
            manager = _build_real_manager(args.city, args.db_file, args.sds_url)
            markers = _capture_world_markers(manager)
            # 判断点・ユーザー会話 Pulse は同期実行 (DES 単一スレッド)。実
            # PulseController はレーン管理を持つため、シナリオ実行中だけ
            # 差し替える。ユーザー発話は実チャット経路 (building_messages 記録
            # → Track activate → main_line Pulse) を通すドライバで注入する。
            original_controller = manager.pulse_controller
            manager.pulse_controller = SyncJudgmentDispatcher(manager)
            try:
                player = ScenarioPlayer(
                    user_event_driver=RealConversationUserEventDriver(),
                )
                result = player.run(manager, scenario)
            finally:
                manager.pulse_controller = original_controller
            LOGGER.info("scenario finished: events=%d judgments=%d",
                        result.executed_events, len(result.judgments))
        else:
            manager, result, markers = run_mock_scenario(scenario, args.db_file)
            LOGGER.info("scenario finished: events=%d judgments=%d",
                        result.executed_events, len(result.judgments))

        report = generate_day_report(manager, scenario.persona_id, scenario.plan_date)
        print(report)

        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(report, encoding="utf-8")
        else:
            out_path = save_day_report(
                manager, scenario.persona_id, scenario.plan_date, report_text=report,
            )
        LOGGER.info("report saved: %s", out_path)

        # 生データはレポートと対で出す (シナリオを実際に回した場合のみ)
        if result is not None and markers is not None and not args.no_raw_log:
            raw_text = generate_raw_log(
                manager, scenario, result, markers,
                mode="real" if args.real else "mock",
            )
            if args.raw_log_out:
                raw_path = Path(args.raw_log_out)
            else:
                raw_path = Path(out_path).with_name(f"{scenario.plan_date}_raw.md")
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(raw_text, encoding="utf-8")
            LOGGER.info("raw log saved: %s", raw_path)
        return 0
    finally:
        clock.disable_virtual()


if __name__ == "__main__":
    sys.exit(main())
