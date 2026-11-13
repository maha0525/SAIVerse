"""実験2 — 手帳を書き出して、もう一度取り込むと何が往復するか。

観測表の 2 行目 (書き出しと再取り込み) に対応する。

**確かめること**

- ``GET /people/{id}/memopedia/export`` の実装 (``export_memopedia``) が返す中身 —
  どのカテゴリの記憶が入り、1 ページあたりどの項目が運ばれるか。
- その書き出しを、別の合成ペルソナへ ``POST .../memopedia/import`` (``clear=false``、
  既存に足す形) で取り込んだあと、取り込み先の記憶がどうなるか。
- 取り込み後に正規の読み取り (``list_core_memories``) とペルソナの頭のコア記憶の章が
  何を返すか。落ちるなら例外の原文。
- コア記憶 API の一覧 (``GET /people/{id}/core-memory``) が同じ状態で何を返すか。

**確かめないこと**

- 実際の HTTP と、ブラウザがファイルを保存・選択する部分。ルート関数を Python から
  直接呼んでいる。
- 画面の表示 (「N ページをエクスポートしました」の数字は、フロントが配列の長さを
  数えたもので、API は件数を返さない)。
- ペルソナが実際に喋れなくなるところまで。頭の章の capture が落ちることは見るが、
  Pulse を回して発話が止まる様子は見ていない。
- 埋め込み検索の結果 (埋め込みは 0 ベクトルの偽物に差し替えてある)。

実行: ``<repo>/.venv/Scripts/python.exe exp2_export_roundtrip.py``
(日本語が化けるときは ``PYTHONIOENCODING=utf-8`` を付ける)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memopedia_fixture as fx  # noqa: E402

HOME = fx.new_world("exp2_export_roundtrip")

from api.routes.people.core_memory import list_core_memory  # noqa: E402
from api.routes.people.memopedia import export_memopedia, import_memopedia  # noqa: E402


def main() -> dict:
    source = fx.ProbePersona("probe_export_source")
    target = fx.ProbePersona("probe_export_target")
    manager = fx.make_manager(source, target)

    out: dict = {
        "実験": "exp2_export_roundtrip — 書き出しと再取り込みで何が往復するか",
        "隔離ホーム": str(HOME),
        "書き出し元の記憶 DB": source.db_path,
        "取り込み先の記憶 DB": target.db_path,
    }
    out["書き出し元に用意したもの"] = source.seed()
    out["取り込み先に用意したもの"] = target.seed(
        core_memories=("取り込み先にもとからあった記憶",),
        trashed_core_memory=None,
        knowledge_title="取り込み先の知識ページ",
        with_fragment=False,
        with_desk=False,
        with_theme=False,
    )

    # ---- 書き出し ----
    exported = export_memopedia(source.persona_id, manager=manager)
    pages = exported.get("pages", [])
    out["書き出しの応答 (形)"] = {
        "トップレベルのキー": sorted(exported.keys()),
        "version": exported.get("version"),
        "ページ数": len(pages),
    }
    out["書き出しに入ったカテゴリ"] = sorted({p.get("category") for p in pages})
    out["書き出しの 1 ページが運ぶ項目"] = sorted(pages[0].keys()) if pages else []
    out["書き出しに入ったページ"] = [
        {"title": p.get("title"), "category": p.get("category"),
         "content": p.get("content")}
        for p in pages
    ]
    out["書き出しが運ばなかった項目"] = sorted(
        {"metadata", "is_important", "is_trunk", "keywords", "short_id",
         "last_referenced_at", "is_deleted"}
        - set(pages[0].keys() if pages else set())
    )

    # ---- 再取り込み (既存に足す形) ----
    out["取り込み前"] = target.observe()
    out["呼んだもの"] = (
        "api.routes.people.memopedia.import_memopedia"
        '("probe_export_target", body=<書き出しそのもの>, clear=False,'
        " manager=<偽 manager>)"
    )
    out["取り込みの応答 (原文)"] = import_memopedia(
        target.persona_id, body=exported, clear=False, manager=manager,
    )
    out["取り込み後"] = target.observe()

    # ---- 画面が呼ぶコア記憶の一覧 API ----
    out["コア記憶の一覧 API (GET /people/{id}/core-memory)"] = fx.capture(
        lambda: list_core_memory(target.persona_id, manager=manager).model_dump()
    )

    out["読み取りの前後"] = {
        "コア記憶の一覧": {
            "前": out["取り込み前"]["正規の読み取り"]["コア記憶の一覧 (list_core_memories)"],
            "後": out["取り込み後"]["正規の読み取り"]["コア記憶の一覧 (list_core_memories)"],
        },
        "頭のコア記憶の章": {
            "前": out["取り込み前"]["頭のコア記憶の章"],
            "後": out["取り込み後"]["頭のコア記憶の章"],
        },
    }

    source.close()
    target.close()
    return out


if __name__ == "__main__":
    result = main()
    path = fx.write_result(__file__, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n結果を書きました: {path}")
