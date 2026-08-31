"""継承エッジ (継承 DAG) の操作モジュール — 範囲ノード間の認識の連続性。

experience_structure.md §3.3「継承 DAG (認識の連続性)」の永続化レイヤー。
包含の木 (saiverse/episodes.py の kind + occurrence 親子) とは**独立**な、
体験の連続性を表す有向エッジ (``episode_inheritance`` テーブル、
database/models.py) を CRUD する。

設計上の約束:

- **継承 ≠ 時刻**。エッジは created_at の代用ではない (§3.3)。時刻は
  Episode の属性、語りの文脈はこのエッジに従う。
- エッジは**層の型**を持つ: :data:`LAYER_FACT` (事実層 — スレッド継続・
  リプランティング・分岐再生成の直接の元) / :data:`LAYER_DIGEST` (咀嚼層 —
  メモリ・digest 経由で知っている非直接親)。
- **記帳は範囲が開いた瞬間に機械的** (§11-4): 呼び出し元が「どれを前駆と
  するか」の選択を渡せば、LLM 判断なしに決定論で行が刻まれる。選択が無ければ
  エッジ 0 本 = 直列の縮退 (既存データはこれで無害)。
- 範囲ノードは ``episode:N`` / UUID どちらの参照でも受け、EPISODE_ID (UUID) に
  解決して格納する (saiverse/episodes.py._resolve_episode_id を再利用)。
- 時刻刻印は必ず ``saiverse.clock.now()`` 経由 (仮想クロック尊重、Episode と同じ)。
- DB access は ``manager.SessionLocal()`` → try/finally close の既存流儀。
  ``open_episode`` の予約 tx に相乗りする記帳は :func:`record_edges` に
  ``session=`` を渡す (episodes.py の session モードと同じ口)。

W13 (体験の構造 工程(3)) のスコープは**器 + 範囲オープン時の機械的記帳**まで。
継承チェーンに閉じた咀嚼生成 (§3.3 生成規律) や分岐・再生成 UI・メティス
取り込みの配線は後続 wave。本モジュールはその前提となる記帳と探索の口を持つ。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from database.models import Episode, EpisodeInheritance
from saiverse import clock
from saiverse.episodes import _resolve_episode_id  # 参照解決を一点管理で再利用

LOGGER = logging.getLogger(__name__)

# --- 層の型 (experience_structure.md §3.3) ---
LAYER_FACT = "fact"      # 事実層の継承 (生ログごと繋がる — スレッド継続・リプランティング)
LAYER_DIGEST = "digest"  # 咀嚼層の継承 (メモリ・digest 経由で知っている)
INHERITANCE_LAYERS = frozenset({LAYER_FACT, LAYER_DIGEST})


class InheritanceError(Exception):
    """Base error for inheritance-edge operations."""


def _now_epoch() -> int:
    """現在時刻 (epoch 秒)。仮想クロック尊重のため必ず clock.now() を通す。"""
    return int(clock.now().timestamp())


def _resolve_owned_episode_id(db: Session, persona_id: str, ref: str) -> str:
    """``ref`` を EPISODE_ID に解決し、**その行が当該ペルソナに存在する**ことを確かめる。

    :func:`saiverse.episodes._resolve_episode_id` は ``episode:N`` は persona 付きで
    照会するが、**UUID は形式検査だけで存在も所属も見ない**。継承エッジの参照先には
    外部キーが無く、記帳の親参照は将来 (分岐再生成・並列統合・メティス取り込み) の
    呼び出し元から来る外部入力なので、別ペルソナや存在しない UUID をそのまま
    自分の継承グラフへ焼き込む穴になる。両端をここで存在 + 所属まで検証して塞ぐ
    (``episode:N`` 経路も冪等に通る二重確認)。
    """
    episode_id = _resolve_episode_id(db, persona_id, ref)
    exists = (
        db.query(Episode.EPISODE_ID)
        .filter(Episode.EPISODE_ID == episode_id, Episode.PERSONA_ID == persona_id)
        .first()
    )
    if exists is None:
        raise InheritanceError(
            f"episode reference does not resolve to an episode owned by persona "
            f"{persona_id!r}: {ref!r}"
        )
    return episode_id


def _to_dict(edge: EpisodeInheritance) -> Dict[str, Any]:
    """detached な dict に直列化する (ORM オブジェクトを外に出さない)。"""
    try:
        meta = json.loads(edge.META_JSON) if edge.META_JSON else None
    except (TypeError, ValueError):
        LOGGER.warning("[inheritance] META_JSON is not valid JSON: %r", edge.META_JSON)
        meta = None
    return {
        "edge_id": edge.EDGE_ID,
        "persona_id": edge.PERSONA_ID,
        "child_episode_id": edge.CHILD_EPISODE_ID,
        "parent_episode_id": edge.PARENT_EPISODE_ID,
        "layer": edge.LAYER,
        "anchor_ref": edge.ANCHOR_REF,
        "origin": edge.ORIGIN,
        "created_at": edge.CREATED_AT,
        "meta": meta,
    }


def _normalize_predecessor(spec: Any) -> Dict[str, Any]:
    """前駆の指定 (dict) を検証して正規化する。

    受け付ける形:
        ``{"parent_ref": "episode:3", "layer": "fact",
           "anchor_ref": "message:...", "origin": "branch", "meta": {...}}``

    ``parent_ref`` / ``layer`` は必須。``layer`` は :data:`INHERITANCE_LAYERS`。
    ``anchor_ref`` / ``origin`` / ``meta`` は任意。
    """
    if not isinstance(spec, dict):
        raise InheritanceError(
            f"predecessor must be a dict, got {type(spec).__name__}: {spec!r}"
        )
    parent_ref = spec.get("parent_ref")
    if not parent_ref or not isinstance(parent_ref, str):
        raise InheritanceError(f"predecessor requires a non-empty 'parent_ref': {spec!r}")
    layer = spec.get("layer")
    if layer not in INHERITANCE_LAYERS:
        raise InheritanceError(
            f"invalid inheritance layer: {layer!r} "
            f"(expected one of {sorted(INHERITANCE_LAYERS)})"
        )
    meta = spec.get("meta")
    if meta is not None and not isinstance(meta, dict):
        raise InheritanceError(f"predecessor 'meta' must be a dict or None: {meta!r}")
    return {
        "parent_ref": parent_ref.strip(),
        "layer": layer,
        "anchor_ref": spec.get("anchor_ref"),
        "origin": spec.get("origin"),
        "meta": meta,
    }


def _ancestor_ids(db: Session, persona_id: str, start: str) -> set:
    """``start`` から child→parent エッジを層無視で辿った**全**祖先 EPISODE_ID 集合。

    書き込み時の非巡回検査 (:func:`_insert_edges`) 用。session 内で flush 済みの
    エッジも見えるので、同一バッチで先に刻んだ辺も反映される。層は問わない
    (どの層の組み合わせでも巡回は巡回 — DAG 不変条件はノードグラフ全体の性質)。

    深さ上限は置かない — 上限で打ち切ると 129 段先の祖先を見逃して循環を通す。
    ``visited`` 集合が単調増加でノード数有限なので停止は保証される (万一既存
    データが壊れて循環していても visited が再訪を止める)。
    """
    visited: set = set()
    frontier = [start]
    while frontier:
        rows = (
            db.query(EpisodeInheritance.PARENT_EPISODE_ID)
            .filter(
                EpisodeInheritance.PERSONA_ID == persona_id,
                EpisodeInheritance.CHILD_EPISODE_ID.in_(frontier),
            )
            .all()
        )
        parents = {r[0] for r in rows}
        new_parents = parents - visited
        visited |= new_parents
        frontier = list(new_parents)
    return visited


def _insert_edges(
    db: Session,
    persona_id: str,
    child_episode_id: str,
    predecessors: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """検証済み child + 正規化済み predecessors から重複を避けてエッジを刻む。

    ``db`` に add/flush するだけで commit はしない (呼び出し元 tx が確定させる)。
    自己ループ (parent == child) と**多段の循環** (parent が child の子孫 =
    child が parent の祖先) は DAG 不変条件を破るので弾く (§3.3「継承 DAG」)。
    同一 (子, 親, 層) の既存エッジは冪等に読み替え (再挿入しない)。
    """
    now = _now_epoch()
    out: List[Dict[str, Any]] = []
    seen_in_batch: set = set()
    for spec in predecessors:
        parent_episode_id = _resolve_owned_episode_id(db, persona_id, spec["parent_ref"])
        layer = spec["layer"]
        if parent_episode_id == child_episode_id:
            raise InheritanceError(
                f"inheritance edge cannot be a self-loop (episode {child_episode_id})"
            )
        # 多段循環の検査: child→parent を足すと parent は child の祖先になる。
        # もし child が既に parent の祖先なら (parent→…→child→parent) 巡回する。
        if child_episode_id in _ancestor_ids(db, persona_id, parent_episode_id):
            raise InheritanceError(
                f"inheritance edge would create a cycle: "
                f"{child_episode_id} is already an ancestor of {parent_episode_id}"
            )
        key = (parent_episode_id, layer)
        if key in seen_in_batch:
            # 同一呼び出し内の重複指定は 1 本に畳む (最初を採用)。
            continue
        seen_in_batch.add(key)

        existing = (
            db.query(EpisodeInheritance)
            .filter(
                EpisodeInheritance.CHILD_EPISODE_ID == child_episode_id,
                EpisodeInheritance.PARENT_EPISODE_ID == parent_episode_id,
                EpisodeInheritance.LAYER == layer,
            )
            .first()
        )
        if existing is not None:
            LOGGER.debug(
                "[inheritance] edge already exists (idempotent): %s -%s-> %s",
                child_episode_id, layer, parent_episode_id,
            )
            out.append(_to_dict(existing))
            continue

        meta = spec.get("meta")
        edge = EpisodeInheritance(
            EDGE_ID=str(uuid.uuid4()),
            PERSONA_ID=persona_id,
            CHILD_EPISODE_ID=child_episode_id,
            PARENT_EPISODE_ID=parent_episode_id,
            LAYER=layer,
            ANCHOR_REF=spec.get("anchor_ref"),
            ORIGIN=spec.get("origin"),
            CREATED_AT=now,
            META_JSON=json.dumps(meta, ensure_ascii=False) if meta else None,
        )
        db.add(edge)
        db.flush()
        out.append(_to_dict(edge))
    return out


def record_edges(
    manager: Any,
    persona_id: str,
    child_ref: str,
    predecessors: Sequence[Any],
    *,
    session: Optional[Session] = None,
) -> List[Dict[str, Any]]:
    """範囲ノード ``child_ref`` に継承エッジ (0..n 親) を機械的に刻む。

    Args:
        child_ref: 継承する側 (新しく開いた範囲ノード) の ``episode:N`` / UUID。
        predecessors: 前駆指定の列。各要素は
            ``{"parent_ref": ..., "layer": "fact"|"digest",
               "anchor_ref"?: ..., "origin"?: ..., "meta"?: {...}}``。
            空列なら何もせず空を返す (選択なし = 直列、エッジ 0 本)。
        session: 呼び出し元が開いている Session。指定した場合、本関数は
            commit も close もしない (open_episode の予約 tx に相乗りする口)。
            指定なしなら自前 Session で commit する。

    Returns:
        刻んだ (または既存の) エッジ dict の列。

    Raises:
        InheritanceError: 前駆指定が不正 / 自己ループ / 参照解決失敗。
    """
    if not persona_id:
        raise ValueError("persona_id is required")
    if not predecessors:
        return []
    normalized = [_normalize_predecessor(spec) for spec in predecessors]

    def _run(db: Session) -> List[Dict[str, Any]]:
        child_episode_id = _resolve_owned_episode_id(db, persona_id, child_ref)
        return _insert_edges(db, persona_id, child_episode_id, normalized)

    if session is not None:
        result = _insert_edges(
            session,
            persona_id,
            _resolve_owned_episode_id(session, persona_id, child_ref),
            normalized,
        )
        LOGGER.info(
            "[inheritance] recorded(staged) %d edge(s) for %s persona=%s (commit is caller's)",
            len(result), child_ref, persona_id,
        )
        return result

    db = manager.SessionLocal()
    try:
        result = _run(db)
        db.commit()
        LOGGER.info(
            "[inheritance] recorded %d edge(s) for %s persona=%s",
            len(result), child_ref, persona_id,
        )
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 探索 (継承祖先を遡る咀嚼生成・分岐再生成の消費者向け)
# ---------------------------------------------------------------------------


def get_parents(
    manager: Any,
    persona_id: str,
    child_ref: str,
    *,
    layer: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """範囲ノードの直接の親エッジを返す (継承元)。``layer`` で絞れる。"""
    if not persona_id:
        return []
    db = manager.SessionLocal()
    try:
        child_episode_id = _resolve_episode_id(db, persona_id, child_ref)
        q = db.query(EpisodeInheritance).filter(
            EpisodeInheritance.PERSONA_ID == persona_id,
            EpisodeInheritance.CHILD_EPISODE_ID == child_episode_id,
        )
        if layer is not None:
            q = q.filter(EpisodeInheritance.LAYER == layer)
        return [_to_dict(e) for e in q.order_by(EpisodeInheritance.CREATED_AT.asc()).all()]
    finally:
        db.close()


def get_children(
    manager: Any,
    persona_id: str,
    parent_ref: str,
    *,
    layer: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """範囲ノードを継承元とする子エッジを返す (この範囲を元にした続き)。"""
    if not persona_id:
        return []
    db = manager.SessionLocal()
    try:
        parent_episode_id = _resolve_episode_id(db, persona_id, parent_ref)
        q = db.query(EpisodeInheritance).filter(
            EpisodeInheritance.PERSONA_ID == persona_id,
            EpisodeInheritance.PARENT_EPISODE_ID == parent_episode_id,
        )
        if layer is not None:
            q = q.filter(EpisodeInheritance.LAYER == layer)
        return [_to_dict(e) for e in q.order_by(EpisodeInheritance.CREATED_AT.asc()).all()]
    finally:
        db.close()


def get_ancestors(
    manager: Any,
    persona_id: str,
    child_ref: str,
    *,
    layer: Optional[str] = None,
) -> List[str]:
    """継承祖先の EPISODE_ID 集合を幅優先で辿って返す (自分は含まない)。

    experience_structure.md §3.3 の生成規律「咀嚼は継承チェーンに閉じる
    (文脈参照は継承祖先のみ)」の探索原始関数。``layer`` を指定するとその層の
    エッジだけを辿る (事実層だけの直系など)。**全**祖先を返す — 深さ上限は
    置かない (打ち切ると継承チェーンの古い側を落とす)。循環 (壊れたデータの
    異常系) は visited で止まり、ノード数有限で必ず停止する。決定論のため
    EPISODE_ID 昇順で返す。
    """
    if not persona_id:
        return []
    db = manager.SessionLocal()
    try:
        start = _resolve_episode_id(db, persona_id, child_ref)
        visited: set = set()
        frontier = [start]
        while frontier:
            q = db.query(EpisodeInheritance.PARENT_EPISODE_ID).filter(
                EpisodeInheritance.PERSONA_ID == persona_id,
                EpisodeInheritance.CHILD_EPISODE_ID.in_(frontier),
            )
            if layer is not None:
                q = q.filter(EpisodeInheritance.LAYER == layer)
            parents = {row[0] for row in q.all()}
            new_parents = parents - visited - {start}
            visited |= new_parents
            frontier = list(new_parents)
        return sorted(visited)
    finally:
        db.close()
