"""SAIVerse バージョンアップグレードハンドラの登録モジュール。

各ハンドラは :class:`saiverse.upgrade.UpgradeHandler` の定義に従い、特定の
``from_version → to_version`` 遷移で1度だけ実行される。冪等性は基本的に
Phase 1 の機構（``current >= target`` で no-op）に頼っているため、各ハンドラ
は「状態の冪等性」（再実行で同じ状態になる）を満たす必要がある。

設計詳細: ``docs/intent/version_aware_world_and_persona.md``
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, List

from saiverse.upgrade import UpgradeHandler

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from database.models import AI

LOGGER = logging.getLogger(__name__)


# ---- v0.3.0 第1号: dynamic_state captured_at リセット ----

def _v0_3_0_dynamic_state_reset(*, session: "Session", ai: "AI") -> None:
    """v0.3.0 で dynamic_state の Memopedia 判定がタイムスタンプベースに変わった
    ため、各ペルソナの ``PersonaBuildingState.LAST_NOTIFIED_JSON`` の
    ``captured_at`` を現在時刻にリセットし、旧形式の ``memopedia_pages`` を
    空配列化する。

    あわせて SAIMemory に「アップデート検知」通知を1件挿入してペルソナに
    アップデートがあったことを伝える。

    冪等性:
    - captured_at リセット: 何度走っても「現在時刻」が入るので状態として同じ
    - memopedia_pages: 既に空でも問題なし
    - SAIMemory 通知: Phase 1 の機構によりアップグレード時に1度だけ走る前提
      （テストで強制的に複数回走らせると通知が累積するが、これは許容範囲）
    """
    from database.models import PersonaBuildingState

    persona_id = ai.AIID
    LOGGER.info("[handler:v0_3_0_dynamic_state_reset] starting for persona=%s", persona_id)

    rows = session.query(PersonaBuildingState).filter_by(PERSONA_ID=persona_id).all()
    LOGGER.info(
        "[handler:v0_3_0_dynamic_state_reset] persona=%s: %d PersonaBuildingState row(s) to process",
        persona_id, len(rows),
    )

    now_ts = time.time()
    reset_count = 0
    skipped_count = 0
    for row in rows:
        if not row.LAST_NOTIFIED_JSON:
            skipped_count += 1
            continue
        try:
            data = json.loads(row.LAST_NOTIFIED_JSON)
        except json.JSONDecodeError as exc:
            # データ破損ケース。スキップして他を続ける（このハンドラ単体は失敗扱いにしない）
            LOGGER.warning(
                "[handler:v0_3_0_dynamic_state_reset] persona=%s building=%s: "
                "malformed LAST_NOTIFIED_JSON, skipping: %s",
                persona_id, row.BUILDING_ID, exc,
            )
            skipped_count += 1
            continue

        old_captured_at = data.get("captured_at")
        old_pages_count = len(data.get("memopedia_pages") or [])
        data["captured_at"] = now_ts
        data["memopedia_pages"] = []
        row.LAST_NOTIFIED_JSON = json.dumps(data, ensure_ascii=False)
        reset_count += 1
        LOGGER.debug(
            "[handler:v0_3_0_dynamic_state_reset] persona=%s building=%s: "
            "captured_at %r -> %s, memopedia_pages %d -> 0",
            persona_id, row.BUILDING_ID, old_captured_at, now_ts, old_pages_count,
        )

    LOGGER.info(
        "[handler:v0_3_0_dynamic_state_reset] persona=%s: reset=%d skipped=%d",
        persona_id, reset_count, skipped_count,
    )

    # SAIMemory にアップデート通知を挿入（失敗してもハンドラ全体は成功扱い：
    # 状態側のリセットは既に成功しているので）
    _insert_upgrade_notification(persona_id)


def _insert_upgrade_notification(persona_id: str) -> None:
    """ペルソナの SAIMemory にアップデート検知通知を1件挿入する。"""
    try:
        from saiverse_memory.adapter import SAIMemoryAdapter
    except ImportError as exc:
        LOGGER.warning(
            "[handler] cannot import SAIMemoryAdapter, skipping notification for %s: %s",
            persona_id, exc, exc_info=True,
        )
        return

    try:
        adapter = SAIMemoryAdapter(persona_id)
    except Exception as exc:
        LOGGER.warning(
            "[handler] failed to initialise SAIMemory adapter for %s, "
            "skipping notification: %s",
            persona_id, exc, exc_info=True,
        )
        return

    if not adapter.is_ready():
        LOGGER.warning(
            "[handler] SAIMemory not ready for %s, skipping notification",
            persona_id,
        )
        return

    # `event_message` タグがペルソナの会話コンテキストに取り込まれるキー
    # (sea/runtime_context.py の required_tags 参照)。dynamic_state.py の
    # 既存イベント通知と同じ扱いにする。`system_event` / `version_upgrade` は
    # 後から検索/フィルタするための識別子。
    message = {
        "role": "user",
        "content": (
            "<system>[システム通知]\n"
            "- SAIVerse v0.3.0 へのアップデートを検知しました。"
            "Memopediaの状態同期がリセットされました</system>"
        ),
        "metadata": {
            "tags": ["internal", "event_message", "system_event", "version_upgrade"],
        },
    }
    try:
        adapter.append_persona_message(message)
        LOGGER.info(
            "[handler] inserted v0.3.0 upgrade notification into SAIMemory for %s",
            persona_id,
        )
    except Exception as exc:
        LOGGER.warning(
            "[handler] failed to insert notification for %s: %s",
            persona_id, exc, exc_info=True,
        )


# ---- ハンドラ登録リスト ----

# 各ハンドラは to_version の昇順に書くと読みやすい（実行順は upgrade.py 側で
# select_handlers() がソートする）。
HANDLERS: List[UpgradeHandler] = [
    UpgradeHandler(
        name="v0_3_0_dynamic_state_reset",
        scope="ai",
        from_version="0.0.0",
        # dev サフィックスを使うことで 0.2.x → 0.3.0.dev0 への遷移でも走り、
        # 0.3.0.dev0 → 0.3.0 (release) では走らない（既に走り済みのため）。
        to_version="0.3.0.dev0",
        run=_v0_3_0_dynamic_state_reset,
        description=(
            "Reset dynamic_state captured_at for all PersonaBuildingState rows "
            "and clear legacy memopedia_pages snapshot. Notify the persona via "
            "SAIMemory."
        ),
    ),
]
