"""document_* スペルのアイテム参照解決を固定する回帰テスト。

2026-07-23 実機で、エアが document_create の戻り値 (生 UUID) を ``item:<uuid>``
の形で document_read に撃ち返し、"Item not found" を連発して作業セッションの
予算を使い切った。原因は二つ:

1. document_read / document_search が ``resolve_item_ref_for_persona`` を通さず
   ``item_service.items`` を直に引いていた (document_edit / item_view /
   item_move / item_annotate は通していた — 参照切替工事からの漏れ)。
2. document_create が世界の表示語彙 (``item:N``) ではなく生 UUID を返していた。

このテストは「ペルソナが受け取る参照が、そのままペルソナの使うスペルに通る」
という往復の契約を押さえる。片側だけ直すと再発するため両方向を見る。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tool_loader import load_builtin_tool

PERSONA_ID = "air_city_a"
UUID_A = "e6164a23-4c43-40de-8ab2-9ff428fa6f29"
SHORT_ID = 404
DOC_TEXT = "1行目\n2行目 ペルモン\n3行目\n"


class FakeItemService:
    """resolve_item_ref の実挙動 (short_id 解決 / UUID 素通し) を写したスタブ。"""

    def __init__(self, doc_path):
        self.items = {
            UUID_A: {
                "item_id": UUID_A,
                "short_id": SHORT_ID,
                "name": "ペルモン設計書",
                "type": "document",
                "file_path": str(doc_path),
            }
        }
        self._doc_path = doc_path

    def resolve_item_ref(self, ref, persona_id, building_id):
        ref = ref.strip()
        if len(ref) == 36 and ref.count("-") == 4:
            return ref
        if not ref.startswith("item:"):
            raise RuntimeError(f"無効なアイテム参照形式: '{ref}' (例: item:3)")
        try:
            short_id = int(ref.split(":", 1)[1])
        except ValueError:
            raise RuntimeError(f"アイテム番号が無効: '{ref}'")
        for item_id, item in self.items.items():
            if item.get("short_id") == short_id:
                return item_id
        raise RuntimeError(f"item:{short_id} に対応するアイテムが見つかりません。")

    def _resolve_file_path(self, file_path_str):
        return self._doc_path


@pytest.fixture
def doc_path(tmp_path):
    path = tmp_path / "permon.md"
    path.write_text(DOC_TEXT, encoding="utf-8")
    return path


@pytest.fixture
def manager(doc_path):
    item_service = FakeItemService(doc_path)
    persona = SimpleNamespace(persona_id=PERSONA_ID, current_building_id="air_city_a_room")

    mgr = SimpleNamespace(item_service=item_service, personas={PERSONA_ID: persona})

    def resolve_item_ref_for_persona(persona_id, ref):
        p = mgr.personas.get(persona_id)
        building_id = getattr(p, "current_building_id", None) if p else None
        return mgr.item_service.resolve_item_ref(ref, persona_id, building_id)

    mgr.resolve_item_ref_for_persona = resolve_item_ref_for_persona
    return mgr


def _ctx(manager, tmp_path):
    from tools.context import persona_context
    return persona_context(PERSONA_ID, tmp_path, manager=manager)


# ---------------------------------------------------------------------------
# document_read — 世界の表示参照 (item:N) と生 UUID の両方が通ること
# ---------------------------------------------------------------------------

def test_document_read_accepts_short_ref(manager, tmp_path):
    mod = load_builtin_tool("document_read")
    with _ctx(manager, tmp_path):
        text = mod.document_read(item_id=f"item:{SHORT_ID}")
    assert "ペルモン" in text


def test_document_read_accepts_raw_uuid(manager, tmp_path):
    mod = load_builtin_tool("document_read")
    with _ctx(manager, tmp_path):
        text = mod.document_read(item_id=UUID_A)
    assert "ペルモン" in text


def test_document_read_unknown_short_ref_reports_ref_not_item_lookup(manager, tmp_path):
    """未知の item:N は解決層のメッセージで落ちる (「見つかりません」)。

    直叩きに戻ると "Item 'item:99' not found." になり、ペルソナには参照形式が
    悪いのか実体が無いのか区別できなくなる。
    """
    mod = load_builtin_tool("document_read")
    with _ctx(manager, tmp_path):
        with pytest.raises(RuntimeError, match="item:99"):
            mod.document_read(item_id="item:99")


# ---------------------------------------------------------------------------
# document_search — 同じ契約
# ---------------------------------------------------------------------------

def test_document_search_accepts_short_ref(manager, tmp_path):
    mod = load_builtin_tool("document_search")
    with _ctx(manager, tmp_path):
        text = mod.document_search(item_id=f"item:{SHORT_ID}", pattern="ペルモン")
    assert "ペルモン" in text


def test_document_search_accepts_raw_uuid(manager, tmp_path):
    mod = load_builtin_tool("document_search")
    with _ctx(manager, tmp_path):
        text = mod.document_search(item_id=UUID_A, pattern="ペルモン")
    assert "ペルモン" in text


# ---------------------------------------------------------------------------
# create → read の往復: create が返した参照がそのまま read に通ること
# ---------------------------------------------------------------------------

def test_create_document_returns_short_ref_usable_by_read(manager, tmp_path, monkeypatch):
    """**実物の** create_document_item が返した参照が document_read にそのまま通る。

    生 UUID を返す実装に戻すと、ペルソナは表示語彙 (item:N) と混ぜて
    ``item:<uuid>`` を組み立てる余地が生まれる — それが実機の事故だった。

    初版はここで戻り値の組み立て式をテスト内に書き写しており、実装が UUID を
    返すよう戻っても通り続けた (2026-07-23 Codex レビュー指摘)。実物の
    ``ItemService.create_document_item`` を呼び、DB 書き込み等の周辺だけを
    差し替えて戻り値そのものを検証する。
    """
    import re

    from manager.items import ItemService

    svc = ItemService.__new__(ItemService)
    svc.items = manager.item_service.items
    svc.item_locations = {}
    svc.items_by_building = {"air_city_a_room": []}

    doc_path = manager.item_service._doc_path
    persona = manager.personas[PERSONA_ID]
    persona.is_proxy = False
    persona.persona_name = "エア"

    class _Session:
        def add(self, *_a, **_k): pass
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass

    svc.manager = SimpleNamespace(
        personas={PERSONA_ID: persona},
        SessionLocal=lambda: _Session(),
        saiverse_home=doc_path.parent,
        building_map={},
        record_persona_event=lambda *a, **k: None,
        _append_building_history_note=lambda *a, **k: None,
    )
    svc._assign_slot = lambda *a, **k: 1
    # UUID_A とは異なる short_id を返す。両方 404 にすると resolve_item_ref の
    # 挿入順一致 (最初に見つかった方=UUID_A) で新規文書への解決漏れが隠れる
    # (2026-07-24 Codex レビュー指摘)。
    NEW_SHORT_ID = SHORT_ID + 1
    svc._short_id_of = lambda item_id: NEW_SHORT_ID if item_id != UUID_A else SHORT_ID
    svc.refresh_building_system_instruction = lambda *a, **k: None

    monkeypatch.setattr(
        "saiverse.media_utils.store_document_text",
        lambda content, source=None: ({}, doc_path),
    )
    monkeypatch.setattr(
        "saiverse.media_summary.ensure_document_summary", lambda path: "要約"
    )

    ids_before = set(svc.items.keys())

    message = svc.create_document_item(
        PERSONA_ID, "ペルモン設計書", "感情パラメータの実装仕様案", DOC_TEXT,
    )

    new_ids = set(svc.items.keys()) - ids_before
    assert len(new_ids) == 1, f"新規作成されたアイテムが一意に特定できること (実際: {new_ids})"
    new_item_id = new_ids.pop()

    match = re.search(r"アイテムID:\s*(\S+)", message)
    assert match, f"戻り値からアイテム参照を拾えること (実際: {message!r})"
    ref = match.group(1)
    assert ref.startswith("item:"), (
        f"表示語彙 item:N で返すこと。生 UUID を返すとペルソナが item:<uuid> を"
        f" 組み立てて読めなくなる (実際: {ref})"
    )

    # 参照が指す先が実際に新規作成されたアイテムであること (UUID_A への取り違えでない)。
    # short_id 衝突があっても、同じ内容のファイルを読むテストだけでは検知できない
    # (2026-07-24 Codex レビュー指摘)。
    resolved_id = manager.resolve_item_ref_for_persona(PERSONA_ID, ref)
    assert resolved_id == new_item_id, (
        f"item:N が新規作成されたアイテムでなく既存アイテムに解決された "
        f"(resolved={resolved_id!r}, expected={new_item_id!r}, UUID_A={UUID_A!r})"
    )

    # 実物が返した参照を、そのまま document_read に渡して読めること (往復)
    mod = load_builtin_tool("document_read")
    with _ctx(manager, tmp_path):
        text = mod.document_read(item_id=ref)
    assert "ペルモン" in text


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
