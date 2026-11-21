"""main.py が uvicorn に渡す ``timeout_keep_alive`` を固定する。

uvicorn の既定値 (5 秒) のままだと、Next.js の中継が再利用している接続を、
イベントループが遅れている最中に uvicorn がリクエストを読まないまま閉じることが
あり、リクエストが失われて画面に 500 が出る
(docs/intent/backend_stall_handling.md の「仕組み 1」)。

``main.main()`` は起動処理そのものなので、呼ばずにソースを構文木として読む。
"""

from __future__ import annotations

import ast
from pathlib import Path

MIN_KEEP_ALIVE_TIMEOUT_SEC = 3600


def _uvicorn_run_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "uvicorn"
        ):
            calls.append(node)
    return calls


def test_main_passes_long_keep_alive_timeout_to_uvicorn() -> None:
    # main.py は先頭に BOM を持つので、utf-8-sig で読んで取り除く。
    source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8-sig")
    calls = _uvicorn_run_calls(ast.parse(source))
    assert len(calls) == 1, "main.py には uvicorn.run の呼び出しが一つだけある想定"

    keywords = {kw.arg: kw.value for kw in calls[0].keywords}
    assert "timeout_keep_alive" in keywords, (
        "uvicorn.run に timeout_keep_alive が無いと、既定値の 5 秒に戻る"
    )
    value = keywords["timeout_keep_alive"]
    assert isinstance(value, ast.Constant) and isinstance(value.value, int)
    assert value.value >= MIN_KEEP_ALIVE_TIMEOUT_SEC
