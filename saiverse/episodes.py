"""出来事 (Episode) の操作モジュール — 「いま」の実体の薄い封筒。

life_concept_map.md §8「出来事 — いまの実体」/ §8.1「出来事の実体化」の
永続化レイヤー。会話・作業セッション・コマ実績など既存の実体を置換せず、
kind + 実体参照で包む共通エンベロープ (``episodes`` テーブル、database/models.py)
を CRUD する。一日新聞・ライフビューの一次データ源で、一覧は SELECT のみ
(LLM 不要 — 新聞の決定論構築原則の延長)。

設計上の約束:

- **時刻は必ず ``saiverse.clock.now()`` 経由** (epoch 秒 int で刻む)。実時刻の
  直書きは一日シミュレータの「ペルソナの今日」を壊す (autonomous_behavior_v2.md
  §12 の不変条件)。
- 行はペルソナ単位 (複数主観 §9)。同一の世界的出来事は ``occurrence_id`` で束ねる。
- 閉じ処理は意味を書かない — 再訪の鍵 (``digest_ref``) だけ書く (§9)。
- 参照は統一文法 (saiverse/references.py) の ``episode:N`` に相乗りする (§8.1)。
- DB access は ``manager.SessionLocal()`` → try/finally close の既存流儀
  (desire_engine と同じく manager を第一引数に取るモジュール関数群)。

P1 (DB 基盤) 時点では本モジュールはどこからも呼ばれない (休眠)。配線は P2 以降。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from database.models import Episode
from saiverse import clock

LOGGER = logging.getLogger(__name__)

# episode:N 短縮参照子 (track:N / task:N と対称)。
_EPISODE_REF_RE = re.compile(r"^episode:(\d+)$", re.IGNORECASE)

# --- 状態定数 ---
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"

# --- kind 語彙 (life_concept_map.md §8.1「kind ＋ 実体への参照」) ---
KIND_CONVERSATION = "conversation"
KIND_WORK_SESSION = "work_session"
KIND_SLOT = "slot"            # コマ実績
KIND_PRESENCE = "presence"
KIND_STROLL = "stroll"        # 散策
KIND_OTHER = "other"
EPISODE_KINDS = frozenset({
    KIND_CONVERSATION, KIND_WORK_SESSION, KIND_SLOT,
    KIND_PRESENCE, KIND_STROLL, KIND_OTHER,
})


class EpisodeError(Exception):
    """Base error for episode operations."""


class EpisodeNotFoundError(EpisodeError):
    """Raised when an episode ref/id cannot be resolved."""


def _now_epoch() -> int:
    """現在時刻 (epoch 秒)。仮想クロック尊重のため必ず clock.now() を通す。"""
    return int(clock.now().timestamp())


def _next_short_id(db: Session, persona_id: str) -> int:
    """ペルソナ内で次に使う short_id (MAX + 1, 初回は 1。ActionTrack と同流儀)。

    行を物理削除しない限り MAX は単調増加し、一度 episode:N が指した出来事が
    消えても N が別物に化けない。
    """
    current_max = (
        db.query(sa_func.max(Episode.SHORT_ID))
        .filter(Episode.PERSONA_ID == persona_id)
        .scalar()
    )
    return (current_max or 0) + 1


def _to_dict(ep: Episode) -> Dict[str, Any]:
    """detached な dict に直列化する (ORM オブジェクトを外に出さない)。"""
    try:
        participants = json.loads(ep.PARTICIPANTS_JSON) if ep.PARTICIPANTS_JSON else []
    except (TypeError, ValueError):
        LOGGER.warning("[episode] PARTICIPANTS_JSON is not valid JSON: %r", ep.PARTICIPANTS_JSON)
        participants = []
    try:
        meta = json.loads(ep.META_JSON) if ep.META_JSON else None
    except (TypeError, ValueError):
        LOGGER.warning("[episode] META_JSON is not valid JSON: %r", ep.META_JSON)
        meta = None
    return {
        "episode_id": ep.EPISODE_ID,
        "short_id": ep.SHORT_ID,
        "episode_ref": f"episode:{ep.SHORT_ID}" if ep.SHORT_ID is not None else None,
        "persona_id": ep.PERSONA_ID,
        "kind": ep.KIND,
        "occurrence_id": ep.OCCURRENCE_ID,
        "started_at": ep.STARTED_AT,
        "ended_at": ep.ENDED_AT,
        "building_id": ep.BUILDING_ID,
        "participants": participants,
        "origin_ref": ep.ORIGIN_REF,
        "status": ep.STATUS,
        "digest_ref": ep.DIGEST_REF,
        "meta": meta,
    }


def _resolve_episode_id(db: Session, persona_id: str, ref: str) -> str:
    """``episode:N`` / UUID を EPISODE_ID に解決する (TrackManager.resolve_track_ref と対称)。"""
    if not ref:
        raise EpisodeNotFoundError("empty episode reference")
    m = _EPISODE_REF_RE.match(ref.strip())
    if m:
        short_id = int(m.group(1))
        row = (
            db.query(Episode.EPISODE_ID)
            .filter(
                Episode.PERSONA_ID == persona_id,
                Episode.SHORT_ID == short_id,
            )
            .first()
        )
        if row is None:
            raise EpisodeNotFoundError(
                f"episode not found: episode:{short_id} (persona={persona_id})"
            )
        return row[0]
    ref_stripped = ref.strip()
    if len(ref_stripped) == 36 and ref_stripped.count("-") == 4:
        return ref_stripped
    raise EpisodeNotFoundError(
        f"invalid episode reference: {ref!r} (expected 'episode:N' or UUID)"
    )


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------


def open_episode(
    manager: Any,
    persona_id: str,
    kind: str,
    *,
    building_id: Optional[str] = None,
    participants: Optional[Sequence[str]] = None,
    origin_ref: Optional[str] = None,
    occurrence_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """出来事を開く (life_concept_map.md §8「いまとは開いている出来事の先端」)。

    Args:
        kind: :data:`EPISODE_KINDS` のいずれか。
        origin_ref: 出自 (コマ・呼びかけ等) への参照。**None = 自発** —
            無計画の出来事は出自なしが合法 (§6「事後に偽のコマを起こさない」)。
        occurrence_id: 同一の世界的出来事を束ねる相関 ID (§8.1 複数主観)。

    Returns:
        直列化した出来事 dict (episode_ref = ``episode:N`` を含む)。
    """
    if not persona_id:
        raise ValueError("persona_id is required")
    if kind not in EPISODE_KINDS:
        raise ValueError(f"invalid episode kind: {kind!r} (expected one of {sorted(EPISODE_KINDS)})")

    episode_id = str(uuid.uuid4())
    now_epoch = _now_epoch()
    db = manager.SessionLocal()
    try:
        short_id = _next_short_id(db, persona_id)
        ep = Episode(
            EPISODE_ID=episode_id,
            PERSONA_ID=persona_id,
            SHORT_ID=short_id,
            KIND=kind,
            OCCURRENCE_ID=occurrence_id,
            STARTED_AT=now_epoch,
            ENDED_AT=None,
            BUILDING_ID=building_id,
            PARTICIPANTS_JSON=(
                json.dumps([str(p) for p in participants], ensure_ascii=False)
                if participants else None
            ),
            ORIGIN_REF=origin_ref,
            STATUS=STATUS_OPEN,
            DIGEST_REF=None,
            META_JSON=json.dumps(meta, ensure_ascii=False) if meta else None,
        )
        db.add(ep)
        db.commit()
        db.refresh(ep)
        result = _to_dict(ep)
        LOGGER.info(
            "[episode] opened %s (episode:%d) persona=%s kind=%s origin=%s",
            episode_id, short_id, persona_id, kind, origin_ref,
        )
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def close_episode(
    manager: Any,
    persona_id: str,
    ref: str,
    *,
    digest_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """出来事を閉じる (ENDED_AT を刻み status='closed')。

    閉じ処理の規範 (§9): **意味を書かず再訪の鍵だけ書く**。``digest_ref`` は
    閉じダイジェスト (事実の記録) への参照で、意味づけの完了ではなく開始である。
    既に closed の出来事を再度閉じるのは InvalidStateError にせず no-op で返す
    (運用の線は「安い・撤回可能」§8 — 冪等に倒す)。
    """
    db = manager.SessionLocal()
    try:
        episode_id = _resolve_episode_id(db, persona_id, ref)
        ep = (
            db.query(Episode)
            .filter(Episode.EPISODE_ID == episode_id, Episode.PERSONA_ID == persona_id)
            .first()
        )
        if ep is None:
            raise EpisodeNotFoundError(f"episode not found: {episode_id}")
        if ep.STATUS == STATUS_CLOSED:
            LOGGER.debug("[episode] close no-op (already closed): %s", episode_id)
            return _to_dict(ep)
        ep.STATUS = STATUS_CLOSED
        ep.ENDED_AT = _now_epoch()
        if digest_ref is not None:
            ep.DIGEST_REF = digest_ref
        db.commit()
        db.refresh(ep)
        result = _to_dict(ep)
        LOGGER.info(
            "[episode] closed %s persona=%s digest=%s", episode_id, persona_id, digest_ref,
        )
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def list_today(
    manager: Any,
    persona_id: str,
    day_start_epoch: int,
    day_end_epoch: int,
) -> List[Dict[str, Any]]:
    """ペルソナの「今日の出来事」一覧 (§8.1 用途起点: 今日は何があったかを眺める)。

    「今日」との重なりで選ぶ: ``STARTED_AT < day_end`` かつ
    (``ENDED_AT`` が NULL = まだ開いている、または ``ENDED_AT >= day_start``)。
    日を跨いで開きっぱなしの出来事も「今日あったこと」として載る。
    STARTED_AT 昇順。SELECT のみで LLM は呼ばない。
    """
    db = manager.SessionLocal()
    try:
        rows = (
            db.query(Episode)
            .filter(
                Episode.PERSONA_ID == persona_id,
                Episode.STARTED_AT < day_end_epoch,
                (Episode.ENDED_AT.is_(None)) | (Episode.ENDED_AT >= day_start_epoch),
            )
            .order_by(Episode.STARTED_AT.asc())
            .all()
        )
        return [_to_dict(ep) for ep in rows]
    finally:
        db.close()


def get_by_ref(manager: Any, persona_id: str, ref: str) -> Dict[str, Any]:
    """``episode:N`` / UUID から出来事を引く。無ければ EpisodeNotFoundError。"""
    db = manager.SessionLocal()
    try:
        episode_id = _resolve_episode_id(db, persona_id, ref)
        ep = (
            db.query(Episode)
            .filter(Episode.EPISODE_ID == episode_id, Episode.PERSONA_ID == persona_id)
            .first()
        )
        if ep is None:
            raise EpisodeNotFoundError(f"episode not found: {episode_id}")
        return _to_dict(ep)
    finally:
        db.close()
