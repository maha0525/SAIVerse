"""実験5 — 操作のあと、ペルソナへ次に差し出されるものがどうなるか。

観測表の 5 行目 (操作後の次の提示) に対応する。

**確かめること**

- ペルソナの頭のコア記憶の章 (``CoreMemorySection``) が、各操作のあと何を返すか。
  この章は ``required = True`` で、読み取り例外を意図的に握らない。
- 手帳の変更をペルソナへ知らせる仕組み (``MemopediaIndexSection`` の
  ``capture_changes_since`` → ``diff_to_notifications``) が、どの出来事を
  「Memopedia が作成/更新/削除されました」と告げるか。
  コア記憶を刻んだとき・あらすじが編まれたときにも、その文が出るかを見る。
- 手帳の通常ページを 1 枚作ったときの通知 (これは正当な通知) が、ちゃんと出続けるか。
- 全削除のあと、消えたことが通知に出るか。

**確かめないこと**

- 通知が実際にペルソナの文脈へ差し込まれるところ。ここで呼んでいるのは章の
  ``capture_changes_since`` と ``diff_to_notifications`` の 2 つだけで、
  ``HeadPipeline`` / ``flush_diffs`` / Pulse は動かしていない。
- 頭の据え置き (stale-but-real) と Metabolism の間隔。実際には操作の直後には
  何も起きず、次に頭を撮り直すまで症状が出ない — その時間差はここでは再現していない。
- バックエンドの起動、ペルソナの発話、LLM の呼び出し。

**時刻の扱い**: 差分は「基準時刻より後に作られた/更新された行」を拾う。用意の直後に
基準時刻を秒単位で切り下げて取り、次の秒に入るまで待ってから操作する。こうしないと
用意した行と操作で作った行が同じ秒に並んで区別できない。

実行: ``<repo>/.venv/Scripts/python.exe exp5_next_presentation.py``
(日本語が化けるときは ``PYTHONIOENCODING=utf-8`` を付ける)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memopedia_fixture as fx  # noqa: E402

HOME = fx.new_world("exp5_next_presentation")

from api.routes.people.memopedia import (  # noqa: E402
    delete_all_memopedia_pages,
    export_memopedia,
    import_memopedia,
)
from sea.head_pipeline.sections.memopedia_index import (  # noqa: E402
    MemopediaIndexSection,
    MemopediaIndexSnapshot,
)
from sea.head_pipeline.types import LineHeadInput  # noqa: E402


def notifications(persona: fx.ProbePersona, since: float) -> list:
    """``since`` 以降の変化から、ペルソナへ届く通知の文を組み立てる。"""
    section = MemopediaIndexSection()
    ctx = LineHeadInput(
        persona_id=persona.persona_id,
        model_key="probe-model",
        persona=SimpleNamespace(sai_memory=persona.adapter),
    )
    old = MemopediaIndexSnapshot(captured_at=since, pages=())
    new = section.capture_changes_since(ctx, since)
    return [
        {"kind": label.kind, "ペルソナへ届く文": label.label}
        for label in section.diff_to_notifications(old, new)
    ]


def wait_past(since: float) -> None:
    """基準時刻の次の秒に入るまで待つ (行の作成時刻を基準より後にするため)。"""
    while time.time() < since + 1.1:
        time.sleep(0.05)


def main() -> dict:
    out: dict = {
        "実験": "exp5_next_presentation — 操作後に次へ差し出されるもの",
        "隔離ホーム": str(HOME),
        "呼んだもの": [
            "sea.head_pipeline.sections.core_memory.CoreMemorySection.capture / render",
            "sea.head_pipeline.sections.memopedia_index.MemopediaIndexSection"
            ".capture_changes_since / diff_to_notifications",
            "api.routes.people.memopedia.delete_all_memopedia_pages / import_memopedia",
        ],
        "場面": [],
    }

    # 5 体ぶん用意して、基準時刻を 1 つ取り、1 回だけ待つ。
    personas = {
        name: fx.ProbePersona(f"probe_notice_{name}")
        for name in ("core", "chronicle", "page", "delete_all", "replace_import")
    }
    for persona in personas.values():
        persona.seed()
    source = fx.ProbePersona("probe_notice_source")
    source.seed(
        core_memories=("持ち出し元のコア記憶",), trashed_core_memory=None,
        knowledge_title="持ち出し元の知識ページ", with_fragment=False,
        with_desk=False, with_chronicle=False, with_theme=False,
    )
    manager = fx.make_manager(*personas.values(), source)

    since = float(int(time.time()))
    out["差分の基準時刻 (epoch 秒)"] = since
    wait_past(since)

    # --- 場面 1: ペルソナがコア記憶を 1 件刻む ---
    persona = personas["core"]
    from sai_memory.core_memory import add_core_memory
    with persona.adapter._db_lock:
        add_core_memory(persona.adapter.conn, "新しく刻んだコア記憶")
    out["場面"].append({
        "場面": "1. ペルソナがコア記憶を 1 件刻む",
        "見たいこと": "コア記憶を刻んだことが「Memopedia が作成された」として通知されないか",
        "ペルソナへ届く通知": notifications(persona, since),
        "頭のコア記憶の章": persona.head_core_memory(),
    })

    # --- 場面 2: あらすじが 1 件編まれる ---
    persona = personas["chronicle"]
    from sai_memory.arasuji.storage import create_entry
    with persona.adapter._db_lock:
        create_entry(
            persona.adapter.conn, level=1, content="新しいあらすじ",
            source_ids=["m2"], start_time=2000, end_time=2400,
            source_count=1, message_count=1,
        )
    out["場面"].append({
        "場面": "2. あらすじが 1 件編まれる",
        "見たいこと": "あらすじの確定が「Memopedia が作成された」として通知されないか",
        "ペルソナへ届く通知": notifications(persona, since),
    })

    # --- 場面 3: 手帳の通常ページを 1 枚作る (正当な通知) ---
    persona = personas["page"]
    from sai_memory.memopedia.storage import create_page
    with persona.adapter._db_lock:
        create_page(
            persona.adapter.conn, parent_id=None, title="新しい知識ページ",
            summary="", content="本文", category="terms",
        )
    out["場面"].append({
        "場面": "3. 手帳の通常ページを 1 枚作る (これは正当な通知)",
        "見たいこと": "既存の正当な通知が出続けるか",
        "ペルソナへ届く通知": notifications(persona, since),
    })

    # --- 場面 4: 手帳の全削除 ---
    persona = personas["delete_all"]
    response = delete_all_memopedia_pages(persona.persona_id, manager=manager)
    out["場面"].append({
        "場面": "4. 手帳の全削除",
        "見たいこと": "消えたことがペルソナに届くか / 消えた後の頭は何を載せるか",
        "API の応答 (原文)": response,
        "ペルソナへ届く通知": notifications(persona, since),
        "頭のコア記憶の章": persona.head_core_memory(),
        "正規の読み取り": persona.reads(),
    })

    # --- 場面 5: 置き換え取り込み ---
    persona = personas["replace_import"]
    exported = export_memopedia(source.persona_id, manager=manager)
    response = import_memopedia(
        persona.persona_id, body=exported, clear=True, manager=manager,
    )
    out["場面"].append({
        "場面": "5. 置き換え取り込み",
        "見たいこと": "入れ直した分が「全部いま作られた」として一度に届かないか",
        "API の応答 (原文)": response,
        "ペルソナへ届く通知": notifications(persona, since),
        "頭のコア記憶の章": persona.head_core_memory(),
        "正規の読み取り": persona.reads(),
    })

    for persona in personas.values():
        persona.close()
    source.close()
    return out


if __name__ == "__main__":
    result = main()
    path = fx.write_result(__file__, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n結果を書きました: {path}")
