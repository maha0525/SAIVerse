"""Cached Head Architecture: LineHeadSnapshot の永続化 store。

DB との load / save / delete を担当。pipeline はこの store を経由して
in-memory state と DB を同期する。

詳細: docs/intent/cached_head_architecture.md §3.3
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from sea.head_pipeline.registry import HeadSectionRegistry
from sea.head_pipeline.types import LineHeadSnapshot

LOGGER = logging.getLogger(__name__)


@dataclass
class StoredLineState:
    """DB から復元した 1 ライン分の永続化レコード。

    snapshot は A 相当、last_notified_sections は B 相当 (= 各 Section の
    最後に通知済み snapshot を name -> snapshot で保持)。pipeline はこれを
    使って in-memory state を再構築する。
    """
    snapshot: LineHeadSnapshot
    last_notified_sections: dict[str, Any]


class LineHeadSnapshotStore:
    """LineHeadSnapshot テーブルへの load / save / delete を担当する。

    Section ごとの serialize / deserialize は registry 経由で各 Section に委譲する
    (= store は Section snapshot の中身を知らない、Section の責務)。
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        registry: HeadSectionRegistry,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry

    # ---- save ----

    def save(
        self,
        snapshot: LineHeadSnapshot,
        last_notified_sections: dict[str, Any],
    ) -> None:
        """snapshot (A) + last_notified (B) を upsert で保存する。

        Section ごとに serialize_snapshot を呼んで JSON 文字列化し、
        ``section.name -> string`` の dict を最終的に JSON で包んで 1 カラムに入れる。
        """
        from database.models import LineHeadSnapshot as LineHeadSnapshotRow

        sections_serialized: dict[str, str] = {}
        notified_serialized: dict[str, str] = {}
        for section in self._registry.all_sections():
            name = section.name
            section_snapshot = snapshot.sections.get(name)
            if section_snapshot is not None:
                try:
                    sections_serialized[name] = section.serialize_snapshot(section_snapshot)
                except Exception:
                    LOGGER.exception(
                        "head_pipeline_store: serialize failed (snapshot) section=%s",
                        name,
                    )
            notified_value = last_notified_sections.get(name)
            if notified_value is not None:
                try:
                    notified_serialized[name] = section.serialize_snapshot(notified_value)
                except Exception:
                    LOGGER.exception(
                        "head_pipeline_store: serialize failed (notified) section=%s",
                        name,
                    )

        sections_json = json.dumps(sections_serialized, ensure_ascii=False)
        notified_json = json.dumps(notified_serialized, ensure_ascii=False)

        db = self._session_factory()
        try:
            row = db.query(LineHeadSnapshotRow).filter_by(
                PERSONA_ID=snapshot.persona_id,
                LINE_ID=snapshot.line_id,
            ).first()
            now = datetime.now()
            if row is None:
                row = LineHeadSnapshotRow(
                    PERSONA_ID=snapshot.persona_id,
                    LINE_ID=snapshot.line_id,
                    LINE_ROLE=snapshot.line_role,
                    MODEL_KEY=snapshot.model_key,
                    SECTIONS_JSON=sections_json,
                    LAST_NOTIFIED_JSON=notified_json,
                    SNAPSHOT_VERSION=snapshot.snapshot_version,
                    CAPTURED_AT=now,
                    UPDATED_AT=now,
                )
                db.add(row)
            else:
                row.LINE_ROLE = snapshot.line_role
                row.MODEL_KEY = snapshot.model_key
                row.SECTIONS_JSON = sections_json
                row.LAST_NOTIFIED_JSON = notified_json
                row.SNAPSHOT_VERSION = snapshot.snapshot_version
                row.CAPTURED_AT = now
                row.UPDATED_AT = now
            db.commit()
        except Exception:
            db.rollback()
            LOGGER.exception(
                "head_pipeline_store: save failed persona=%s line=%s",
                snapshot.persona_id, snapshot.line_id,
            )
        finally:
            db.close()

    def save_last_notified(
        self,
        persona_id: str,
        line_id: str,
        last_notified_sections: dict[str, Any],
    ) -> None:
        """B (last_notified) のみを更新する (= snapshot 本体は据え置きで diff 通知後の B 進行に使う)。

        該当行が無ければ no-op (= snapshot 不在の状態で B だけ書くのは不正)。
        """
        from database.models import LineHeadSnapshot as LineHeadSnapshotRow

        notified_serialized: dict[str, str] = {}
        for section in self._registry.all_sections():
            value = last_notified_sections.get(section.name)
            if value is not None:
                try:
                    notified_serialized[section.name] = section.serialize_snapshot(value)
                except Exception:
                    LOGGER.exception(
                        "head_pipeline_store: serialize failed (notified-only) section=%s",
                        section.name,
                    )

        notified_json = json.dumps(notified_serialized, ensure_ascii=False)

        db = self._session_factory()
        try:
            row = db.query(LineHeadSnapshotRow).filter_by(
                PERSONA_ID=persona_id, LINE_ID=line_id,
            ).first()
            if row is None:
                LOGGER.debug(
                    "head_pipeline_store: save_last_notified skipped (no row) persona=%s line=%s",
                    persona_id, line_id,
                )
                return
            row.LAST_NOTIFIED_JSON = notified_json
            row.UPDATED_AT = datetime.now()
            db.commit()
        except Exception:
            db.rollback()
            LOGGER.exception(
                "head_pipeline_store: save_last_notified failed persona=%s line=%s",
                persona_id, line_id,
            )
        finally:
            db.close()

    # ---- load ----

    def load(self, persona_id: str, line_id: str) -> Optional[StoredLineState]:
        """指定ラインの永続化 state を復元する。なければ None。

        Section ごとの deserialize は registry に登録されている Section の
        deserialize_snapshot に委ねる。registry に未登録の Section の保存値は
        無視される (= Section が後で動的に消えた場合の自然な振る舞い)。
        """
        from database.models import LineHeadSnapshot as LineHeadSnapshotRow

        db = self._session_factory()
        try:
            row = db.query(LineHeadSnapshotRow).filter_by(
                PERSONA_ID=persona_id, LINE_ID=line_id,
            ).first()
            if row is None:
                return None

            try:
                sections_serialized = json.loads(row.SECTIONS_JSON or "{}")
                notified_serialized = json.loads(row.LAST_NOTIFIED_JSON or "{}")
            except json.JSONDecodeError:
                LOGGER.exception(
                    "head_pipeline_store: corrupt JSON in row persona=%s line=%s",
                    persona_id, line_id,
                )
                return None

            sections: dict[str, Any] = {}
            for section in self._registry.all_sections():
                data = sections_serialized.get(section.name)
                if data is not None:
                    try:
                        sections[section.name] = section.deserialize_snapshot(data)
                    except Exception:
                        LOGGER.exception(
                            "head_pipeline_store: deserialize failed (snapshot) section=%s",
                            section.name,
                        )

            last_notified: dict[str, Any] = {}
            for section in self._registry.all_sections():
                data = notified_serialized.get(section.name)
                if data is not None:
                    try:
                        last_notified[section.name] = section.deserialize_snapshot(data)
                    except Exception:
                        LOGGER.exception(
                            "head_pipeline_store: deserialize failed (notified) section=%s",
                            section.name,
                        )

            captured_at = row.CAPTURED_AT.timestamp() if row.CAPTURED_AT else 0.0

            snapshot = LineHeadSnapshot(
                persona_id=row.PERSONA_ID,
                line_id=row.LINE_ID,
                line_role=row.LINE_ROLE,
                model_key=row.MODEL_KEY,
                captured_at=captured_at,
                snapshot_version=row.SNAPSHOT_VERSION or 1,
                sections=sections,
            )
            return StoredLineState(
                snapshot=snapshot,
                last_notified_sections=last_notified,
            )
        finally:
            db.close()

    # ---- delete ----

    def delete(self, persona_id: str, line_id: str) -> None:
        from database.models import LineHeadSnapshot as LineHeadSnapshotRow
        db = self._session_factory()
        try:
            row = db.query(LineHeadSnapshotRow).filter_by(
                PERSONA_ID=persona_id, LINE_ID=line_id,
            ).first()
            if row is not None:
                db.delete(row)
                db.commit()
        except Exception:
            db.rollback()
            LOGGER.exception(
                "head_pipeline_store: delete failed persona=%s line=%s",
                persona_id, line_id,
            )
        finally:
            db.close()
