"""Cached Head Architecture: head snapshot の永続化 store。

DB との load / save / delete を担当。pipeline はこの store を経由して
in-memory state と DB を同期する。

永続化先は ``session_head_snapshot`` テーブル (PK=(PERSONA_ID, MODEL_KEY)、
beat_execution_context.md §3.1)。旧 ``line_head_snapshot`` (キー=(persona, line))
の読み口は廃止済み — 旧データは起動時の backfill
(database/migrate.py: backfill_session_head_snapshots) が新テーブルへ移行する。

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
from sea.head_pipeline.types import LineHeadSnapshot, SnapshotStaleError

LOGGER = logging.getLogger(__name__)


@dataclass
class StoredLineState:
    """DB から復元した 1 Session (persona, model) 分の永続化レコード。

    snapshot は A 相当、last_notified_sections は B 相当 (= 各 Section の
    最後に通知済み snapshot を name -> snapshot で保持)。pipeline はこれを
    使って in-memory state を再構築する。クラス名の "Line" は歴史的名称。
    """
    snapshot: LineHeadSnapshot
    last_notified_sections: dict[str, Any]


class LineHeadSnapshotStore:
    """session_head_snapshot テーブルへの load / save / delete を担当する。

    Section ごとの serialize / deserialize は registry 経由で各 Section に委譲する
    (= store は Section snapshot の中身を知らない、Section の責務)。
    クラス名の "Line" は歴史的名称 (旧テーブルが line キーだった頃の名残)。
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
    ) -> bool:
        """snapshot (A) + last_notified (B) を upsert で保存し、commit 成否を返す。

        Section ごとに serialize_snapshot を呼んで JSON 文字列化し、
        ``section.name -> string`` の dict を最終的に JSON で包んで 1 カラムに入れる。

        戻り値 False (W6 fail-closed) = DB commit 失敗、または required Section
        (``section.required = True``) の serialize 失敗 / snapshot からの欠損。
        どちらも **DB に一切書き込まずに** 失敗を返す — Section を欠いた行を
        commit すると、既存の正常な永続 snapshot (stale-but-real な復旧元) を
        不完全な行で上書きしてしまう (Codex 二巡 P2 / 三巡 P2 — capture 失敗で
        key ごと省かれた場合は serialize を通らないため、欠損自体も検査する)。
        optional Section の serialize 失敗は該当 Section を省いて保存し True
        (restart 後は ensure_snapshot の欠損再 capture で自己回復する)。

        既存行の SNAPSHOT_VERSION が incoming より新しい場合も書き込まずに
        False — 並行 capture の遅延保存 (旧版の commit が新版の commit の後に
        届く) が DB を旧 head に巻き戻す競合の封鎖 (Codex 二巡 P1)。判定は
        **版条件付き UPDATE 一文**で行う (SELECT→UPDATE の check-then-act は
        並行 session の間で原子でない — Codex 三巡 P1)。
        """
        from database.models import SessionHeadSnapshot as SessionHeadSnapshotRow

        required_serialize_failed: dict[str, str] = {}
        sections_serialized: dict[str, str] = {}
        notified_serialized: dict[str, str] = {}
        for section in self._registry.all_sections():
            name = section.name
            section_snapshot = snapshot.sections.get(name)
            if section_snapshot is not None:
                try:
                    sections_serialized[name] = section.serialize_snapshot(section_snapshot)
                except Exception as exc:
                    LOGGER.exception(
                        "head_pipeline_store: serialize failed (snapshot) section=%s",
                        name,
                    )
                    if getattr(section, "required", False):
                        required_serialize_failed[name] = f"serialize failed: {exc!r}"
            notified_value = last_notified_sections.get(name)
            if notified_value is not None:
                try:
                    notified_serialized[name] = section.serialize_snapshot(notified_value)
                except Exception:
                    LOGGER.exception(
                        "head_pipeline_store: serialize failed (notified) section=%s",
                        name,
                    )

        required_missing = {
            section.name
            for section in self._registry.all_sections()
            if getattr(section, "required", False)
            and snapshot.sections.get(section.name) is None
        }
        if required_serialize_failed or required_missing:
            LOGGER.error(
                "head_pipeline_store: refusing to persist snapshot without required "
                "section(s) %s persona=%s model=%s (existing row left intact)",
                sorted(set(required_serialize_failed) | required_missing),
                snapshot.persona_id, snapshot.model_key,
            )
            return False

        sections_json = json.dumps(sections_serialized, ensure_ascii=False)
        notified_json = json.dumps(notified_serialized, ensure_ascii=False)

        from sqlalchemy import or_

        db = self._session_factory()
        try:
            now = datetime.now()
            values = {
                "LINE_ROLE": snapshot.line_role,
                "SECTIONS_JSON": sections_json,
                "LAST_NOTIFIED_JSON": notified_json,
                "SNAPSHOT_VERSION": snapshot.snapshot_version,
                "CAPTURED_AT": now,
                "UPDATED_AT": now,
            }
            # 版条件付き UPDATE (単一文で比較と書き込みを行う)。同版は許可
            # (内容の収束再保存)、より新しい版が居たら rowcount=0 になる。
            updated = db.query(SessionHeadSnapshotRow).filter(
                SessionHeadSnapshotRow.PERSONA_ID == snapshot.persona_id,
                SessionHeadSnapshotRow.MODEL_KEY == snapshot.model_key,
                or_(
                    SessionHeadSnapshotRow.SNAPSHOT_VERSION.is_(None),
                    SessionHeadSnapshotRow.SNAPSHOT_VERSION <= snapshot.snapshot_version,
                ),
            ).update(values, synchronize_session=False)
            if updated == 0:
                existing = db.query(SessionHeadSnapshotRow.SNAPSHOT_VERSION).filter_by(
                    PERSONA_ID=snapshot.persona_id,
                    MODEL_KEY=snapshot.model_key,
                ).first()
                if existing is not None:
                    db.rollback()
                    LOGGER.warning(
                        "head_pipeline_store: refusing stale save (incoming v%d < stored v%s) "
                        "persona=%s model=%s",
                        snapshot.snapshot_version, existing[0],
                        snapshot.persona_id, snapshot.model_key,
                    )
                    return False
                # 行なし → INSERT (並行 INSERT との競合は UNIQUE 違反で except に
                # 落ち False — 呼び出し側の再試行が UPDATE 経路で収束する)
                db.add(SessionHeadSnapshotRow(
                    PERSONA_ID=snapshot.persona_id,
                    MODEL_KEY=snapshot.model_key,
                    **values,
                ))
            db.commit()
        except Exception:
            db.rollback()
            LOGGER.exception(
                "head_pipeline_store: save failed persona=%s model=%s",
                snapshot.persona_id, snapshot.model_key,
            )
            return False
        finally:
            db.close()
        return True

    def save_last_notified(
        self,
        persona_id: str,
        model_key: str,
        last_notified_sections: dict[str, Any],
    ) -> bool:
        """B (last_notified) のみを更新し、commit 成否を返す。

        snapshot 本体は据え置きで diff 通知後の B 進行に使う。該当行が無ければ
        no-op (= snapshot 不在の状態で B だけ書くのは不正) で False。
        B の永続化失敗は fail-closed の対象外 (restart 後の再通知重複は
        自己限定的で、人格の欠損ではない) — 成否は観測用。
        """
        from database.models import SessionHeadSnapshot as SessionHeadSnapshotRow

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
            row = db.query(SessionHeadSnapshotRow).filter_by(
                PERSONA_ID=persona_id, MODEL_KEY=model_key,
            ).first()
            if row is None:
                LOGGER.debug(
                    "head_pipeline_store: save_last_notified skipped (no row) persona=%s model=%s",
                    persona_id, model_key,
                )
                return False
            row.LAST_NOTIFIED_JSON = notified_json
            row.UPDATED_AT = datetime.now()
            db.commit()
        except Exception:
            db.rollback()
            LOGGER.exception(
                "head_pipeline_store: save_last_notified failed persona=%s model=%s",
                persona_id, model_key,
            )
            return False
        finally:
            db.close()
        return True

    # ---- load ----

    def load_version(self, persona_id: str, model_key: str) -> Optional[int]:
        """指定 Session の永続行の SNAPSHOT_VERSION のみを返す。行が無ければ None。

        pipeline._next_version が「state 不在で store に既存行がある」再起動直後の
        採番継続に使う (全 deserialize を伴う load より軽い)。
        """
        from database.models import SessionHeadSnapshot as SessionHeadSnapshotRow

        db = self._session_factory()
        try:
            row = db.query(SessionHeadSnapshotRow.SNAPSHOT_VERSION).filter_by(
                PERSONA_ID=persona_id, MODEL_KEY=model_key,
            ).first()
            return int(row[0]) if row is not None and row[0] else None
        finally:
            db.close()

    def load(self, persona_id: str, model_key: str) -> Optional[StoredLineState]:
        """指定 Session (persona, model) の永続化 state を復元する。なければ None。

        Section ごとの deserialize は registry に登録されている Section の
        deserialize_snapshot に委ねる。registry に未登録の Section の保存値は
        無視される (= Section が後で動的に消えた場合の自然な振る舞い)。
        """
        from database.models import SessionHeadSnapshot as SessionHeadSnapshotRow

        db = self._session_factory()
        try:
            row = db.query(SessionHeadSnapshotRow).filter_by(
                PERSONA_ID=persona_id, MODEL_KEY=model_key,
            ).first()
            if row is None:
                return None

            try:
                sections_serialized = json.loads(row.SECTIONS_JSON or "{}")
                notified_serialized = json.loads(row.LAST_NOTIFIED_JSON or "{}")
            except json.JSONDecodeError:
                LOGGER.exception(
                    "head_pipeline_store: corrupt JSON in row persona=%s model=%s",
                    persona_id, model_key,
                )
                return None

            sections: dict[str, Any] = {}
            for section in self._registry.all_sections():
                data = sections_serialized.get(section.name)
                if data is not None:
                    try:
                        sections[section.name] = section.deserialize_snapshot(data)
                    except SnapshotStaleError:
                        # 意図的な失効 (Section が再 capture を要求) — 破損では
                        # ないので ERROR/traceback にしない。詳細は Section 側が
                        # INFO で記録済み。
                        LOGGER.info(
                            "head_pipeline_store: snapshot stale (recapture) section=%s",
                            section.name,
                        )
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
                    except SnapshotStaleError:
                        LOGGER.info(
                            "head_pipeline_store: snapshot stale (recapture) section=%s (notified)",
                            section.name,
                        )
                    except Exception:
                        LOGGER.exception(
                            "head_pipeline_store: deserialize failed (notified) section=%s",
                            section.name,
                        )

            captured_at = row.CAPTURED_AT.timestamp() if row.CAPTURED_AT else 0.0

            snapshot = LineHeadSnapshot(
                persona_id=row.PERSONA_ID,
                model_key=row.MODEL_KEY,
                line_role=row.LINE_ROLE,
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

    def delete(self, persona_id: str, model_key: str) -> None:
        from database.models import SessionHeadSnapshot as SessionHeadSnapshotRow
        db = self._session_factory()
        try:
            row = db.query(SessionHeadSnapshotRow).filter_by(
                PERSONA_ID=persona_id, MODEL_KEY=model_key,
            ).first()
            if row is not None:
                db.delete(row)
                db.commit()
        except Exception:
            db.rollback()
            LOGGER.exception(
                "head_pipeline_store: delete failed persona=%s model=%s",
                persona_id, model_key,
            )
        finally:
            db.close()
