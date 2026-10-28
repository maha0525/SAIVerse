"""playbook_sync (起動時プレイブック自動同期) の回帰テスト。

対象は「提供元の層の移動」(例: builtin → addon 切り出し) の扱い:
内容が同一 (ハッシュ一致) のままファイルの置き場所だけが変わった場合、
source_file を新しい提供元へ付け替えないと、直後の orphan prune が
「提供元あり」の Playbook を孤児と誤認して削除し、PlaybookPermission も
道連れになる (permission は再 import では戻らない)。

2026-08-01 ComfyUI アドオン切り出しの Codex レビュー指摘で発覚。
"""
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, Playbook, PlaybookPermission
from saiverse import playbook_sync
from saiverse.playbook_sync import _canonical_hash, sync_playbooks_from_files


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


def _playbook_data(name):
    return {
        "name": name,
        "description": "d",
        "start_node": "n1",
        "nodes": [{"id": "n1", "type": "memorize", "action": "x", "next": None}],
    }


def _insert_playbook(session_factory, name, source_file, source_hash):
    db = session_factory()
    try:
        data = _playbook_data(name)
        db.add(Playbook(
            name=name,
            scope="public",
            schema_json=json.dumps({"name": name}),
            nodes_json=json.dumps(data),
            source_file=source_file,
            source_hash=source_hash,
        ))
        db.add(PlaybookPermission(CITYID=1, playbook_name=name))
        db.commit()
    finally:
        db.close()


def test_layer_move_repoints_source_file_and_survives_prune(
    session_factory, tmp_path, monkeypatch
):
    """内容同一のままの層移動で、Playbook と Permission が prune されないこと。"""
    name = "pb_moved"
    data = _playbook_data(name)
    digest = _canonical_hash(data)

    # 新しい提供元 (addon 側) は実在するファイル
    new_src = tmp_path / f"{name}_playbook.json"
    new_src.write_text(json.dumps(data), encoding="utf-8")

    # DB の記録は消滅済みの旧 builtin パス + 同一ハッシュ
    _insert_playbook(
        session_factory, name,
        source_file="builtin_data/playbooks/public/pb_moved_playbook.json",
        source_hash=digest,
    )

    monkeypatch.setattr(playbook_sync, "_collect_file_playbooks", lambda: {
        name: {
            "path": new_src,
            "data": data,
            "source_rel": str(new_src),
            "hash": digest,
        },
    })

    counts = sync_playbooks_from_files(session_factory)

    assert counts["pruned"] == 0
    assert counts["errors"] == 0
    db = session_factory()
    try:
        pb = db.query(Playbook).filter(Playbook.name == name).first()
        assert pb is not None, "層移動した Playbook が prune で消えてはならない"
        assert pb.source_file == str(new_src)
        perm = (
            db.query(PlaybookPermission)
            .filter(PlaybookPermission.playbook_name == name)
            .first()
        )
        assert perm is not None, "PlaybookPermission が道連れ削除されてはならない"
    finally:
        db.close()


def test_genuine_orphan_is_still_pruned(session_factory, monkeypatch):
    """どの層にも提供元が無い Playbook は従来どおり prune されること。"""
    name = "pb_gone"
    _insert_playbook(
        session_factory, name,
        source_file="builtin_data/playbooks/public/pb_gone_playbook.json",
        source_hash="deadbeefdeadbeef",
    )

    monkeypatch.setattr(playbook_sync, "_collect_file_playbooks", lambda: {})

    counts = sync_playbooks_from_files(session_factory)

    assert counts["pruned"] == 1
    db = session_factory()
    try:
        assert db.query(Playbook).filter(Playbook.name == name).first() is None
    finally:
        db.close()


def _i18n_playbook_data(name):
    data = _playbook_data(name)
    data["input_schema"] = []
    data["display_name"] = {"ja": "ローカル画像生成", "en": "Local Image Generation"}
    data["description"] = {"ja": "説明", "en": "Description"}
    return data


def _patch_single_file(monkeypatch, tmp_path, name, data):
    src = tmp_path / f"{name}_playbook.json"
    src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(playbook_sync, "_collect_file_playbooks", lambda: {
        name: {
            "path": src,
            "data": data,
            "source_rel": str(src),
            "hash": _canonical_hash(data),
        },
    })


def _assert_i18n_columns(session_factory, name):
    db = session_factory()
    try:
        pb = db.query(Playbook).filter(Playbook.name == name).first()
        assert pb is not None
        assert pb.display_name == "ローカル画像生成"
        assert pb.display_name_en == "Local Image Generation"
        assert pb.description == "説明"
        assert pb.description_en == "Description"
    finally:
        db.close()


def test_i18n_dict_fields_import_into_string_columns(
    session_factory, tmp_path, monkeypatch
):
    """言語辞書形の display_name / description を新規取り込みできること。

    DB 列は文字列なので、辞書をそのまま渡すと SQLite の bind で落ちる
    (2026-09-25、ComfyUI アドオンの generate_image_local で毎起動エラー)。
    日本語を本体の列、英語を ``_en`` 列へ分けて書く。
    """
    name = "pb_i18n_new"
    _patch_single_file(monkeypatch, tmp_path, name, _i18n_playbook_data(name))

    counts = sync_playbooks_from_files(session_factory)

    assert counts["errors"] == 0
    assert counts["imported"] == 1
    _assert_i18n_columns(session_factory, name)


def test_i18n_dict_fields_update_existing_row(session_factory, tmp_path, monkeypatch):
    """既存行のハッシュ変化による更新でも、言語辞書が分解されて書かれること。"""
    name = "pb_i18n_changed"
    _insert_playbook(
        session_factory, name,
        source_file="builtin_data/playbooks/public/pb_i18n_changed_playbook.json",
        source_hash="0000000000000000",
    )
    _patch_single_file(monkeypatch, tmp_path, name, _i18n_playbook_data(name))

    counts = sync_playbooks_from_files(session_factory)

    assert counts["errors"] == 0
    assert counts["updated"] == 1
    _assert_i18n_columns(session_factory, name)


def test_save_playbook_accepts_i18n_dict_fields(tmp_path, monkeypatch):
    """save_playbook (import_playbook.py の実体) も言語辞書を分解して書くこと。"""
    from builtin_data.tools import save_playbook as save_mod

    db_file = tmp_path / "saiverse.db"
    monkeypatch.setattr(save_mod, "default_db_path", lambda: db_file)

    name = "pb_i18n_saved"
    data = _i18n_playbook_data(name)
    # import_playbook.py と同じく、JSON の description (辞書) をそのまま渡す
    save_mod.save_playbook(
        name=name,
        description=data["description"],
        scope="public",
        playbook_json=json.dumps(data, ensure_ascii=False),
    )

    engine = create_engine(f"sqlite:///{db_file}")
    try:
        _assert_i18n_columns(sessionmaker(bind=engine), name)
    finally:
        engine.dispose()


def test_hash_change_updates_content(session_factory, tmp_path, monkeypatch):
    """ハッシュが変わった場合は従来どおり内容ごと更新されること (退行確認)。"""
    name = "pb_changed"
    old_data = _playbook_data(name)
    _insert_playbook(
        session_factory, name,
        source_file="builtin_data/playbooks/public/pb_changed_playbook.json",
        source_hash=_canonical_hash(old_data),
    )

    new_data = _playbook_data(name)
    new_data["description"] = "changed"
    new_src = tmp_path / f"{name}_playbook.json"
    new_src.write_text(json.dumps(new_data), encoding="utf-8")

    monkeypatch.setattr(playbook_sync, "_collect_file_playbooks", lambda: {
        name: {
            "path": new_src,
            "data": new_data,
            "source_rel": str(new_src),
            "hash": _canonical_hash(new_data),
        },
    })

    counts = sync_playbooks_from_files(session_factory)

    assert counts["updated"] == 1
    db = session_factory()
    try:
        pb = db.query(Playbook).filter(Playbook.name == name).first()
        assert pb.description == "changed"
        assert pb.source_file == str(new_src)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 利用者の手で更新が要る Playbook の警告 (docs/intent/system_notice_delivery.md)
# ---------------------------------------------------------------------------


def _add_rows(session_factory, playbooks):
    db = session_factory()
    try:
        for name, nodes in playbooks.items():
            db.add(Playbook(
                name=name, scope="public",
                schema_json=json.dumps({"name": name}),
                nodes_json=json.dumps({"name": name, "start_node": "n1", "nodes": nodes}),
            ))
        db.commit()
    finally:
        db.close()


def test_compat_alerts_flag_legacy_notices_and_missing_tools(session_factory, monkeypatch):
    from saiverse.playbook_sync import playbook_compat_alerts
    import tools

    monkeypatch.setitem(tools.TOOL_REGISTRY, "known_tool", lambda: None)
    monkeypatch.setitem(tools.TOOL_REGISTRY, "myaddon__addon_tool", lambda: None)
    _add_rows(session_factory, {
        "tagged_memorize": [
            {"id": "n1", "type": "memorize", "action": "<system>{last}</system>", "role": "user"},
        ],
        "user_role_tool_result": [
            {"id": "n1", "type": "tool", "action": "known_tool", "output_key": "found"},
            {"id": "n2", "type": "memorize", "action": "結果: {found.title}", "role": "user"},
        ],
        "missing_tool": [
            {"id": "n1", "type": "tool", "action": "get_situation_snapshot"},
            {"id": "n2", "type": "tool", "action": "addon_tool"},  # 素名でアドオンのツールに解決
        ],
        "new_form": [
            {"id": "n1", "type": "tool", "action": "known_tool"},
            {"id": "n2", "type": "memorize", "action": "{last}", "role": "system"},
            # ユーザーの入力を user で記録するのは旧形式ではない
            {"id": "n3", "type": "memorize", "action": "{input}", "role": "user"},
            {"id": "n4", "type": "llm", "action": "<system_prompt> は別の語"},
        ],
    })
    before = {
        pb.name: pb.nodes_json for pb in session_factory().query(Playbook).all()
    }

    alerts = {a["id"]: a for a in playbook_compat_alerts(session_factory)}

    assert alerts["playbook_legacy_system_notice"]["details"]["playbooks"] == [
        "tagged_memorize", "user_role_tool_result",
    ]
    assert alerts["playbook_missing_tool"]["details"]["references"] == [
        {"playbook": "missing_tool", "tool": "get_situation_snapshot"},
    ]
    assert "missing_tool → get_situation_snapshot" in alerts["playbook_missing_tool"]["message"]
    # 自動では書き換えない
    assert {pb.name: pb.nodes_json for pb in session_factory().query(Playbook).all()} == before


def test_memorize_with_omitted_action_is_judged_with_the_runtime_default():
    """action の省略・空文字は実行側と同じく ``{last}`` として判定する (Codex 3 巡目)。"""
    from saiverse.playbook_sync import _creates_legacy_system_notice

    tool = {"id": "n1", "type": "tool", "action": "known_tool"}
    for memorize in (
        {"id": "n2", "type": "memorize", "role": "user"},
        {"id": "n2", "type": "memorize", "role": "user", "action": ""},
        {"id": "n2", "type": "memorize", "role": "user", "action": None},
    ):
        assert _creates_legacy_system_notice([tool, memorize]), memorize
    # role=system なら新しい形 (action を省略しても同じ)
    assert not _creates_legacy_system_notice(
        [tool, {"id": "n2", "type": "memorize", "role": "system"}],
    )


def test_memorize_of_last_without_any_tool_node_is_not_legacy():
    """TOOL / TOOL_CALL ノードの無い Playbook の ``{last}`` はツールの結果ではない (Codex 4 巡目)。

    SET でユーザーの入力を ``last`` に入れて role=user で記録するのは正常な形。
    """
    from saiverse.playbook_sync import _creates_legacy_system_notice

    set_node = {"id": "n1", "type": "set", "assignments": {"last": "{input}"}}
    for memorize in (
        {"id": "n2", "type": "memorize", "role": "user", "action": "{last}"},
        {"id": "n2", "type": "memorize", "role": "user"},
    ):
        assert not _creates_legacy_system_notice([set_node, memorize]), memorize
    # TOOL_CALL ノードが一つでもあれば、{last} はツールの結果でありうる
    tool_call = {"id": "n0", "type": "tool_call", "call_source": "fc"}
    assert _creates_legacy_system_notice(
        [tool_call, set_node, {"id": "n2", "type": "memorize", "role": "user"}],
    )


def test_compat_alerts_separate_tools_of_unconnected_mcp_servers(session_factory, monkeypatch):
    """設定にある MCP サーバーの名前で始まる参照は、未接続の可能性として別の警告に出す。"""
    from saiverse.playbook_sync import playbook_compat_alerts
    import tools.mcp_config

    monkeypatch.setattr(
        tools.mcp_config, "load_mcp_configs",
        lambda: {"weather": {}, "someaddon__elyth": {}},
    )
    _add_rows(session_factory, {
        "uses_mcp": [
            {"id": "n1", "type": "tool", "action": "weather__forecast"},
            {"id": "n2", "type": "tool", "action": "someaddon__elyth__post"},
        ],
        "uses_gone_tool": [
            {"id": "n1", "type": "tool", "action": "unknown_server__tool"},
            {"id": "n2", "type": "tool", "action": "forecast"},  # 素の名前は見分けられない
        ],
    })

    alerts = {a["id"]: a for a in playbook_compat_alerts(session_factory)}

    assert alerts["playbook_missing_mcp_tool"]["details"]["references"] == [
        {"playbook": "uses_mcp", "tool": "someaddon__elyth__post"},
        {"playbook": "uses_mcp", "tool": "weather__forecast"},
    ]
    assert "MCP サーバーが未接続なだけの可能性" in alerts["playbook_missing_mcp_tool"]["message"]
    assert alerts["playbook_missing_tool"]["details"]["references"] == [
        {"playbook": "uses_gone_tool", "tool": "forecast"},
        {"playbook": "uses_gone_tool", "tool": "unknown_server__tool"},
    ]


def test_compat_alerts_skip_tools_of_per_persona_mcp_servers(session_factory, monkeypatch):
    """ペルソナごとに接続するサーバーのツールは起動時に無いのが正常なので、どちらの警告にも出さない。"""
    from saiverse.playbook_sync import playbook_compat_alerts
    import tools.mcp_config

    monkeypatch.setattr(
        tools.mcp_config, "load_mcp_configs",
        lambda: {
            "weather": {"scope": "global"},
            "someaddon__elyth": {"scope": "Per_Persona"},  # 起動処理と同じく小文字で読む
            # 名前が前方で重なるときは長い方のサーバーの種別で判定する
            "someaddon": {},
        },
    )
    _add_rows(session_factory, {
        "uses_mcp": [
            {"id": "n1", "type": "tool", "action": "weather__forecast"},
            {"id": "n2", "type": "tool", "action": "someaddon__elyth__post"},
            {"id": "n3", "type": "tool", "action": "someaddon__other"},
        ],
    })

    alerts = {a["id"]: a for a in playbook_compat_alerts(session_factory)}

    assert alerts["playbook_missing_mcp_tool"]["details"]["references"] == [
        {"playbook": "uses_mcp", "tool": "someaddon__other"},
        {"playbook": "uses_mcp", "tool": "weather__forecast"},
    ]
    assert "playbook_missing_tool" not in alerts


def test_compat_alerts_are_quiet_when_only_per_persona_mcp_tools_are_missing(session_factory, monkeypatch):
    from saiverse.playbook_sync import playbook_compat_alerts
    import tools.mcp_config

    monkeypatch.setattr(
        tools.mcp_config, "load_mcp_configs",
        lambda: {"someaddon__elyth": {"scope": "per_persona"}},
    )
    _add_rows(session_factory, {
        "uses_mcp": [{"id": "n1", "type": "tool", "action": "someaddon__elyth__post"}],
    })

    assert playbook_compat_alerts(session_factory) == []


def test_compat_alerts_are_quiet_for_the_builtin_playbooks(session_factory):
    """同梱の public Playbook は、旧形式も存在しないツールの参照も持たない。"""
    from saiverse.playbook_sync import _creates_legacy_system_notice
    from tools import TOOL_REGISTRY, canonicalize_tool_name

    builtin = Path(__file__).resolve().parent.parent / "builtin_data" / "playbooks" / "public"
    for path in sorted(builtin.glob("*.json")):
        nodes = json.loads(path.read_text(encoding="utf-8")).get("nodes") or []
        assert not _creates_legacy_system_notice(nodes), path.name
        for node in nodes:
            if node.get("type") == "tool":
                assert canonicalize_tool_name(node["action"]) in TOOL_REGISTRY, (
                    path.name, node["action"],
                )
