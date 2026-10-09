"""刺激の受領記録 (stimulus_receipt) — 同じ刺激の再配送で二度反応しないための照合。

外から届く刺激 (現象の封筒 ``TriggerEvent``) は、供給源が発行する一意な永続 ID
(``stimulus_id``) を義務として持つ (``phenomena/triggers.py`` の契約)。ペルソナへ
届く入口がこのモジュールの :func:`claim_stimulus` で受領を一行記録し、同じ
(ペルソナ, 刺激 ID) の二度目は「再配送」として何も起動しない。

決まったこと (docs/issues/on_event_judgment_has_no_idempotency_key.md、
2026-10-09 まはー決定):

- 受け取り口での代理採番はしない (ID が無い刺激は入口が fail-closed で落とす)。
- 受領記録は行ごとに受領時刻を持ち、**保持期間を過ぎた行だけを個別に消す**。
  記録全体を丸ごと消す瞬間は作らない。
- 古さの判定は実時間 (``time.time()``) で、``clock.now()`` の仮想時刻ではない。
- 本文のハッシュでの同一視はしない。

テーブル定義は ``database/models.py`` の :class:`StimulusReceipt`。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy.exc import IntegrityError

LOGGER = logging.getLogger(__name__)

#: 受領記録の保持期間 (秒)。これより古い行は個別に消され、同じ ID の刺激が
#: もう一度届けば新しい刺激として受け付けられる (再配送の窓は 7 日)。
RETENTION_SECONDS = 7 * 24 * 60 * 60

#: :func:`claim_stimulus` の結末
CLAIM_ACCEPTED = "accepted"     # 初めての刺激 — 受領を記録した
CLAIM_DUPLICATE = "duplicate"   # 同じ刺激の再配送 — 何も起動してはいけない
CLAIM_MISSING_ID = "missing_id"  # 刺激 ID が無い — 義務違反 (fail-closed)
#: 受領記録の書き込みが DB 障害で転んだ。刺激は落とさずに通す (呼び出し側は
#: 応対へ進む)。判断の経路は on_event の冪等キー (同じ stimulus_id) が二段目の
#: 歯止めになる。
CLAIM_UNRECORDED = "unrecorded"


def _now_epoch() -> int:
    """受領時刻。**実時間** — clock.now() (仮想時刻) は使わない。"""
    return int(time.time())


def claim_stimulus(manager: Any, persona_id: str, stimulus_id: Any) -> str:
    """刺激の受領を記録する。初回なら :data:`CLAIM_ACCEPTED`。

    同じトランザクションで、保持期間 (:data:`RETENTION_SECONDS`) を過ぎた行
    **だけ**を消す (全行を消す操作は持たない)。

    Returns:
        :data:`CLAIM_ACCEPTED` / :data:`CLAIM_DUPLICATE` / :data:`CLAIM_MISSING_ID`
        / :data:`CLAIM_UNRECORDED`。
    """
    if not isinstance(stimulus_id, str) or not stimulus_id.strip():
        LOGGER.error(
            "[stimulus-receipt] stimulus without an ID reached persona %s; "
            "refusing it (the source must issue a durable stimulus_id)",
            persona_id,
        )
        return CLAIM_MISSING_ID

    session_factory = getattr(manager, "SessionLocal", None)
    if session_factory is None:
        LOGGER.error(
            "[stimulus-receipt] manager has no SessionLocal; cannot record the "
            "receipt of %s for persona %s (passing it through)",
            stimulus_id, persona_id,
        )
        return CLAIM_UNRECORDED

    from database.models import StimulusReceipt

    now = _now_epoch()
    db = session_factory()
    try:
        # 期限切れの行だけを個別に消す。新しい行 (保持期間内) には触れない。
        db.query(StimulusReceipt).filter(
            StimulusReceipt.RECEIVED_AT < now - RETENTION_SECONDS,
        ).delete(synchronize_session=False)
        db.add(StimulusReceipt(
            PERSONA_ID=persona_id,
            STIMULUS_ID=stimulus_id,
            RECEIVED_AT=now,
        ))
        db.commit()
        return CLAIM_ACCEPTED
    except IntegrityError:
        db.rollback()
        LOGGER.info(
            "[stimulus-receipt] stimulus %s was already received by persona %s; "
            "treating this as a redelivery (nothing will be started)",
            stimulus_id, persona_id,
        )
        return CLAIM_DUPLICATE
    except Exception:
        db.rollback()
        LOGGER.error(
            "[stimulus-receipt] failed to record the receipt of %s for persona %s; "
            "passing it through unrecorded", stimulus_id, persona_id,
            exc_info=True,
        )
        return CLAIM_UNRECORDED
    finally:
        db.close()
