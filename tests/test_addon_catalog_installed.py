"""GET /api/addon-catalog/installed が、表示名を言語ごとの辞書で持つアドオンでも落ちないこと。

2026-10-04、表示名が {"ja": ..., "en": ...} のアドオン (ComfyUI、Stack-chan Vessel v0.5.0) が
1 件でも入っていると、一覧全体が検証エラーで 500 になり、カタログから更新できなかった。
"""

import json

from api.routes import addon_catalog


def _write_manifest(root, addon_id: str, display_name) -> None:
    addon_dir = root / addon_id
    addon_dir.mkdir()
    (addon_dir / "addon.json").write_text(
        json.dumps(
            {
                "name": addon_id,
                "display_name": display_name,
                "version": "1.2.3",
                "manifest_version": 2,
                "setup_version": 4,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_installed_list_accepts_localized_and_plain_display_names(tmp_path, monkeypatch):
    _write_manifest(tmp_path, "addon-localized", {"ja": "日本語の名前", "en": "English name"})
    _write_manifest(tmp_path, "addon-plain", "ただの名前")
    monkeypatch.setattr(addon_catalog, "EXPANSION_DATA_DIR", tmp_path)

    result = {info.addon_id: info for info in addon_catalog.list_installed()}

    assert result["addon-localized"].display_name == "日本語の名前"
    assert result["addon-localized"].version == "1.2.3"
    assert result["addon-localized"].setup_version == 4
    assert result["addon-plain"].display_name == "ただの名前"
