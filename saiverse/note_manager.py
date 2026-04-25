"""NoteManager: Note と Note ↔ Page/Message/Track の関連を管理する。

Intent A v0.9 / Intent B v0.6 に準拠。Note は「関心の固まり」で、
Memopedia ページ群とメッセージ群を束ねる恒久的な資産。

Note の type は person / project / vocation の 3 種のみ (Intent A v0.6 で確定、
ノート乱発防止のため絞られている)。Track が close されても Note は残り続ける。

責務:
- Note の CRUD と検索
- Note ↔ Memopedia ページの多対多
- Note ↔ メッセージの多対多 (3 人会話のメッセージ重複問題を解決する核)
- Track ↔ 開いている Note の多対多

責務外:
- Memopedia ページ自体の管理 (SAIMemory 側の memopedia)
- audience を起点にしたメッセージの自動 Note メンバーシップ生成
  (Metabolism 連携が必要、別レイヤーで実装)
- LLM ツールへの登録 (tools/ 配下で別途行う)

詳細: docs/intent/persona_action_tracks.md
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Callable, List, Optional

from sqlalchemy.orm import Session

from database.models import Note, NoteMessage, NotePage, TrackOpenNote

# --- Note type 定数 (Intent A v0.6 確定) ---
NOTE_TYPE_PERSON = "person"      # 関わる相手 (ペルソナ・ユーザー・外部 AI 等)
NOTE_TYPE_PROJECT = "project"    # 期限のある取り組み
NOTE_TYPE_VOCATION = "vocation"  # 恒久的な専門性・アイデンティティ

ALL_NOTE_TYPES = frozenset({NOTE_TYPE_PERSON, NOTE_TYPE_PROJECT, NOTE_TYPE_VOCATION})


class NoteError(Exception):
    """Base error for note manager."""


class NoteNotFoundError(NoteError):
    """Raised when note_id is not found."""


class InvalidNoteTypeError(NoteError):
    """Raised when note_type is not one of person/project/vocation."""


class NoteManager:
    """Note とその関連の CRUD を担う。

    全メソッドは 1 トランザクション内で完結する (内部で SessionLocal を開閉する)。
    Track 側の永続化は TrackManager が、メッセージ・Memopedia ページ自体の
    永続化は SAIMemory 側が責務を持つ。本クラスはそれらの「関連付け」のみ扱う。
    """

    def __init__(self, session_factory: Callable[[], Session]):
        self.SessionLocal = session_factory

    # ------------------------------------------------------------------
    # Note CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        persona_id: str,
        title: str,
        note_type: str,
        description: Optional[str] = None,
        metadata: Optional[str] = None,
    ) -> str:
        """新規 Note を作成する。

        Returns:
            note_id (UUID 文字列)
        """
        if not persona_id:
            raise ValueError("persona_id is required")
        if not title:
            raise ValueError("title is required")
        if note_type not in ALL_NOTE_TYPES:
            raise InvalidNoteTypeError(
                f"note_type must be one of {sorted(ALL_NOTE_TYPES)}, got {note_type!r}"
            )

        note_id = str(uuid.uuid4())
        db = self.SessionLocal()
        try:
            note = Note(
                note_id=note_id,
                persona_id=persona_id,
                title=title,
                note_type=note_type,
                description=description,
                note_metadata=metadata,
                is_active=True,
            )
            db.add(note)
            db.commit()
            logging.info(
                "[note] created %s persona=%s type=%s title=%r",
                note_id, persona_id, note_type, title,
            )
            return note_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, note_id: str) -> Note:
        db = self.SessionLocal()
        try:
            note = db.query(Note).filter_by(note_id=note_id).first()
            if note is None:
                raise NoteNotFoundError(f"note not found: {note_id}")
            db.expunge(note)
            return note
        finally:
            db.close()

    def list_for_persona(
        self,
        persona_id: str,
        note_type: Optional[str] = None,
        include_inactive: bool = False,
    ) -> List[Note]:
        if note_type is not None and note_type not in ALL_NOTE_TYPES:
            raise InvalidNoteTypeError(f"unknown note_type: {note_type!r}")
        db = self.SessionLocal()
        try:
            query = db.query(Note).filter_by(persona_id=persona_id)
            if note_type is not None:
                query = query.filter_by(note_type=note_type)
            if not include_inactive:
                query = query.filter_by(is_active=True)
            notes = query.order_by(Note.last_opened_at.desc().nullslast()).all()
            for n in notes:
                db.expunge(n)
            return notes
        finally:
            db.close()

    def archive(self, note_id: str) -> Note:
        """Note を非アクティブ化する (is_active=False)。

        恒久的に残るが、通常リスティングからは外れる。完全削除はしない。
        """
        db = self.SessionLocal()
        try:
            note = self._fetch_or_raise(db, note_id)
            note.is_active = False
            db.commit()
            db.refresh(note)
            db.expunge(note)
            logging.info("[note] archived %s", note_id)
            return note
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def unarchive(self, note_id: str) -> Note:
        db = self.SessionLocal()
        try:
            note = self._fetch_or_raise(db, note_id)
            note.is_active = True
            db.commit()
            db.refresh(note)
            db.expunge(note)
            logging.info("[note] unarchived %s", note_id)
            return note
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def close_project(self, note_id: str) -> Note:
        """project 系 Note を完了状態にする (closed_at をセット)。

        Note の type が project 以外なら ValueError。close 後も
        is_active は保たれる (記憶として参照可能)。
        """
        db = self.SessionLocal()
        try:
            note = self._fetch_or_raise(db, note_id)
            if note.note_type != NOTE_TYPE_PROJECT:
                raise ValueError(
                    f"close_project only valid for project notes, got {note.note_type}"
                )
            note.closed_at = datetime.now()
            db.commit()
            db.refresh(note)
            db.expunge(note)
            logging.info("[note] project closed %s", note_id)
            return note
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def touch_opened(self, note_id: str) -> None:
        """last_opened_at を現在時刻に更新する (Note 一覧の並び替えに使用)。"""
        db = self.SessionLocal()
        try:
            note = self._fetch_or_raise(db, note_id)
            note.last_opened_at = datetime.now()
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Note ↔ Memopedia ページ
    # ------------------------------------------------------------------

    def add_page(self, note_id: str, page_id: str) -> None:
        """Note に Memopedia ページを関連付ける (idempotent)。"""
        if not page_id:
            raise ValueError("page_id is required")
        db = self.SessionLocal()
        try:
            self._fetch_or_raise(db, note_id)  # 存在確認
            existing = (
                db.query(NotePage)
                .filter_by(note_id=note_id, page_id=page_id)
                .first()
            )
            if existing is not None:
                return
            db.add(NotePage(note_id=note_id, page_id=page_id))
            db.commit()
            logging.info("[note] +page %s -> %s", note_id, page_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def remove_page(self, note_id: str, page_id: str) -> None:
        db = self.SessionLocal()
        try:
            link = (
                db.query(NotePage)
                .filter_by(note_id=note_id, page_id=page_id)
                .first()
            )
            if link is not None:
                db.delete(link)
                db.commit()
                logging.info("[note] -page %s -> %s", note_id, page_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_pages(self, note_id: str) -> List[str]:
        """Note に紐づく Memopedia ページ ID 一覧。"""
        db = self.SessionLocal()
        try:
            rows = db.query(NotePage).filter_by(note_id=note_id).all()
            return [row.page_id for row in rows]
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Note ↔ メッセージ
    # ------------------------------------------------------------------

    def add_message(
        self,
        note_id: str,
        message_id: str,
        auto_added: bool = False,
    ) -> None:
        """Note にメッセージを関連付ける (idempotent)。

        auto_added: True なら audience による自動メンバーシップ由来。
        False ならペルソナまたはメタレイヤーが明示的に追加した。
        """
        if not message_id:
            raise ValueError("message_id is required")
        db = self.SessionLocal()
        try:
            self._fetch_or_raise(db, note_id)
            existing = (
                db.query(NoteMessage)
                .filter_by(note_id=note_id, message_id=message_id)
                .first()
            )
            if existing is not None:
                return
            db.add(NoteMessage(
                note_id=note_id,
                message_id=message_id,
                auto_added=auto_added,
            ))
            db.commit()
            logging.info(
                "[note] +msg %s -> %s (auto=%s)",
                note_id, message_id, auto_added,
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def remove_message(self, note_id: str, message_id: str) -> None:
        db = self.SessionLocal()
        try:
            link = (
                db.query(NoteMessage)
                .filter_by(note_id=note_id, message_id=message_id)
                .first()
            )
            if link is not None:
                db.delete(link)
                db.commit()
                logging.info("[note] -msg %s -> %s", note_id, message_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_messages(self, note_id: str) -> List[str]:
        """Note に紐づくメッセージ ID 一覧 (時系列降順)。"""
        db = self.SessionLocal()
        try:
            rows = (
                db.query(NoteMessage)
                .filter_by(note_id=note_id)
                .order_by(NoteMessage.added_at.desc())
                .all()
            )
            return [row.message_id for row in rows]
        finally:
            db.close()

    def get_notes_for_message(self, message_id: str) -> List[str]:
        """指定メッセージが属する Note ID 一覧 (3 人会話のメッセージ重複参照に使う)。"""
        db = self.SessionLocal()
        try:
            rows = db.query(NoteMessage).filter_by(message_id=message_id).all()
            return [row.note_id for row in rows]
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Track ↔ 開いている Note
    # ------------------------------------------------------------------

    def attach_to_track(self, track_id: str, note_id: str) -> None:
        """Track に Note を「開く」(idempotent)。

        Track の running 中はこの関連が再開コンテキスト構築に使われる。
        """
        if not track_id:
            raise ValueError("track_id is required")
        db = self.SessionLocal()
        try:
            self._fetch_or_raise(db, note_id)
            existing = (
                db.query(TrackOpenNote)
                .filter_by(track_id=track_id, note_id=note_id)
                .first()
            )
            if existing is not None:
                return
            db.add(TrackOpenNote(track_id=track_id, note_id=note_id))
            db.commit()
            logging.info("[note] attach %s to track %s", note_id, track_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def detach_from_track(self, track_id: str, note_id: str) -> None:
        db = self.SessionLocal()
        try:
            link = (
                db.query(TrackOpenNote)
                .filter_by(track_id=track_id, note_id=note_id)
                .first()
            )
            if link is not None:
                db.delete(link)
                db.commit()
                logging.info("[note] detach %s from track %s", note_id, track_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_open_notes(self, track_id: str) -> List[Note]:
        """Track が開いている Note の一覧。"""
        db = self.SessionLocal()
        try:
            note_ids = [
                row.note_id
                for row in db.query(TrackOpenNote).filter_by(track_id=track_id).all()
            ]
            if not note_ids:
                return []
            notes = db.query(Note).filter(Note.note_id.in_(note_ids)).all()
            for n in notes:
                db.expunge(n)
            return notes
        finally:
            db.close()

    def list_tracks_with_note(self, note_id: str) -> List[str]:
        """指定 Note を開いている Track ID 一覧。"""
        db = self.SessionLocal()
        try:
            rows = db.query(TrackOpenNote).filter_by(note_id=note_id).all()
            return [row.track_id for row in rows]
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _fetch_or_raise(self, db: Session, note_id: str) -> Note:
        note = db.query(Note).filter_by(note_id=note_id).first()
        if note is None:
            raise NoteNotFoundError(f"note not found: {note_id}")
        return note
