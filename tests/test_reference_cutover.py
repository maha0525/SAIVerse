"""参照アドレッシング統一の切替後の挙動を固定する回帰テスト。

test_references.py が純粋な書式層 (short ⇄ URI) を押さえるのに対し、こちらは
実解決に近い層を押さえる:

- item: ``item:N`` (安定 short_id) → item_id、UUID 素通し、位置 (``b:N``) 拒否
- item_db_filter: short_id (数字) と UUID の振り分け
- uri_resolver.parse_sai_uri: 平坦化した URI (``memopedia/{id}`` / ``chronicle/{id}``
  / ``message/{id}``) が正しい scheme + path に落ちる。messagelog → message。
"""
from types import SimpleNamespace

import pytest

from manager.items import ItemService, item_db_filter
from saiverse.uri_resolver import parse_sai_uri, PERSONA_RESOURCE_TYPES


# ---------------------------------------------------------------------------
# resolve_item_ref: 同一性は item:N (short_id)、位置 (slot) は使わない (I5)
# ---------------------------------------------------------------------------

def _item_service_with(items):
    svc = ItemService(manager=SimpleNamespace(), state=SimpleNamespace())
    svc.items = items
    return svc


UUID_A = "11111111-2222-3333-4444-555555555555"


def test_resolve_item_ref_by_short_id():
    svc = _item_service_with({UUID_A: {"item_id": UUID_A, "short_id": 5}})
    assert svc.resolve_item_ref("item:5", "air", "bld") == UUID_A


def test_resolve_item_ref_uuid_passthrough():
    svc = _item_service_with({UUID_A: {"item_id": UUID_A, "short_id": 5}})
    assert svc.resolve_item_ref(UUID_A, "air", "bld") == UUID_A


def test_resolve_item_ref_rejects_positional_slot():
    # 旧 b:N / i:N (位置) は同一性参照ではないので拒否する。
    svc = _item_service_with({UUID_A: {"item_id": UUID_A, "short_id": 5}})
    with pytest.raises(RuntimeError):
        svc.resolve_item_ref("b:1", "air", "bld")


def test_resolve_item_ref_unknown_short_id_raises():
    svc = _item_service_with({UUID_A: {"item_id": UUID_A, "short_id": 5}})
    with pytest.raises(RuntimeError):
        svc.resolve_item_ref("item:99", "air", "bld")


def test_item_dict_by_key_short_id_or_uuid():
    svc = _item_service_with({UUID_A: {"item_id": UUID_A, "short_id": 5}})
    assert svc.item_dict_by_key(5)["item_id"] == UUID_A
    assert svc.item_dict_by_key("5")["item_id"] == UUID_A
    assert svc.item_dict_by_key(UUID_A)["item_id"] == UUID_A
    assert svc.item_dict_by_key("999") is None


# ---------------------------------------------------------------------------
# item_db_filter: short_id (数字) は SHORT_ID、それ以外は ITEM_ID
# ---------------------------------------------------------------------------

def test_item_db_filter_digit_targets_short_id():
    clause = item_db_filter("7")
    # SQLAlchemy の比較式。左辺カラムで short_id / item_id を判別する。
    assert clause.left.name == "SHORT_ID"
    assert clause.right.value == 7


def test_item_db_filter_uuid_targets_item_id():
    clause = item_db_filter(UUID_A)
    assert clause.left.name == "ITEM_ID"
    assert clause.right.value == UUID_A


# ---------------------------------------------------------------------------
# parse_sai_uri: 平坦化 + messagelog → message
# ---------------------------------------------------------------------------

def test_message_scheme_is_persona_scoped():
    # messagelog は message へ改名済み。
    assert "message" in PERSONA_RESOURCE_TYPES
    assert "messagelog" not in PERSONA_RESOURCE_TYPES


def test_parse_flattened_memopedia_self():
    p = parse_sai_uri("saiverse://self/memopedia/12", context_persona_id="air_city_a")
    assert p.scheme == "memopedia"
    assert p.path_parts == ["12"]  # 旧 page/ 階層は無い
    assert p.is_persona_scoped


def test_parse_flattened_chronicle_self():
    p = parse_sai_uri("saiverse://self/chronicle/abc-uuid", context_persona_id="air_city_a")
    assert p.scheme == "chronicle"
    assert p.path_parts == ["abc-uuid"]  # 旧 entry/ 階層は無い


def test_parse_flattened_message_self():
    p = parse_sai_uri("saiverse://self/message/msg-uuid", context_persona_id="air_city_a")
    assert p.scheme == "message"
    assert p.path_parts == ["msg-uuid"]


def test_parse_item_uri_is_global():
    p = parse_sai_uri("saiverse://item/5")
    assert p.scheme == "item"
    assert p.path_parts == ["5"]
    assert not p.is_persona_scoped


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
