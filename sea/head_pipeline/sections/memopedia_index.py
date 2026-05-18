"""MemopediaIndexSection — Memopedia ページの created / updated / deleted 差分検知。

旧 ``DynamicStateManager`` の memopedia 差分計算ロジックを Section interface に
移植。head には何も render しない (= MemoryWeaveSection が中身を担当する)。

タイムスタンプベース判定で、b.captured_at 以降に変化したページのみ通知する。
全件比較ではないので memopedia 規模に依存しない。

詳細: docs/intent/cached_head_architecture.md §5.1 / dynamic_state_sync.md
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Optional

from sea.head_pipeline.types import (
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemopediaPageEntry:
    page_id: str
    title: str
    created_at: int
    updated_at: int
    is_deleted: bool


@dataclass(frozen=True)
class MemopediaIndexSnapshot:
    captured_at: float                            # epoch seconds (= since の基準)
    pages: tuple[MemopediaPageEntry, ...]         # captured_at 以降に変化したページのみ


class MemopediaIndexSection:
    name = "memopedia_index"
    order = 1200
    refresh_on_events = frozenset()  # Metabolism のみ

    def capture(self, ctx: LineHeadInput) -> MemopediaIndexSnapshot:
        persona = ctx.persona
        if persona is None:
            return MemopediaIndexSnapshot(captured_at=time.time(), pages=())

        sai_mem = getattr(persona, "sai_memory", None)
        if not sai_mem or not getattr(sai_mem, "conn", None):
            return MemopediaIndexSnapshot(captured_at=time.time(), pages=())

        # Metabolism / 初回 capture 時は「これ以降の変化」を追うための基準 timestamp。
        # ここでは since=time.time() を渡すと事実上空 list が返るので、capture では
        # 全ページのインデックスを取らずに「baseline」だけ確立する形にする。
        # diff_to_notifications では old.captured_at を since として再 capture を
        # 走らせる必要があるため、本 Section は別経路で per-tick の差分を取りに行く。
        # → 本 Section の capture は **空 list の baseline** だけ返し、
        # diff_to_notifications で old.captured_at 以降の変化を SQL で問い合わせる。
        now = time.time()
        return MemopediaIndexSnapshot(captured_at=now, pages=())

    def render(self, snapshot: MemopediaIndexSnapshot) -> Optional[RenderedSection]:
        return None

    def diff_to_notifications(
        self,
        old: Optional[MemopediaIndexSnapshot],
        new: Optional[MemopediaIndexSnapshot],
    ) -> list[NotificationLabel]:
        # new は本 Section の capture 結果。pages は空のはず。差分検出のためには
        # old.captured_at 以降の変化分を SAIMemory から直接読み出す必要があるが、
        # diff_to_notifications シグネチャは old/new のみ受け取る前提なので、
        # 「変化情報を呼び出し側でセットしてくれる」形式に頼れない。
        #
        # 暫定実装: new.pages にすでに「old.captured_at 以降の変化」が積まれている
        # 前提で diff を出す。pipeline 側 (= flush_diffs) で MemopediaIndexSection
        # の new 取得時に since=old.captured_at で再 capture できる仕組みが必要。
        # → 専用の hook を Section に持たせず、pipeline 側で「diff 専用 capture」
        #    の経路を作る (Phase 3-e で配線)。
        if old is None or new is None:
            return []
        cutoff = old.captured_at
        labels: list[NotificationLabel] = []
        for page in new.pages:
            if page.is_deleted and page.updated_at > cutoff:
                labels.append(NotificationLabel(
                    kind="memopedia_deleted",
                    label=f"Memopedia「{page.title}」が削除されました",
                ))
            elif page.created_at > cutoff:
                labels.append(NotificationLabel(
                    kind="memopedia_created",
                    label=f"Memopedia「{page.title}」が作成されました",
                ))
            elif page.updated_at > cutoff:
                labels.append(NotificationLabel(
                    kind="memopedia_updated",
                    label=f"Memopedia「{page.title}」が更新されました",
                ))
        return labels

    def capture_changes_since(
        self, ctx: LineHeadInput, since: float,
    ) -> MemopediaIndexSnapshot:
        """``since`` 以降に変化した Memopedia ページのみを集めた snapshot を返す。

        本 Section は capture 時には baseline (空 list) を返し、差分判定タイミング
        (= pipeline.flush_diffs) で本メソッドが呼ばれて「old.captured_at 以降の変化」
        を実際に拾う。pipeline 側でこの拡張メソッドを認識する経路を Phase 3-e で配線。
        """
        persona = ctx.persona
        if persona is None:
            return MemopediaIndexSnapshot(captured_at=time.time(), pages=())
        sai_mem = getattr(persona, "sai_memory", None)
        if not sai_mem or not getattr(sai_mem, "conn", None):
            return MemopediaIndexSnapshot(captured_at=time.time(), pages=())

        since_int = int(since)
        pages: list[MemopediaPageEntry] = []
        try:
            cur = sai_mem.conn.execute(
                "SELECT id, title, created_at, updated_at, COALESCE(is_deleted, 0) "
                "FROM memopedia_pages "
                "WHERE updated_at >= ? OR created_at >= ?",
                (since_int, since_int),
            )
            for row in cur.fetchall():
                pages.append(MemopediaPageEntry(
                    page_id=str(row[0]),
                    title=str(row[1]) if row[1] is not None else "",
                    created_at=int(row[2] or 0),
                    updated_at=int(row[3] or 0),
                    is_deleted=bool(row[4]),
                ))
        except Exception:
            LOGGER.warning(
                "memopedia_index: failed to query memopedia_pages since=%s",
                since_int, exc_info=True,
            )
        return MemopediaIndexSnapshot(captured_at=time.time(), pages=tuple(pages))

    def serialize_snapshot(self, snapshot: MemopediaIndexSnapshot) -> str:
        return json.dumps(
            {
                "captured_at": snapshot.captured_at,
                "pages": [asdict(p) for p in snapshot.pages],
            },
            ensure_ascii=False,
        )

    def deserialize_snapshot(self, data: str) -> MemopediaIndexSnapshot:
        payload = json.loads(data)
        pages = tuple(
            MemopediaPageEntry(**p) for p in payload.get("pages", [])
        )
        return MemopediaIndexSnapshot(
            captured_at=float(payload.get("captured_at", 0.0)),
            pages=pages,
        )
