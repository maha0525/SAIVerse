"""出来事 (Episode) の**読み取り専用**モジュール — 旧データの残置を読む口。

⚠ **書き込み API は束 6c (2026-08-22) で全て退役した。**
autonomous_behavior_v3.md §7 の裁定「エピソードという専用の記録行は持たない」で、
v1.4 のエピソードが持っていた情報は全て別の持ち主へ返された:

| 旧エピソードが持っていたもの | いまの持ち主 |
|---|---|
| どの件の実行か | メッセージへの記録 |
| 始まり・終わりの実在イベント | **記録しない** (会話に区切りは保存しない = docs/intent/episode.md の不変条件。2026-08-23 裁定) |
| 完了・成果物 | 台帳の一件の状態欄 |
| できごと UI の一行 | 表示時にログから導出 |
| チャンクの切れ目の目印 | 導出 (発言の時刻と沈黙の幅から。刻印は持たない) |

``episodes`` テーブルと既存の行は **読み取り専用の残置** として残す (v3 §9-8 ①:
「中身はログと台帳にほぼ重複しており、変換コードを書く価値がない。削除はいつでも
できる」)。本モジュールに残るのは、その旧データを読む口だけ。

読み取り側の約束 (据え置き):

- 行はペルソナ単位 (複数主観 §9)。同一の世界的出来事は ``occurrence_id`` で束ねる。
- 参照は統一文法 (saiverse/references.py) の ``episode:N`` に相乗りする (§8.1)。
- DB access は ``manager.SessionLocal()`` → try/finally close の既存流儀。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from database.models import Episode

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

    ⚠ 束 6c (2026-08-22) 以降、**新しく開く手は存在しない** (v3 §7)。返るのは
    旧世代のデータで閉じ損ねた行だけなので、挙動の判定に使わないこと
    (「いま会話中か」は ``saiverse.user_conversation.get_open_conversation``)。

    ``kind=None`` は per-persona の in-memory キャッシュを使う (プロセス再起動後は
    初回読み出しで DB から復元)。**無効化の口は持たない** — このプロセスに行を
    書き換える手が無いので、一度読んだ答えが自分の書き込みで古くなることは起き得
    ない。古くなる余地が残るのは外からの書き換え (別プロセス・memory.db の差し替え・
    手作業の SQL) だけで、その並びは v0.3 の運転では起きない。
    ``kind`` 指定時はキャッシュを通らず毎回 DB を引く。
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
# 読み取り API (書き込み API は束 6c / 2026-08-22 で退役 — v3 §7)
# ---------------------------------------------------------------------------


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

    「いま閉じたばかりの出来事」を引く入口。open と同じく SHORT_ID 降順で
    選ぶ (仮想クロック下の同秒 ENDED_AT に頑健)。
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


def get_open_non_conversation_episode(
    manager: Any, persona_id: str
) -> Optional[Dict[str, Any]]:
    """「別の活動中か」= 開いている会話以外の出来事 (無ければ None)。

    :func:`get_open_episode` (kind 無指定) は「最後に開いた 1 件」しか返さない
    ので、会話と作業の出来事が同時に開いていると、**どちらが後に開いたか**で
    「別の活動中」の答えが変わってしまう (会話が後なら作業が見えない)。
    「会話以外の open が 1 件でもあるか」は開いた順に依存しない問いなので、
    conversation を除いた集合から直接引く。

    複数開いていれば最後に開いた 1 件 (SHORT_ID 最大) を代表として返す。

    ⚠ 束 6c (2026-08-22) で**本番の読み手はゼロになった**。「別の活動中か」を
    答える器そのものが v0.3 に無いため、旧実装の消費者 (仲裁の判定と、ペルソナへ
    見せる「いまの活動」) は揃って None へ縮退している
    (``user_conversation._get_open_non_conversation_episode`` を参照)。旧データを
    調べる口として残す。
    """
    if not persona_id:
        return None
    db = manager.SessionLocal()
    try:
        ep = (
            db.query(Episode)
            .filter(
                Episode.PERSONA_ID == persona_id,
                Episode.STATUS == STATUS_OPEN,
                Episode.KIND != KIND_CONVERSATION,
            )
            .order_by(Episode.SHORT_ID.desc())
            .first()
        )
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
