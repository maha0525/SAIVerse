"""update_default_model が、AdminService が起動時に写した標準モデルも揃えること。

update_default_model (saiverse/saiverse_manager.py) は、個別の標準モデルを持たない
ペルソナの標準モデル (``_base_model``) を切り替える。いまこれを呼ぶのはチュートリアルの
自動設定 (api/routes/tutorial.py の auto_configure_models) だけ。

AdminService は起動時に ``_base_model`` を写して持ち (manager/admin.py の __init__)、
ワールドエディタから作るペルソナの標準モデルに使う (manager/admin.py の create_ai が
呼ぶ manager/persona.py の _create_persona)。update_default_model がその写しも揃えないと、
標準モデルを変えた後に作ったペルソナだけが、再起動まで古いモデルで動く。

グローバル設定の保存 (POST /api/admin/env) は update_default_model を呼ばない。保存した
標準モデルがまだ動いているペルソナに反映されていないことは、モデル設定の警告が知らせる
(tests/test_missing_model_startup_warnings.py)。

SAIVerseManager の実体は作らず、update_default_model が読む属性だけを持つ偽物に載せて
呼ぶ。LLM は呼ばない。
"""
from __future__ import annotations

from types import SimpleNamespace

NEW_MODEL = "test-new-default-model"


class _Session:
    def close(self):
        pass


def test_update_default_model_also_updates_the_admin_service_copy():
    from saiverse.saiverse_manager import SAIVerseManager

    host = SimpleNamespace(
        _base_model="old-model",
        admin=SimpleNamespace(_base_model="old-model"),
        personas={},
        SessionLocal=_Session,
    )

    SAIVerseManager.update_default_model(host, NEW_MODEL)

    assert host._base_model == NEW_MODEL
    assert host.admin._base_model == NEW_MODEL
