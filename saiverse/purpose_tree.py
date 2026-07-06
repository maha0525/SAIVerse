"""目的の木の統一ビュー — ActionTrack と persona_task を写像で束ねる薄いアダプタ。

life_concept_map.md §3「目的の木」/ §3.1「種・段階・位置」の読み書き面。
**テーブル統合はしない** (§10.1「ActionTrack の行データは残す — 第一階層の
目的ノードへ写像」)。既存の TrackManager / PersonaTaskManager に委譲し、両者の
行を同じ「目的ノード」語彙 (node dict) に写して見せるだけの層である。

- 第一階層 = 生きている ActionTrack (構成系の営みノード等) ＋ 親なし採用ノード
  (persona_task の parent-less / stage=adopted)
- 参照は統一文法 (saiverse/references.py) の ``track:N`` / ``task:N`` に乗る
  (§10.1「short_id 参照は目的ノード参照として意味が広がるだけ」)

node dict の形 (両実体を同じ語彙に写す):
``{"node_kind": "track"|"task"|"step", "ref", "id", "title", "stage",
   "nature", "status", "promoted_from", ...}``

P1 (DB 基盤) 時点では本モジュールはどこからも呼ばれない (休眠)。判断点
playbook の track_op / track_ref enum の目的ノード語彙への改修 (P2〜P5) で
配線される (life_concept_map.md §14)。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from saiverse import references
from saiverse.persona_task_manager import (
    NATURE_PRACTICE,
    NATURE_VENTURE,
    STAGE_ABORTED,
    STAGE_ADOPTED,
    STAGE_CANDIDATE,
    STAGE_COMPLETED,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    STATUS_PENDING,
    PersonaTaskManager,
    TaskNotFoundError,
)
from saiverse.track_manager import (
    LIVE_STATUSES,
    STATUS_ABORTED as TRACK_STATUS_ABORTED,
    STATUS_COMPLETED as TRACK_STATUS_COMPLETED,
    TrackManager,
    TrackNotFoundError,
)

LOGGER = logging.getLogger(__name__)

#: 目的ノードとして「生きている」task status (desire_engine の候補プールと同一)
LIVE_TASK_STATUSES = (STATUS_PENDING, STATUS_ACTIVE, STATUS_PAUSED)

#: 本ビューの操作が刻む actor / origin 識別子
ACTOR = "purpose_tree"


class PurposeTreeError(Exception):
    """Base error for purpose tree operations."""


class NodeNotFoundError(PurposeTreeError):
    """Raised when a node ref cannot be resolved."""


# ---------------------------------------------------------------------------
# 写像 (実体 → node dict)
# ---------------------------------------------------------------------------


def _track_stage(status: str) -> str:
    """Track の状態機械を段階語彙に写す (§10「2 軸の混在」の目的側だけ読む)。

    running/alert/pending/unstarted は時間側の事実であって段階ではない —
    木の中に生きている = adopted。completed/aborted は目的側のライフサイクル。
    """
    if status == TRACK_STATUS_COMPLETED:
        return STAGE_COMPLETED
    if status == TRACK_STATUS_ABORTED:
        return STAGE_ABORTED
    return STAGE_ADOPTED


def _track_to_node(track: Any) -> Dict[str, Any]:
    """ActionTrack 行を目的ノード dict に写す (§10.1: 行データは残し写像で見せる)。

    nature はビュー層の導出: 永続 Track (対ユーザー/交流 = 構成系) は営み
    (practice §3.1 誕生経路「構成」)、非永続 Track は終わりのある企て (venture)。
    """
    ref = (
        references.to_short_ref("track", track.short_id)
        if track.short_id is not None else track.track_id
    )
    return {
        "node_kind": "track",
        "ref": ref,
        "id": track.track_id,
        "title": track.title,
        "stage": _track_stage(track.status),
        "nature": NATURE_PRACTICE if track.is_persistent else NATURE_VENTURE,
        "status": track.status,
        "promoted_from": [],
        "is_persistent": bool(track.is_persistent),
        "intent": track.intent,
    }


def _task_to_node(task: Dict[str, Any]) -> Dict[str, Any]:
    """persona_task dict (PersonaTaskManager 直列化) を目的ノード dict に写す。"""
    return {
        "node_kind": "task",
        "ref": task.get("task_ref") or task["id"],
        "id": task["id"],
        "title": task.get("title"),
        "stage": task.get("stage"),  # PersonaTaskManager 側で実効値 (NULL は導出) 済み
        "nature": task.get("nature"),
        "status": task.get("status"),
        "promoted_from": task.get("promoted_from") or [],
        "source_ref": task.get("desire_source"),
        "parent_kind": task.get("parent_kind"),
        "track_id": task.get("track_id"),
        "note_id": task.get("note_id"),
    }


def _step_to_node(step: Dict[str, Any]) -> Dict[str, Any]:
    """persona_task_step dict を末端ノード dict に写す (§3「葉: タスク → ステップ」)。"""
    done = step.get("status") == "completed"
    return {
        "node_kind": "step",
        "ref": step["id"],
        "id": step["id"],
        "title": step.get("title"),
        "stage": STAGE_COMPLETED if done else STAGE_ADOPTED,
        "nature": None,
        "status": step.get("status"),
        "promoted_from": [],
    }


# ---------------------------------------------------------------------------
# 読み出し
# ---------------------------------------------------------------------------


def list_first_tier(manager: Any, persona_id: str) -> List[Dict[str, Any]]:
    """木の第一階層を返す (§3.1 / §10)。

    第一階層 = 生きている ActionTrack (LIVE_STATUSES、忘却済み除外) ＋
    親なし採用ノード (persona_task の parent-less / 実効 stage=adopted / 生存
    status)。候補 (stage=candidate) は木の外なので含めない (§3.1: 段階の区別)。
    「第一階層は少数・粗粒に保つ」(§10) の手入れ対象を読むのはこの関数。
    """
    tm = TrackManager(session_factory=manager.SessionLocal)
    nodes = [
        _track_to_node(t)
        for t in tm.list_for_persona(persona_id, statuses=LIVE_STATUSES)
    ]
    ptm = PersonaTaskManager(manager.SessionLocal)
    for task in ptm.list_tasks(
        persona_id, statuses=LIVE_TASK_STATUSES, include_steps=False
    ):
        if task.get("parent_kind") is not None:
            continue  # 親がある = 第一階層でない
        if task.get("stage") != STAGE_ADOPTED:
            continue  # 候補は木の外 (§3.1)
        nodes.append(_task_to_node(task))
    return nodes


def resolve_ref(manager: Any, persona_id: str, ref: str) -> Dict[str, Any]:
    """統一文法の参照 (``track:N`` / ``task:N`` / UUID / saiverse:// URI) を
    目的ノード dict に解決する (§8.1 / reference_addressing.md への相乗り)。

    Raises:
        NodeNotFoundError: 解決できないとき (未知 kind・実体なし)。
    """
    text = (ref or "").strip()
    if not text:
        raise NodeNotFoundError("empty node reference")
    try:
        parsed = references.parse_ref(text)
        kind, key = parsed.kind, parsed.key
    except ValueError:
        # 素の UUID (track は 36 桁ハイフンあり / task は 32 桁 hex)
        if len(text) == 36 and text.count("-") == 4:
            kind, key = "track", text
        elif len(text) == 32 and "-" not in text:
            kind, key = "task", text
        else:
            raise NodeNotFoundError(f"unresolvable node reference: {ref!r}")
    if kind == "track":
        tm = TrackManager(session_factory=manager.SessionLocal)
        try:
            track_id = tm.resolve_track_ref(
                persona_id, key if not key.isdigit() else f"track:{key}"
            )
            return _track_to_node(tm.get(track_id))
        except TrackNotFoundError as exc:
            raise NodeNotFoundError(str(exc)) from exc
    if kind == "task":
        ptm = PersonaTaskManager(manager.SessionLocal)
        try:
            task_id = ptm.resolve_task_ref(
                persona_id, key if not key.isdigit() else f"task:{key}"
            )
            return _task_to_node(ptm.get_task(task_id, persona_id=persona_id))
        except TaskNotFoundError as exc:
            raise NodeNotFoundError(str(exc)) from exc
    raise NodeNotFoundError(
        f"reference kind {kind!r} is not a purpose node: {ref!r}"
    )


def list_children(manager: Any, persona_id: str, node_ref: str) -> List[Dict[str, Any]]:
    """ノードの子を返す — 木はフラクタル (§3「どの階層も目的の細分化」)。

    - track ノード → その Track に束ねられた persona_task (Track 内小目標)
    - task ノード → その steps (末端 §3「葉: タスク → ステップ」)
    - step ノード → 空 (これ以上の細分化は現行スキーマに無い)
    """
    node = resolve_ref(manager, persona_id, node_ref)
    ptm = PersonaTaskManager(manager.SessionLocal)
    if node["node_kind"] == "track":
        return [
            _task_to_node(t)
            for t in ptm.list_tasks(
                persona_id, track_id=node["id"], include_steps=False
            )
        ]
    if node["node_kind"] == "task":
        task = ptm.get_task(node["id"], persona_id=persona_id)
        return [_step_to_node(s) for s in task.get("steps", [])]
    return []


# ---------------------------------------------------------------------------
# 書き込み
# ---------------------------------------------------------------------------


def create_candidate(
    manager: Any, persona_id: str, title: str, source_ref: str
) -> Dict[str, Any]:
    """候補ノードを作る (§3.1: 収穫されて初めて候補が生まれる)。

    ``source_ref`` は必須 = 接地原則 (§3.1 来歴リンク / §12 主張の接地)。
    mark 参照・実経験の引用など「この候補が何から生まれたか」を必ず刻む。
    候補は木の外 (stage=candidate) に親なしで置かれ、adopt で木に接がれる。
    規模は宣言でなく獲得 (§3.1) — 候補は常に小さく生まれる。
    """
    if not source_ref or not str(source_ref).strip():
        raise ValueError("source_ref is required (接地原則: 候補は必ず来歴を持つ)")
    ptm = PersonaTaskManager(manager.SessionLocal)
    task = ptm.create_task(
        persona_id=persona_id,
        title=title,
        goal=title,
        origin=ACTOR,
        actor=ACTOR,
        auto_activate=False,
        desire_source=str(source_ref).strip(),
        stage=STAGE_CANDIDATE,
    )
    LOGGER.info(
        "[purpose_tree] candidate created: persona=%s ref=%s source=%s",
        persona_id, task.get("task_ref"), source_ref,
    )
    return _task_to_node(task)


def adopt(
    manager: Any,
    persona_id: str,
    candidate_ref: str,
    parent_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """候補を採用して木に接ぐ (§3.1: desire と task は同一実体の段階違い)。

    行をコピー/破棄せず段階と親だけ変える (来歴・履歴は連続する)。

    - ``parent_ref=None``: 収まる親が無い → 第一階層に**小さく**立つ
      (§3.1 誕生経路「採用時の親なし」— 短命でよい・蛍)。親バインドを外し
      parent-less の採用ノードにする。
    - ``parent_ref='track:N'``: その Track (第一階層ノード) の下に接ぐ。
    - task を親にする接ぎ木は現行スキーマ (persona_task に自己参照なし) では
      表現できないため ValueError (§10.1: テーブル統合はしない — 写像の限界)。

    Raises:
        NodeNotFoundError: candidate_ref / parent_ref が解決できないとき。
        PurposeTreeError: candidate_ref が候補 (stage=candidate) でないとき。
    """
    node = resolve_ref(manager, persona_id, candidate_ref)
    if node["node_kind"] != "task" or node["stage"] != STAGE_CANDIDATE:
        raise PurposeTreeError(
            f"not a candidate node: {candidate_ref!r} "
            f"(kind={node['node_kind']}, stage={node['stage']})"
        )
    ptm = PersonaTaskManager(manager.SessionLocal)
    task_id = node["id"]
    if parent_ref is None:
        if node.get("parent_kind") is not None:
            # 旧 desire 実装 (parent_kind='note') の正規化 (§10.1)。
            ptm.detach_parent(task_id, persona_id=persona_id, actor=ACTOR)
    else:
        parent = resolve_ref(manager, persona_id, parent_ref)
        if parent["node_kind"] != "track":
            raise ValueError(
                f"unsupported parent kind for adopt: {parent['node_kind']!r} "
                "(current schema binds tasks to tracks only)"
            )
        ptm.promote_to_track(
            task_id, parent["id"], persona_id=persona_id, actor=ACTOR
        )
    task = ptm.set_purpose_fields(
        task_id, persona_id=persona_id, actor=ACTOR, stage=STAGE_ADOPTED
    )
    LOGGER.info(
        "[purpose_tree] adopted: persona=%s ref=%s parent=%s",
        persona_id, task.get("task_ref"), parent_ref,
    )
    return _task_to_node(task)


def name_theme(
    manager: Any, persona_id: str, title: str, member_refs: Sequence[str]
) -> Dict[str, Any]:
    """航跡クラスタに名前を付けて第一階層ノードを立てる (§3.1 誕生経路「命名」)。

    「Track は作るものでも太るものでもなく、命名された航跡」— 完了ノード・
    休眠欲求・mark の繰り返しに事後に名前を付け、過去に未来への入札権を与える
    (§3.1 束ねの効果: アドレス・磁石・顔・書庫)。テーマノードは親なしの採用
    ノード (persona_task 行) として立ち、``promoted_from`` にメンバー参照群を
    来歴リンクとして刻む (命名は航跡を木に変換する唯一の操作)。

    メンバー行そのものは動かさない (歴史は歴史のまま。タグは後付け・多対多 §9)。

    Raises:
        NodeNotFoundError: member_refs に解決できない参照があるとき (接地の検証)。
    """
    if not title or not title.strip():
        raise ValueError("title is required")
    members = [str(r).strip() for r in (member_refs or []) if str(r).strip()]
    if not members:
        raise ValueError("member_refs is required (テーマは航跡クラスタへの命名 — 空では立てられない)")
    # 接地の検証: 全メンバーが実在するノードであること。正規 ref に揃えて刻む。
    canonical: List[str] = []
    for m in members:
        canonical.append(resolve_ref(manager, persona_id, m)["ref"])
    ptm = PersonaTaskManager(manager.SessionLocal)
    task = ptm.create_task(
        persona_id=persona_id,
        title=title.strip(),
        goal=title.strip(),
        origin=ACTOR,
        actor=ACTOR,
        auto_activate=False,
        stage=STAGE_ADOPTED,
        promoted_from=canonical,
    )
    LOGGER.info(
        "[purpose_tree] theme named: persona=%s ref=%s members=%s",
        persona_id, task.get("task_ref"), canonical,
    )
    return _task_to_node(task)
