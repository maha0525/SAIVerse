"""コマ種別カタログ (saiverse/slot_kind_catalog.py) のテスト — 時間割改修 T1。

- builtin セット (builtin_data/slot_kinds/) の 7 種が正しく読み込まれる
- 三層優先 (user_data > expansion_data > builtin_data) と id / name の先勝ち
- 検証: 不正 execution_type / work_session の instruction_template 欠落の拒否
- day_plan.reload_kind_vocabulary: カタログ変更が kind 語彙・ハンドラ配線へ届く

3 層のディレクトリはテスト内で tmp_path に組み、slot_kind_catalog のモジュール
グローバル (USER_DATA_DIR 等) を差し替えて読ませる。teardown で必ず実配置へ
戻して reload する (他テストが見る語彙を汚さない)。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from saiverse import day_plan, slot_kind_catalog


def _write_kind(dir_path: Path, filename: str, **fields) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / filename).write_text(
        json.dumps(fields, ensure_ascii=False), encoding="utf-8",
    )


def _valid_kind(kind_id: str, name: str, **over) -> dict:
    base = {
        "id": kind_id,
        "name": name,
        "execution_type": "stay_home",
        "description": f"{name} のテスト定義",
    }
    base.update(over)
    return base


@contextmanager
def _catalog_dirs(user: Path, expansion: Path, builtin: Path):
    """slot_kind_catalog の 3 層ディレクトリを一時差し替えする。

    exit で実配置へ戻し、カタログと day_plan の kind 語彙を必ず再構築する。
    """
    saved = (
        slot_kind_catalog.USER_DATA_DIR,
        slot_kind_catalog.EXPANSION_DATA_DIR,
        slot_kind_catalog.BUILTIN_DATA_DIR,
    )
    try:
        slot_kind_catalog.USER_DATA_DIR = user
        slot_kind_catalog.EXPANSION_DATA_DIR = expansion
        slot_kind_catalog.BUILTIN_DATA_DIR = builtin
        slot_kind_catalog.reload_catalog()
        yield
    finally:
        (
            slot_kind_catalog.USER_DATA_DIR,
            slot_kind_catalog.EXPANSION_DATA_DIR,
            slot_kind_catalog.BUILTIN_DATA_DIR,
        ) = saved
        day_plan.reload_kind_vocabulary()


# ---------------------------------------------------------------------------
# builtin セット
# ---------------------------------------------------------------------------


def test_builtin_catalog_has_the_seven_initial_kinds(tmp_path):
    """builtin 初期セット (intent §5.5 のまはー確定リスト) がリポジトリ実配置から読める。

    user_data / expansion_data は空の tmp に差し替える — 実環境に override が
    置かれていてもこのテストは builtin セットそのものを検証する。
    """
    repo_builtin = slot_kind_catalog.BUILTIN_DATA_DIR
    with _catalog_dirs(tmp_path / "u", tmp_path / "e", repo_builtin):
        catalog = slot_kind_catalog.SLOT_KIND_CATALOG
        by_name = {d["name"]: d for d in catalog.values()}
        expected = {
            "調べる": "work_session",
            "絵を描く": "work_session",
            "日記を書く": "work_session",
            "随筆を書く": "work_session",
            "出かける": "outing",
            "自室で過ごす": "stay_home",
            "自由時間": "free_choice",
        }
        assert len(catalog) == len(expected)
        for name, execution_type in expected.items():
            assert name in by_name, f"builtin kind {name!r} missing"
            assert by_name[name]["execution_type"] == execution_type
            assert by_name[name]["builtin"] is True
            assert by_name[name]["description"].strip()

        # work_session 4 種は指示書テンプレート必須 + {note}/{target} 契約 +
        # 接地文言 (「実際にやったこと以外を書かない」系) を含む
        templates = slot_kind_catalog.instruction_templates()
        assert set(templates) == {"調べる", "絵を描く", "日記を書く", "随筆を書く"}
        for name, template in templates.items():
            assert "{note}" in template and "{target}" in template, name
            assert "実際に" in template, name


def test_kind_names_are_deterministic_and_lookup_works(tmp_path):
    repo_builtin = slot_kind_catalog.BUILTIN_DATA_DIR
    with _catalog_dirs(tmp_path / "u", tmp_path / "e", repo_builtin):
        names = slot_kind_catalog.kind_names()
        assert names == tuple(
            d["name"] for d in slot_kind_catalog.SLOT_KIND_CATALOG.values()
        )
        assert slot_kind_catalog.get_kind("research")["name"] == "調べる"
        assert slot_kind_catalog.get_kind_by_name("調べる")["id"] == "research"
        assert slot_kind_catalog.get_kind("no_such_kind") is None
        assert slot_kind_catalog.get_kind_by_name("そんな種別は無い") is None
        assert slot_kind_catalog.is_builtin("research") is True
        assert slot_kind_catalog.is_builtin("no_such_kind") is False


# ---------------------------------------------------------------------------
# 三層優先と先勝ち
# ---------------------------------------------------------------------------


def test_three_layer_priority_user_wins(tmp_path):
    user = tmp_path / "user_data"
    expansion = tmp_path / "expansion_data"
    builtin = tmp_path / "builtin_data"
    _write_kind(
        builtin / "slot_kinds", "10_research.json",
        **_valid_kind("research", "調べる", description="builtin 版"),
    )
    _write_kind(
        expansion / "pack_a" / "slot_kinds", "10_research.json",
        **_valid_kind("research", "調べる", description="expansion 版"),
    )
    _write_kind(
        expansion / "pack_a" / "slot_kinds", "20_post.json",
        **_valid_kind("post", "投稿する", description="アドオン追加種別"),
    )
    _write_kind(
        user / "slot_kinds", "my_research.json",
        **_valid_kind("research", "調べる", description="user 版"),
    )

    with _catalog_dirs(user, expansion, builtin):
        catalog = slot_kind_catalog.SLOT_KIND_CATALOG
        assert set(catalog) == {"research", "post"}
        # id の先勝ち: user > expansion > builtin
        assert catalog["research"]["description"] == "user 版"
        # user_data 由来の override は builtin 扱いにならない
        assert not catalog["research"].get("builtin")
        assert slot_kind_catalog.is_builtin("research") is False
        # アドオン追加種別はそのまま現れる
        assert catalog["post"]["name"] == "投稿する"


def test_name_duplicate_is_first_win(tmp_path):
    builtin = tmp_path / "builtin_data"
    _write_kind(
        builtin / "slot_kinds", "10_research.json",
        **_valid_kind("research", "調べる", description="先に読まれる方"),
    )
    # id は違うが name が衝突する定義 — 後から読まれる方が捨てられる
    _write_kind(
        builtin / "slot_kinds", "90_dup.json",
        **_valid_kind("research2", "調べる", description="後から読まれる方"),
    )

    with _catalog_dirs(tmp_path / "u", tmp_path / "e", builtin):
        catalog = slot_kind_catalog.SLOT_KIND_CATALOG
        assert set(catalog) == {"research"}
        assert catalog["research"]["description"] == "先に読まれる方"


# ---------------------------------------------------------------------------
# 検証 (不正定義の拒否)
# ---------------------------------------------------------------------------


def test_invalid_definitions_are_skipped_not_fatal(tmp_path):
    builtin = tmp_path / "builtin_data"
    _write_kind(
        builtin / "slot_kinds", "10_ok.json",
        **_valid_kind("ok_kind", "正しい種別"),
    )
    # execution_type が語彙外
    _write_kind(
        builtin / "slot_kinds", "20_bad_type.json",
        **_valid_kind("bad_type", "型が不正", execution_type="teleport"),
    )
    # work_session なのに instruction_template が無い
    _write_kind(
        builtin / "slot_kinds", "30_no_template.json",
        **_valid_kind("no_template", "指示書なし", execution_type="work_session"),
    )
    # id が不正 (パス安全でない)
    _write_kind(
        builtin / "slot_kinds", "40_bad_id.json",
        **_valid_kind("bad/id", "不正なID"),
    )
    # name が空
    _write_kind(
        builtin / "slot_kinds", "50_no_name.json",
        **_valid_kind("no_name", "  "),
    )
    # JSON として壊れている
    (builtin / "slot_kinds" / "60_broken.json").write_text(
        "{not json", encoding="utf-8",
    )

    with _catalog_dirs(tmp_path / "u", tmp_path / "e", builtin):
        # 壊れた定義たちはカタログ全体を落とさず、正しい 1 件だけが残る
        assert set(slot_kind_catalog.SLOT_KIND_CATALOG) == {"ok_kind"}


def test_broken_upper_layer_file_falls_back_to_builtin(tmp_path):
    """壊れた上位層の同名ファイルは builtin の正常定義を隠さない (Codex三巡目)。

    ファイル名の縄張り (層間シャドーイング) を検証より先に主張すると、user
    層の部分書き込み・設定ミス 1 枚で builtin kind が消え、既存テンプレートや
    穴埋め経路が壊れる。隠してよいのは検証に通った定義だけ。
    """
    builtin = tmp_path / "builtin_data"
    user = tmp_path / "user_data"
    _write_kind(
        builtin / "slot_kinds", "10_kind.json",
        **_valid_kind("builtin_kind", "組み込みの種別"),
    )
    # user 層に同名ファイル — JSON として壊れている
    (user / "slot_kinds").mkdir(parents=True, exist_ok=True)
    (user / "slot_kinds" / "10_kind.json").write_text("{not json", encoding="utf-8")

    with _catalog_dirs(user, tmp_path / "e", builtin):
        assert set(slot_kind_catalog.SLOT_KIND_CATALOG) == {"builtin_kind"}

    # 正常な上位定義は従来どおり下位を隠す (シャドーイング自体の回帰)
    _write_kind(
        user / "slot_kinds", "10_kind.json",
        **_valid_kind("user_kind", "ユーザーの種別"),
    )
    with _catalog_dirs(user, tmp_path / "e", builtin):
        assert set(slot_kind_catalog.SLOT_KIND_CATALOG) == {"user_kind"}


# ---------------------------------------------------------------------------
# day_plan との統合 (kind 語彙とハンドラ配線の reload)
# ---------------------------------------------------------------------------


def test_reload_kind_vocabulary_rewires_day_plan(tmp_path):
    builtin = tmp_path / "builtin_data"
    _write_kind(
        builtin / "slot_kinds", "10_worker.json",
        **_valid_kind(
            "worker", "試験作業", execution_type="work_session",
            instruction_template="目的: {note}。対象: {target}。テスト用。",
        ),
    )
    _write_kind(
        builtin / "slot_kinds", "20_walk.json",
        **_valid_kind("walk", "試験散歩", execution_type="outing"),
    )

    original_kinds = day_plan.ALL_KINDS
    with _catalog_dirs(tmp_path / "u", tmp_path / "e", builtin):
        day_plan.reload_kind_vocabulary()
        assert day_plan.ALL_KINDS == ("試験作業", "試験散歩")
        assert day_plan.WORKER_SESSION_KINDS == ("試験作業",)
        assert day_plan.all_kinds() == day_plan.ALL_KINDS
        assert day_plan.worker_session_kinds() == day_plan.WORKER_SESSION_KINDS
        # ハンドラ配線: 作業系は予算ゲート対象、スタブは対象外
        assert "試験作業" in day_plan._SLOT_HANDLERS
        assert "試験作業" in day_plan._BUDGET_GATED_KINDS
        assert "試験散歩" in day_plan._SLOT_HANDLERS
        assert "試験散歩" not in day_plan._BUDGET_GATED_KINDS
        # 旧語彙のハンドラは掃除されている
        for stale in original_kinds:
            assert stale not in day_plan._SLOT_HANDLERS
        # テンプレートは同期 (旧 assert の新構成)
        assert set(day_plan._WORKER_INSTRUCTION_TEMPLATES) == {"試験作業"}

    # teardown (実配置への復帰) で builtin 7 種が戻っている
    assert day_plan.ALL_KINDS == original_kinds
    for kind in day_plan.ALL_KINDS:
        assert kind in day_plan._SLOT_HANDLERS
