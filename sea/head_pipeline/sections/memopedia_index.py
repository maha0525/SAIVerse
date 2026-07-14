"""MemopediaIndexSection — Memopedia ページの created / updated / deleted 差分検知
と、opt-in head 目次（``AI.MEMOPEDIA_INDEX_ENABLED`` トグル、旧方式の後方互換復元）。

旧 ``DynamicStateManager`` の memopedia 差分計算ロジックを Section interface に
移植。head には何も render しない (= MemoryWeaveSection が中身を担当する)。

タイムスタンプベース判定で、b.captured_at 以降に変化したページのみ通知する。
全件比較ではないので memopedia 規模に依存しない。

P4-d: ``AI.MEMOPEDIA_INDEX_ENABLED`` フラグが ON のペルソナに限り、
Metabolism のみで更新される**目次スナップショット**を head に render する。

このトグルは「Memopedia 全ページ一覧の常時表示（旧方式）」への後方互換として
2026-07-04 に作られたもの (旧実装: ``builtin_data/tools/get_memory_weave_context.py``
の ``_get_memopedia_context``、summary あり・深さ制限なし)。P4-d (2026-07-11) は
器をこの Section に一本化する際に描画内容を「summary なし・深さ2階層まで」に
すり替えてしまい、トグルの意味（旧方式復元）が失われる回帰を生んでいた。
2026-07-14 の裁定でこれを修正し、summary 表示・深さ無制限の旧方式相当に戻した
（[OPEN]（机基準）・★（is_important）・件数は P4-d の改善なので残す）。
旧実装 ``_get_memopedia_context`` / ``include_memopedia`` 引数は本 Section への
一本化に伴い削除済み（本番から呼ばれない死にコードだったため）。

設計の「(persona,model) 固定」head 規律との整合:
- enabled_sections 登録は固定 (on/off ゲートは per-persona DB フラグで、
  Section の登録自体を切り替えない)。
- ON の場合のみ toc_markdown を snapshot に保持し、render がテキストを返す。
- OFF の場合は toc_markdown=None で render が None を返す。
- refresh_on_events = frozenset() = Metabolism のみ更新——キャッシュ整合を守る。

詳細: docs/intent/concept_consolidation.md §P4-d
旧詳細: docs/intent/cached_head_architecture.md §5.1 / dynamic_state_sync.md
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
    # P4-d: opt-in 目次。None = flag OFF または capture 失敗。
    index_enabled: bool = False
    toc_markdown: Optional[str] = None


def _build_toc_markdown(conn) -> Optional[str]:
    """目次 Markdown を組み立てる（決定論・LLM ゼロ）。

    旧方式（``_get_memopedia_context``、Memory Atlas 以前）を本 Section へ
    一本化した実装。旧方式の忠実な復元として:

    - 各ページに summary を表示する（旧: ``- {title}{id_suffix}: {summary}``）
    - 深さ制限なし（全階層を表示）
    - カテゴリは ``category_keys("extractable")``（旧実装が使っていた集合。
      ``category_keys("in_tree")`` との差分は "theme" のみで、旧方式は
      theme トランクを掲示していなかったため後方互換のためこちらを踏襲する）

    P4-d で追加された改善はそのまま残す:

    - [OPEN] マーカー（机 (desk_items) 基準）
    - ★(is_important) マーカー
    - 各カテゴリ／各 trunk の総ページ数
    """
    try:
        from sai_memory.memopedia import Memopedia, init_memopedia_tables
        from sai_memory.memopedia.storage import category_keys, category_label

        init_memopedia_tables(conn)
        mem = Memopedia(conn)
        tree = mem.get_tree(thread_id=None)

        # [OPEN] は**机 (desk_items)** を見る。get_tree の is_open は旧 PageState
        # (thread 単位の開閉、机に置き換えられて事実上未使用) 由来で、
        # thread_id=None では常に False — 机こそが現行の「開いている」の実体。
        try:
            from sai_memory import desk as _desk
            open_refs = {item.ref for item in _desk.list_open(conn)}
        except Exception:
            open_refs = set()

        lines: list[str] = []

        def _count_all(pages: list) -> int:
            """ページリスト（children 含む再帰）の総数を返す。trunk は除く。"""
            total = 0
            for p in pages:
                if not p.get("id", "").startswith("root_") and not p.get("is_trunk"):
                    total += 1
                total += _count_all(p.get("children") or [])
            return total

        def _render_page(page: dict, depth: int = 0) -> None:
            # trunk（root_ ページ）はスキップ、子は処理する
            if page.get("is_trunk") or page.get("id", "").startswith("root_"):
                for child in page.get("children") or []:
                    _render_page(child, depth)
                return

            indent = "  " * depth
            sid = page.get("short_id")
            id_part = f" [m:{sid}]" if sid is not None else ""
            title = page.get("title") or "(無題)"

            # [OPEN] マーカー (机に開いているページ)
            page_ref = f"m:{sid}" if sid is not None else None
            open_mark = " [OPEN]" if page_ref and page_ref in open_refs else ""
            # ★ important マーカー
            star_mark = " ★" if page.get("is_important") else ""

            # 子の総数（このページの下の全ページ数）
            children = page.get("children") or []
            child_total = _count_all(children)
            count_part = f" ({child_total})" if child_total > 0 else ""

            # summary（旧方式の復元。空文字列でも ": " を出す旧実装の挙動を踏襲）
            summary = page.get("summary") or ""

            lines.append(
                f"{indent}- {title}{id_part}{open_mark}{star_mark}{count_part}: {summary}"
            )

            # 深さ制限なし。全階層を描画する（旧方式の復元）。
            for child in children:
                _render_page(child, depth + 1)

        for cat_key in category_keys("extractable"):
            cat_label = category_label(cat_key)
            pages = tree.get(cat_key) or []
            if not pages:
                continue
            total = _count_all(pages)
            lines.append(f"\n### {cat_label}（{total} ページ）")
            for p in pages:
                _render_page(p, depth=0)

        if not lines:
            return None
        return "\n".join(lines)
    except Exception:
        LOGGER.warning("memopedia_index: failed to build toc_markdown", exc_info=True)
        return None


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

        # P4-d: opt-in フラグを読む
        index_enabled = self._resolve_memopedia_index_enabled(ctx)

        # Metabolism / 初回 capture 時は「これ以降の変化」を追うための基準 timestamp。
        # ここでは since=time.time() を渡すと事実上空 list が返るので、capture では
        # 全ページのインデックスを取らずに「baseline」だけ確立する形にする。
        # diff_to_notifications では old.captured_at を since として再 capture を
        # 走らせる必要があるため、本 Section は別経路で per-tick の差分を取りに行く。
        # → 本 Section の capture は **空 list の baseline** だけ返し、
        # diff_to_notifications で old.captured_at 以降の変化を SQL で問い合わせる。
        now = time.time()

        # P4-d: flag ON のときのみ目次を構築する
        toc_markdown: Optional[str] = None
        if index_enabled:
            toc_markdown = _build_toc_markdown(sai_mem.conn)

        return MemopediaIndexSnapshot(
            captured_at=now,
            pages=(),
            index_enabled=index_enabled,
            toc_markdown=toc_markdown,
        )

    def render(self, snapshot: MemopediaIndexSnapshot) -> Optional[RenderedSection]:
        # P4-d: flag OFF / 目次なし → None を返す（既存の「何も render しない」挙動を維持）
        if not snapshot.index_enabled or not snapshot.toc_markdown:
            return None
        text = (
            "## 記憶の目次（Memopedia Index）\n"
            "以下は地図帳の目次です。各ページの概要 (summary) を示しています。"
            "[OPEN] は机に開いているページ、★ は重要ページです。"
            "ページの本文まで読みたい場合は memory_read で読んでください。\n"
            + snapshot.toc_markdown
        )
        return RenderedSection(text=text)

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
                "index_enabled": snapshot.index_enabled,
                "toc_markdown": snapshot.toc_markdown,
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
            index_enabled=bool(payload.get("index_enabled", False)),
            toc_markdown=payload.get("toc_markdown"),
        )

    # ---- 内部ヘルパー ----

    def _resolve_memopedia_index_enabled(self, ctx: LineHeadInput) -> bool:
        """per-persona DB フラグ ``AI.MEMOPEDIA_INDEX_ENABLED`` を読む。

        P4-d: weave 側が持っていた ``_resolve_memopedia_index_enabled`` と同じ
        実装——フラグの意味を「weave 内掲示」から「head 目次セクション」へ付け替え。
        """
        manager = ctx.manager
        persona_id = ctx.persona_id
        if not manager or not persona_id:
            return False
        session_factory = getattr(manager, "SessionLocal", None)
        if not session_factory:
            return False
        db = session_factory()
        try:
            from database.models import AI as AIModel
            ai = db.query(AIModel).filter_by(AIID=persona_id).first()
            return bool(ai.MEMOPEDIA_INDEX_ENABLED) if ai else False
        except Exception:
            LOGGER.warning(
                "memopedia_index: failed to resolve MEMOPEDIA_INDEX_ENABLED "
                "persona=%s", persona_id, exc_info=True,
            )
            return False
        finally:
            db.close()
