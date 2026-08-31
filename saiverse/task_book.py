"""タスク帳 (task_book) の操作モジュール — 相手のある一件の台帳。

autonomous_behavior_v3.md §4.1-2「タスク帳」の永続化レイヤー。中央 DB の
``task_book`` テーブル (database/models.py の :class:`TaskBookEntry`) を CRUD
する。生 SQL や直接の ORM 書き込みで台帳を触らず、必ずこのモジュールを通す。

設計上の約束 (intent §4.1 / §4.2 / §9-5):

- **器を決めるのは相手の有無**。相手のいる約束・依頼は期限が無くても入る。
- **DUE_AT の NULL = 期限なし** — 期限のない約束は正当な行で、嘘の期限を
  発明させない。機械の締め切り引き当て (:func:`list_open_with_due`) に乗るのは
  期限のある行だけ。
- この軸から、受け入れられる行は三形: **相手のある一件** (期限任意) /
  **期限つきの自分だけの一件** (§4 の例「帰宅前に仕上げたいもの」) /
  **システムタスク** (ORIGIN='system'、期限も相手も任意)。期限も相手も
  無い一件はタスク帳ではなく手帳 (やりたいメモ) の領分で、受け入れない。
- 状態は三値: 'open' (ある) / 'done' (やり終えた) / 'withdrawn' (取り下げた)。
  閉じた行は再オープンしない (掃除は論理状態で、物理削除しない)。物理削除の
  唯一の例外はペルソナ削除の後始末 (:func:`purge_persona_entries`)。
- **完遂の接地** (§9-5): 相手のある一件 (COUNTERPART あり) の完了には
  成果物参照 (artifact_ref) か顛末一行 (outcome) のどちらかが要る —
  v2 の空洞完了を防ぐ証跡。自分だけの一件 (COUNTERPART なし) は任意。
- システムタスク (ORIGIN='system') は機械がペルソナに差し込む急ぎでない
  依頼の一件。引き当て順は 締め切り → システムタスク → プール (§5)。
- 時刻刻印は必ず ``saiverse.clock.now()`` 経由 (仮想クロック尊重)。
- DB access は ``manager.SessionLocal()`` → try/finally close の既存流儀
  (saiverse/experience_inheritance.py と同じ)。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError

from database.models import AI, TaskBookEntry
from saiverse import clock

LOGGER = logging.getLogger(__name__)

# --- 状態 (intent §4.2「ある・やり終えた・取り下げた」) ---
STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_WITHDRAWN = "withdrawn"

# 出自 (intent §9-5) — 検査には使わない開語彙 (新しい書き手が増えたら増える)。
ORIGIN_SYSTEM = "system"

# update_entry の「変更しない」目印。None が正当な値 (期限なし) なので
# None をセンチネルにできない。
_UNSET = object()


class TaskBookError(Exception):
    """Base error for task-book operations."""


def _now_epoch() -> int:
    """現在時刻 (epoch 秒)。仮想クロック尊重のため必ず clock.now() を通す。"""
    return int(clock.now().timestamp())


def _validate_epoch(name: str, value: Any) -> None:
    """期限・時刻引数の検査 — int か None だけを通す。

    SQLite は列型を強制しないため、文字列や float の due_at がそのまま永続して
    期限順比較 (DUE_AT 昇順の引き当て) を毒する。bool は int のサブクラスなので
    isinstance だけでは通ってしまう — 明示的に拒否する。
    """
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TaskBookError(
            f"{name} must be an int (epoch seconds) or None: {value!r}"
        )


def _strip_or_none(name: str, value: Any) -> Optional[str]:
    """None か str だけを受けて strip し、空になったら None に正規化する。

    str() の暗黙変換はしない — ``complete_entry(outcome=True)`` の True が
    "True" として保存され、相手のある一件の完遂証跡ガード (§9-5) を通過する
    類の口を塞ぐ。str 以外は引数名を添えて TaskBookError。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TaskBookError(f"{name} must be a string or None: {value!r}")
    s = value.strip()
    return s or None


def _require_str(name: str, value: Any) -> str:
    """必須の文字列引数の検査 — str 以外・空白のみは TaskBookError。

    str() の暗黙変換はしない (content=123 が "123" で通る口を塞ぐ)。
    返り値は strip 済みで、保存にはこちらを使う。
    """
    if not isinstance(value, str):
        raise TaskBookError(f"{name} must be a string: {value!r}")
    s = value.strip()
    if not s:
        raise TaskBookError(f"{name} must be a non-empty string: {value!r}")
    return s


def _validate_id(name: str, value: Any) -> None:
    """ID 引数 (persona_id / task_id) の検査 — 非空の str だけを通す。

    str 以外は SQLite の TEXT affinity 変換で別の顔 (int 1 ↔ '1') に着地して
    別ペルソナ・別タスクの行に一致しうるため入口で拒否する。値は正規化しない
    (ID は与えられたまま照合する)。
    """
    if not isinstance(value, str) or not value.strip():
        raise TaskBookError(f"{name} must be a non-empty string: {value!r}")


def _validate_meta(meta: Any) -> None:
    """meta の検査 — dict か None、キーは str 限定。

    json.dumps は int / bool のキーを黙って "1" / "true" に文字列化するため、
    読み戻した dict が書いた dict と別の姿になる — 入口で拒否する。
    """
    if meta is None:
        return
    if not isinstance(meta, dict):
        raise TaskBookError(f"meta must be a dict or None: {meta!r}")
    for key in meta:
        if not isinstance(key, str):
            raise TaskBookError(f"meta keys must be strings: {key!r}")


def _validate_optional_str(name: str, value: Any) -> None:
    """str か None だけを通す (正規化なし — origin_ref は不透明に保持する)。"""
    if value is not None and not isinstance(value, str):
        raise TaskBookError(f"{name} must be a string or None: {value!r}")


def _validate_idem_key(value: Any) -> None:
    """idem_key の検査 — None か非空の str。

    空白のみのキーを None 扱いに黙って倒すと冪等性が静かに無効化され、逆に
    そのまま通すと空白キー同士が UNIQUE で衝突して無関係な add が既存返しに
    化ける — どちらも嘘なので入口で拒否する。
    """
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise TaskBookError(
            f"idem_key must be a non-empty string or None: {value!r}"
        )


def _to_dict(entry: TaskBookEntry) -> Dict[str, Any]:
    """detached な dict に直列化する (ORM オブジェクトを外に出さない)。"""
    try:
        meta = json.loads(entry.META_JSON) if entry.META_JSON else None
    except (TypeError, ValueError):
        LOGGER.warning("[task_book] META_JSON is not valid JSON: %r", entry.META_JSON)
        meta = None
    return {
        "task_id": entry.TASK_ID,
        "persona_id": entry.PERSONA_ID,
        "content": entry.CONTENT,
        "due_at": entry.DUE_AT,
        "counterpart": entry.COUNTERPART,
        "origin": entry.ORIGIN,
        "origin_ref": entry.ORIGIN_REF,
        "status": entry.STATUS,
        "artifact_ref": entry.ARTIFACT_REF,
        "outcome": entry.OUTCOME,
        "created_at": entry.CREATED_AT,
        "closed_at": entry.CLOSED_AT,
        "meta": meta,
        "idem_key": entry.IDEM_KEY,
        "revision": entry.REVISION,
    }


def _get_open_entry(db, persona_id: str, task_id: str) -> TaskBookEntry:
    """当該ペルソナの open な一件を取得する。無ければ・閉じていれば例外。

    閉じた行 (done / withdrawn) への再操作を状態遷移の入口で一律に弾く —
    'open' 以外からの遷移は存在しない (再オープンなし、intent §4.2 の三値)。
    """
    entry = (
        db.query(TaskBookEntry)
        .filter(
            TaskBookEntry.TASK_ID == task_id,
            TaskBookEntry.PERSONA_ID == persona_id,
        )
        .first()
    )
    if entry is None:
        raise TaskBookError(
            f"task not found for persona {persona_id!r}: {task_id!r}"
        )
    if entry.STATUS != STATUS_OPEN:
        raise TaskBookError(
            f"task {task_id!r} is not open (status={entry.STATUS!r}); "
            "closed entries cannot be modified"
        )
    return entry


def _reload_dict(db, persona_id: str, task_id: str) -> Dict[str, Any]:
    """遷移コミット後の行を読み直して dict を作る (返り値は更新後の姿)。"""
    entry = (
        db.query(TaskBookEntry)
        .filter(
            TaskBookEntry.TASK_ID == task_id,
            TaskBookEntry.PERSONA_ID == persona_id,
        )
        .first()
    )
    if entry is None:  # 遷移直後の消失は起きない想定 (物理削除しない台帳)
        raise TaskBookError(
            f"task disappeared after transition: {task_id!r} (persona {persona_id!r})"
        )
    return _to_dict(entry)


def _guarded_transition(
    db,
    persona_id: str,
    task_id: str,
    values: Dict[Any, Any],
    expected_revision: Optional[int],
) -> None:
    """open な行にだけ効く単一条件 UPDATE で遷移を確定する。

    遷移の最終保証はこの WHERE 句が持つ — 事前 SELECT はエラーメッセージの
    出し分けにすぎない。WHERE は二段:

    - ``STATUS='open'`` — SELECT と UPDATE の間に他セッションが行を閉じた
      競合 (close/close, close/update) を検出する。
    - ``REVISION = 読んだ時点の値`` (楽観ロック) — 他セッションの update が
      先に確定した競合 (update/update) を検出する。open のままでも REVISION
      が進んでいれば rowcount=0 になり、後書きが先書きを黙って上書きする
      消失を防ぐ。成功した遷移は SET で REVISION を +1 する。

    ``expected_revision=None`` は軽量シンクの後付け列が未 backfill の行
    (NULL) を読んだ場合 — SQLAlchemy の ``== None`` は IS NULL に落ちるので
    その行にだけ一致し、遷移後は 1 になる。
    """
    new_revision = (expected_revision or 0) + 1
    updated = (
        db.query(TaskBookEntry)
        .filter(
            TaskBookEntry.TASK_ID == task_id,
            TaskBookEntry.PERSONA_ID == persona_id,
            TaskBookEntry.STATUS == STATUS_OPEN,
            TaskBookEntry.REVISION == expected_revision,
        )
        .update(
            {**values, TaskBookEntry.REVISION: new_revision},
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.rollback()
        raise TaskBookError(
            f"task {task_id!r} could not be transitioned: it was closed or "
            "updated concurrently (revision mismatch) — re-read the entry "
            "and re-apply the change to the current state"
        )
    db.commit()


def add_entry(
    manager: Any,
    persona_id: str,
    content: str,
    *,
    origin: str,
    due_at: Optional[int] = None,
    counterpart: Optional[str] = None,
    origin_ref: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    idem_key: Optional[str] = None,
) -> Dict[str, Any]:
    """タスク帳に一件を追加する。

    受け入れ不変条件 (§4.1 の軸二本から): 拒否するのは**期限も相手も無い行**
    だけ。受け入れる三形は 相手のある一件 (期限任意) / 期限つきの自分だけの
    一件 / システムタスク (origin='system'、期限も相手も任意)。

    Args:
        content: 中身 — 実行の瞬間に再発明が要らない具体さ (指示書)。
        origin: 出自 ('user' | 'sluice' | 'persona' = 本人が手帳のスペルで書いた |
            'system' | 'migration' 等)。必須。
        due_at: 期限 (epoch 秒)。**省略 = 期限なし** — 期限のない約束は正当な行。
        counterpart: 相手 ('user' / ペルソナ ID / 'system' 等)。strip され、
            空白のみは None (相手なし) と同じ扱い。
        origin_ref: 出どころへの参照 (メッセージ ID 等)。不透明に保持する。
        meta: 任意の付帯情報 (JSON 化して保存)。
        idem_key: 冪等キー。与えると (persona_id, idem_key) で get-or-create に
            なり、スルースの再試行で同じ約束が別 TASK_ID として増えるのを防ぐ。
            キーは呼び手 (スルース実行 ID + 操作番号) が採番する。
    """
    _validate_id("persona_id", persona_id)
    content_norm = _require_str("content", content)
    origin_norm = _require_str("origin", origin)
    _validate_meta(meta)
    _validate_epoch("due_at", due_at)
    _validate_optional_str("origin_ref", origin_ref)
    _validate_idem_key(idem_key)

    counterpart_norm = _strip_or_none("counterpart", counterpart)
    if counterpart_norm is None and due_at is None and origin_norm != ORIGIN_SYSTEM:
        raise TaskBookError(
            "期限も相手もない一件はタスク帳ではなく手帳 (やりたいメモ) の領分 — "
            "due_at か counterpart のどちらかを与えるか、手帳に書くこと"
        )

    db = manager.SessionLocal()
    try:
        # ペルソナ実在検査 (アプリ層) — PERSONA_ID の FK (ai.AIID) は宣言のみで
        # DB は強制しない (repo は 2026-07-11 からアプリ層検査の方針)。ここで
        # 弾かないと、存在しないペルソナ名義の孤児行が静かに積もる
        # (pocketbook.add_memo の activity 実在確認と同じ筋)。
        if db.query(AI.AIID).filter(AI.AIID == persona_id).first() is None:
            raise TaskBookError(f"persona not found: {persona_id!r}")
        if idem_key is not None:
            existing = (
                db.query(TaskBookEntry)
                .filter(
                    TaskBookEntry.PERSONA_ID == persona_id,
                    TaskBookEntry.IDEM_KEY == idem_key,
                )
                .first()
            )
            if existing is not None:
                return _to_dict(existing)
        entry = TaskBookEntry(
            TASK_ID=str(uuid.uuid4()),
            PERSONA_ID=persona_id,
            CONTENT=content_norm,
            DUE_AT=due_at,
            COUNTERPART=counterpart_norm,
            ORIGIN=origin_norm,
            ORIGIN_REF=origin_ref,
            STATUS=STATUS_OPEN,
            CREATED_AT=_now_epoch(),
            META_JSON=json.dumps(meta, ensure_ascii=False) if meta else None,
            IDEM_KEY=idem_key,
            REVISION=0,
        )
        db.add(entry)
        try:
            db.commit()
        except IntegrityError:
            # 並行の add が同じ (persona_id, idem_key) を先に入れた —
            # UNIQUE (uq_task_book_idem) で弾かれたので既存行を読み直して返す。
            db.rollback()
            if idem_key is None:
                raise
            existing = (
                db.query(TaskBookEntry)
                .filter(
                    TaskBookEntry.PERSONA_ID == persona_id,
                    TaskBookEntry.IDEM_KEY == idem_key,
                )
                .first()
            )
            if existing is None:
                raise
            return _to_dict(existing)
        return _to_dict(entry)
    finally:
        db.close()


def update_entry(
    manager: Any,
    persona_id: str,
    task_id: str,
    *,
    content: Any = _UNSET,
    due_at: Any = _UNSET,
    expected_revision: Optional[int] = None,
) -> Dict[str, Any]:
    """open な一件の中身 (content) と期限 (due_at) を変更する。

    ``due_at=None`` の明示指定は「期限を外す」(期限なしへ戻す) — 省略
    (変更しない) と区別する。閉じた行 (done / withdrawn) は変更できない。

    ``expected_revision`` を与えると、行の読み直しではなく**その値**を楽観ロック
    (:func:`_guarded_transition` の WHERE) に使う — スナップショット時点の
    revision で CAS する口 (スルースがプロンプト同梱一覧の時点の姿に対して
    判断した更新を、実行中のユーザー編集へ黙って上書きしないため)。スナップ
    ショット以降に他所の更新が確定していれば revision mismatch の
    TaskBookError になる。省略 (None) は従来どおり読み直した現在値で CAS する
    (None が正当な期待値になるのは軽量シンク後付け列の未 backfill 行だけで、
    その行は読み直しでも None を得るため、意味は同じに落ちる)。

    受け入れ不変条件 (§4.1) は更新後の姿にも効く: 期限を外した結果が
    「相手なし・期限なし・origin 非 system」になる更新は拒否する
    (COUNTERPART と ORIGIN は update_entry では変わらないので既存行の値で判定)。
    """
    _validate_id("persona_id", persona_id)
    _validate_id("task_id", task_id)
    if content is _UNSET and due_at is _UNSET:
        raise TaskBookError("update_entry requires content and/or due_at")
    if content is not _UNSET:
        content = _require_str("content", content)
    if due_at is not _UNSET:
        _validate_epoch("due_at", due_at)
    if expected_revision is not None and (
        isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
    ):
        raise TaskBookError(
            f"expected_revision must be an int or None: {expected_revision!r}"
        )

    db = manager.SessionLocal()
    try:
        entry = _get_open_entry(db, persona_id, task_id)
        new_due = entry.DUE_AT if due_at is _UNSET else due_at
        if (
            new_due is None
            and _strip_or_none("counterpart", entry.COUNTERPART) is None
            and str(entry.ORIGIN).strip() != ORIGIN_SYSTEM
        ):
            raise TaskBookError(
                "期限も相手もない一件はタスク帳ではなく手帳 (やりたいメモ) の領分 — "
                "相手のいないこの一件から期限を外すことはできない "
                "(外すなら取り下げて手帳に書くこと)"
            )
        values: Dict[Any, Any] = {}
        if content is not _UNSET:
            values[TaskBookEntry.CONTENT] = content  # _require_str で strip 済み
        if due_at is not _UNSET:
            values[TaskBookEntry.DUE_AT] = due_at
        revision_for_cas = (
            entry.REVISION if expected_revision is None else expected_revision
        )
        _guarded_transition(db, persona_id, task_id, values, revision_for_cas)
        return _reload_dict(db, persona_id, task_id)
    finally:
        db.close()


def complete_entry(
    manager: Any,
    persona_id: str,
    task_id: str,
    *,
    artifact_ref: Optional[str] = None,
    outcome: Optional[str] = None,
) -> Dict[str, Any]:
    """一件を完了 (done) にする。

    完遂の接地 (intent §9-5): 相手のある一件 (COUNTERPART あり) は
    ``artifact_ref`` (成果物参照) か ``outcome`` (顛末一行) のどちらかが必須 —
    ユーザーとの約束を証拠なしに完了にしない。自分だけの一件は任意。
    どちらも strip され、空白のみは None (無い) と同じ扱い — 空白の証跡で
    接地をすり抜けさせない。str 以外 (True や 123) も "True"/"123" の顔で
    証跡ガードを通過させない (暗黙 str() 変換なし、TaskBookError)。
    保存されるのは strip 済みの文字列。
    """
    _validate_id("persona_id", persona_id)
    _validate_id("task_id", task_id)
    artifact_ref = _strip_or_none("artifact_ref", artifact_ref)
    outcome = _strip_or_none("outcome", outcome)
    db = manager.SessionLocal()
    try:
        entry = _get_open_entry(db, persona_id, task_id)
        if entry.COUNTERPART and not artifact_ref and not outcome:
            raise TaskBookError(
                f"task {task_id!r} has a counterpart ({entry.COUNTERPART!r}); "
                "completion requires artifact_ref or outcome (grounding, v3 §9-5)"
            )
        _guarded_transition(
            db,
            persona_id,
            task_id,
            {
                TaskBookEntry.STATUS: STATUS_DONE,
                TaskBookEntry.ARTIFACT_REF: artifact_ref,
                TaskBookEntry.OUTCOME: outcome,
                TaskBookEntry.CLOSED_AT: _now_epoch(),
            },
            entry.REVISION,
        )
        return _reload_dict(db, persona_id, task_id)
    finally:
        db.close()


def withdraw_entry(
    manager: Any,
    persona_id: str,
    task_id: str,
) -> Dict[str, Any]:
    """一件を取り下げ (withdrawn) にする。本人の明示かユーザーの掃除 (§4.1)。"""
    _validate_id("persona_id", persona_id)
    _validate_id("task_id", task_id)
    db = manager.SessionLocal()
    try:
        entry = _get_open_entry(db, persona_id, task_id)
        _guarded_transition(
            db,
            persona_id,
            task_id,
            {
                TaskBookEntry.STATUS: STATUS_WITHDRAWN,
                TaskBookEntry.CLOSED_AT: _now_epoch(),
            },
            entry.REVISION,
        )
        return _reload_dict(db, persona_id, task_id)
    finally:
        db.close()


def purge_persona_entries(db, persona_id: str) -> int:
    """ペルソナ削除の後始末 — 当該ペルソナの行を状態を問わず物理削除する。

    「閉じた行は物理削除しない」(§4.2) はペルソナが生きている間の台帳の話で、
    本人ごと消えるペルソナ削除は唯一の例外。PERSONA_ID の FK (ai.AIID) は
    宣言のみで DB は強制しない (アプリ層検査の方針) ため、ここで消さないと
    存在しないペルソナ名義の孤児行が残る。

    他の関数と違い ``manager`` ではなく **開いた session を受け取る** —
    呼び手 (manager の delete_ai) の transaction に参加し、ai 行の削除と
    同じ commit で確定させるため。commit / rollback は呼び手の責任。

    Returns:
        削除した行数。
    """
    _validate_id("persona_id", persona_id)
    return (
        db.query(TaskBookEntry)
        .filter(TaskBookEntry.PERSONA_ID == persona_id)
        .delete(synchronize_session=False)
    )


def list_open(manager: Any, persona_id: str) -> List[Dict[str, Any]]:
    """open な一件の一覧 (作成順)。空きティックの選択材料・UI 表示用。"""
    _validate_id("persona_id", persona_id)
    db = manager.SessionLocal()
    try:
        rows = (
            db.query(TaskBookEntry)
            .filter(
                TaskBookEntry.PERSONA_ID == persona_id,
                TaskBookEntry.STATUS == STATUS_OPEN,
            )
            .order_by(TaskBookEntry.CREATED_AT.asc(), TaskBookEntry.TASK_ID.asc())
            .all()
        )
        return [_to_dict(r) for r in rows]
    finally:
        db.close()


def list_open_with_due(manager: Any, persona_id: str) -> List[Dict[str, Any]]:
    """期限のある open な一件の一覧 (DUE_AT 昇順)。

    将来のティックの締め切り引き当て (決定論・締め切り優先、intent §5) 用。
    **期限なし (DUE_AT が NULL) の行はここに出ない** — 引き当てに乗るのは
    期限のある行だけで、期限なしの行は急かされない (§4.1 の軸二本)。
    """
    _validate_id("persona_id", persona_id)
    db = manager.SessionLocal()
    try:
        rows = (
            db.query(TaskBookEntry)
            .filter(
                TaskBookEntry.PERSONA_ID == persona_id,
                TaskBookEntry.STATUS == STATUS_OPEN,
                TaskBookEntry.DUE_AT.isnot(None),
            )
            .order_by(TaskBookEntry.DUE_AT.asc(), TaskBookEntry.TASK_ID.asc())
            .all()
        )
        return [_to_dict(r) for r in rows]
    finally:
        db.close()


def list_open_system_tasks(manager: Any, persona_id: str) -> List[Dict[str, Any]]:
    """システムタスク (ORIGIN='system') の open 一覧 (作成順)。

    機械がペルソナに差し込む急ぎでない依頼 (intent §9-5)。引き当て順は
    締め切り → システムタスク → プール。未解消なら次の空きティックが再び引く。
    """
    _validate_id("persona_id", persona_id)
    db = manager.SessionLocal()
    try:
        rows = (
            db.query(TaskBookEntry)
            .filter(
                TaskBookEntry.PERSONA_ID == persona_id,
                TaskBookEntry.STATUS == STATUS_OPEN,
                TaskBookEntry.ORIGIN == ORIGIN_SYSTEM,
            )
            .order_by(TaskBookEntry.CREATED_AT.asc(), TaskBookEntry.TASK_ID.asc())
            .all()
        )
        return [_to_dict(r) for r in rows]
    finally:
        db.close()


def get_entry(manager: Any, persona_id: str, task_id: str) -> Dict[str, Any]:
    """一件を状態を問わず取得する (閉じた行の顛末参照用)。無ければ例外。"""
    _validate_id("persona_id", persona_id)
    _validate_id("task_id", task_id)
    db = manager.SessionLocal()
    try:
        entry = (
            db.query(TaskBookEntry)
            .filter(
                TaskBookEntry.TASK_ID == task_id,
                TaskBookEntry.PERSONA_ID == persona_id,
            )
            .first()
        )
        if entry is None:
            raise TaskBookError(
                f"task not found for persona {persona_id!r}: {task_id!r}"
            )
        return _to_dict(entry)
    finally:
        db.close()
