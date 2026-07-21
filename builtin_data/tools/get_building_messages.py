"""Get new messages from building history and add to persona history.

W5/M8 (記憶監査「Building→個人記憶の転記が cursor を先行確定」) の転記規律:

- cursor は**連続して消費できた最大 seq** までしか進めない。転記 (memory 書き
  込み / ingested marker) が失敗したらそのラウンドはその場で停止し、cursor は
  失敗メッセージの手前に留まる — 次のラウンドが同じ場所から再試行する。
- 「消費」= ルール上のスキップ (heard_by 外 / 取り込み済み / 空 content /
  occupancy イベント等) または転記成功 (memory append + marker の両方成功)。
- 冪等性: 転記 entry の metadata に ``building_msg_ref`` (= "{building_id}:
  {message_id}") を刻む。「append 成功 → marker 失敗」で宙に浮いた転記は停止
  規律により常に高々 1 件 = 次ラウンドの最初の転記候補なので、その 1 件だけ
  memory.db へ provenance 照会し、既在なら append を跳ばして marker の修復のみ
  行う (再起動を跨いでも欠落も重複もしない)。
"""
from __future__ import annotations

import copy
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from tools.context import get_active_persona_id, get_active_manager
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

#: 転記 entry の metadata に刻む provenance キー
#: (saiverse_memory.adapter.SAIMemoryAdapter.BUILDING_MSG_REF_META_KEY と同名)。
BUILDING_MSG_REF_KEY = "building_msg_ref"


class _TranscribeFailed(RuntimeError):
    """転記の失敗 (memory 書き込みが failed)。ラウンドはここで停止する。"""


def _msg_seq(msg: Dict[str, Any]) -> int:
    try:
        return int(msg.get("seq", 0))
    except (TypeError, ValueError):
        return 0


def _building_msg_ref(msg: Dict[str, Any]) -> Optional[str]:
    building_id = msg.get("_building_id") or msg.get("building_id")
    message_id = msg.get("message_id")
    if not building_id or not message_id:
        return None
    return f"{building_id}:{message_id}"


def _memory_has_building_ref(persona: Any, ref: str) -> bool:
    """memory.db に同じ provenance キーの転記が既在するか (M8 の冪等照会)。

    照会機能そのものが無い (adapter 未対応・旧テストスタブ) 場合は「見つから
    なかった」に倒す — 冪等修復という付加機能が使えないだけで、append 自体は
    通常どおり試みる。

    照会の**実行が失敗した** (DB ロック等の一時的な例外) 場合は、ここでは
    吸収しない — 呼び出し元 (:func:`_append_and_mark`) が例外を伝播させて
    ラウンドを止める (2026-07-21 Codex レビュー P2)。「見つからなかった」に
    倒すと、たまたま append 自体は成功してしまうケースで、既に転記済みの
    内容を二重保存する — 照会失敗を append 失敗と同一視できないため、
    「未確認」は「見つからなかった」より安全側 (停止) に倒す必要がある。
    """
    adapter = getattr(persona, "sai_memory", None)
    finder = getattr(adapter, "find_message_by_building_ref", None)
    if finder is None:
        return False
    return finder(ref) is not None


def _mark_ingested(msg: Dict[str, Any], persona_id: str, manager: Any) -> None:
    """Mark a message as ingested by this persona (DB single-source-of-truth)。

    manager は**引数で受ける** (W5/M8): 旧実装は tools.context の
    ``get_active_manager()`` に頼っていたが、pulse 開始時の auto_ingest は
    persona_context の外で走るため常に None になり、DB の ingested_by 更新が
    静かにスキップされていた (cursor 先行バグと相殺して隠れていた)。

    DB エラーは raise する (database.building_messages.mark_ingested 準拠) —
    呼び出し元のラウンド停止規律がマーク失敗を跨いだ cursor 確定を防ぐ。
    in-memory dict の更新は同ラウンド内の整合のためだけに行う (building
    history は毎回 DB から読み直されるコピー)。
    """
    bucket = msg.setdefault("ingested_by", [])
    if isinstance(bucket, list) and persona_id not in bucket:
        bucket.append(persona_id)
    session_factory = getattr(manager, "SessionLocal", None) if manager else None
    building_id = msg.get("_building_id") or msg.get("building_id")
    message_id = msg.get("message_id")
    if session_factory and building_id and message_id:
        from database.building_messages import mark_ingested as db_mark_ingested
        db_mark_ingested(session_factory, building_id, message_id, persona_id)


def _append_and_mark(
    entry: Dict[str, Any],
    m: Dict[str, Any],
    *,
    persona: Any,
    manager: Any,
    persona_id: str,
    history_manager: Any,
    origin_track_id: Optional[str],
    check_provenance: bool,
) -> None:
    """転記 entry を memory 先行で追記し、成功後に ingested marker を立てる。

    順序契約 (M8): 「memory append 成功 → marker 成功」を確認できたものだけが
    「転記成功」。途中失敗は例外 = ラウンド停止。
    """
    ref = _building_msg_ref(m)
    if ref:
        metadata = entry.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata[BUILDING_MSG_REF_KEY] = ref
    already_recorded = False
    if check_provenance and ref:
        try:
            already_recorded = _memory_has_building_ref(persona, ref)
        except Exception as exc:
            # 照会の成否が「未確認」のまま append へ進むと、実は既に転記済み
            # の内容を二重保存しかねない — ラウンドを停止し、次ラウンドで
            # 同じ候補から再照会させる (2026-07-21 Codex レビュー P2)。
            LOGGER.warning(
                "[building_ingest] provenance lookup failed (ref=%s)",
                ref, exc_info=True,
            )
            raise _TranscribeFailed(
                f"provenance lookup failed (ref={ref})"
            ) from exc
    if already_recorded:
        LOGGER.info(
            "[building_ingest] provenance repair: memory already has %s; "
            "re-marking only (no duplicate append)", ref,
        )
    else:
        status, _mid = history_manager.add_to_persona_only(
            entry, origin_track_id=origin_track_id, memory_first=True
        )
        if status == "failed":
            raise _TranscribeFailed(f"memory append failed (ref={ref})")
    _mark_ingested(m, persona_id, manager)


def _transcribe_message(
    m: Dict[str, Any],
    *,
    persona: Any,
    manager: Any,
    persona_id: str,
    building_id: str,
    history_manager: Any,
    id_to_name_map: Dict[str, str],
    origin_track_id: Optional[str],
    check_provenance: bool,
) -> Tuple[str, Optional[str], bool]:
    """building message 1 件をペルソナ記憶へ転記する (tool 版 / auto 版の共通核)。

    Returns:
        ``(outcome, speaker_label, append_attempted)``。outcome は
        ``"ingested"`` (転記した) / ``"consumed_private"`` (発話はあったが
        非公開部のみでマークだけ = tool 版は件数に数える) / ``"consumed"``
        (ルール上の消費 — マークのみ or 何もせず)。

    Raises:
        転記失敗 (memory append の failed / marker の DB エラー) — 呼び出し元は
        ラウンドを停止し、cursor をこのメッセージの手前に留める。
    """
    role = m.get("role")
    pid = m.get("persona_id")
    content = m.get("content", "")

    # Skip if already ingested
    ingested = m.get("ingested_by") or []
    if isinstance(ingested, list) and persona_id in ingested:
        return ("consumed", None, False)

    # Skip empty and system-like summary notes
    if not content or ("note-box" in content and role == "assistant"):
        return ("consumed", None, False)

    # Convert other personas' messages to user role
    if role == "assistant" and pid and pid != persona_id:
        from saiverse.content_tags import strip_for_other_persona
        speaker = id_to_name_map.get(pid, pid)
        stripped = strip_for_other_persona(content)
        if stripped is None:
            _mark_ingested(m, persona_id, manager)
            return ("consumed_private", speaker, False)
        entry: Dict[str, Any] = {
            "role": "user",
            "content": f"{speaker}: {stripped}",
        }
        metadata = m.get("metadata")
        entry["metadata"] = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
        entry["metadata"]["with"] = [pid]
        ts_value = m.get("timestamp")
        if isinstance(ts_value, str):
            entry["timestamp"] = ts_value
        _append_and_mark(
            entry, m,
            persona=persona, manager=manager, persona_id=persona_id,
            history_manager=history_manager, origin_track_id=origin_track_id,
            check_provenance=check_provenance,
        )
        return ("ingested", speaker, True)

    # System / movement / world events (role="host"). These come from
    # OccupancyManager.move_entity, world event broadcasts, etc. They use
    # note-box HTML for the chat UI. We strip the markup and inject them as a
    # system notification in the persona's memory.
    #
    # Filter rules:
    #  - Occupancy events (自分 / 他者問わず): head_pipeline の BuildingSection /
    #    BuildingOccupantsSection が diff 通知を出す。この経路は撤去済み
    #    (= 旧 A-3-a の rich format も pipeline 側に統合済み)。
    #  - User-related events (logout/login etc): presence は
    #    BuildingOccupantsSection の diff で扱う。
    #  - Legacy logout/login messages without metadata: filter by content match.
    #
    # Building name is prepended so the notification is self-contained in the
    # persona's memory (the original event text leaves the destination
    # implicit = "this building").
    if role == "host":
        metadata_obj = m.get("metadata") or {}
        event_obj = metadata_obj.get("event") or {}
        if event_obj.get("type") == "occupancy":
            _mark_ingested(m, persona_id, manager)
            return ("consumed", None, False)
        if event_obj.get("entity_type") == "user":
            _mark_ingested(m, persona_id, manager)
            return ("consumed", None, False)
        if "オフラインになりました" in content or "オンラインになりました" in content:
            _mark_ingested(m, persona_id, manager)
            return ("consumed", None, False)
        building_obj = getattr(persona, "buildings", {}).get(building_id)
        building_name = getattr(building_obj, "name", building_id) if building_obj else building_id
        cleaned = re.sub(r"<[^>]+>", "", content).strip()
        if not cleaned:
            return ("consumed", None, False)
        entry = {
            "role": "user",
            "content": f"<system>[{building_name}] {cleaned}</system>",
            "metadata": {"tags": ["internal", "event_message"]},
        }
        ts_value = m.get("timestamp")
        if isinstance(ts_value, str):
            entry["timestamp"] = ts_value
        # host event は世界の変化通知 = Track 横断のメタログ扱いで
        # origin_track_id は付けない (handoff_2026-05-10)。
        _append_and_mark(
            entry, m,
            persona=persona, manager=manager, persona_id=persona_id,
            history_manager=history_manager, origin_track_id=None,
            check_provenance=check_provenance,
        )
        return ("ingested", "システム", True)

    # Ingest user messages directly
    if role == "user" and (pid is None or pid != persona_id):
        entry = {"role": "user", "content": content}
        metadata = m.get("metadata")
        entry["metadata"] = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
        entry["metadata"]["with"] = ["user"]
        ts_value = m.get("timestamp")
        if isinstance(ts_value, str):
            entry["timestamp"] = ts_value
        _append_and_mark(
            entry, m,
            persona=persona, manager=manager, persona_id=persona_id,
            history_manager=history_manager, origin_track_id=origin_track_id,
            check_provenance=check_provenance,
        )
        return ("ingested", "ユーザー", True)

    # 上記のどの型でもない (自分自身の発話等) — 従来どおり対象外の素通り。
    return ("consumed", None, False)


def _ingest_round(
    *,
    persona: Any,
    manager: Any,
    history_manager: Any,
    building_id: str,
    persona_id: str,
    id_to_name_map: Dict[str, str],
    origin_track_id: Optional[str],
    log_prefix: str,
) -> Tuple[int, int, Dict[str, int]]:
    """building history の未読分を 1 ラウンド転記する (tool 版 / auto 版の共通核)。

    cursor 規律 (M8): ラウンド完走なら従来どおり見た最大 seq まで前進。途中で
    転記が失敗したら停止し、cursor は「失敗メッセージの手前」= 連続消費の水位
    に留める (次ラウンドが同じメッセージから再試行する)。

    Returns:
        ``(ingested_count, private_count, perceived_counts)``。
    """
    hist = history_manager.get_building_history(building_id)
    if not hist:
        return (0, 0, {})
    # _mark_ingested で DB UPDATE する時に building_id を引けるよう埋め込む
    for _m in hist:
        _m["_building_id"] = building_id

    pulse_cursors = getattr(persona, "pulse_cursors", None)
    if pulse_cursors is None:
        persona.pulse_cursors = {}
        pulse_cursors = persona.pulse_cursors
    entry_markers = getattr(persona, "entry_markers", {})
    last_cursor = pulse_cursors.get(building_id, 0)
    entry_limit = entry_markers.get(building_id, last_cursor)

    candidates: List[Dict[str, Any]] = []
    max_seen_seq = last_cursor
    for msg in hist:
        seq = _msg_seq(msg)
        max_seen_seq = max(max_seen_seq, seq)
        if seq <= last_cursor or seq <= entry_limit:
            continue
        heard_by = msg.get("heard_by") or []
        if persona_id not in heard_by:
            LOGGER.debug(
                "%s %s skipping seq=%d (not in heard_by=%s)",
                log_prefix, persona_id, seq, heard_by,
            )
            continue
        candidates.append(msg)
    candidates.sort(key=_msg_seq)

    ingested_count = 0
    private_count = 0
    perceived: Dict[str, int] = {}
    # 最初の転記候補だけ provenance 照会 (M8: 宙に浮いた転記は高々 1 件で、
    # 必ず次ラウンドの最初の転記候補になる — 走査をラウンド 1 回に有界化)。
    provenance_pending = True
    stop_seq: Optional[int] = None
    for m in candidates:
        seq = _msg_seq(m)
        try:
            outcome, label, attempted = _transcribe_message(
                m,
                persona=persona, manager=manager, persona_id=persona_id,
                building_id=building_id, history_manager=history_manager,
                id_to_name_map=id_to_name_map, origin_track_id=origin_track_id,
                check_provenance=provenance_pending,
            )
        except Exception:
            LOGGER.warning(
                "%s transcription failed at seq=%d (building=%s persona=%s); "
                "stopping this round — cursor stays before the failed message "
                "so the next round retries it",
                log_prefix, seq, building_id, persona_id, exc_info=True,
            )
            stop_seq = seq
            break
        if attempted:
            provenance_pending = False
        if outcome == "ingested":
            ingested_count += 1
            if label:
                perceived[label] = perceived.get(label, 0) + 1
        elif outcome == "consumed_private":
            private_count += 1
            if label:
                perceived[label] = perceived.get(label, 0) + 1

    if stop_seq is None:
        pulse_cursors[building_id] = max_seen_seq
    else:
        pulse_cursors[building_id] = max(last_cursor, stop_seq - 1)
    return (ingested_count, private_count, perceived)


def get_building_messages(building_id: Optional[str] = None) -> str:
    """Get new messages from building history that this persona hasn't seen yet.

    - Filters by heard_by (messages the persona could hear)
    - Filters by ingested_by (messages not yet processed)
    - Converts other personas' messages to user role with speaker prefix
    - Adds messages to persona history
    - Marks messages as ingested

    Returns a summary of perceived messages.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    manager = get_active_manager()
    if not manager:
        raise RuntimeError("Manager reference is not available")

    # Get persona from manager
    persona = manager.all_personas.get(persona_id)
    if not persona:
        raise RuntimeError(f"Persona {persona_id} not found in manager")

    # Use current building if not specified
    if not building_id:
        building_id = getattr(persona, "current_building_id", None)
    if not building_id:
        raise RuntimeError("Building ID not specified and persona has no current building")

    history_manager = getattr(persona, "history_manager", None)
    if not history_manager:
        raise RuntimeError("Persona has no history_manager")

    # Get id_to_name_map for speaker name resolution
    id_to_name_map = getattr(persona, "id_to_name_map", {})

    # Resolve origin_track_id from the persona's currently-running Track so the
    # ingested messages can be persisted with the correct Track scope. Tool-path
    # callers come in mid-Pulse, so a running Track is normally present; if not
    # we leave it None (matches pre-fix behavior).
    origin_track_id: Optional[str] = None
    try:
        track_manager = getattr(manager, "track_manager", None)
        if track_manager is not None:
            running = track_manager.get_running(persona_id)
            if running is not None:
                origin_track_id = running.track_id
    except Exception:
        LOGGER.debug("get_building_messages: failed to resolve running track", exc_info=True)

    ingested_count, private_count, perceived = _ingest_round(
        persona=persona, manager=manager, history_manager=history_manager,
        building_id=building_id, persona_id=persona_id,
        id_to_name_map=id_to_name_map, origin_track_id=origin_track_id,
        log_prefix="[get_building_messages]",
    )

    perceived_count = ingested_count + private_count
    if perceived_count == 0:
        return "新規メッセージはありません"

    details = ", ".join(f"{name}: {count}件" for name, count in perceived.items())
    return f"{perceived_count}件の新規メッセージを認識しました（{details}）"


def auto_ingest_building_messages(
    persona: Any,
    manager: Any,
    *,
    origin_track_id: Optional[str] = None,
) -> int:
    """Automatically ingest new building messages at pulse start.

    Mirrors get_building_messages() but takes persona/manager directly
    instead of using tool context vars. Called automatically before each pulse.

    ``origin_track_id`` is forwarded to ``add_to_persona_only`` so the ingested
    user/assistant/host messages persist with the correct Track scope in
    SAIMemory (handoff_2026-05-10). The caller (``run_meta_user``) supplies
    this from the Pulse-bound Track.

    Returns the number of messages ingested.
    """
    persona_id = getattr(persona, "persona_id", None)
    building_id = getattr(persona, "current_building_id", None)
    if not persona_id or not building_id:
        return 0

    history_manager = getattr(persona, "history_manager", None)
    if not history_manager:
        return 0

    id_to_name_map = getattr(manager, "id_to_name_map", {})

    pulse_cursors = getattr(persona, "pulse_cursors", None) or {}
    LOGGER.info(
        "[auto_ingest] %s building=%s last_cursor=%d",
        persona_id, building_id, pulse_cursors.get(building_id, 0),
    )

    ingested_count, _private_count, _perceived = _ingest_round(
        persona=persona, manager=manager, history_manager=history_manager,
        building_id=building_id, persona_id=persona_id,
        id_to_name_map=id_to_name_map, origin_track_id=origin_track_id,
        log_prefix="[auto_ingest]",
    )

    if ingested_count:
        LOGGER.info(
            "[auto_ingest] %s ingested %d new message(s) from building %s",
            persona_id, ingested_count, building_id,
        )

    return ingested_count


def schema() -> ToolSchema:
    return ToolSchema(
        name="get_building_messages",
        description="Get new messages from building history that this persona hasn't seen yet. Adds them to persona history.",
        parameters={
            "type": "object",
            "properties": {
                "building_id": {
                    "type": "string",
                    "description": "Building ID to get messages from. Defaults to current building."
                }
            },
            "required": [],
        },
        result_type="string",
    )
