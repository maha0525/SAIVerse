"""ライフビュー (Persona Activity View) API。

ペルソナの自律行動の観察面 + 再生/停止トグルを提供する。表示データは既存の
Track / pulse_logs / messages のテンプレート整形のみで作り、LLM は呼ばない
(persona_activity_view.md 不変条件 2)。

エンドポイント:
- GET  /{persona_id}/activity-view     : 集約ビュー (いま / 最近)
- POST /{persona_id}/activity/start    : 再生 (Active 化 + 即時メタ判断 tick)
- POST /{persona_id}/activity/stop     : 停止 (自律 Track pause + Idle +
                                          対ユーザー Track のサイレント activate)

v0.5 (life.md §9.2-2, 改修B): 「間隔 2 種」(review_minutes/pulse_seconds) の
設定 UI と対応 API (``PUT /activity/intervals``) は v1 (50分 tick 主駆動・
連続 sub_line Pulse) 時代のものとして退役した。現行の自律駆動は時間割の
コマ発火 + 判断点 (autonomy_wiring.py) — ユーザーが触る意味のある間隔設定は
残っていない。バックエンドの watchdog 機構 (AutonomyManager) と既定値運用は
引き続き生きている。

詳細: docs/intent/persona_activity_view.md §6, §8
"""
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_manager
from database.models import AI, Playbook
from saiverse.activity_view import (
    build_digest_label,
    parse_tool_calls_json,
    resolve_autonomous_pulse_interval,
)
from saiverse.track_manager import (
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    InvalidTrackStateError,
    TrackNotFoundError,
)

from .autonomy import _get_or_create_autonomy
from .utils import get_adapter

LOGGER = logging.getLogger(__name__)

router = APIRouter()

# 「最近」の初版表示件数 (persona_activity_view.md §10 決定 1)。
_RECENT_LIMIT = 20
# Track 種別フィルタ前に messages から引く Pulse サマリの件数。
# user_conversation Pulse が混ざるので多めに取る。
_RECENT_SCAN_LIMIT = 80


# ----------------------------------------------------------------------
# Response models
# ----------------------------------------------------------------------

class ActivityNowItem(BaseModel):
    track_id: str
    title: str
    intent: Optional[str] = None
    started_at: Optional[float] = None
    next_pulse_eta_seconds: Optional[int] = None


class ActivityRecentItem(BaseModel):
    at: Optional[float] = None
    label: str
    important: bool = False
    pulse_id: str
    # Track ラベル: どの Track での出来事か分かるように表示する
    track_title: Optional[str] = None
    track_type: Optional[str] = None
    is_meta_judgment: bool = False


class ActivityBuildingInfo(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None


class ActivityViewResponse(BaseModel):
    persona_id: str
    autonomy_enabled: bool
    autonomy_running: bool
    building: ActivityBuildingInfo
    now: List[ActivityNowItem]
    recent: List[ActivityRecentItem]
    # 次に自発行動が起きうるタイミング (秒)。各トリガーの ETA を個別に返し、
    # フロントで状況に応じた表示を組み立てる。
    next_meta_tick_eta_seconds: Optional[int] = None
    next_wait_response_timeout_seconds: Optional[int] = None


class ActivityToggleResponse(BaseModel):
    success: bool
    message: str
    autonomy_enabled: bool
    autonomy_running: bool


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _get_persona_or_404(manager, persona_id: str):
    persona = manager.personas.get(persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail="Persona not found")
    return persona


def _load_judgment_config(manager, persona) -> Dict[str, Any]:
    """META_JUDGMENT_CONFIG を既定値マージ済みで読む (MetaLayer 経由)。"""
    meta_layer = getattr(manager, "meta_layer", None)
    if meta_layer is None:
        return {}
    try:
        config = meta_layer._load_judgment_config(persona)
        return config if isinstance(config, dict) else {}
    except Exception:
        LOGGER.exception(
            "[activity] Failed to load judgment config for %s",
            getattr(persona, "persona_id", "?"),
        )
        return {}


def _set_autonomy_enabled(manager, persona_id: str, persona, enabled: bool) -> None:
    """AUTONOMY_ENABLED を DB + メモリ上の persona の両方で更新する。"""
    db = manager.SessionLocal()
    try:
        ai = db.query(AI).filter(AI.AIID == persona_id).first()
        if ai is None:
            raise HTTPException(status_code=404, detail="Persona not found in DB")
        ai.AUTONOMY_ENABLED = enabled
        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        LOGGER.exception("[activity] Failed to set AUTONOMY_ENABLED for %s", persona_id)
        raise HTTPException(status_code=500, detail="Failed to update autonomy state")
    finally:
        db.close()
    persona.autonomy_enabled = enabled
    LOGGER.info("[activity] AUTONOMY_ENABLED=%s for %s", enabled, persona_id)


def _track_metadata_dict(track) -> Dict[str, Any]:
    if not track.track_metadata:
        return {}
    try:
        data = json.loads(track.track_metadata)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _to_epoch(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


# messages から最近の Pulse サマリを引く (pulse_timeline.py の _SUMMARY_SQL と同系)。
def _resolve_playbook_display_names(manager, names: set) -> Dict[str, str]:
    """playbook 名 → display_name を DB から一括解決する。"""
    if not names:
        return {}
    result: Dict[str, str] = {}
    db = manager.SessionLocal()
    try:
        rows = db.query(Playbook.name, Playbook.display_name).filter(
            Playbook.name.in_(list(names))
        ).all()
        for name, display_name in rows:
            if display_name:
                result[name] = display_name
    except Exception:
        LOGGER.warning("[activity] Failed to resolve playbook display_names", exc_info=True)
    finally:
        db.close()
    return result


def _get_wait_response_timeout_eta(manager, persona_id: str) -> Optional[int]:
    """running な user_conversation Track の wait_response timeout 残り秒数。"""
    tm = getattr(manager, "track_manager", None)
    scheduler = getattr(manager, "event_scheduler", None)
    if tm is None or scheduler is None:
        return None
    for track in tm.list_for_persona(persona_id, statuses=[STATUS_RUNNING]):
        if track.track_type != "user_conversation":
            continue
        key = tm._wait_response_timeout_key(track.track_id)
        entry = getattr(scheduler, "_entries_by_key", {}).get(key)
        if entry is not None and not entry.cancelled:
            remaining = entry.fire_at_ts - time.time()
            return max(0, int(remaining))
    return None


_RECENT_PULSES_SQL = """
    SELECT pulse_id,
           origin_track_id,
           MAX(created_at) AS last_ca,
           GROUP_CONCAT(DISTINCT line_role) AS roles
    FROM messages
    WHERE thread_id LIKE ? AND pulse_id IS NOT NULL
    GROUP BY pulse_id
    ORDER BY last_ca DESC
    LIMIT ?
"""

_PULSE_TOOLS_SQL_TEMPLATE = """
    SELECT pulse_id, tool_calls, important, playbook_name
    FROM pulse_logs
    WHERE pulse_id IN ({placeholders})
    ORDER BY created_at ASC, rowid ASC
"""


def _build_recent_items(manager, persona_id: str) -> List[ActivityRecentItem]:
    """「最近」ダイジェストを組み立てる。1 Pulse = 1 行 (§10 決定 2)。

    対象: origin Track が autonomous 種別の Pulse、および meta_judgment Pulse。
    user_conversation Pulse はチャットに既に見えているため除外する。
    """
    thread_pattern = f"{persona_id}:%"
    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            cur = adapter.conn.execute(
                _RECENT_PULSES_SQL, (thread_pattern, _RECENT_SCAN_LIMIT)
            )
            pulse_rows = cur.fetchall()

        # Track 解決 (autonomous 判定)
        track_ids = {r[1] for r in pulse_rows if r[1]}
        track_map: Dict[str, Any] = {}
        tm = getattr(manager, "track_manager", None)
        if tm is not None:
            for tid in track_ids:
                try:
                    track_map[tid] = tm.get(tid)
                except Exception:
                    track_map[tid] = None

        selected: List[tuple] = []
        for pulse_id, track_id, last_ca, roles in pulse_rows:
            line_roles = (roles or "").split(",") if roles else []
            track = track_map.get(track_id) if track_id else None
            track_type = getattr(track, "track_type", None)
            is_autonomous = track_type == "autonomous"
            is_meta = "meta_judgment" in line_roles
            if not (is_autonomous or is_meta):
                continue
            selected.append((pulse_id, track, line_roles, last_ca))
            if len(selected) >= _RECENT_LIMIT:
                break

        # pulse_logs からツール実行 + important + playbook 名を引く
        tools_by_pulse: Dict[str, List] = {}
        important_by_pulse: Dict[str, bool] = {}
        playbooks_by_pulse: Dict[str, List[str]] = {}
        if selected:
            pulse_ids = [s[0] for s in selected]
            placeholders = ",".join("?" for _ in pulse_ids)
            sql = _PULSE_TOOLS_SQL_TEMPLATE.format(placeholders=placeholders)
            with adapter._db_lock:
                cur = adapter.conn.execute(sql, pulse_ids)
                log_rows = cur.fetchall()
            for pulse_id, tool_calls_json, important, playbook_name in log_rows:
                if tool_calls_json:
                    tools_by_pulse.setdefault(pulse_id, []).extend(
                        parse_tool_calls_json(tool_calls_json)
                    )
                if important:
                    important_by_pulse[pulse_id] = True
                if playbook_name:
                    playbooks_by_pulse.setdefault(pulse_id, []).append(playbook_name)

    # meta_judgment Pulse 用: メッセージ本文から spells_emitted + 判断種別を引く
    meta_pulse_ids = [s[0] for s in selected if "meta_judgment" in s[2]]
    meta_details: Dict[str, Dict[str, Any]] = {}
    if meta_pulse_ids:
        meta_details = _load_meta_judgment_details(manager, persona_id, meta_pulse_ids)

    # Track 解決マップ: spells_emitted 内の track_id を解決するのに使う
    # (既に track_map にある + spells の中の未知 track_id を追加解決)
    all_spell_track_ids: set = set()
    for detail in meta_details.values():
        for spell in detail.get("spells") or []:
            args = spell.get("args", {})
            tid = args.get("track_id", "")
            if tid:
                all_spell_track_ids.add(tid)
    spell_track_resolver: Dict[str, Any] = {}
    if all_spell_track_ids and tm is not None:
        for tid in all_spell_track_ids:
            if tid in track_map:
                spell_track_resolver[tid] = track_map[tid]
            else:
                try:
                    resolved_tid = tm.resolve_track_ref(persona_id, tid)
                    spell_track_resolver[tid] = tm.get(resolved_tid)
                except Exception:
                    pass

    # Playbook display_name を DB から一括解決
    all_pb_names: set = set()
    for pbs in playbooks_by_pulse.values():
        all_pb_names.update(pbs)
    pb_display_names = _resolve_playbook_display_names(manager, all_pb_names)

    items: List[ActivityRecentItem] = []
    for pulse_id, track, line_roles, last_ca in selected:
        is_meta = "meta_judgment" in line_roles
        detail = meta_details.get(pulse_id, {})
        label = build_digest_label(
            track_title=getattr(track, "title", None),
            track_type=getattr(track, "track_type", None),
            line_roles=line_roles,
            tool_calls=tools_by_pulse.get(pulse_id, []),
            playbook_names=playbooks_by_pulse.get(pulse_id),
            playbook_display_names=pb_display_names,
            spells_emitted=detail.get("spells"),
            track_resolver=spell_track_resolver,
            judgment_kind=detail.get("judgment_kind"),
        )
        items.append(
            ActivityRecentItem(
                at=float(last_ca) if last_ca is not None else None,
                label=label,
                important=important_by_pulse.get(pulse_id, False),
                pulse_id=pulse_id,
                track_title=getattr(track, "title", None),
                track_type=getattr(track, "track_type", None),
                is_meta_judgment=is_meta,
            )
        )
    return items


def _load_meta_judgment_details(
    manager, persona_id: str, pulse_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    """meta_judgment 系 Pulse (MetaLayer の alert/running/idle_pending/
    idle_empty/life_purpose と、判断点 day_open/post_conversation/
    post_session/on_event/day_close の両方) の /spell 実行内容と判断種別を
    pulse_id ごとに引く。

    meta_judgment_log には pulse_id カラムが無いため、judged_at の時系列と
    messages の created_at を突合する必要がある…のだが、judgment_id は
    meta_judgment_finalize 内で独自に生成した UUID で pulse_id とは一致しない。

    代わりに messages テーブルの content から /spell 行を直接パースする方式を取る。
    messages テーブルには line_role='meta_judgment' + pulse_id が確実に入る (§11 実装記録)。

    role は意図的にフィルタしない。MetaLayer 側
    (builtin_data/tools/meta_judgment_finalize.py) は 2026-07-07
    (commit b07c520, few-shot 汚染回避) 以降 ``role='user'`` の
    ``<system>…</system>`` ナレーションとして保存するのに対し、判断点側
    (builtin_data/tools/judgment_finalize.py) は今も ``role='assistant'``。
    この関数がかつて ``role = 'assistant'`` だけを見ていたため、MetaLayer
    側の判断が常に 0 件になり (実際は動いていても検出できず)、ライフビュー
    「最近」のメタ判断行が常に既定の「現状を続けることにした」に落ちる
    バグがあった (2026-07-14 発覚)。

    metadata.judgment.kind (判断点のみが埋め込む) が取れれば judgment_kind
    として返す。MetaLayer 側にはこのタグが無いため None のままになる。
    """
    if not pulse_ids:
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    try:
        from .utils import get_adapter
        thread_pattern = f"{persona_id}:%"
        with get_adapter(persona_id, manager) as adapter:
            placeholders = ",".join("?" for _ in pulse_ids)
            sql = f"""
                SELECT pulse_id, content, metadata FROM messages
                WHERE thread_id LIKE ?
                  AND pulse_id IN ({placeholders})
                  AND line_role = 'meta_judgment'
                ORDER BY created_at DESC, rowid DESC
            """
            with adapter._db_lock:
                cur = adapter.conn.execute(sql, [thread_pattern] + pulse_ids)
                rows = cur.fetchall()
        for pulse_id, content, metadata_json in rows:
            entry = result.setdefault(pulse_id, {"spells": [], "judgment_kind": None})
            if content:
                spells = _parse_spell_lines(content)
                if spells:
                    entry["spells"].extend(spells)
            if entry["judgment_kind"] is None and metadata_json:
                try:
                    meta = json.loads(metadata_json)
                except (TypeError, ValueError):
                    meta = None
                if isinstance(meta, dict):
                    kind = (meta.get("judgment") or {}).get("kind")
                    if isinstance(kind, str) and kind:
                        entry["judgment_kind"] = kind
    except Exception:
        LOGGER.exception(
            "[activity] Failed to load meta judgment details for %s", persona_id,
        )
    return result


def _parse_spell_lines(content: str) -> List[Dict[str, Any]]:
    """メタ判断の assistant メッセージから /spell 行をパースする。

    正準形式 ``/spell name='track_activate' args={"track_id": "t:3"}`` を解釈する。
    runtime の堅牢なパーサ (正準/fuzzy 両対応) に委譲して二重実装を避ける。
    戻り値: [{"name": "track_activate", "args": {"track_id": "t:3"}}]
    """
    from sea.runtime_llm import _parse_spell_lines as _runtime_parse

    return [
        {"name": tool_name, "args": tool_args}
        for tool_name, tool_args, _match, _normalized in _runtime_parse(content, quiet=True)
    ]


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------

@router.get("/{persona_id}/activity-view", response_model=ActivityViewResponse)
def get_activity_view(persona_id: str, manager=Depends(get_manager)):
    """ライフビューの集約データ (いま / 最近 / 設定) を返す。"""
    persona = _get_persona_or_404(manager, persona_id)
    config = _load_judgment_config(manager, persona)

    am = _get_or_create_autonomy(persona_id, manager)

    # Building
    building_id = getattr(persona, "current_building_id", None)
    building_name = None
    if building_id and building_id in getattr(manager, "building_map", {}):
        building_name = getattr(manager.building_map[building_id], "name", None)

    # いま: running な autonomous Track + 次 Pulse ETA
    now_items: List[ActivityNowItem] = []
    tm = manager.track_manager
    for track in tm.list_for_persona(persona_id, statuses=[STATUS_RUNNING]):
        if track.track_type != "autonomous":
            continue
        meta = _track_metadata_dict(track)
        interval = resolve_autonomous_pulse_interval(meta, config)
        eta: Optional[int] = None
        last_pulse_at = meta.get("last_pulse_at")
        if last_pulse_at is not None:
            try:
                eta = max(0, int(float(last_pulse_at) + interval - time.time()))
            except (TypeError, ValueError):
                eta = None
        else:
            # まだ一度も Pulse が走っていない → 次の poll で発火する
            eta = 0
        now_items.append(
            ActivityNowItem(
                track_id=track.track_id,
                title=track.title or "(無題)",
                intent=track.intent,
                started_at=_to_epoch(track.last_active_at or track.created_at),
                next_pulse_eta_seconds=eta,
            )
        )

    recent_items = _build_recent_items(manager, persona_id)

    return ActivityViewResponse(
        persona_id=persona_id,
        autonomy_enabled=bool(getattr(persona, "autonomy_enabled", True)),
        autonomy_running=am.is_running,
        building=ActivityBuildingInfo(id=building_id, name=building_name),
        now=now_items,
        recent=recent_items,
        next_meta_tick_eta_seconds=am.get_next_tick_eta_seconds(),
        next_wait_response_timeout_seconds=_get_wait_response_timeout_eta(manager, persona_id),
    )


@router.post("/{persona_id}/activity/start", response_model=ActivityToggleResponse)
def start_activity(persona_id: str, manager=Depends(get_manager)):
    """再生: 自律行動を始めさせる (persona_activity_view.md §6.1)。

    1. AUTONOMY_ENABLED → True
    2. AutonomyManager.start() — 即時に watchdog チェックが走る (自律行動 v2)。
       起床時間帯で今日の day_plan が無ければ、その場で起床判断 (day_open) が
       火入れされ、時間割の編成から一日が始まる。pause 済み Track の自動
       resume はしない。
    """
    persona = _get_persona_or_404(manager, persona_id)

    _set_autonomy_enabled(manager, persona_id, persona, True)

    # 候補補充 Track (autonomous_desire.md §11) は _on_persona_registered で
    # 起動時/作成時に常設済みなので、ここでの ensure は不要。

    am = _get_or_create_autonomy(persona_id, manager)
    started = am.start()
    LOGGER.info(
        "[activity] start: persona=%s autonomy_started=%s", persona_id, started,
    )
    return ActivityToggleResponse(
        success=True,
        message="自律行動を開始しました" if started else "自律行動は既に動いています",
        autonomy_enabled=True,
        autonomy_running=am.is_running,
    )


@router.post("/{persona_id}/activity/stop", response_model=ActivityToggleResponse)
def stop_activity(persona_id: str, manager=Depends(get_manager)):
    """停止: 「いつもの、プロンプトを静かに待っている AI」に戻す
    (persona_activity_view.md §6.2)。

    1. AutonomyManager.stop() — watchdog tick の予約 cancel
    2. running な autonomous Track を全て pause (帳簿を待機状態に揃える)
    3. AUTONOMY_ENABLED → False (判断点・watchdog のゲートが閉じる)
    4. 対ユーザー Track をサイレント activate (suppress_pulse=True —
       停止ボタンで自動発言させない、不変条件 4)
    """
    # 存在確認のみ (実処理は manager.stop_autonomy に集約)。
    _get_persona_or_404(manager, persona_id)

    # 停止の 4 ステップ (AM 停止 / Track pause / OFF / 対ユーザー Track 復帰) は
    # manager.stop_autonomy に集約。メタ判断連続失敗リカバリと同じ経路を通す。
    result = manager.stop_autonomy(persona_id)

    LOGGER.info(
        "[activity] stop: persona=%s paused_tracks=%s user_track_activated=%s",
        persona_id, result["paused_tracks"], result["user_track_activated"],
    )
    return ActivityToggleResponse(
        success=True,
        message="自律行動を停止して待機に戻しました",
        autonomy_enabled=False,
        autonomy_running=result["autonomy_running"],
    )


