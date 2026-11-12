"""合成ペルソナの用意と、正規の読み取りによる観測をまとめた共通 fixture。

この fixture が答えを出せること:

- 手帳の通常ページ・その Fragment・机に開いた参照・コア記憶 (生存とごみ箱)・
  Chronicle・テーマページを **1 つの記憶 DB に同居させた** 合成ペルソナを作る。
- その状態を「利用者と読み手にとっての姿」で観測する — 表の行を直に数えるだけでなく、
  ``list_core_memories`` / ``get_all_entries_ordered`` / head の ``CoreMemorySection.capture``
  という**正規の読み取り**を通す。読み取りが例外で落ちたら、その例外を観測値として記録する。

この fixture が答えを出せないこと:

- 実際の HTTP、画面の操作、バックエンドの起動、ペルソナの発話、LLM の呼び出し。
  API は ``api.routes.people.memopedia`` のルート関数を Python から直接呼ぶ形で、
  FastAPI のルーティング・認証・シリアライズは通っていない。
- 埋め込み (意味検索) の挙動。``probe_env.use_dummy_embedder`` で 0 ベクトルに
  差し替えているので、想起の順位は何も確かめていない。

本番ペルソナ・本番データ (``~/.saiverse``) には触れない。触れそうになったら
``probe_env`` の検査が AssertionError で止める。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_env  # noqa: E402


def new_world(name: str) -> Path:
    """隔離ホームを作り、本番でないことを検査し、埋め込みを偽物へ差し替える。

    実験ファイルの冒頭で、SAIVerse のモジュールを import するより前に呼ぶ。
    """
    home = probe_env.setup_home(name)
    probe_env.guard_not_production()
    probe_env.use_dummy_embedder()
    return home


def capture(fn: Callable[[], Any]) -> Any:
    """読み取りを 1 つ実行し、例外が出たらそれを観測値 (文字列) として返す。

    「読めなくなった」ことそのものが今回の観測対象なので、例外を握り潰さず
    結果欄に残す。
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return f"EXCEPTION {type(exc).__name__}: {exc}"


class ProbePersona:
    """隔離された合成ペルソナ 1 体 (記憶 DB を 1 つ持つ)。"""

    def __init__(self, persona_id: str) -> None:
        from saiverse_memory.adapter import SAIMemoryAdapter

        self.persona_id = persona_id
        self.adapter = SAIMemoryAdapter(persona_id)
        self.db_path = probe_env.guard_db_path(self.adapter.persona_dir / "memory.db")

    # ----- 用意 -----

    def seed(
        self,
        *,
        core_memories: tuple = ("まはーは野菜がほぼ食べられない", "工房の棚は三段で、上段に定規を置く"),
        trashed_core_memory: Optional[str] = "自分でごみ箱へ入れたコア記憶",
        knowledge_title: str = "知識ページ",
        with_fragment: bool = True,
        with_desk: bool = True,
        with_chronicle: bool = True,
        with_theme: bool = True,
    ) -> Dict[str, Any]:
        """4 種の記憶を 1 つの DB に同居させる。作ったものの id を返す。"""
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.storage import create_entry
        from sai_memory.core_memory import (
            add_core_memory,
            init_core_memory_table,
            remove_core_memory,
        )
        from sai_memory.desk import init_desk_tables, open_item
        from sai_memory.memopedia.storage import create_fragment, create_page
        from sai_memory.theme_pages import create_theme_page, ensure_root_theme

        made: Dict[str, Any] = {}
        with self.adapter._db_lock:
            init_core_memory_table(self.adapter.conn)
            init_desk_tables(self.adapter.conn)

            made["core_ids"] = [
                add_core_memory(self.adapter.conn, text) for text in core_memories
            ]
            if trashed_core_memory is not None:
                trashed_id = add_core_memory(self.adapter.conn, trashed_core_memory)
                remove_core_memory(self.adapter.conn, trashed_id)
                made["trashed_core_id"] = trashed_id

            page = create_page(
                self.adapter.conn, parent_id=None, title=knowledge_title,
                summary="", content="手帳の本文", category="terms",
            )
            made["knowledge_page_id"] = page.id
            made["knowledge_short_id"] = page.short_id

            if with_fragment:
                frag = create_fragment(
                    self.adapter.conn, entity_id=page.id, content="この知識の断片",
                )
                made["fragment_id"] = frag.id
            if with_desk and page.short_id is not None:
                ref = f"memopedia:{page.short_id}"
                open_item(self.adapter.conn, ref)
                made["desk_ref"] = ref
            if with_chronicle:
                init_arasuji_tables(self.adapter.conn)
                entry = create_entry(
                    self.adapter.conn, level=1, content="あらすじ",
                    source_ids=["m1"], start_time=1000, end_time=1400,
                    source_count=1, message_count=1,
                )
                made["chronicle_entry_id"] = entry.id
            if with_theme:
                ensure_root_theme(self.adapter.conn)
                made["theme_page_id"] = create_theme_page(
                    self.adapter.conn, title="テーマページ", member_refs=["task:1"],
                )
        return made

    # ----- 観測 -----

    def rows(self) -> list:
        """``memopedia_pages`` の生きている/消された行を、種別が分かる形で並べる。"""
        with self.adapter._db_lock:
            cur = self.adapter.conn.execute(
                "SELECT id, category, parent_id, title, COALESCE(is_deleted, 0), "
                "CASE WHEN metadata IS NULL THEN 'metadata なし' ELSE 'metadata あり' END, "
                "short_id FROM memopedia_pages ORDER BY category, title, id"
            )
            return [
                {
                    "id": r[0], "category": r[1], "parent_id": r[2], "title": r[3],
                    "is_deleted": r[4], "metadata": r[5], "short_id": r[6],
                }
                for r in cur.fetchall()
            ]

    def counts(self) -> Dict[str, Any]:
        """ページ以外の付随データの件数 (道連れ削除の射程を見るため)。"""
        def count(sql: str) -> Any:
            return capture(
                lambda: self.adapter.conn.execute(sql).fetchone()[0]
            )

        with self.adapter._db_lock:
            return {
                "生きているページ": count(
                    "SELECT COUNT(*) FROM memopedia_pages "
                    "WHERE is_deleted = 0 OR is_deleted IS NULL"
                ),
                "Fragment": count("SELECT COUNT(*) FROM memopedia_fragments"),
                "ページの埋め込み": count("SELECT COUNT(*) FROM memopedia_embeddings"),
                "ページの編集来歴": count(
                    "SELECT COUNT(*) FROM memopedia_page_edit_history"
                ),
                "机に開いた参照": capture(
                    lambda: [
                        r[0] for r in self.adapter.conn.execute(
                            "SELECT ref FROM desk_items ORDER BY ref"
                        ).fetchall()
                    ]
                ),
            }

    def reads(self) -> Dict[str, Any]:
        """正規の読み取り口を通した結果 (落ちたら例外を値として残す)。"""
        from sai_memory.arasuji.storage import get_all_entries_ordered
        from sai_memory.core_memory import (
            list_core_memories,
            list_deleted_core_memories,
            total_core_memory_chars,
        )

        with self.adapter._db_lock:
            return {
                "コア記憶の一覧 (list_core_memories)": capture(
                    lambda: [
                        f"c:{c.id} {c.content}"
                        for c in list_core_memories(self.adapter.conn)
                    ]
                ),
                "コア記憶のごみ箱 (list_deleted_core_memories)": capture(
                    lambda: [
                        f"c:{c.id} {c.content}"
                        for c in list_deleted_core_memories(self.adapter.conn)
                    ]
                ),
                "コア記憶の合計字数 (total_core_memory_chars)": capture(
                    lambda: total_core_memory_chars(self.adapter.conn)
                ),
                "あらすじの一覧 (get_all_entries_ordered)": capture(
                    lambda: [
                        e.content for e in get_all_entries_ordered(self.adapter.conn)
                    ]
                ),
            }

    def head_core_memory(self) -> Dict[str, Any]:
        """ペルソナの頭 (head) のコア記憶の章を、実際に capture して render する。

        ``CoreMemorySection`` は ``required = True`` の章で、読み取り例外を
        意図的に握らない。ここで落ちるということは、頭の組み立てが落ちるということ。
        """
        from sea.head_pipeline.sections.core_memory import CoreMemorySection
        from sea.head_pipeline.types import LineHeadInput

        section = CoreMemorySection()
        ctx = LineHeadInput(
            persona_id=self.persona_id,
            model_key="probe-model",
            persona=SimpleNamespace(sai_memory=self.adapter),
        )
        snapshot = capture(lambda: section.capture(ctx))
        if isinstance(snapshot, str):  # 例外だった
            return {"capture": snapshot, "render": "(capture が落ちたので描画に至らない)"}
        rendered = capture(lambda: section.render(snapshot))
        return {
            "capture": [f"c:{i.memory_id} {i.content}" for i in snapshot.items],
            "render": None if rendered is None else rendered.text,
        }

    def observe(self) -> Dict[str, Any]:
        """1 回ぶんの全観測をまとめて取る。"""
        return {
            "保存表の行": self.rows(),
            "付随データ": self.counts(),
            "正規の読み取り": self.reads(),
            "頭のコア記憶の章": self.head_core_memory(),
        }

    def close(self) -> None:
        self.adapter.close()


def make_manager(*personas: ProbePersona) -> SimpleNamespace:
    """API のルート関数に渡す偽の manager (DB もサーバも要らない)。

    ``api.routes.people.memopedia`` の ``get_adapter`` が
    ``manager.personas[persona_id].sai_memory`` を引く形に合わせてある。
    """
    return SimpleNamespace(
        personas={
            p.persona_id: SimpleNamespace(sai_memory=p.adapter) for p in personas
        },
        SessionLocal=None,
    )


def write_result(script_path: str, payload: Dict[str, Any]) -> Path:
    """結果を同じディレクトリの ``<実験名>_result.json`` へ書き、パスを返す。"""
    import json

    out = Path(script_path).resolve().with_name(
        Path(script_path).stem + "_result.json"
    )
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return out
