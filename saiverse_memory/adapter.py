from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sai_memory.config import Settings, load_settings
from sai_memory.memory.chunking import chunk_text
from sai_memory.memory.recall import (
    Embedder,
    semantic_recall_groups,
)
from sai_memory.memory.storage import (
    add_message,
    Message,
    get_all_messages_for_search,
    get_messages_around,
    get_messages_last,
    get_messages_paginated,
    get_or_create_thread,
    init_db,
    compose_message_content,
    replace_message_embeddings,
    get_messages_with_persona_in_audience,
    # Stelis thread management
    StelisThread,
    create_stelis_thread,
    get_stelis_thread,
    get_stelis_thread_depth,
    get_stelis_children,
    get_active_stelis_threads,
    complete_stelis_thread,
    get_stelis_ancestor_chain,
    calculate_stelis_window_tokens,
    delete_stelis_thread,
    # Pulse logs
    add_pulse_log,
    get_pulse_logs_by_pulse,
    delete_pulse_logs_before,
    list_pulse_ids,
    count_pulse_ids,
    # Memory notes
    MemoryNote,
    add_memory_notes,
    get_unresolved_notes,
    get_unplanned_notes,
    get_planned_notes_by_group,
    get_planned_group_labels,
    set_note_plan,
    clear_note_plan,
    resolve_memory_notes,
    delete_resolved_notes_before,
    count_unresolved_notes,
    count_unplanned_notes,
    count_planned_groups,
)
from sai_memory.backup import BackupError, run_backup_auto

LOGGER = logging.getLogger(__name__)


def _auto_backup_enabled() -> bool:
    value = os.getenv("SAIMEMORY_BACKUP_ON_START", "true").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _coerce_content_to_text(content: Any) -> str:
    """Normalize a message ``content`` to text for the TEXT column.

    A memorize node whose upstream produced structured output (a dict/list)
    used to pass that object straight through to the SQLite bind, which raised
    ``type 'dict' is not supported`` — swallowed as a WARNING, so the memorize
    was silently lost (observed 2026-07-18, sophie_city_a). Normalizing at this
    single write entry (``_append_message``) fulfills the "record this" intent
    for every caller rather than each producer having to remember to stringify.
    dict/list become JSON (readable, round-trippable); other non-str values
    fall back to ``str()``. See docs/issues/memorize_dict_content_silently_dropped.md.
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, (dict, list)):
        try:
            return json.dumps(content, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(content)
    return str(content)


class SAIMemoryAdapter:
    """Thin integration layer that lets SAIVerse talk to SAIMemory storage."""

    _PERSONA_THREAD_SUFFIX = "__persona__"
    _ACTIVE_STATE_FILENAME = "active_state.json"
    _PULSE_SCOPED_PARENT_KEY = "pulse_scoped_parent"

    def __init__(
        self,
        persona_id: str,
        *,
        persona_dir: Optional[Path] = None,
        resource_id: Optional[str] = None,
        settings: Optional[Settings] = None,
        startup_backup: bool = False,
        recover_orphaned_thread: bool = False,
    ) -> None:
        """Create an adapter bound to ``personas/<persona_id>/memory.db``.

        ``startup_backup``: opt-in for the automatic startup backup thread.
        ペルソナ登録経路 (persona/bootstrap.py の initialise_memory_adapter)
        だけが True を渡す。ツール・API・スクリプトの使い捨て adapter が
        呼び出しごとに DB バックアップを走らせないための門 (P1: memory 系
        スペルの DB ロック玉突き)。環境変数 SAIMEMORY_BACKUP_ON_START との
        AND で最終判定する。

        ``recover_orphaned_thread``: opt-in for pulse-scoped thread orphan
        recovery (S4, beat_execution_context.md 不変条件6)。Stelis/subagent の
        thread 切替中にプロセスが死ぬと ``active_state.json`` に
        ``pulse_scoped_parent`` が残る — このフラグが True の初期化時にそれを
        検出して親 thread へ復元する。startup_backup と同じくペルソナ登録経路
        だけが True を渡す。ツール・API の使い捨て adapter が「走行中の
        Stelis」を誤って巻き戻さないための門。
        """
        base_settings = settings or load_settings()
        self.persona_id = persona_id
        if persona_dir:
            self.persona_dir = persona_dir
        else:
            from saiverse.data_paths import get_saiverse_home
            self.persona_dir = get_saiverse_home() / "personas" / persona_id
        self.persona_dir.mkdir(parents=True, exist_ok=True)

        if recover_orphaned_thread:
            # プロセス死で孤児化した Pulse スコープ thread (Stelis/subagent) の
            # 自然回復。active_state.json のみを触るので conn 構築より前に行う。
            self._recover_orphaned_pulse_thread()

        db_path = self.persona_dir / "memory.db"

        resolved_resource = resource_id or (base_settings.resource_id or persona_id)
        self.settings = replace(base_settings, db_path=str(db_path), resource_id=resolved_resource)
        # 錠前は「この DB ファイルのもの」。配り所から取るので、同じ persona の
        # memory.db を開く他の書き手 (API のバックグラウンドワーカー、Memopedia、
        # ツール) は、渡されなくても同じ錠前を持つ (まはー裁定 2026-08-06、案A。
        # docs/issues/memopedia_writers_bypass_adapter_lock.md)
        from sai_memory.db_locks import lock_for_path
        self._db_lock = lock_for_path(str(db_path))

        if not self.settings.memory_enabled:
            LOGGER.warning("SAIMemory disabled via settings; adapter will no-op")
            self.conn = None
            self.embedder = None
            return

        try:
            self.conn = init_db(self.settings.db_path, check_same_thread=False)
            # Create working_memory table if not exists
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS working_memory (
                    persona_id TEXT PRIMARY KEY,
                    data TEXT,
                    updated_at REAL
                )
            """)
            self.conn.commit()

            # Initialize Memopedia tables (idempotent; runs migrations for older DBs).
            # Memopedia tables share this connection's memory.db, so any code that
            # accesses self.conn directly (e.g. persona.history_manager) can rely on
            # the schema being migrated without first instantiating Memopedia(conn).
            from sai_memory.memopedia.storage import init_memopedia_tables
            init_memopedia_tables(self.conn)

            # Initialize core_memories table (記憶アーキv2 ゾーン A, 冪等)。
            # Memopedia と同様、self.conn 直参照経路 (core_memory スペル / head
            # セクション) がテーブルの存在を前提にできるよう、ここで作成する。
            from sai_memory.core_memory import init_core_memory_table
            init_core_memory_table(self.conn)

            # Initialize clips table (土地参照の統一プリミティブ, 冪等)。旧 marks
            # (層1 観測点) は点クリップとして clips に一般化・移行される。クリップの
            # アンカーは SAIMemory メッセージなので memory.db に相乗りする
            # (sai_memory/clips.py の module docstring 参照)。
            from sai_memory.clips import init_clips_tables
            init_clips_tables(self.conn)

            # Initialize purpose_tags table (層2〜4 目的タグ, life_concept_map.md
            # §9.1, 冪等)。タグの target の主流は SAIMemory メッセージ・出来事
            # なので memory.db に相乗りする (sai_memory/purpose_tags.py 参照)。
            from sai_memory.purpose_tags import init_purpose_tags_tables
            init_purpose_tags_tables(self.conn)

            # Initialize desk_items table (机の物理, 冪等)。Memory Atlas の
            # memory_open/close が使う「開いている」真実源。memory.db に相乗り
            # する (sai_memory/desk.py の module docstring 参照)。
            from sai_memory.desk import init_desk_tables
            init_desk_tables(self.conn)

            # Initialize perception_buffer table (知覚台帳, 冪等)。未消費の知覚を
            # 溜め、消費バッチ (確定文面) を持つ永続テーブル。会話履歴とは
            # 別テーブルだが memory.db に同居する (perception_buffer.py 参照)。
            # resource_id は旧二段 flush の一度きり清算が (resource_id,
            # created_at) 索引で範囲限定するために渡す。
            from sai_memory.perception_buffer import init_perception_buffer_table
            init_perception_buffer_table(
                self.conn, resource_id=self.settings.resource_id,
            )

            # P4-c 一回きり移行: vividness='vivid' のページを desk_items へ open。
            # 「鮮明＝常設掲示」という旧意図を、desk が生まれた後の正しい後継へ
            # 繋ぎ直す。vividness カラムはこの移行後、読み書きが止まる（死置き）
            # ため移行を逃すと意図が永遠に失われる（冪等・毎回 no-op が既定）。
            from sai_memory.memopedia.vivid_to_desk_migration import migrate_vivid_pages_to_desk
            migrate_vivid_pages_to_desk(self.conn)

            # P4-a: 編纂プランテーブル（curation_plans）冪等初期化。
            # just_sleep バッチ（P4-a2）が pending プランを読んで実行する。
            from sai_memory.curation_ops import init_curation_tables
            init_curation_tables(self.conn)
        except Exception as exc:
            LOGGER.exception("Failed to initialise SAIMemory DB at %s", self.settings.db_path)
            # init 途中で失敗したら開きかけの接続を閉じてから raise する
            # (未コミットのトランザクションが SQLite ロックを握り続けるのを防ぐ)
            half_open = getattr(self, "conn", None)
            if half_open is not None:
                try:
                    half_open.close()
                except Exception:
                    LOGGER.warning("Failed to close half-initialised SAIMemory connection", exc_info=True)
            self.conn = None
            self.embedder = None
            raise exc

        try:
            self.embedder = Embedder(
                model=self.settings.embed_model,
                local_model_path=self.settings.embed_model_path,
                model_dim=self.settings.embed_model_dim,
            )
        except Exception:
            LOGGER.warning(
                "Failed to load embedding model '%s'. "
                "Message storage will work but semantic search/recall will be unavailable.",
                self.settings.embed_model,
                exc_info=True,
            )
            self.embedder = None

        # Detect embedding model changes
        self.embed_model_changed = False
        if self.conn and self.embedder:
            self._check_embed_model_change()

        LOGGER.info(
            "SAIMemory adapter initialised for persona=%s db=%s (resource=%s)",
            self.persona_id,
            self.settings.db_path,
            self.settings.resource_id,
        )

        if startup_backup and _auto_backup_enabled():
            threading.Thread(target=self._run_startup_backup, daemon=True).start()

    # ------------------------------------------------------------------
    # Embedding model change detection
    # ------------------------------------------------------------------
    def _check_embed_model_change(self) -> None:
        """Detect if the embedding model has changed since the last reembed."""
        from sai_memory.memory.storage import get_embed_metadata, set_embed_metadata

        recorded_model = get_embed_metadata(self.conn, "embed_model")
        current_model = self.settings.embed_model

        row = self.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM message_embeddings LIMIT 1)"
        ).fetchone()
        has_embeddings = bool(row and row[0])

        if recorded_model is None and has_embeddings:
            # Upgraded from old version: no metadata recorded but embeddings exist
            self.embed_model_changed = True
            LOGGER.warning(
                "Persona %s: embed_model metadata not found but embeddings exist. "
                "Likely upgraded from old version. Reembed recommended.",
                self.persona_id,
            )
        elif recorded_model and recorded_model != current_model:
            # Model explicitly changed
            self.embed_model_changed = True
            LOGGER.warning(
                "Persona %s: embed_model changed from '%s' to '%s'. Reembed recommended.",
                self.persona_id,
                recorded_model,
                current_model,
            )
        elif not has_embeddings:
            # Fresh database — record current model immediately
            set_embed_metadata(self.conn, "embed_model", current_model)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def load_working_memory(self) -> Dict[str, Any]:
        """Load working memory from DB.

        Returns:
            Dict containing working memory data, or empty dict if not found.
        """
        if not self._ready:
            return {}
        try:
            with self._db_lock:
                cur = self.conn.execute(
                    "SELECT data FROM working_memory WHERE persona_id = ?",
                    (self.persona_id,)
                )
                row = cur.fetchone()
                if row and row[0]:
                    return json.loads(row[0])
                return {}
        except Exception as exc:
            LOGGER.warning("Failed to load working_memory for %s: %s", self.persona_id, exc)
            return {}

    def save_working_memory(self, data: Dict[str, Any]) -> None:
        """Save working memory to DB.

        Args:
            data: Dict to persist as working memory.
        """
        if not self._ready:
            return
        try:
            with self._db_lock:
                json_data = json.dumps(data, ensure_ascii=False)
                updated_at = time.time()
                self.conn.execute(
                    """
                    INSERT INTO working_memory (persona_id, data, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(persona_id) DO UPDATE SET data = ?, updated_at = ?
                    """,
                    (self.persona_id, json_data, updated_at, json_data, updated_at)
                )
                self.conn.commit()
                LOGGER.debug("Saved working_memory for %s", self.persona_id)
        except Exception as exc:
            LOGGER.warning("Failed to save working_memory for %s: %s", self.persona_id, exc)

    # ------------------------------------------------------------------
    # Recalled IDs (working memory subset) — DEPRECATED
    # recall_entry / recall_navigate ツールが廃止されたため、これらのメソッドは
    # 現在呼び出し元がなく非推奨。working_memory テーブルへの書き込みも停止済み。
    # ------------------------------------------------------------------
    RECALLED_IDS_KEY = "recalled_ids"
    RECALLED_IDS_MAX = 10

    def get_recalled_ids(self) -> List[Dict[str, Any]]:
        """[DEPRECATED] Get recalled IDs from working memory."""
        wm = self.load_working_memory()
        return wm.get(self.RECALLED_IDS_KEY, [])

    def add_recalled_id(
        self,
        source_type: str,
        source_id: str,
        title: str,
        uri: str,
    ) -> None:
        """[DEPRECATED] Add a recalled ID to working memory."""
        if not self._ready:
            return
        wm = self.load_working_memory()
        ids: list = wm.get(self.RECALLED_IDS_KEY, [])

        ids = [item for item in ids if item.get("id") != source_id]
        ids.append({
            "type": source_type,
            "id": source_id,
            "title": title,
            "uri": uri,
            "recalled_at": time.time(),
        })
        if len(ids) > self.RECALLED_IDS_MAX:
            ids = ids[-self.RECALLED_IDS_MAX:]

        wm[self.RECALLED_IDS_KEY] = ids
        self.save_working_memory(wm)

    def remove_recalled_id(self, source_id: str) -> bool:
        """[DEPRECATED] Remove a specific recalled ID."""
        if not self._ready:
            return False
        wm = self.load_working_memory()
        ids: list = wm.get(self.RECALLED_IDS_KEY, [])
        new_ids = [item for item in ids if item.get("id") != source_id]
        if len(new_ids) == len(ids):
            return False
        wm[self.RECALLED_IDS_KEY] = new_ids
        self.save_working_memory(wm)
        return True

    def clear_recalled_ids(self) -> int:
        """[DEPRECATED] Clear all recalled IDs."""
        if not self._ready:
            return 0
        wm = self.load_working_memory()
        ids: list = wm.get(self.RECALLED_IDS_KEY, [])
        count = len(ids)
        if count > 0:
            wm[self.RECALLED_IDS_KEY] = []
            self.save_working_memory(wm)
        return count

    def append_building_message(
        self,
        building_id: str,
        message: dict,
        *,
        thread_suffix: Optional[str] = None,
    ) -> Optional[str]:
        return self._append_message(building_id=building_id, message=message, thread_suffix=thread_suffix)

    def append_persona_message(
        self,
        message: dict,
        *,
        thread_suffix: Optional[str] = None,
    ) -> Optional[str]:
        return self._append_message(building_id=None, message=message, thread_suffix=thread_suffix)

    # ------------------------------------------------------------------
    # 知覚バッファ (Perception Buffer) — docs/intent/perception_buffer.md
    # ------------------------------------------------------------------
    #
    # 未消費の知覚を溜め (push)、Beat 頭で型別 reduce して台帳に消費印を打つ
    # (flush = 消費, §10.2)。書き込みは客観時間で随時、消費は主観時間の一歩
    # (Beat) でのみ。提示は runtime_context の時刻順マージが担う (§10.3)。

    def push_perception(
        self,
        kind: str,
        content: str,
        *,
        reduce_key: Optional[str] = None,
        salient: bool = False,
        media: Optional[list] = None,
        metadata: Optional[str] = None,
    ) -> None:
        """知覚を 1 件バッファに積む (ペルソナはまだ知覚しない)。

        ``media`` は画像等の添付 (``[{"path","mime_type","role"}, ...]``)。提示時に
        マージブロックの metadata.media へ載る。
        """
        if not self._ready:
            return
        from sai_memory.perception_buffer import push_perception
        with self._db_lock:
            push_perception(
                self.conn, kind, content,
                reduce_key=reduce_key, salient=salient, media=media, metadata=metadata,
            )

    def count_pending_perceptions(self, kind: str) -> Optional[int]:
        """未消費の知覚バッファにある指定 kind の件数。

        フィード配送の膨張ガード (saiverse/feed_manager.py) の読み口。未 ready の
        ときは 0 でなく **None** を返す — 呼び出し側は「数えられない」を「空」と
        区別して配送を見送る (0 を返すと上限ガードが素通りするため)。
        """
        if not self._ready:
            return None
        from sai_memory.perception_buffer import count_pending
        with self._db_lock:
            return count_pending(self.conn, kind)

    def has_pending_perception_marker(self, key: str, value: str) -> bool:
        """未消費の知覚バッファに metadata[key] == value の項目があるかを返す。

        フィード配送 (saiverse/feed_manager.py) 等の冪等ガード用の読み口。
        push_ledger_perception の outbox_id 照合と同じ流儀 (LIKE で絞って
        JSON parse で確定)。消費済みの分は照合しない — 消費済み位置の管理は
        呼び出し側のカーソル等が担う (消費済み行も台帳に残るようになったので、
        ``consumed_at IS NULL`` で明示的に未消費へ絞る)。
        """
        if not self._ready or not key or not value:
            return False
        with self._db_lock:
            rows = self.conn.execute(
                "SELECT metadata FROM perception_buffer "
                "WHERE consumed_at IS NULL AND metadata LIKE ?",
                (f"%{value}%",),
            ).fetchall()
        for row in rows:
            try:
                meta = json.loads(row[0]) if row[0] else None
            except (TypeError, ValueError):
                continue
            if isinstance(meta, dict) and meta.get(key) == value:
                return True
        return False

    def has_perception_marker(self, key: str, value: str) -> bool:
        """知覚台帳の全行 (未消費 + 消費済み) を対象に metadata[key] == value を照合する。

        「一度きり」の通知 (アップグレード通知など) の冪等ガード用。消費済み行が
        台帳に残るようになった (W14) ので、消費を跨いだ「もう届けたか」を台帳
        だけで答えられる。
        """
        if not self._ready or not key or not value:
            return False
        with self._db_lock:
            rows = self.conn.execute(
                "SELECT metadata FROM perception_buffer WHERE metadata LIKE ?",
                (f"%{value}%",),
            ).fetchall()
        for row in rows:
            try:
                meta = json.loads(row[0]) if row[0] else None
            except (TypeError, ValueError):
                continue
            if isinstance(meta, dict) and meta.get(key) == value:
                return True
        return False

    def flush_perception_buffer(
        self,
        *,
        pulse_id: Optional[str] = None,
        manager: Optional[Any] = None,
    ) -> bool:
        """未消費の知覚を消費する (Beat 頭の消費、bool ラッパー)。

        Returns: 1 件以上消費したら True。
        """
        return (
            self.flush_perception_buffer_payload(pulse_id=pulse_id, manager=manager)
            is not None
        )

    def flush_perception_buffer_payload(
        self,
        *,
        pulse_id: Optional[str] = None,
        manager: Optional[Any] = None,
    ) -> Optional[dict]:
        """未消費の知覚を型別 reduce し、消費バッチを確定する (Beat 頭の消費)。

        W14 知覚レンダリング (perception_buffer.md §10.2): 消費は「メッセージ行を
        書く → 項目を削除する」の二段ではなく、**単一トランザクションの消費バッチ
        確定** (``create_consumption_batch``) — バッチ行にレンダリング済み文面
        (reduce → format の結果 = ペルソナが見た文そのもの) が永続化され、項目には
        (consumed_at, batch_id) の印が入る。messages に event_message 行は作らない
        — 以後の提示はコンテキスト組み立て (sea/runtime_context.py) が messages と
        未付記バッチを時刻順マージして行う。二段 commit の隙間を塞ぐためにあった
        C6 の照合機構 (perception_ids 突き合わせ) は、二度書きの口が構造ごと
        消えたので退役した (§10.7 C6。旧実装は git 履歴が保険)。

        戻り値は ``{"content": "<system>…</system>", "media": [...]}``
        (消費するものが無い / バッチを確定できなかったときは None)。ラウンド途中の
        Beat 頭消費 (sea/runtime_llm.py の spell ループ) は、この戻り値をそのまま
        作業中の messages に append して「バッチに確定した内容」と「続きの生成が
        見る内容」を一致させる。tx 失敗時は pending のまま残り、次の Beat 頭で
        再試行される (SEA 監査 S5: 知覚を落とさない)。

        ``pulse_id``: 消費した Beat の属する Pulse (呼び出し側が持っていれば)。
        ``manager``: 開いている出来事 (episode) の照会用。層0タグと同じ供給源
        (saiverse.episodes.get_open_episode) から batch の ``episode_id`` を引く。
        どちらも無ければ NULL で記帳する。
        """
        if not self._ready:
            return None
        from sai_memory.perception_buffer import (
            create_consumption_batch,
            format_perception_message,
            list_pending,
            reduce_perceptions,
        )
        # 消費時に開いている出来事 (episode)。sea/runtime.py の _store_memory が
        # origin_episode を刻むのと同じ供給源 (per-persona キャッシュ付きの
        # get_open_episode)。失敗しても消費は止めない (記帳が NULL になるだけ)。
        episode_id: Optional[str] = None
        if manager is not None and getattr(manager, "SessionLocal", None) is not None:
            try:
                from saiverse.episodes import get_open_episode
                open_ep = get_open_episode(manager, self.persona_id)
                if open_ep and open_ep.get("episode_ref"):
                    episode_id = str(open_ep["episode_ref"])
            except Exception:
                LOGGER.debug(
                    "[perception_buffer] open-episode lookup failed; consuming "
                    "without an episode_id on the batch", exc_info=True,
                )
        try:
            with self._db_lock:
                items = list_pending(self.conn)
                if not items:
                    return None
                reduced = reduce_perceptions(items)
                text = format_perception_message(reduced)
                # reduce 後の全知覚の添付メディアを集約して 1 ブロックに載せる。
                # path で重複排除 (同じ画像を二重添付しない)。
                media: list = []
                seen_media: set = set()
                for it in reduced:
                    for m in it.media_list():
                        key = m.get("path") if isinstance(m, dict) else None
                        if key and key in seen_media:
                            continue
                        if key:
                            seen_media.add(key)
                        media.append(m)
                # 境界キー: バッチ確定時点で最後に保存済みの message の正典
                # 順序キー (created_at, rowid)。Chronicle 無効ペルソナの窓絞りが
                # anchor 行と同秒のバッチを正典順どおりに判定するための記帳。
                # 取れなければ NULL (旧世代と同じ epoch 比較へフォールバック)。
                boundary_created_at = boundary_rowid = None
                try:
                    boundary = self.conn.execute(
                        "SELECT created_at, rowid FROM messages "
                        "ORDER BY created_at DESC, rowid DESC LIMIT 1"
                    ).fetchone()
                    if boundary is not None:
                        boundary_created_at = int(boundary[0])
                        boundary_rowid = int(boundary[1])
                except Exception:
                    LOGGER.debug(
                        "[perception_buffer] boundary key lookup failed; "
                        "recording batch without one", exc_info=True,
                    )
                # 消費バッチを単一 tx で確定 (バッチ INSERT + 項目への印)。
                # reduce で畳まれて本文に出なかった分も消費済みになる
                # (相殺は未消費の間だけ = C2)。
                create_consumption_batch(
                    self.conn,
                    [it.id for it in items],
                    consumed_at=int(time.time()),
                    rendered_text=text,
                    pulse_id=pulse_id,
                    episode_id=episode_id,
                    media=media or None,
                    boundary_created_at=boundary_created_at,
                    boundary_rowid=boundary_rowid,
                )
        except Exception:
            # tx 失敗 (rollback 済み) = 消費不成立。pending は無傷なので次の
            # Beat 頭で再試行される。ここで本文を返すと「知覚した」ことになるのに
            # バッチが無い = 証跡と提示がズレるため、返さない。
            LOGGER.warning(
                "[perception_buffer] flush could not record the consumption "
                "batch; keeping items pending for retry", exc_info=True,
            )
            return None
        return {"content": f"<system>{text}</system>", "media": media}

    # ------------------------------------------------------------------
    # 実行台帳の配送口 (Execution Ledger outbox delivery)
    # docs/intent/execution_ledger.md §2.2 / 不変条件 3 —
    # 配送先の書き込みは execution_id を冪等キーとして刻み、再配送しても
    # 二重にならない。冪等性の強制は配送先 (= 本 adapter) の責務。
    #
    # 通常の append_* / push_perception と違い、失敗は**例外で表明する**。
    # 送信トレイの配送器 (saiverse/execution_ledger.py) は handler の例外で
    # 失敗を数え、握り潰された失敗は「配送成功の偽装」= 記録の消失になるため。
    # ------------------------------------------------------------------

    #: 台帳配送で messages.metadata / perception_buffer.metadata に刻む冪等キー名。
    #: outbox_id は world DB 側で一意 (AUTOINCREMENT) なので、1 実行が複数の
    #: 配送を持っても衝突しない。execution_id は照合・監査用に併記する。
    LEDGER_OUTBOX_META_KEY = "ledger_outbox_id"

    def append_ledger_message(
        self,
        message: dict,
        *,
        execution_id: str,
        outbox_id: int,
        building_id: Optional[str] = None,
        thread_suffix: Optional[str] = None,
    ) -> str:
        """outbox 配送 (target='saimemory.append') 専用の厳格な書き込み口。

        - 冪等: metadata.ledger_outbox_id が同じ既存行があれば追記せず既存 id を
          返す (配送成功→delivered 記帳前のクラッシュによる再配送で二重にしない)。
        - 本文・名義・実行時刻は payload のまま変形しない (不変条件 6)。刻むのは
          metadata の冪等キーだけ。
        - 失敗は例外 (_append_message の「None を返して握る」流儀を踏襲しない)。

        Returns: 書き込んだ (または既存の) message id。
        """
        if not self._ready:
            raise RuntimeError(
                f"SAIMemory adapter not ready (resource={self.settings.resource_id})"
            )
        if not isinstance(message, dict):
            raise ValueError("message must be a dict")
        existing = self._find_ledger_message(outbox_id)
        if existing is not None:
            LOGGER.info(
                "[ledger-delivery] duplicate append suppressed: outbox_id=%s "
                "already stored as message=%s", outbox_id, existing,
            )
            return existing
        msg = dict(message)
        metadata = msg.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata[self.LEDGER_OUTBOX_META_KEY] = int(outbox_id)
        metadata["execution_id"] = str(execution_id)
        msg["metadata"] = metadata
        mid = self._append_message(
            building_id=building_id, message=msg, thread_suffix=thread_suffix
        )
        if not mid:
            raise RuntimeError(
                f"SAIMemory append failed (outbox_id={outbox_id}, "
                f"execution={execution_id}, resource={self.settings.resource_id})"
            )
        return mid

    def _find_ledger_message(self, outbox_id: int) -> Optional[str]:
        """metadata に同じ ledger_outbox_id を刻んだ既存メッセージの id を返す。

        LIKE でキー名を含む行 (= 台帳配送された行だけ。通常メッセージは含まない)
        に絞ってから JSON 照合する — 直列化の空白差に依存する文字列一致を避ける。
        """
        with self._db_lock:
            rows = self.conn.execute(
                "SELECT id, metadata FROM messages WHERE metadata LIKE ?",
                (f"%{self.LEDGER_OUTBOX_META_KEY}%",),
            ).fetchall()
        for row in rows:
            try:
                meta = json.loads(row[1]) if row[1] else None
            except (TypeError, ValueError):
                continue
            if isinstance(meta, dict) and meta.get(self.LEDGER_OUTBOX_META_KEY) == int(outbox_id):
                return str(row[0])
        return None

    #: Building→個人記憶転記の provenance キー名 (metadata に刻む)。監査 M8。
    BUILDING_MSG_REF_META_KEY = "building_msg_ref"

    def find_message_by_building_ref(self, ref: str) -> Optional[str]:
        """Building 転記の provenance キー (metadata.building_msg_ref) で既存
        メッセージ id を引く (無ければ None)。

        監査 M8: 「memory.db への append 成功 → Building 側 ingested marker
        失敗」で宙に浮いた転記は、転記ループの停止規律 (失敗で即停止) により
        常に高々 1 件で、必ず次ラウンドの最初の転記候補になる。その 1 件だけが
        この照会を通るため、LIKE 走査はラウンドあたり最大 1 回に有界。
        LIKE には値 (ref) そのものを使う — building message_id を含むため実質
        その行しか当たらず、直列化の空白差にも依存しない (照合は JSON parse で
        確定する)。
        """
        if not self._ready or not ref:
            return None
        with self._db_lock:
            rows = self.conn.execute(
                "SELECT id, metadata FROM messages WHERE metadata LIKE ?",
                (f"%{ref}%",),
            ).fetchall()
        for row in rows:
            try:
                meta = json.loads(row[1]) if row[1] else None
            except (TypeError, ValueError):
                continue
            if (
                isinstance(meta, dict)
                and meta.get(self.BUILDING_MSG_REF_META_KEY) == ref
            ):
                return str(row[0])
        return None

    def push_ledger_perception(
        self,
        *,
        execution_id: str,
        outbox_id: int,
        kind: str,
        content: str,
        reduce_key: Optional[str] = None,
        salient: bool = False,
        media: Optional[list] = None,
        metadata: Optional[str] = None,
    ) -> bool:
        """outbox 配送 (target='perception.push') 専用の厳格な書き込み口。

        - 冪等: 専用列 ``ledger_outbox_id`` の UNIQUE 索引で **DB 側が原子的に**
          重複を弾く (2026-08-19 Codex 第八巡 #1 — 旧実装の「metadata JSON を
          LIKE 走査 → 無ければ INSERT」は check-then-act で同時配送の競合に
          破れ、消費済み行が溜まるほど走査が線形悪化した)。消費済み行も台帳に
          残るので、この冪等は消費を自然に跨ぐ (「配送成功 → 消費 → 再配送」
          でも二重にならない)。metadata には従来どおり照合・監査用に併記する。
        - 失敗は例外 (push_perception の「未 ready なら黙って return」を踏襲しない)。

        Returns: 積んだら True、冪等スキップなら False。
        """
        if not self._ready:
            raise RuntimeError(
                f"SAIMemory adapter not ready (resource={self.settings.resource_id})"
            )
        if not kind or content is None:
            raise ValueError("perception delivery requires kind and content")
        marker = {
            self.LEDGER_OUTBOX_META_KEY: int(outbox_id),
            "execution_id": str(execution_id),
        }
        # 送り主 (各処理) の metadata は変形せず持ち越す。dict-JSON なら
        # 冪等キーをマージ、それ以外は producer_metadata として包む。
        if metadata:
            try:
                parsed = json.loads(metadata)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                parsed.update(marker)
                marker = parsed
            else:
                marker["producer_metadata"] = metadata
        from sai_memory.perception_buffer import push_perception
        with self._db_lock:
            item_id = push_perception(
                self.conn, kind, content,
                reduce_key=reduce_key, salient=salient, media=media,
                metadata=json.dumps(marker, ensure_ascii=False),
                ledger_outbox_id=str(int(outbox_id)),
            )
        if item_id is None:
            LOGGER.info(
                "[ledger-delivery] duplicate perception suppressed: outbox_id=%s",
                outbox_id,
            )
            return False
        return True

    def add_clips(self, message_id: str, spans) -> int:
        """層1マーカー (``==語句==``) から抽出された観測点を点クリップとして保存する。

        ``spans`` は ``saiverse.marker_parser.MarkSpan`` 互換 (``quote`` /
        ``purpose_ref`` 属性を持つ) の列。保存経路 (_store_memory) から
        メッセージ insert の直後に呼ばれる想定で、失敗しても例外を上げず
        WARNING に落とす (クリップはメッセージ本体より優先度が低い)。

        Returns: 保存できたクリップの枚数。
        """
        if not self._ready or not message_id or not spans:
            return 0
        from sai_memory.clips import add_clip
        saved = 0
        with self._db_lock:
            for span in spans:
                try:
                    add_clip(
                        self.conn,
                        message_id=message_id,
                        quote=span.quote,
                        purpose_ref=span.purpose_ref,
                    )
                    saved += 1
                except Exception:
                    LOGGER.warning(
                        "Failed to add clip for message=%s quote=%r",
                        message_id, getattr(span, "quote", None), exc_info=True,
                    )
        return saved

    def add_purpose_tag(self, target_ref: str, purpose_ref: str, layer: int) -> bool:
        """目的タグ 1 件を purpose_tags テーブルへ永続化する (upsert)。

        層2 棚入れ (judgment_finalize) 等の書き込み口。同一 (target, purpose)
        ペアは sai_memory/purpose_tags.py の add_tag が再訪として同じ行に
        濃さを積む。失敗しても例外を上げず WARNING に落とす (タグは
        メッセージ本体より優先度が低い — add_clips と同じ姿勢)。

        Returns: 保存 (upsert) できたら True。
        """
        if not self._ready or not target_ref or not purpose_ref:
            return False
        from sai_memory.purpose_tags import add_tag
        with self._db_lock:
            try:
                add_tag(
                    self.conn,
                    target_ref=str(target_ref),
                    purpose_ref=str(purpose_ref),
                    layer=int(layer),
                )
                return True
            except Exception:
                LOGGER.warning(
                    "Failed to add purpose tag target=%r purpose=%r layer=%r",
                    target_ref, purpose_ref, layer, exc_info=True,
                )
                return False

    def recent_messages(self, building_id: str, max_chars: int) -> List[dict]:
        if not self._ready:
            return []
        thread_id = self._thread_id(building_id)
        try:
            with self._db_lock:
                rows = get_messages_last(self.conn, thread_id, self.settings.last_messages)  # type: ignore[arg-type]
                payloads = [self._payload_from_message_locked(msg, viewing_thread_id=thread_id) for msg in rows]
        except Exception as exc:
            LOGGER.warning("Failed to fetch recent messages for %s: %s", thread_id, exc)
            return []

        selected: List[dict] = []
        consumed = 0
        for payload in reversed(payloads):
            text = payload.get("content", "") or ""
            consumed += len(text)
            if consumed > max_chars:
                break
            selected.insert(0, payload)
        return selected

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_startup_backup(self) -> None:
        db_path = Path(self.settings.db_path)
        rdiff_path = os.getenv("SAIMEMORY_RDIFF_PATH")
        prefer_simple = os.getenv("SAIMEMORY_BACKUP_SIMPLE", "").strip().lower() in {"1", "true", "yes", "on"}
        try:
            result = run_backup_auto(
                persona_id=self.persona_id,
                db_path=db_path,
                rdiff_path=rdiff_path,
                prefer_simple=prefer_simple,
            )
            if result is None:
                LOGGER.info("Auto SAIMemory backup skipped for persona=%s (no changes)", self.persona_id)
            else:
                LOGGER.info("Auto SAIMemory backup completed for persona=%s: %s", self.persona_id, result)
        except BackupError as exc:
            LOGGER.warning("Auto SAIMemory backup failed for persona=%s: %s", self.persona_id, exc)
        except Exception:
            LOGGER.exception("Unexpected error during auto SAIMemory backup for %s", self.persona_id)

    def recent_persona_messages(
        self,
        max_chars: int,
        *,
        required_tags: Optional[List[str]] = None,
        required_line_roles: Optional[List[str]] = None,
        required_scopes: Optional[List[str]] = None,
        pulse_id: Optional[str] = None,
    ) -> List[dict]:
        if not self._ready:
            return []
        thread_id = self._thread_id(None)
        try:
            with self._db_lock:
                all_rows = _fetch_all_messages(self.conn, thread_id)
                payloads = self._expand_paired_action_payloads([self._payload_from_message_locked(msg, viewing_thread_id=thread_id) for msg in all_rows])
        except Exception as exc:
            LOGGER.warning("Failed to fetch persona messages for %s: %s", thread_id, exc)
            return []

        selected: List[dict] = []
        consumed = 0
        for payload in reversed(payloads):
            if not _payload_passes_context_filter(
                payload,
                required_tags=required_tags,
                required_line_roles=required_line_roles,
                required_scopes=required_scopes,
                pulse_id=pulse_id,
            ):
                continue
            text = payload.get("content", "") or ""
            consumed += len(text)
            if consumed > max_chars:
                break
            selected.insert(0, payload)
        return selected

    def recent_persona_messages_by_count(
        self,
        max_messages: int,
        *,
        required_tags: Optional[List[str]] = None,
        required_line_roles: Optional[List[str]] = None,
        required_scopes: Optional[List[str]] = None,
        pulse_id: Optional[str] = None,
        strict_tags: bool = False,
    ) -> List[dict]:
        """Get recent persona messages limited by message count instead of characters.

        strict_tags=True は required_tags の legacy 救済 (タグ無し行の素通し) を
        無効化する。タグで「その種類の記録だけ」を数えたい呼び出し (作業
        ダイジェスト収集など) は必ずこれを立てる — 素通しのままだと、
        paired_action 展開行 (タグ無し) が取得枠 (max_messages) を占拠し、
        本物のタグ付き行が取得段階で押し出される (2026-07-29 Codex 指摘)。
        """
        if not self._ready:
            return []
        thread_id = self._thread_id(None)
        try:
            with self._db_lock:
                all_rows = _fetch_all_messages(self.conn, thread_id)
                payloads = self._expand_paired_action_payloads([self._payload_from_message_locked(msg, viewing_thread_id=thread_id) for msg in all_rows])
        except Exception as exc:
            LOGGER.warning("Failed to fetch persona messages for %s: %s", thread_id, exc)
            return []

        selected: List[dict] = []
        for payload in reversed(payloads):
            if not _payload_passes_context_filter(
                payload,
                required_tags=required_tags,
                required_line_roles=required_line_roles,
                required_scopes=required_scopes,
                pulse_id=pulse_id,
                strict_tags=strict_tags,
            ):
                continue
            selected.insert(0, payload)
            if len(selected) >= max_messages:
                break
        return selected

    def persona_messages_from_anchor(
        self,
        anchor_message_id: str,
        *,
        required_tags: Optional[List[str]] = None,
        required_line_roles: Optional[List[str]] = None,
        required_scopes: Optional[List[str]] = None,
        pulse_id: Optional[str] = None,
    ) -> List[dict]:
        """Get persona messages from anchor message onwards.

        Uses an efficient SQL query to fetch only messages at or after
        the anchor's timestamp, keeping the context window prefix stable
        for LLM cache optimization.
        """
        if not self._ready:
            return []
        thread_id = self._thread_id(None)
        try:
            with self._db_lock:
                from sai_memory.memory.storage import get_messages_from_id
                rows = get_messages_from_id(self.conn, thread_id, anchor_message_id)
                payloads = [self._payload_from_message_locked(msg, viewing_thread_id=thread_id) for msg in rows]
        except Exception as exc:
            LOGGER.warning("Failed to fetch persona messages from anchor %s: %s", anchor_message_id, exc)
            return []

        selected: List[dict] = []
        for payload in payloads:  # already in chronological order
            if not _payload_passes_context_filter(
                payload,
                required_tags=required_tags,
                required_line_roles=required_line_roles,
                required_scopes=required_scopes,
                pulse_id=pulse_id,
            ):
                continue
            selected.append(payload)

        return selected

    def persona_messages_before_anchor(
        self,
        anchor_message_id: str,
        *,
        max_chars: int,
        required_tags: Optional[List[str]] = None,
        required_line_roles: Optional[List[str]] = None,
        required_scopes: Optional[List[str]] = None,
    ) -> List[dict]:
        """anchor より正典順で前のメッセージを遡って返す (読み戻し §15 の材料読み)。

        :meth:`persona_messages_from_anchor` の対。フィルタ**通過分**の content
        合計が ``max_chars`` に達するまで新しい側から遡り、時系列昇順で返す。
        フィルタは提示ウィンドウの読み (from_anchor) と同じ規則で適用する —
        でないと読み戻しの文字勘定が提示の勘定とズレる。
        """
        if not self._ready or max_chars <= 0:
            return []
        thread_id = self._thread_id(None)
        selected: List[dict] = []  # 新しい順に積む
        acc = 0
        boundary = anchor_message_id
        try:
            with self._db_lock:
                from sai_memory.memory.storage import get_messages_before_id
                while acc < max_chars:
                    rows = get_messages_before_id(
                        self.conn, thread_id, boundary, limit=500,
                    )
                    if not rows:
                        break
                    boundary = rows[0].id  # 最古行が次ページの排他境界
                    for msg in reversed(rows):  # ページ内を新しい順に
                        payload = self._payload_from_message_locked(
                            msg, viewing_thread_id=thread_id,
                        )
                        if not _payload_passes_context_filter(
                            payload,
                            required_tags=required_tags,
                            required_line_roles=required_line_roles,
                            required_scopes=required_scopes,
                            pulse_id=None,
                        ):
                            continue
                        selected.append(payload)
                        acc += len(str(payload.get("content") or ""))
                        if acc >= max_chars:
                            break
        except Exception as exc:
            LOGGER.warning(
                "Failed to fetch persona messages before anchor %s: %s",
                anchor_message_id, exc,
            )
            return []
        selected.reverse()
        return selected

    def recent_persona_messages_balanced(
        self,
        max_chars: int,
        participant_ids: List[str],
        *,
        required_tags: Optional[List[str]] = None,
        required_line_roles: Optional[List[str]] = None,
        required_scopes: Optional[List[str]] = None,
        pulse_id: Optional[str] = None,
    ) -> List[dict]:
        """Get recent messages balanced across conversation partners.

        Allocates max_chars equally among participants, retrieving recent
        messages from each partner's conversations.

        Args:
            max_chars: Total character budget
            participant_ids: List of partner IDs to balance (e.g., ["user", "persona_b"])
            required_tags: Optional legacy tag filter (kept for search/recall compatibility)
            required_line_roles: Line-role filter (e.g. ['main_line']) — preferred for context construction
            required_scopes: Scope filter (e.g. ['committed']) — preferred for context construction
            pulse_id: Always include messages with this pulse ID
        """
        if not self._ready or not participant_ids:
            return []

        thread_id = self._thread_id(None)
        try:
            with self._db_lock:
                all_rows = _fetch_all_messages(self.conn, thread_id)
                payloads = self._expand_paired_action_payloads([self._payload_from_message_locked(msg, viewing_thread_id=thread_id) for msg in all_rows])
        except Exception as exc:
            LOGGER.warning("Failed to fetch persona messages for balancing: %s", exc)
            return []

        # Group messages by participant
        # Key: participant_id, Value: list of (index, payload) tuples
        participant_groups: Dict[str, List[tuple]] = {pid: [] for pid in participant_ids}
        other_messages: List[tuple] = []  # Messages without "with" or with unknown participants

        for idx, payload in enumerate(payloads):
            if not _payload_passes_context_filter(
                payload,
                required_tags=required_tags,
                required_line_roles=required_line_roles,
                required_scopes=required_scopes,
                pulse_id=pulse_id,
            ):
                continue

            metadata = payload.get("metadata") or {}
            with_list = metadata.get("with", []) if isinstance(metadata, dict) else []

            # Assign to participant groups
            if with_list:
                for partner in with_list:
                    if partner in participant_groups:
                        participant_groups[partner].append((idx, payload))
            else:
                # Messages without "with" go to other
                other_messages.append((idx, payload))

        # Calculate per-participant budget
        num_participants = len(participant_ids)
        per_participant_chars = max_chars // num_participants if num_participants > 0 else max_chars

        # Select messages from each participant (most recent first)
        selected_with_idx: List[tuple] = []

        for pid in participant_ids:
            group = participant_groups.get(pid, [])
            consumed = 0
            for idx, payload in reversed(group):
                text = payload.get("content", "") or ""
                if consumed + len(text) > per_participant_chars:
                    break
                consumed += len(text)
                selected_with_idx.append((idx, payload))

        # Add some "other" messages if there's remaining budget
        total_consumed = sum(len(p.get("content", "") or "") for _, p in selected_with_idx)
        remaining = max_chars - total_consumed
        if remaining > 0 and other_messages:
            for idx, payload in reversed(other_messages):
                text = payload.get("content", "") or ""
                if len(text) > remaining:
                    break
                remaining -= len(text)
                selected_with_idx.append((idx, payload))

        # Sort by original index to maintain chronological order
        selected_with_idx.sort(key=lambda x: x[0])
        return [payload for _, payload in selected_with_idx]

    def list_thread_summaries(self, max_preview_chars: int = 120) -> List[Dict[str, Any]]:
        if not self._ready:
            return []
        try:
            with self._db_lock:
                cur = self.conn.execute("SELECT id FROM threads ORDER BY id ASC")
                rows = cur.fetchall()
                active_suffix = self._active_persona_suffix()
                summaries: List[Dict[str, Any]] = []
                for (thread_id,) in rows:
                    first_messages = get_messages_paginated(self.conn, thread_id, page=0, page_size=1)
                    preview = ""
                    first_id: Optional[str] = None
                    if first_messages:
                        first_msg = first_messages[0]
                        first_id = first_msg.id
                        preview = compose_message_content(self.conn, first_msg)
                        if max_preview_chars > 0 and len(preview) > max_preview_chars:
                            preview = preview[: max_preview_chars - 1] + "…"
                    suffix = thread_id.split(":", 1)[1] if ":" in thread_id else thread_id

                    # Get Stelis thread info
                    stelis_info = get_stelis_thread(self.conn, thread_id)
                    is_stelis = stelis_info is not None

                    summary: Dict[str, Any] = {
                        "thread_id": thread_id,
                        "suffix": suffix,
                        "preview": preview.strip(),
                        "first_message_id": first_id,
                        "active": bool(active_suffix and suffix == active_suffix),
                        "is_stelis": is_stelis,
                    }

                    if is_stelis and stelis_info:
                        summary["stelis_parent_id"] = stelis_info.parent_thread_id
                        summary["stelis_depth"] = stelis_info.depth
                        summary["stelis_status"] = stelis_info.status
                        summary["stelis_label"] = stelis_info.label

                    summaries.append(summary)
                return summaries
        except Exception as exc:
            LOGGER.warning("Failed to list threads for persona %s: %s", self.persona_id, exc)
            return []

    def get_thread_messages(self, thread_id: str, page: int = 0, page_size: int = 100) -> List[dict]:
        if not self._ready:
            return []
        try:
            with self._db_lock:
                msgs = get_messages_paginated(self.conn, thread_id, page=page, page_size=page_size)  # type: ignore[arg-type]
                return [self._payload_from_message_locked(msg, viewing_thread_id=thread_id) for msg in msgs]
        except Exception as exc:
            LOGGER.warning("Failed to get messages for thread %s: %s", thread_id, exc)
            return []

    def count_thread_messages(self, thread_id: str) -> int:
        if not self._ready:
            return 0
        try:
            with self._db_lock:
                cur = self.conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE thread_id=? "
                    "AND (scope IS NULL OR scope != 'discardable')",
                    (thread_id,),
                )  # type: ignore[attr-defined]
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception as exc:
            LOGGER.warning("Failed to count messages for thread %s: %s", thread_id, exc)
            return 0

    def update_message_content(self, message_id: str, new_content: str) -> bool:
        """Legacy wrapper for update_message with only content."""
        return self.update_message(message_id, new_content=new_content)

    def update_message(
        self, 
        message_id: str, 
        new_content: Optional[str] = None, 
        new_created_at: Optional[int] = None
    ) -> bool:
        """Update message content and/or timestamp.
        
        Args:
            message_id: ID of the message to update
            new_content: New content (optional, if None content is unchanged)
            new_created_at: New timestamp as Unix epoch (optional)
            
        Returns:
            True if successful, False otherwise
        """
        if not self._ready:
            return False
        if new_content is None and new_created_at is None:
            return True  # Nothing to update
        try:
            with self._db_lock:
                # Check message exists
                cur = self.conn.execute("SELECT 1 FROM messages WHERE id=?", (message_id,))  # type: ignore[attr-defined]
                if cur.fetchone() is None:
                    return False

                # Update fields as needed
                if new_content is not None and new_created_at is not None:
                    self.conn.execute(  # type: ignore[attr-defined]
                        "UPDATE messages SET content=?, created_at=? WHERE id=?",
                        (new_content, new_created_at, message_id),
                    )
                elif new_content is not None:
                    self.conn.execute(  # type: ignore[attr-defined]
                        "UPDATE messages SET content=? WHERE id=?",
                        (new_content, message_id),
                    )
                elif new_created_at is not None:
                    self.conn.execute(  # type: ignore[attr-defined]
                        "UPDATE messages SET created_at=? WHERE id=?",
                        (new_created_at, message_id),
                    )
                
                # Update embeddings only if content changed
                if new_content is not None:
                    self.conn.execute("DELETE FROM message_embeddings WHERE message_id=?", (message_id,))  # type: ignore[attr-defined]
                    content_strip = new_content.strip()
                    if content_strip and self.embedder is not None:
                        chunks = chunk_text(
                            content_strip,
                            min_chars=self.settings.chunk_min_chars,
                            max_chars=self.settings.chunk_max_chars,
                        )
                        payload = [chunk.strip() for chunk in chunks if chunk and chunk.strip()]
                        if payload:
                            vectors = self.embedder.embed(payload, is_query=False)
                            replace_message_embeddings(self.conn, message_id, vectors)   # type: ignore[attr-defined]
                
                self.conn.commit()  # type: ignore[attr-defined]
                return True
        except Exception as exc:
            LOGGER.warning("Failed to update message %s: %s", message_id, exc)
            return False

    def delete_message(self, message_id: str) -> bool:
        if not self._ready:
            return False
        try:
            with self._db_lock:
                self.conn.execute("DELETE FROM message_embeddings WHERE message_id=?", (message_id,))  # type: ignore[attr-defined]
                self.conn.execute("DELETE FROM messages WHERE id=?", (message_id,))  # type: ignore[attr-defined]
                self.conn.commit()  # type: ignore[attr-defined]
                return True
        except Exception as exc:
            LOGGER.warning("Failed to delete message %s: %s", message_id, exc)
            return False

    def delete_thread(self, thread_id: str) -> bool:
        if not self._ready:
            return False
        from sai_memory.memory.storage import delete_thread
        try:
            with self._db_lock:
                return delete_thread(self.conn, thread_id)
        except Exception as exc:
            LOGGER.warning("Failed to delete thread %s: %s", thread_id, exc)
            return False

    def recall_snippet(
        self,
        building_id: Optional[str] = None,
        query_text: str = "",
        *,
        max_chars: int = 800,
        exclude_created_at: Optional[int | List[int]] = None,
        topk: Optional[int] = None,
        range_before: Optional[int] = None,
        range_after: Optional[int] = None,
    ) -> str:
        if not self.can_embed():
            return ""
        if not query_text or not query_text.strip():
            return ""

        thread_id = self._thread_id(building_id)
        # Disable both thread_id and resource_id filters to search across all threads
        search_thread_id = None
        search_resource_id = None

        guard_ids: set[str] = set()
        try:
            with self._db_lock:
                recall_topk = self.settings.topk if topk is None else max(1, int(topk))
                before = self.settings.range_before if range_before is None else max(0, int(range_before))
                after = self.settings.range_after if range_after is None else max(0, int(range_after))
                guard_count = max(0, self.settings.last_messages)
                if guard_count > 0:
                    recent_msgs = get_messages_last(self.conn, thread_id, guard_count)
                    guard_ids = {m.id for m in recent_msgs}
                effective_topk = recall_topk + len(guard_ids)
                groups_raw = semantic_recall_groups(
                    self.conn,
                    self.embedder,
                    query_text,
                    thread_id=search_thread_id,
                    resource_id=search_resource_id,
                    topk=effective_topk,
                    range_before=before,
                    range_after=after,
                    scope=self.settings.scope,
                    exclude_message_ids=guard_ids,
                    required_tags=["conversation"],
                    exclude_tags=["handy_tool", "spell"],
                )
                groups = []
                for seed, bundle, score in groups_raw:
                    formatted = [
                        (msg, compose_message_content(self.conn, msg))
                        for msg in bundle
                    ]
                    groups.append((seed, formatted, score))
        except Exception as exc:
            LOGGER.warning("SAIMemory recall failed for %s: %s", thread_id, exc)
            return ""

        lines: List[str] = ["[Memory Recall]", "```"]
        exclude_created_values: set[int] = set()
        if exclude_created_at is not None:
            if isinstance(exclude_created_at, (list, tuple, set)):
                candidates = exclude_created_at
            else:
                candidates = [exclude_created_at]
            for value in candidates:
                if value is None:
                    continue
                try:
                    exclude_created_values.add(int(value))
                except (TypeError, ValueError):
                    continue
        seen: set[str] = set()
        for seed, bundle, score in groups:
            for msg, rendered in bundle:
                if msg.id in seen or msg.id in guard_ids:
                    continue
                seen.add(msg.id)
                if exclude_created_values and msg.created_at in exclude_created_values:
                    continue
                if msg.role == "system":
                    continue
                content = (rendered or "").strip()
                if not content:
                    continue
                dt = datetime.fromtimestamp(msg.created_at)
                ts = dt.strftime("%Y-%m-%d %H:%M")
                role = msg.role
                entry = f"- {role} @ {ts}: {content}"
                if score is not None and msg.id == seed.id:
                    entry = f"- {role} @ {ts} (score={score:.3f}): {content}"
                candidate = lines + [entry, "```"]
                combined = "\n".join(candidate)
                if len(combined) > max_chars:
                    lines.append("```")
                    return "\n".join(lines)
                lines.append(entry)

        if len(lines) <= 2:
            return ""
        lines.append("```")
        return "\n".join(lines)

    def recall_hybrid(
        self,
        query_text: str = "",
        keywords: Optional[List[str]] = None,
        *,
        max_chars: int = 800,
        topk: Optional[int] = None,
        range_before: Optional[int] = None,
        range_after: Optional[int] = None,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> str:
        """Hybrid recall: keyword matching + semantic search, combined with RRF."""
        if not self.can_embed():
            return ""
        if not query_text and not keywords:
            return ""

        from collections import defaultdict

        recall_topk = self.settings.topk if topk is None else max(1, int(topk))
        before = self.settings.range_before if range_before is None else max(0, int(range_before))
        after = self.settings.range_after if range_after is None else max(0, int(range_after))
        rrf_k = 60

        # Guard: exclude recent messages
        thread_id = self._thread_id(None)
        guard_ids: set[str] = set()
        guard_count = max(0, self.settings.last_messages)
        if guard_count > 0:
            with self._db_lock:
                recent_msgs = get_messages_last(self.conn, thread_id, guard_count)
                guard_ids = {m.id for m in recent_msgs}

        message_scores: dict[str, float] = defaultdict(float)
        message_data: dict[str, Any] = {}  # msg_id -> Message object

        # 1. Keyword search
        if keywords:
            with self._db_lock:
                all_msgs = get_all_messages_for_search(
                    self.conn,
                    required_tags=["conversation"],
                )
            keyword_scored = []
            for msg in all_msgs:
                if msg.id in guard_ids:
                    continue
                if start_ts and msg.created_at < start_ts:
                    continue
                if end_ts and msg.created_at > end_ts:
                    continue
                content_lower = msg.content.lower() if msg.content else ""
                match_count = sum(1 for kw in keywords if kw.lower() in content_lower)
                if match_count > 0:
                    keyword_scored.append((msg, match_count))

            keyword_scored.sort(key=lambda x: x[1], reverse=True)
            for rank, (msg, _count) in enumerate(keyword_scored[:recall_topk * 2], start=1):
                if msg.id not in message_data:
                    message_data[msg.id] = msg
                message_scores[msg.id] += 1.0 / (rrf_k + rank)

        # 2. Semantic search
        if query_text and query_text.strip():
            search_topk = recall_topk * 2 + len(guard_ids)
            with self._db_lock:
                groups_raw = semantic_recall_groups(
                    self.conn,
                    self.embedder,
                    query_text,
                    thread_id=None,
                    resource_id=None,
                    topk=search_topk,
                    range_before=0,
                    range_after=0,
                    scope=self.settings.scope,
                    exclude_message_ids=guard_ids,
                    required_tags=["conversation"],
                    exclude_tags=["handy_tool", "spell"],
                )
            rank_counter = 0
            for seed, _bundle, _score in groups_raw:
                if start_ts and seed.created_at < start_ts:
                    continue
                if end_ts and seed.created_at > end_ts:
                    continue
                rank_counter += 1
                if seed.id not in message_data:
                    message_data[seed.id] = seed
                message_scores[seed.id] += 1.0 / (rrf_k + rank_counter)

        if not message_scores:
            return ""

        # Sort by RRF score and pick top-k
        sorted_ids = sorted(message_scores.keys(), key=lambda x: message_scores[x], reverse=True)
        top_ids = sorted_ids[:recall_topk]

        # Expand context around each seed
        groups = []
        try:
            with self._db_lock:
                for msg_id in top_ids:
                    msg = message_data[msg_id]
                    score = message_scores[msg_id]
                    if before > 0 or after > 0:
                        around = get_messages_around(self.conn, msg.thread_id, msg.id, before, after)
                        bundle = [*around[:before], msg, *around[before:]]
                        bundle.sort(key=lambda m: m.created_at)
                    else:
                        bundle = [msg]
                    formatted = [
                        (m, compose_message_content(self.conn, m))
                        for m in bundle
                    ]
                    groups.append((msg, formatted, score))
        except Exception as exc:
            LOGGER.warning("SAIMemory hybrid recall context expansion failed: %s", exc)
            return ""

        # Format output (same format as recall_snippet)
        lines: List[str] = ["[Memory Recall]", "```"]
        seen: set[str] = set()
        for seed, bundle, score in groups:
            for msg, rendered in bundle:
                if msg.id in seen or msg.id in guard_ids:
                    continue
                seen.add(msg.id)
                if msg.role == "system":
                    continue
                content = (rendered or "").strip()
                if not content:
                    continue
                dt = datetime.fromtimestamp(msg.created_at)
                ts = dt.strftime("%Y-%m-%d %H:%M")
                role = msg.role
                entry = f"- {role} @ {ts}: {content}"
                if msg.id == seed.id:
                    entry = f"- {role} @ {ts} (score={score:.4f}): {content}"
                candidate = lines + [entry, "```"]
                combined = "\n".join(candidate)
                if len(combined) > max_chars:
                    lines.append("```")
                    return "\n".join(lines)
                lines.append(entry)

        if len(lines) <= 2:
            return ""
        lines.append("```")
        return "\n".join(lines)

    def update_overview(self, building_id: str, provider) -> Optional[str]:
        if not self._ready or provider is None:
            return None
        from sai_memory.summary import update_overview_with_llm

        thread_id = self._thread_id(building_id)
        try:
            with self._db_lock:
                return update_overview_with_llm(
                    self.conn,
                    provider,
                    thread_id=thread_id,
                    max_chars=self.settings.summary_max_chars,
                )
        except Exception as exc:
            LOGGER.warning("Failed to update overview for %s: %s", thread_id, exc)
            return None

    def backfill_missing_message_embeddings(
        self,
        *,
        progress_callback: Optional[Any] = None,
    ) -> Dict[str, int]:
        """埋め込みが欠落しているメッセージ (message_embeddings に無い) を一括生成する。

        通常は書き込み時 (``_append_message``) にローカル embedder でその場埋め込まれる
        (LLM 不使用)。ここが必要になるのは、書き込み時に embedder が未初期化だった場合
        (embedding モデルのロード失敗・初回起動時のダウンロード未完了等) や、大量インポート
        直後の検証・保険としてまとめて埋めたい場合。

        Returns:
            {"total_missing": N, "embedded": M, "failed": K} の集計。
            can_embed() が False の場合は total_missing のみ数えて embedded=0 で返す
            (embedder が無いので埋められない)。
        """
        result = {"total_missing": 0, "embedded": 0, "failed": 0}
        if not self._ready:
            return result

        from sai_memory.memory.storage import get_messages_without_embeddings

        try:
            with self._db_lock:
                missing = get_messages_without_embeddings(self.conn)
        except Exception as exc:
            LOGGER.warning("Failed to list messages without embeddings: %s", exc)
            return result

        result["total_missing"] = len(missing)
        if not missing or self.embedder is None:
            return result

        for idx, msg in enumerate(missing):
            try:
                content = (msg.content or "").strip()
                if not content:
                    continue
                chunks = chunk_text(
                    content,
                    min_chars=self.settings.chunk_min_chars,
                    max_chars=self.settings.chunk_max_chars,
                )
                payload = [c.strip() for c in chunks if c and c.strip()]
                if not payload:
                    continue
                with self._db_lock:
                    vectors = self.embedder.embed(payload, is_query=False)
                    replace_message_embeddings(self.conn, msg.id, vectors)
                result["embedded"] += 1
            except Exception as exc:
                result["failed"] += 1
                LOGGER.warning("Failed to backfill embedding for message %s: %s", msg.id, exc)
            if progress_callback:
                try:
                    progress_callback(idx + 1, len(missing))
                except Exception:
                    LOGGER.debug("progress_callback raised during embedding backfill", exc_info=True)

        return result

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                LOGGER.exception("Failed to close SAIMemory connection")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def is_ready(self) -> bool:
        """Check if the adapter can store and retrieve messages (requires DB connection)."""
        return self.conn is not None

    def can_embed(self) -> bool:
        """Check if the adapter can perform semantic search (requires DB + embedding model)."""
        return self.conn is not None and self.embedder is not None

    @property
    def _ready(self) -> bool:
        return self.is_ready()

    def _thread_id(
        self,
        building_id: Optional[str] = None,
        *,
        thread_suffix: Optional[str] = None,
    ) -> str:
        if thread_suffix:
            suffix = thread_suffix
        else:
            if building_id is not None:
                suffix = building_id
            else:
                suffix = self._active_persona_suffix() or self._PERSONA_THREAD_SUFFIX
        return f"{self.persona_id}:{suffix}"

    def _payload_from_message_locked(self, msg, viewing_thread_id: Optional[str] = None) -> dict:
        if self.conn is None:
            content = msg.content or ""
        else:
            content = compose_message_content(
                self.conn, msg, viewing_thread_id=viewing_thread_id
            ) or ""
        original_role = msg.role
        role = "assistant" if original_role == "model" else original_role
        if isinstance(role, str) and role.lower() == "system":
            role = "user"
            if content:
                content = f"<system>\n{content}\n</system>"
            else:
                content = "<system></system>"
        payload: Dict[str, Any] = {
            "id": msg.id,
            "thread_id": msg.thread_id,
            "role": role,
            "content": content,
            "created_at": msg.created_at,
        }
        if msg.metadata:
            payload["metadata"] = msg.metadata
        # Line metadata (Phase 1, Intent A v0.14): expose for line-based context
        # construction. None when SELECT didn't include the columns or when
        # legacy rows predating Phase 1 lack the values.
        if msg.line_role is not None:
            payload["line_role"] = msg.line_role
        if msg.line_id is not None:
            payload["line_id"] = msg.line_id
        if msg.scope is not None:
            payload["scope"] = msg.scope
        if msg.pulse_id is not None:
            payload["pulse_id"] = msg.pulse_id
        # v0.32 (2026-05-09): Track Chronicle / ユーザー会話 Track 親保持機構で利用。
        if getattr(msg, "origin_track_id", None) is not None:
            payload["origin_track_id"] = msg.origin_track_id
        # 2026-05-20: Gemini 3.x の thoughtSignature をターン跨ぎで送り返すため、
        # payload に乗せて LLM client (gemini.py) で復元する。
        if getattr(msg, "thought_signature", None) is not None:
            payload["thought_signature"] = msg.thought_signature
        # action (paired_action_text) を context 復元用に乗せる。scope=committed
        # (および legacy の None) のみ。volatile は揮発させる (乗せない)。
        # 実際に「action → 応答」順へ展開するのは context 構築側
        # (expand_paired_action_payloads)。docs/issues/spell_judgment_recorded_after_subline.md
        if msg.scope != "volatile" and getattr(msg, "paired_action_text", None):
            payload["paired_action_text"] = msg.paired_action_text
        return payload

    def _expand_paired_action_payloads(self, payloads: List[dict]) -> List[dict]:
        """committed 応答の paired_action_text を「action → 応答」順に展開する。

        payload に paired_action_text があれば、その直前に action メッセージ
        (role=user、内容は記録時に付与済みの ``<system>...</system>``) を挿入し、
        応答側 payload からは paired_action_text キーを除く。これで「指示 → 応答」
        の因果が context に復元される。volatile の action は
        _payload_from_message_locked で乗らない (= 揮発) ので対象外。
        """
        expanded: List[dict] = []
        for p in payloads:
            action_text = p.get("paired_action_text")
            if action_text:
                expanded.append({
                    "role": "user",
                    "content": action_text,
                    "created_at": p.get("created_at"),
                })
                p = {k: v for k, v in p.items() if k != "paired_action_text"}
            expanded.append(p)
        return expanded

    def _active_persona_suffix(self) -> Optional[str]:
        path = self.persona_dir / self._ACTIVE_STATE_FILENAME
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            LOGGER.debug("Failed to read active state for %s: %s", self.persona_id, exc)
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            LOGGER.warning("Invalid JSON in %s: %s", path, exc)
            return None

        candidate: Optional[str] = None
        if isinstance(data, dict):
            for key in ("active_thread_id", "thread_id", "active_thread"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    candidate = value.strip()
                    break
        elif isinstance(data, str) and data.strip():
            candidate = data.strip()

        if candidate:
            return candidate
        return None

    def set_active_thread(self, thread_id: str) -> bool:
        """Set the active thread for this persona.
        
        Args:
            thread_id: Full thread ID (e.g., "persona_id:suffix")
            
        Returns:
            True if successful, False otherwise
        """
        if not thread_id:
            return False
            
        # Extract suffix from thread_id
        suffix = thread_id.split(":", 1)[1] if ":" in thread_id else thread_id
        
        path = self.persona_dir / self._ACTIVE_STATE_FILENAME
        try:
            state_data = {
                "active_thread_id": suffix,
                "updated_at": datetime.now().isoformat()
            }
            path.write_text(json.dumps(state_data, ensure_ascii=False, indent=2), encoding="utf-8")
            LOGGER.info("Set active thread for %s to %s (full_id=%s)", self.persona_id, suffix, thread_id)
            return True
        except Exception as exc:
            LOGGER.warning("Failed to set active thread for %s: %s", self.persona_id, exc)
            return False

    def get_current_thread(self) -> Optional[str]:
        """Get the current active thread suffix.

        Returns:
            The thread suffix (not the full thread_id), or None if not set
        """
        suffix = self._active_persona_suffix()
        if suffix:
            return f"{self.persona_id}:{suffix}"
        return None

    # ------------------------------------------------------------------
    # Pulse-scoped thread marker (S4, beat_execution_context.md 不変条件6)
    # ------------------------------------------------------------------

    def _read_active_state(self) -> Dict[str, Any]:
        """Read ``active_state.json`` as a dict (missing / broken file → {})."""
        path = self.persona_dir / self._ACTIVE_STATE_FILENAME
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            LOGGER.warning("Invalid JSON in %s: %s", path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _write_active_state_dict(self, data: Dict[str, Any]) -> bool:
        path = self.persona_dir / self._ACTIVE_STATE_FILENAME
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except OSError as exc:
            LOGGER.warning("Failed to write active state for %s: %s", self.persona_id, exc)
            return False

    def set_pulse_scoped_parent(self, parent_thread_id: str) -> bool:
        """Record that the current active thread is a pulse-scoped switch.

        Stelis / subagent の thread push 中であることを ``active_state.json``
        の ``pulse_scoped_parent`` キー (最外周の親 thread の suffix) として
        書き残す。書き手は ``PulseContext.push_thread`` / ``pop_thread`` のみ。
        プロセス死で pop が走らなかった場合、次回のペルソナ登録
        (``recover_orphaned_thread=True``) がこのキーを見て親へ復元する。

        Note: ``set_active_thread`` はファイルを丸ごと書き直すため、恒久切替
        (thread_switch ツール / API) はこのマーカーを自然に消す — それが正しい
        意味論 (恒久切替後にクラッシュ復旧で古い親へ巻き戻さない)。
        """
        if not parent_thread_id:
            return False
        suffix = parent_thread_id.split(":", 1)[1] if ":" in parent_thread_id else parent_thread_id
        data = self._read_active_state()
        data[self._PULSE_SCOPED_PARENT_KEY] = suffix
        return self._write_active_state_dict(data)

    def clear_pulse_scoped_parent(self) -> None:
        """Remove the pulse-scoped marker (no-op when absent)."""
        data = self._read_active_state()
        if self._PULSE_SCOPED_PARENT_KEY not in data:
            return
        data.pop(self._PULSE_SCOPED_PARENT_KEY, None)
        self._write_active_state_dict(data)

    def _recover_orphaned_pulse_thread(self) -> None:
        """Restore the parent thread when a pulse-scoped switch was orphaned.

        呼び出しはペルソナ登録経路の初期化 (``recover_orphaned_thread=True``)
        のみ。その時点で当該ペルソナの Pulse は走っていないため、マーカーが
        残っている = 前プロセスが Stelis/subagent 区間内で死んだ、と確定できる。
        """
        data = self._read_active_state()
        parent = data.get(self._PULSE_SCOPED_PARENT_KEY)
        if not isinstance(parent, str) or not parent.strip():
            return
        parent_suffix = parent.strip()
        orphaned = data.get("active_thread_id")
        data["active_thread_id"] = parent_suffix
        data.pop(self._PULSE_SCOPED_PARENT_KEY, None)
        data["updated_at"] = datetime.now().isoformat()
        if not self._write_active_state_dict(data):
            LOGGER.warning(
                "[thread-recovery] persona=%s: failed to restore orphaned pulse-scoped "
                "thread (active=%s, parent=%s)",
                self.persona_id, orphaned, parent_suffix,
            )
            return
        LOGGER.warning(
            "[thread-recovery] persona=%s: pulse_scoped_parent が残っていたため、"
            "孤児化した Stelis/subagent thread (%s) から親 thread (%s) へ復元しました "
            "(前プロセスの復元漏れの自然回復)",
            self.persona_id, orphaned, parent_suffix,
        )

    # ------------------------------------------------------------------
    # Pulse Logs
    # ------------------------------------------------------------------

    def append_pulse_log(
        self,
        pulse_id: str,
        thread_id: Optional[str],
        role: str,
        content: Optional[str],
        *,
        node_id: Optional[str] = None,
        playbook_name: Optional[str] = None,
        important: bool = False,
        tool_calls: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        created_at: Optional[int] = None,
    ) -> Optional[str]:
        """Append a single pulse log entry.

        Args:
            pulse_id: The pulse ID this log belongs to.
            thread_id: The thread context at the time of logging.
            role: Message role (user, assistant, tool, system).
            content: Message content.

        Returns:
            The log entry ID, or None if adapter is not ready.
        """
        if not self._ready:
            return None
        try:
            with self._db_lock:
                return add_pulse_log(
                    self.conn, pulse_id, thread_id, role, content,
                    node_id=node_id, playbook_name=playbook_name,
                    important=important, tool_calls=tool_calls,
                    tool_call_id=tool_call_id, tool_name=tool_name,
                    created_at=created_at,
                )
        except Exception as exc:
            LOGGER.warning("Failed to append pulse_log for pulse=%s: %s", pulse_id, exc)
            return None

    def get_pulse_logs(self, pulse_id: str) -> List[Dict[str, Any]]:
        """Fetch all pulse logs for a given pulse_id.

        Returns:
            List of dicts with pulse log fields, ordered by created_at ASC.
        """
        if not self._ready:
            return []
        try:
            with self._db_lock:
                rows = get_pulse_logs_by_pulse(self.conn, pulse_id)
        except Exception as exc:
            LOGGER.warning("Failed to get pulse_logs for pulse=%s: %s", pulse_id, exc)
            return []
        result = []
        for row in rows:
            result.append({
                "id": row[0],
                "pulse_id": row[1],
                "thread_id": row[2],
                "role": row[3],
                "content": row[4],
                "node_id": row[5],
                "playbook_name": row[6],
                "important": bool(row[7]),
                "tool_calls": row[8],
                "tool_call_id": row[9],
                "tool_name": row[10],
                "created_at": row[11],
            })
        return result

    def list_pulse_summaries(
        self, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Fetch pulse_id summaries (id, entry count, timestamp, playbook).

        Returns:
            List of dicts with keys: pulse_id, entry_count, latest_created_at, playbook_name
        """
        if not self._ready:
            return []
        try:
            with self._db_lock:
                rows = list_pulse_ids(self.conn, limit=limit, offset=offset)
        except Exception as exc:
            LOGGER.warning("Failed to list pulse_ids: %s", exc)
            return []
        return [
            {
                "pulse_id": row[0],
                "entry_count": row[1],
                "latest_created_at": row[2],
                "playbook_name": row[3],
            }
            for row in rows
        ]

    def count_pulses(self) -> int:
        """Count distinct pulses."""
        if not self._ready:
            return 0
        try:
            with self._db_lock:
                return count_pulse_ids(self.conn)
        except Exception as exc:
            LOGGER.warning("Failed to count pulse_ids: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Memory Notes
    # ------------------------------------------------------------------

    def add_memory_notes(
        self,
        notes: List[str],
        *,
        source_pulse_id: Optional[str] = None,
        source_time: Optional[int] = None,
    ) -> List[MemoryNote]:
        """Add lightweight knowledge notes from a conversation.

        Args:
            notes: List of short text items (one knowledge point each).
            source_pulse_id: Pulse that these notes were extracted from.
            source_time: Representative timestamp of the source messages.

        Returns:
            List of created MemoryNote objects.
        """
        if not self._ready or not notes:
            return []
        try:
            with self._db_lock:
                return add_memory_notes(
                    self.conn,
                    thread_id=self._thread_id(None),
                    notes=notes,
                    source_pulse_id=source_pulse_id,
                    source_time=source_time,
                )
        except Exception as exc:
            LOGGER.warning("Failed to add memory notes: %s", exc)
            return []

    def get_unresolved_notes(self, *, limit: int = 100) -> List[MemoryNote]:
        """Get unresolved memory notes for the active thread."""
        if not self._ready:
            return []
        try:
            with self._db_lock:
                return get_unresolved_notes(self.conn, limit=limit)
        except Exception as exc:
            LOGGER.warning("Failed to get unresolved notes: %s", exc)
            return []

    def resolve_memory_notes(self, note_ids: List[str]) -> int:
        """Mark memory notes as resolved."""
        if not self._ready or not note_ids:
            return 0
        try:
            with self._db_lock:
                return resolve_memory_notes(self.conn, note_ids)
        except Exception as exc:
            LOGGER.warning("Failed to resolve memory notes: %s", exc)
            return 0

    def count_unresolved_notes(self) -> int:
        """Count unresolved memory notes."""
        if not self._ready:
            return 0
        try:
            with self._db_lock:
                return count_unresolved_notes(self.conn)
        except Exception as exc:
            LOGGER.warning("Failed to count unresolved notes: %s", exc)
            return 0

    def get_unplanned_notes(self, *, limit: int = 200) -> List[MemoryNote]:
        """Get unresolved notes without organization metadata."""
        if not self._ready:
            return []
        try:
            with self._db_lock:
                return get_unplanned_notes(self.conn, limit=limit)
        except Exception as exc:
            LOGGER.warning("Failed to get unplanned notes: %s", exc)
            return []

    def get_planned_group_labels(self) -> List[str]:
        """Get distinct group labels of planned notes."""
        if not self._ready:
            return []
        try:
            with self._db_lock:
                return get_planned_group_labels(self.conn)
        except Exception as exc:
            LOGGER.warning("Failed to get planned group labels: %s", exc)
            return []

    def get_planned_notes_by_group(self, group_label: str) -> List[MemoryNote]:
        """Get planned notes for a specific group."""
        if not self._ready:
            return []
        try:
            with self._db_lock:
                return get_planned_notes_by_group(self.conn, group_label)
        except Exception as exc:
            LOGGER.warning("Failed to get planned notes for group %s: %s", group_label, exc)
            return []

    def set_note_plan(
        self,
        note_ids: List[str],
        *,
        group_label: str,
        action: str,
        target_page_id: Optional[str] = None,
        suggested_title: Optional[str] = None,
        target_category: Optional[str] = None,
    ) -> int:
        """Set organization metadata on notes."""
        if not self._ready or not note_ids:
            return 0
        try:
            with self._db_lock:
                return set_note_plan(
                    self.conn, note_ids,
                    group_label=group_label,
                    action=action,
                    target_page_id=target_page_id,
                    suggested_title=suggested_title,
                    target_category=target_category,
                )
        except Exception as exc:
            LOGGER.warning("Failed to set note plan: %s", exc)
            return 0

    def count_unplanned_notes(self) -> int:
        """Count unresolved notes without organization metadata."""
        if not self._ready:
            return 0
        try:
            with self._db_lock:
                return count_unplanned_notes(self.conn)
        except Exception as exc:
            LOGGER.warning("Failed to count unplanned notes: %s", exc)
            return 0

    def count_planned_groups(self) -> int:
        """Count distinct groups of planned notes."""
        if not self._ready:
            return 0
        try:
            with self._db_lock:
                return count_planned_groups(self.conn)
        except Exception as exc:
            LOGGER.warning("Failed to count planned groups: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Stelis Thread Management
    # ------------------------------------------------------------------

    def start_stelis_thread(
        self,
        parent_thread_id: Optional[str] = None,
        window_ratio: float = 0.8,
        chronicle_prompt: Optional[str] = None,
        max_depth: int = 3,
        label: Optional[str] = None,
    ) -> Optional[StelisThread]:
        """Create and activate a new Stelis thread.

        Args:
            parent_thread_id: Parent thread ID (uses current active if None)
            window_ratio: Portion of parent's window to allocate (default 0.8)
            chronicle_prompt: Prompt for Chronicle generation on completion
            max_depth: Maximum nesting depth allowed
            label: Human-readable label for the Stelis thread

        Returns:
            Created StelisThread, or None if max depth exceeded or error
        """
        if not self._ready:
            return None

        # Resolve parent thread
        if parent_thread_id is None:
            suffix = self._active_persona_suffix()
            parent_thread_id = self._thread_id(None, thread_suffix=suffix)

        try:
            with self._db_lock:
                # Check depth limit
                current_depth = get_stelis_thread_depth(self.conn, parent_thread_id)
                # -1 means not a Stelis thread (root), so effective depth is 0
                effective_depth = max(0, current_depth + 1) if current_depth >= 0 else 0

                if effective_depth >= max_depth:
                    LOGGER.warning(
                        "Cannot create Stelis thread: depth %d >= max %d",
                        effective_depth, max_depth
                    )
                    return None

                # Generate new thread ID
                import uuid
                stelis_suffix = f"stelis_{uuid.uuid4().hex[:8]}"
                new_thread_id = self._thread_id(None, thread_suffix=stelis_suffix)

                # Create the Stelis thread record
                stelis = create_stelis_thread(
                    self.conn,
                    thread_id=new_thread_id,
                    parent_thread_id=parent_thread_id,
                    window_ratio=window_ratio,
                    chronicle_prompt=chronicle_prompt,
                    label=label,
                )

                # Also create the regular thread entry
                get_or_create_thread(self.conn, new_thread_id, self.settings.resource_id)

                LOGGER.info(
                    "Created Stelis thread %s (parent=%s, depth=%d, ratio=%.2f)",
                    new_thread_id, parent_thread_id, stelis.depth, window_ratio
                )

                return stelis

        except Exception as exc:
            LOGGER.warning("Failed to create Stelis thread: %s", exc)
            return None

    def end_stelis_thread(
        self,
        thread_id: Optional[str] = None,
        status: str = "completed",
        chronicle_summary: Optional[str] = None,
    ) -> bool:
        """End a Stelis thread and optionally store Chronicle summary.

        Args:
            thread_id: Thread ID to end (uses current active if None)
            status: Final status ('completed' or 'aborted')
            chronicle_summary: Summary text to store

        Returns:
            True if successful, False otherwise
        """
        if not self._ready:
            return False

        if thread_id is None:
            suffix = self._active_persona_suffix()
            thread_id = self._thread_id(None, thread_suffix=suffix)

        try:
            with self._db_lock:
                stelis = get_stelis_thread(self.conn, thread_id)
                if not stelis:
                    LOGGER.warning("Not a Stelis thread: %s", thread_id)
                    return False

                success = complete_stelis_thread(
                    self.conn,
                    thread_id,
                    status=status,
                    chronicle_summary=chronicle_summary,
                )

                if success:
                    LOGGER.info(
                        "Ended Stelis thread %s with status=%s",
                        thread_id, status
                    )

                return success

        except Exception as exc:
            LOGGER.warning("Failed to end Stelis thread %s: %s", thread_id, exc)
            return False

    def get_stelis_info(self, thread_id: Optional[str] = None) -> Optional[StelisThread]:
        """Get Stelis thread information.

        Args:
            thread_id: Thread ID to query (uses current active if None)

        Returns:
            StelisThread object or None if not a Stelis thread
        """
        if not self._ready:
            return None

        if thread_id is None:
            suffix = self._active_persona_suffix()
            thread_id = self._thread_id(None, thread_suffix=suffix)

        try:
            with self._db_lock:
                return get_stelis_thread(self.conn, thread_id)
        except Exception as exc:
            LOGGER.warning("Failed to get Stelis info for %s: %s", thread_id, exc)
            return None

    def can_start_stelis(self, max_depth: int = 3, parent_thread_id: Optional[str] = None) -> bool:
        """Check if a new Stelis thread can be started.

        Args:
            max_depth: Maximum allowed depth
            parent_thread_id: Parent thread to check (uses current active if None)

        Returns:
            True if a new Stelis thread can be created
        """
        if not self._ready:
            return False

        if parent_thread_id is None:
            suffix = self._active_persona_suffix()
            parent_thread_id = self._thread_id(None, thread_suffix=suffix)

        try:
            with self._db_lock:
                depth = get_stelis_thread_depth(self.conn, parent_thread_id)
                # -1 means not a Stelis thread, so next would be depth 0
                effective_next_depth = max(0, depth + 1) if depth >= 0 else 0
                return effective_next_depth < max_depth
        except Exception as exc:
            LOGGER.warning("Failed to check Stelis depth: %s", exc)
            return False

    def get_stelis_window_tokens(
        self,
        model_context_length: int,
        thread_id: Optional[str] = None,
    ) -> int:
        """Calculate available window tokens for a thread.

        Args:
            model_context_length: Full model context length
            thread_id: Thread to calculate for (uses current active if None)

        Returns:
            Available tokens for this thread
        """
        if not self._ready:
            return model_context_length

        if thread_id is None:
            suffix = self._active_persona_suffix()
            thread_id = self._thread_id(None, thread_suffix=suffix)

        try:
            with self._db_lock:
                return calculate_stelis_window_tokens(
                    self.conn, thread_id, model_context_length
                )
        except Exception as exc:
            LOGGER.warning("Failed to calculate Stelis window: %s", exc)
            return model_context_length

    def get_stelis_parent_thread(self, thread_id: Optional[str] = None) -> Optional[str]:
        """Get the parent thread ID of a Stelis thread.

        Args:
            thread_id: Thread to query (uses current active if None)

        Returns:
            Parent thread ID or None if not a Stelis thread
        """
        stelis = self.get_stelis_info(thread_id)
        if stelis:
            return stelis.parent_thread_id
        return None

    def list_active_stelis_threads(self, parent_thread_id: Optional[str] = None) -> List[StelisThread]:
        """List all active Stelis threads.

        Args:
            parent_thread_id: Filter by parent (None for all active threads)

        Returns:
            List of active StelisThread objects
        """
        if not self._ready:
            return []

        try:
            with self._db_lock:
                return get_active_stelis_threads(self.conn, parent_thread_id)
        except Exception as exc:
            LOGGER.warning("Failed to list active Stelis threads: %s", exc)
            return []

    def has_assistant_message_since(self, since_epoch: int) -> Optional[bool]:
        """``since_epoch`` 以降にこのペルソナの assistant メッセージがあるか。

        ユーザー発話の仲裁の回収経路 (``saiverse/autonomy_wiring.py``) が「いまの
        会話区間で既に応答が出ているか」を判定するのに使う。区間の切り口は会話の
        出来事の ``started_at`` — adapter はペルソナ単位なので、追加の絞り込みは
        要らない (旧実装は ``origin_track_id`` で絞っていたが、その刻印は Track
        撤廃で書き手ごと退役した)。

        Returns:
            True / False。判定不能 (adapter 未 ready / クエリ失敗) は None —
            呼び出し側がフォールバック (応答済みに倒す) を決める。
        """
        if not self._ready:
            return None
        try:
            with self._db_lock:
                row = self.conn.execute(
                    "SELECT 1 FROM messages "
                    "WHERE role = 'assistant' AND created_at >= ? LIMIT 1",
                    (int(since_epoch),),
                ).fetchone()
        except Exception as exc:
            LOGGER.warning(
                "Failed to query assistant messages since %s: %s", since_epoch, exc,
            )
            return None
        return row is not None

    def get_messages_by_origin_episode(self, episode_ref: str) -> List[Dict[str, Any]]:
        """出来事 (``episode:N``) の原本行を時系列で返す (W1 Chunk C / D10)。

        層0タグの専用列 ``messages.origin_episode`` で直接引く
        (:meth:`has_track_assistant_message_since` と同じ直 SQL 流儀)。
        episode 読み口 (post_session の原本注入 / episode_read スペル) の
        原始関数。volatile も含む全行 — 原本は生ログそのもの。

        Returns:
            時系列 (created_at 昇順、同秒は挿入順 = rowid 昇順) の dict リスト。
            各 dict は原本レンダリングに足る列 (role / content / created_at /
            line_role / scope / metadata / spell 関連) を持つ。adapter 未 ready /
            クエリ失敗は空リスト。
        """
        if not self._ready or not episode_ref:
            return []
        try:
            with self._db_lock:
                rows = self.conn.execute(
                    "SELECT id, thread_id, role, content, created_at, metadata, "
                    "line_role, scope, pulse_id, origin_track_id, "
                    "paired_action_text, spell_origin_id, spell_seq "
                    "FROM messages WHERE origin_episode = ? "
                    "ORDER BY created_at ASC, rowid ASC",
                    (str(episode_ref),),
                ).fetchall()
        except Exception as exc:
            LOGGER.warning(
                "Failed to query messages by origin_episode %s: %s",
                episode_ref, exc,
            )
            return []
        out: List[Dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row[5]) if row[5] else None
            except (TypeError, ValueError):
                metadata = None
            out.append({
                "id": row[0],
                "thread_id": row[1],
                "role": row[2],
                "content": row[3],
                "created_at": int(row[4]) if row[4] is not None else None,
                "metadata": metadata if isinstance(metadata, dict) else None,
                "line_role": row[6],
                "scope": row[7],
                "pulse_id": row[8],
                "origin_track_id": row[9],
                "paired_action_text": row[10],
                "spell_origin_id": row[11],
                "spell_seq": row[12],
            })
        return out

    # NOTE: 旧 ``get_track_last_message_time`` / ``get_track_last_message_times``
    # (``messages.origin_track_id`` で MAX(created_at) を引く読み手) は
    # 2026-08-21 に撤去した。消費者は Tracks API と wait_response タイマーの
    # 基準時刻で、どちらも会話経路の Track なし化で退役した
    # (track_retirement.md §2 住人 3・9)。列と既存データはそのまま残る。

    def get_messages_with_persona_in_audience(
        self,
        persona_id: str,
        *,
        exclude_message_ids: Optional[set[str]] = None,
        required_tags: Optional[List[str]] = None,
        current_thread_only: bool = True,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Get messages where a specific persona is in the audience.

        Args:
            persona_id: Persona ID to search for in audience
            exclude_message_ids: Message IDs to exclude (e.g., recent context)
            required_tags: Only include messages with these tags
            limit: Maximum number of messages to return

        Returns:
            List of message payloads, ordered by created_at DESC
        """
        if not self._ready:
            return []
        try:
            with self._db_lock:
                thread_id = self._thread_id(None) if current_thread_only else None
                messages = get_messages_with_persona_in_audience(
                    self.conn,
                    persona_id,
                    thread_id=thread_id,
                    exclude_message_ids=exclude_message_ids,
                    required_tags=required_tags,
                    limit=limit,
                )
                return [self._payload_from_message_locked(msg, viewing_thread_id=thread_id) for msg in messages]
        except Exception as exc:
            LOGGER.warning("Failed to get messages with persona %s in audience: %s", persona_id, exc)
            return []

    def _append_message(
        self,
        *,
        building_id: Optional[str],
        message: dict,
        thread_suffix: Optional[str] = None,
    ) -> Optional[str]:
        """Insert a message and return its newly-assigned id (or None on failure).

        The id return path is used by Phase 1.3 meta-judgment so the dispatch
        step can later promote the row from ``scope='discardable'`` to
        ``'committed'`` without re-querying. Legacy callers ignoring the
        return value are unaffected.

        ``None`` は「行が入らなかった」ときだけ返す。行の commit 後に作る
        埋め込みの失敗は WARN のみで、id は返す (2026-08-08) — 派生物の失敗を
        保存の失敗として返すと、呼び出し側が保存済みの行を書き直しに来る。
        """
        if not self._ready:
            return None
        try:
            role = message.get("role", "system")
            # Normalize non-str content (structured output dict/list) to text so
            # it survives the TEXT-column bind instead of failing and being
            # swallowed as a WARNING (docs/issues/memorize_dict_content_silently_dropped.md).
            content = _coerce_content_to_text(message.get("content", ""))
            timestamp = message.get("timestamp")
            created_at = self._timestamp_to_epoch(timestamp)
            thread_id = self._thread_id(building_id, thread_suffix=thread_suffix)
            resource_id = building_id or self.settings.resource_id
            metadata = message.get("metadata")
            if not isinstance(metadata, dict):
                metadata = None
            # 7-layer storage metadata (Intent A v0.14, Intent B v0.11). Carried
            # on the message dict by callers that have a PulseContext line frame
            # available; absent here when called from legacy code paths.
            # ``origin_track_id``: 生きた書き手はもう無い (2026-08-21 の Track
            # なし化で全経路が退役)。列と既存データの読み手は残っているので、
            # 「値が来たら書く」受け口だけ残す — 復元・取り込み系が過去の値を
            # 載せた dict を渡してきたときに黙って落とさないため。列ごとの掃除は
            # Track テーブル退役の migration (撤去順序⑦) で行う。
            origin_track_id = message.get("origin_track_id")
            line_role = message.get("line_role")
            line_id = message.get("line_id")
            scope = message.get("scope")
            paired_action_text = message.get("paired_action_text")
            # Phase 2.5: pulse_id 専用カラム (旧 metadata.tags の "pulse:{uuid}" を置換)。
            # _store_memory が message dict にセットする。タグも当面併存。
            pulse_id_val = message.get("pulse_id")
            # 2026-05-20: Gemini 3.x の thoughtSignature をターン跨ぎで保持する。
            # 詳細は docs/intent/thought_signature_persistence.md
            thought_signature = message.get("thought_signature")
            spell_origin_id = message.get("spell_origin_id")
            spell_seq = message.get("spell_seq")
            embedding_chunks = message.get("embedding_chunks")
            skip_embedding = False
            if embedding_chunks is not None:
                try:
                    skip_embedding = int(embedding_chunks) == 0
                except (TypeError, ValueError):
                    skip_embedding = False

            LOGGER.debug(
                "[_append_message] thread_suffix=%s, building_id=%s, thread_id=%s line_role=%s scope=%s",
                thread_suffix, building_id, thread_id, line_role, scope
            )

            with self._db_lock:
                get_or_create_thread(self.conn, thread_id, resource_id)  # type: ignore[arg-type]
                mid = add_message(
                    self.conn,
                    thread_id=thread_id,
                    role=role,
                    content=content,
                    resource_id=resource_id,
                    created_at=created_at,
                    metadata=metadata,
                    origin_track_id=origin_track_id,
                    line_role=line_role,
                    line_id=line_id,
                    scope=scope,
                    paired_action_text=paired_action_text,
                    pulse_id=pulse_id_val,
                    thought_signature=thought_signature,
                    spell_origin_id=spell_origin_id,
                    spell_seq=spell_seq,
                )
                if (not skip_embedding) and content and content.strip() and self.embedder is not None:
                    # 埋め込みは**行の commit 後に作る派生物**。ここの失敗を
                    # 「メッセージを保存できなかった」として None で返すと、
                    # 呼び出し側は保存済みの行をもう一度書きに来る (知覚バッファ
                    # の消費で実際に起きた二重書き込みの根 — Codex レビュー #2)。
                    # 行が入った事実は正直に返し、失われるのは検索用の索引だけに
                    # とどめる (再作成は reembed API で可能)。
                    try:
                        chunks = chunk_text(
                            content,
                            min_chars=self.settings.chunk_min_chars,
                            max_chars=self.settings.chunk_max_chars,
                        )
                        payload = [c.strip() for c in chunks if c and c.strip()]
                        if payload:
                            vectors = self.embedder.embed(payload, is_query=False)
                            replace_message_embeddings(self.conn, mid, vectors)
                    except Exception as embed_exc:
                        LOGGER.warning(
                            "SAIMemory message %s stored but embedding failed "
                            "(building=%s): %s", mid, building_id, embed_exc,
                            exc_info=True,
                        )
            LOGGER.debug(
                "SAIMemory upserted message=%s thread=%s role=%s", mid, thread_id, role
            )
            return mid
        except Exception as exc:
            LOGGER.warning("Failed to append message to SAIMemory (building=%s): %s", building_id, exc)
            return None

    @staticmethod
    def _timestamp_to_epoch(value: Optional[str]) -> int:
        if not value:
            return int(time.time())
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return int(time.time())


def _fetch_all_messages(conn, thread_id: str, page_size: int = 200):
    page = 0
    rows = []
    while True:
        batch = get_messages_paginated(conn, thread_id, page=page, page_size=page_size)  # type: ignore[arg-type]
        if not batch:
            break
        rows.extend(batch)
        page += 1
    return rows


def _payload_passes_context_filter(
    payload: Dict[str, Any],
    *,
    required_tags: Optional[List[str]] = None,
    required_line_roles: Optional[List[str]] = None,
    required_scopes: Optional[List[str]] = None,
    pulse_id: Optional[str] = None,
    strict_tags: bool = False,
) -> bool:
    """Decide if a payload should be included in context construction.

    Filtering is line-based first (line_role / scope), with legacy tags as a
    fallback for messages predating Phase 1. Pulse-scoped overrides:
    - matching pulse_id always wins (included, bypasses line/scope/tag filters)

    Legacy compatibility:
    - line_role IS NULL is treated as 'main_line' (pre-Phase-1 rows)
    - scope IS NULL is treated as 'committed' (pre-Phase-1 rows)
    - For tag fallback, legacy entries without any tags are included unless the
      caller explicitly requires the 'conversation' tag.

    Phase 3 段階 4-D (2026-05-09): 旧 ``exclude_pulse_id`` 引数削除。
    context_profile 経路の廃止に伴い、自分自身の Pulse メッセージを除外する
    ユースケース (= state["_messages"] の重複防止) は state ベースで担保される。
    """
    payload_pulse_id = payload.get("pulse_id")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    raw_tags = metadata.get("tags") if isinstance(metadata, dict) else None
    tags = [str(t) for t in raw_tags if t] if isinstance(raw_tags, list) else []

    legacy_pulse_tag = f"pulse:{pulse_id}" if pulse_id else None

    # Pulse-based override (precedes line/scope/tag filtering)
    if pulse_id:
        if payload_pulse_id == pulse_id:
            return True
        if legacy_pulse_tag and legacy_pulse_tag in tags:
            return True

    # Line-role filter (preferred). Legacy line_role IS NULL maps to 'main_line'.
    if required_line_roles:
        payload_role = payload.get("line_role")
        effective_role = payload_role if payload_role is not None else "main_line"
        role_ok = effective_role in required_line_roles
        # committed なメタ判断は「メインキャッシュに乗った確定来歴」として main_line
        # 文脈に属する (Track 切替の確定独白 / 生きる目的の初回設定など)。
        # meta_judgment_finalize は Track 操作 or 自己定義スペルが発火したとき
        # scope='committed' で書き、_promote_meta_judgment_in_pulse が Track 切替時に
        # discardable→committed へ昇格する。設計意図は committed_to_main_cache=TRUE
        # = 既にメインキャッシュに乗っている
        # (docs/intent/persona_cognition/03_data_model.md §176)。line_role が
        # 'meta_judgment' のままでも committed なら main_line 要求で通す。
        # discardable のメタ判断は従来通り除外され、judge プロンプトへは
        # MetaLayer._build_recent_judgments_block が別途注入する (二重にならない)。
        if (
            not role_ok
            and effective_role == "meta_judgment"
            and "main_line" in required_line_roles
        ):
            payload_scope = payload.get("scope")
            effective_scope = payload_scope if payload_scope is not None else "committed"
            if effective_scope == "committed":
                role_ok = True
        if not role_ok:
            return False

    # Scope filter. Legacy scope IS NULL maps to 'committed'.
    if required_scopes:
        payload_scope = payload.get("scope")
        effective_scope = payload_scope if payload_scope is not None else "committed"
        if effective_scope not in required_scopes:
            return False

    # Tag filter (legacy / opt-in). Kept for callers that haven't migrated to
    # line-based filtering yet (e.g. search/recall paths). When line filters
    # are already in play, tag filter is typically not used.
    if required_tags:
        if strict_tags:
            # 厳格一致: タグを実際に持つ行だけ。legacy 救済 (下) はタグ無し行を
            # 素通しにするため、「その種類の記録だけ数えたい」呼び出しでは
            # paired_action 展開行 (タグ無し) が混入・枠占拠する (2026-07-29)。
            return bool(tags) and any(tag in tags for tag in required_tags)
        if not tags:
            # legacy entries without tags: include unless caller asked for "conversation"
            return "conversation" not in required_tags
        if not any(tag in tags for tag in required_tags):
            return False

    return True
