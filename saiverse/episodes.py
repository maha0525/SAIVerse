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
import threading
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


#: 開いている出来事のペルソナ別キャッシュを守るロック (プロセス内)。
#: キャッシュ本体は manager オブジェクトに持たせる (``_open_episode_cache``:
#: persona_id → 直列化済み episode dict | None)。manager 単位に持つのは、
#: テストが別々の in-memory DB を持つ複数 manager を同一プロセスで作るため
#: (モジュールグローバルだと persona_id 衝突で漏れる)。
_OPEN_CACHE_LOCK = threading.Lock()
_OPEN_CACHE_ATTR = "_open_episode_cache"


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
# 開いている出来事のキャッシュ (層0タグの高頻度読み出し対策)
# ---------------------------------------------------------------------------


def _open_cache(manager: Any) -> Dict[str, Optional[Dict[str, Any]]]:
    """manager にぶら下がるキャッシュ dict を取得 (無ければ生成)。"""
    cache = getattr(manager, _OPEN_CACHE_ATTR, None)
    if cache is None:
        cache = {}
        try:
            setattr(manager, _OPEN_CACHE_ATTR, cache)
        except (AttributeError, TypeError):
            # setattr できない manager (frozen 等)。キャッシュ無しで動く。
            return {}
    return cache


def _cache_set_open(manager: Any, persona_id: str, ep: Optional[Dict[str, Any]]) -> None:
    with _OPEN_CACHE_LOCK:
        _open_cache(manager)[persona_id] = ep


def _cache_drop(manager: Any, persona_id: str) -> None:
    """キャッシュエントリを落とす (次回読み出しで DB から復元)。"""
    with _OPEN_CACHE_LOCK:
        _open_cache(manager).pop(persona_id, None)


def invalidate_open_cache(manager: Any, persona_id: str) -> None:
    """open-episode キャッシュの persona エントリを落とす (次回読みで DB 復元)。

    session モードの :func:`open_episode` / :func:`close_episode` は自分では
    キャッシュを触らない — 呼び出し元の commit がまだなので、未コミット状態を
    キャッシュに映すと rollback で残骸が居座り、逆に stale な ``None`` を
    残したまま新 open を映さないと :func:`get_open_episode` (``kind=None`` の
    cache **hit** は DB フォールバックしない) が古い値を返し続ける。
    呼び出し元は自分の commit **成功後**に本関数を呼び、次の
    ``get_open_episode(kind=None)`` が DB から現在の open 状態を読み直すように
    する。**commit 前に呼ぶと DB がまだ未コミットで stale が復活しうる** —
    必ず commit 後に呼ぶこと。
    """
    _cache_drop(manager, persona_id)


def _query_latest_open(
    db: Session, persona_id: str, kind: Optional[str] = None
) -> Optional[Episode]:
    q = (
        db.query(Episode)
        .filter(Episode.PERSONA_ID == persona_id, Episode.STATUS == STATUS_OPEN)
    )
    if kind is not None:
        q = q.filter(Episode.KIND == kind)
    # SHORT_ID はペルソナ内単調増加なので「最後に開いた open」を一意に選べる
    # (仮想クロック下で STARTED_AT が同秒になっても順序が壊れない)。
    return q.order_by(Episode.SHORT_ID.desc()).first()


def get_open_episode(
    manager: Any, persona_id: str, kind: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """開いている出来事のうち最後に開いた 1 件を返す (無ければ None)。

    ``kind=None`` は層0タグ (メッセージへの origin_episode 付与) の高頻度経路
    なので、per-persona の in-memory キャッシュを使う (open/close 時に更新、
    プロセス再起動後は初回読み出しで DB から復元)。``kind`` 指定時は
    open/close の瞬間にしか呼ばれない低頻度経路なので素直に DB を引く。
    """
    if not persona_id:
        return None
    if kind is None:
        with _OPEN_CACHE_LOCK:
            cache = _open_cache(manager)
            if persona_id in cache:
                return cache[persona_id]
    db = manager.SessionLocal()
    try:
        ep = _query_latest_open(db, persona_id, kind)
        result = _to_dict(ep) if ep is not None else None
    finally:
        db.close()
    if kind is None:
        _cache_set_open(manager, persona_id, result)
    return result


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
    predecessors: Optional[Sequence[Any]] = None,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    """出来事を開く (life_concept_map.md §8「いまとは開いている出来事の先端」)。

    Args:
        kind: :data:`EPISODE_KINDS` のいずれか。
        origin_ref: 出自 (コマ・呼びかけ等) への参照。**None = 自発** —
            無計画の出来事は出自なしが合法 (§6「事後に偽のコマを起こさない」)。
        occurrence_id: 同一の世界的出来事を束ねる相関 ID (§8.1 複数主観)。
        predecessors: 継承エッジの前駆指定 (experience_structure.md §3.3、W13)。
            各要素は ``{"parent_ref": "episode:N"|UUID, "layer": "fact"|"digest",
            "anchor_ref"?: ..., "origin"?: ..., "meta"?: {...}}``。指定すると
            **この出来事を開く同一トランザクション内で**継承エッジ
            (``episode_inheritance``) を機械的に刻む — 範囲が開いた瞬間の記帳
            (§11-4)。**None / 空 = 選択なし = 直列** (エッジ 0 本、既存挙動は
            無変化)。分岐・再生成 / 並列統合 / メティス取り込みの前駆を渡す口。
        session: 呼び出し元が開いている Session。**指定した場合、本関数は
            commit も close もしない** — Episode 行 INSERT が呼び出し元の
            1 commit に同梱される (予約 tx で slot 状態・予算・台帳 running と
            episode open を束ねる口)。採番と制約検査を確定させるため INSERT 後に
            ``flush`` する (SHORT_ID / EPISODE_ID は本関数が明示採番するので
            ``refresh`` は不要)。**open キャッシュは触らない** — 呼び出し元が
            commit 後に :func:`invalidate_open_cache` で整合を負う契約
            (未コミット状態を映さないため)。指定なしなら自前 Session で
            commit + キャッシュ更新する (従来挙動、無傷)。継承エッジも同じ
            tx / 自前 commit に相乗りする。

    Returns:
        直列化した出来事 dict (episode_ref = ``episode:N`` を含む)。
    """
    if not persona_id:
        raise ValueError("persona_id is required")
    if kind not in EPISODE_KINDS:
        raise ValueError(f"invalid episode kind: {kind!r} (expected one of {sorted(EPISODE_KINDS)})")

    episode_id = str(uuid.uuid4())
    now_epoch = _now_epoch()

    def _build(db: Session) -> Episode:
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
        db.flush()
        if predecessors:
            # 継承エッジは範囲が開いた瞬間、同一 tx で機械的に記帳する (§11-4)。
            # 循環 import 回避のため遅延ロード。child は今 flush した episode_id。
            from saiverse import experience_inheritance as EI
            EI.record_edges(
                None, persona_id, episode_id, predecessors, session=db,
            )
        return ep

    if session is not None:
        ep = _build(session)
        result = _to_dict(ep)
        # session モードではキャッシュを触らない (未コミット状態を映さない)。
        # 呼び出し元が commit 後に invalidate_open_cache() で整合を負う。
        LOGGER.info(
            "[episode] opened(staged) %s (episode:%d) persona=%s kind=%s origin=%s "
            "(commit is caller's)",
            episode_id, ep.SHORT_ID, persona_id, kind, origin_ref,
        )
        return result

    db = manager.SessionLocal()
    try:
        ep = _build(db)
        db.commit()
        db.refresh(ep)
        result = _to_dict(ep)
        # いま開いたものが「最後に開いた open」(SHORT_ID 最大) — キャッシュ更新。
        _cache_set_open(manager, persona_id, result)
        LOGGER.info(
            "[episode] opened %s (episode:%d) persona=%s kind=%s origin=%s",
            episode_id, ep.SHORT_ID, persona_id, kind, origin_ref,
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
    meta: Optional[Dict[str, Any]] = None,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    """出来事を閉じる (ENDED_AT を刻み status='closed')。

    閉じ処理の規範 (§9): **意味を書かず再訪の鍵だけ書く**。``digest_ref`` は
    閉じダイジェスト (事実の記録) への参照で、意味づけの完了ではなく開始である。
    既に closed の出来事を再度閉じるのは InvalidStateError にせず no-op で返す
    (運用の線は「安い・撤回可能」§8 — 冪等に倒す)。

    Args:
        meta: META_JSON へ浅くマージする追加情報 (例: 作業セッションの
            ``title`` / ``artifacts`` — meta 書式契約 life_concept_map.md §14)。
            事実の記録のみを書くこと (意味づけは書かない)。
        session: 呼び出し元が開いている Session。**指定した場合、本関数は
            commit も close もしない** — Episode 行 UPDATE が呼び出し元の
            1 commit に同梱される (精算 tx で slot done・予算調整・台帳 applied と
            episode close を束ねる口)。**open キャッシュは触らない** — 呼び出し元が
            commit 後に :func:`invalidate_open_cache` で整合を負う契約。指定なしなら
            自前 Session で commit + キャッシュ無効化する (従来挙動、無傷)。
    """

    def _apply(db: Session) -> Dict[str, Any]:
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
        if meta:
            try:
                existing = json.loads(ep.META_JSON) if ep.META_JSON else {}
            except (TypeError, ValueError):
                LOGGER.warning(
                    "[episode] META_JSON is not valid JSON on close: %r", ep.META_JSON,
                )
                existing = {}
            if not isinstance(existing, dict):
                existing = {}
            existing.update(meta)
            ep.META_JSON = json.dumps(existing, ensure_ascii=False)
        db.flush()
        return _to_dict(ep)

    if session is not None:
        result = _apply(session)
        # session モードではキャッシュを触らない。呼び出し元が commit 後に
        # invalidate_open_cache() で無効化する契約。
        LOGGER.info(
            "[episode] closed(staged) %s persona=%s digest=%s (commit is caller's)",
            result.get("episode_id"), persona_id, digest_ref,
        )
        return result

    db = manager.SessionLocal()
    try:
        result = _apply(db)
        db.commit()
        # 閉じたものがキャッシュ中の「最後に開いた open」だったかに依らず、
        # エントリごと落として次回読み出しで DB から引き直す (外側でまだ開いて
        # いる出来事があればそれが復元される)。
        _cache_drop(manager, persona_id)
        LOGGER.info(
            "[episode] closed %s persona=%s digest=%s",
            result.get("episode_id"), persona_id, digest_ref,
        )
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def set_digest_ref(
    manager: Any,
    persona_id: str,
    ref: str,
    digest_ref: str,
) -> Dict[str, Any]:
    """出来事の DIGEST_REF を後段確定する (W1 Chunk C / D9-5)。

    digest 統合 (judgment_points.md §6 改定) で digest は post_session 判断が
    書き、実行台帳の配送 handler (``saimemory.append_digest``) が SAIMemory へ
    届いた後にここで episode に再訪の鍵を刻む。配送までの間 DIGEST_REF が
    NULL の closed episode = 「適用済み・記録待ち」の観測可能状態。

    冪等: 既に同値なら no-op。既に**別値**が入っている場合は WARN して
    上書きしない (再配送や二重 finalize で鍵をすり替えない)。

    Raises:
        EpisodeNotFoundError: 出来事が見つからない (配送 handler は例外で
            配送失敗を表明する契約 — 握り潰さない)。
    """
    if not digest_ref:
        raise ValueError("digest_ref is required")
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
        if ep.DIGEST_REF == digest_ref:
            LOGGER.debug(
                "[episode] set_digest_ref no-op (already set): %s digest=%s",
                episode_id, digest_ref,
            )
            return _to_dict(ep)
        if ep.DIGEST_REF:
            LOGGER.warning(
                "[episode] digest_ref already set to %r; not overwriting with %r "
                "(episode=%s persona=%s)",
                ep.DIGEST_REF, digest_ref, episode_id, persona_id,
            )
            return _to_dict(ep)
        ep.DIGEST_REF = digest_ref
        db.commit()
        db.refresh(ep)
        LOGGER.info(
            "[episode] digest_ref set: %s persona=%s digest=%s",
            episode_id, persona_id, digest_ref,
        )
        return _to_dict(ep)
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


def get_latest_closed_episode(
    manager: Any, persona_id: str, kind: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """最後に閉じた出来事 1 件を返す (無ければ None)。

    層2 棚入れ (life_concept_map.md §9.1) の判断点が「いま閉じたばかりの
    出来事」を引くフォールバック入口 (post_conversation は close 直後に
    判断が走るため、SHORT_ID 最大の closed 行 = 当該会話)。open と同じく
    SHORT_ID 降順で選ぶ (仮想クロック下の同秒 ENDED_AT に頑健)。
    """
    if not persona_id:
        return None
    db = manager.SessionLocal()
    try:
        q = (
            db.query(Episode)
            .filter(Episode.PERSONA_ID == persona_id, Episode.STATUS == STATUS_CLOSED)
        )
        if kind is not None:
            q = q.filter(Episode.KIND == kind)
        ep = q.order_by(Episode.SHORT_ID.desc()).first()
        return _to_dict(ep) if ep is not None else None
    finally:
        db.close()


def get_open_episode_by_origin(
    manager: Any, persona_id: str, origin_ref: str
) -> Optional[Dict[str, Any]]:
    """出自 (origin_ref) で開いている出来事を引く (無ければ None)。

    コマ発火の回復 (day_plan の settle-close) が、台帳 payload の slot 座標から
    :func:`saiverse.day_plan._slot_origin_ref` で再構成した origin_ref で「その
    コマが開いた出来事」を逆引きするための口。同一 origin_ref に複数 open が
    ありうる異常系では最後に開いた 1 件 (SHORT_ID 最大) を返す。
    """
    if not persona_id or not origin_ref:
        return None
    db = manager.SessionLocal()
    try:
        ep = (
            db.query(Episode)
            .filter(
                Episode.PERSONA_ID == persona_id,
                Episode.STATUS == STATUS_OPEN,
                Episode.ORIGIN_REF == origin_ref,
            )
            .order_by(Episode.SHORT_ID.desc())
            .first()
        )
        return _to_dict(ep) if ep is not None else None
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


# ---------------------------------------------------------------------------
# 会話の出来事 (kind='conversation') 専用ヘルパー
# ---------------------------------------------------------------------------


def _shared_conversation_occurrence(db: Session, building_id: str) -> Optional[str]:
    """同じ Building で開いている会話出来事の occurrence_id を返す (無ければ None)。

    同じ場のユーザー会話は複数ペルソナが同時に参加しうる (§8.1 複数主観)。
    先に開いた行の occurrence_id を共有することで「同じ場の出来事」として
    束ねられる。open な会話行はすべて本モジュール経由で occurrence_id を
    持って作られるため、最初に見つかった 1 件を採用すれば決定論。
    """
    row = (
        db.query(Episode.OCCURRENCE_ID)
        .filter(
            Episode.KIND == KIND_CONVERSATION,
            Episode.STATUS == STATUS_OPEN,
            Episode.BUILDING_ID == building_id,
            Episode.OCCURRENCE_ID.isnot(None),
        )
        .order_by(Episode.STARTED_AT.asc())
        .first()
    )
    return row[0] if row is not None else None


def open_conversation_episode(
    manager: Any,
    persona_id: str,
    *,
    building_id: Optional[str] = None,
    participants: Optional[Sequence[str]] = None,
    origin_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """会話の出来事を開く (冪等)。

    既にこのペルソナで open な kind='conversation' 行があれば **開き直さず**
    それを返す (再入 = 会話継続。user_conversation Track の再 activate は
    新しい会話の開始とは限らない)。

    occurrence_id の生成規則 (決定論):

    - 同じ Building に他ペルソナの open 会話行があれば、その occurrence_id を
      共有する (同じ場の会話 = 同一の世界的出来事)
    - 無ければ ``conv:{building_id}:{開始epoch秒}`` を新規発行
    - building_id 不明 (None) の会話は束ねようが無いので occurrence_id なし
      (NULL = 単独。§8.1)
    """
    existing = get_open_episode(manager, persona_id, kind=KIND_CONVERSATION)
    if existing is not None:
        LOGGER.debug(
            "[episode] conversation already open (idempotent): %s persona=%s",
            existing.get("episode_ref"), persona_id,
        )
        return existing

    occurrence_id: Optional[str] = None
    if building_id:
        db = manager.SessionLocal()
        try:
            occurrence_id = _shared_conversation_occurrence(db, building_id)
        finally:
            db.close()
        if occurrence_id is None:
            occurrence_id = f"conv:{building_id}:{_now_epoch()}"

    return open_episode(
        manager,
        persona_id,
        KIND_CONVERSATION,
        building_id=building_id,
        participants=participants,
        origin_ref=origin_ref,
        occurrence_id=occurrence_id,
    )


def close_conversation_episode(
    manager: Any,
    persona_id: str,
    *,
    digest_ref: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """開いている会話の出来事を閉じる。無ければ no-op で None を返す。

    会話終了 (wait_response タイムアウト / シムの leave イベント) から呼ばれる。
    閉じるのは「最後に開いた open な conversation 行」1 件のみ (会話は同時に
    ひとつ — 排他性は出来事側の性質 §8)。
    """
    open_conv = get_open_episode(manager, persona_id, kind=KIND_CONVERSATION)
    if open_conv is None:
        LOGGER.debug(
            "[episode] close_conversation: no open conversation (persona=%s); no-op",
            persona_id,
        )
        return None
    return close_episode(
        manager, persona_id, open_conv["episode_id"], digest_ref=digest_ref,
    )
