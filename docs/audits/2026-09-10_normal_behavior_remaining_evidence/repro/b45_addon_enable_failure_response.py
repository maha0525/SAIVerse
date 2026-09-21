"""アドオンの有効化で登録が全部失敗したとき、API が何を返し、DB に何が残るかを実測する。

## 何を確かめるものか

`api/routes/addon.py:427` の ``set_addon_enabled`` は、DB へ ``is_enabled`` を
書いてコミットしたあとで 4 つの登録 (MCP / Integration / server_hook /
composite action) を順に呼ぶ。4 つとも ``try/except`` で囲まれ、失敗しても
``LOGGER.warning`` だけで先へ進む。

このスクリプトは、その 4 つを**全部例外にした状態で**ルート関数を直接呼び、

1. HTTP としては何が返るか (例外にならず 200 相当の dict か)
2. 返る本文に失敗の手がかりが含まれるか
3. DB の ``AddonConfig.is_enabled`` が「有効」のまま残るか

を観測する。SAIVerse のバックエンド・LLM・本番ペルソナには一切触れない。
DB は ``SAIVERSE_HOME`` を一時ディレクトリへ向けて作った空の SQLite。

## どう実行するか

    C:/Users/shuhe/workspace/SAIVerse/.venv/Scripts/python.exe \
        docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/\
b45_addon_enable_failure_response.py

## 何が観測されたか (2026-09-10)

- 4 つの登録がすべて例外を投げても、``set_addon_enabled`` は例外を出さずに
  ``{'addon_name': 'synthetic-addon', 'is_enabled': True, 'mcp_settled': None}``
  を返した。**失敗の件数も種別も本文に載らない。**
- ``mcp_settled`` は有効化では常に ``None`` (待たない設計)。つまり有効化では
  この値からも成否は読めない。
- DB の ``AddonConfig.is_enabled`` は ``True`` のまま残った。次の起動でも
  「有効」として扱われる。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]


def main() -> int:
    tmp_home = Path(tempfile.mkdtemp(prefix="b45_saiverse_home_"))
    os.environ["SAIVERSE_HOME"] = str(tmp_home)
    os.environ.pop("SAIVERSE_USER_DATA_DIR", None)
    sys.path.insert(0, str(REPO_ROOT))

    from database.models import AddonConfig, Base
    from database.session import engine
    import database.session as db_session

    print(f"隔離 DB: {engine.url}")
    Base.metadata.create_all(engine)

    import api.routes.addon as addon_routes

    addon_name = "synthetic-addon"
    addon_dir = tmp_home / "expansion_data" / addon_name
    addon_dir.mkdir(parents=True, exist_ok=True)

    # 実在ディレクトリ判定だけを合成アドオンへ向ける (製品コードは書き換えない)。
    addon_routes._get_expansion_data_dir = lambda: tmp_home / "expansion_data"

    # 4 つの登録をすべて失敗させる。呼ばれた事実も数えておく。
    called: list[str] = []

    def _boom(label: str):
        def _raise(*_args, **_kwargs):
            called.append(label)
            raise RuntimeError(f"synthetic failure in {label}")
        return _raise

    import tools.mcp_client as mcp_client
    import saiverse.addon_loader as addon_loader
    import saiverse.composite_actions as composite_actions

    mcp_client.notify_addon_toggled_sync = _boom("mcp")
    addon_loader.register_addon_integrations = _boom("integration")
    addon_loader.register_addon_server_hooks = _boom("server_hook")
    composite_actions.register_action_spells = _boom("composite_action")

    class _FakeManager:
        """integration_manager だけを持つ最小の manager 代役。"""
        integration_manager = object()

    body = addon_routes.SetEnabledRequest(is_enabled=True)
    result = addon_routes.set_addon_enabled(addon_name, body, manager=_FakeManager())

    print(f"呼ばれて失敗した登録: {called}")
    print(f"ルートの戻り値: {result}")
    print(f"  本文に失敗の手がかりがあるか: "
          f"{any(k not in {'addon_name', 'is_enabled', 'mcp_settled'} for k in result)}")

    db = db_session.SessionLocal()
    try:
        row = db.query(AddonConfig).filter(AddonConfig.addon_name == addon_name).first()
        print(f"DB に残った is_enabled: {None if row is None else row.is_enabled}")
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
