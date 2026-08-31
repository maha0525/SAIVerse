"""参照アドレッシング統一グラマー層 (saiverse/references.py) のテスト。

書式と相互変換 (短縮参照 ⇄ URI) の不変条件を固定する。DB は絡まない純粋な文字列
レイヤーなので mock 不要。
"""
import pytest

from saiverse import references as R


# ---- to_short_ref ----

def test_short_ref_persona_scoped():
    assert R.to_short_ref("track", 2) == "track:2"
    assert R.to_short_ref("task", 5) == "task:5"


def test_short_ref_global_and_uuid():
    assert R.to_short_ref("item", 5) == "item:5"
    assert R.to_short_ref("message", "abc-uuid") == "message:abc-uuid"


def test_short_ref_unknown_kind_raises():
    with pytest.raises(ValueError):
        R.to_short_ref("desire", 2)  # desire は task に統合済み (kind として無い)


# ---- to_uri ----

def test_uri_persona_scoped_self_default():
    assert R.to_uri("track", 2) == "saiverse://self/track/2"


def test_uri_persona_scoped_explicit_owner():
    assert R.to_uri("track", 2, persona="city_a/air") == "saiverse://city_a/air/track/2"


def test_uri_global_kind_ignores_persona():
    assert R.to_uri("item", 5) == "saiverse://item/5"
    assert R.to_uri("item", 5, persona="city_a/air") == "saiverse://item/5"


def test_uri_with_subpath():
    assert R.to_uri("persona", "air_city_a", subpath=("image",)) == "saiverse://persona/air_city_a/image"


# ---- parse_ref: short refs ----

def test_parse_short_ref_persona_scoped_is_self():
    p = R.parse_ref("track:2")
    assert (p.kind, p.key, p.persona) == ("track", "2", "self")
    assert p.is_self


def test_parse_short_ref_global_has_no_persona():
    p = R.parse_ref("item:5")
    assert (p.kind, p.key, p.persona) == ("item", "5", None)


def test_parse_short_ref_unknown_kind_raises():
    with pytest.raises(ValueError):
        R.parse_ref("desire:2")


def test_parse_short_ref_empty_key_raises():
    with pytest.raises(ValueError):
        R.parse_ref("track:")


# ---- parse_ref: URIs ----

def test_parse_uri_self():
    p = R.parse_ref("saiverse://self/track/2")
    assert (p.kind, p.key, p.persona) == ("track", "2", "self")


def test_parse_uri_explicit_persona_city_name():
    p = R.parse_ref("saiverse://city_a/air/track/2")
    assert (p.kind, p.key, p.persona) == ("track", "2", "city_a/air")


def test_parse_uri_global_item():
    p = R.parse_ref("saiverse://item/5")
    assert (p.kind, p.key, p.persona) == ("item", "5", None)


def test_parse_uri_global_with_subpath():
    p = R.parse_ref("saiverse://persona/air_city_a/image")
    assert (p.kind, p.key, p.persona, p.subpath) == ("persona", "air_city_a", None, ("image",))


def test_parse_uri_strips_query():
    p = R.parse_ref("saiverse://self/memopedia/12?foo=bar")
    assert (p.kind, p.key) == ("memopedia", "12")


def test_parse_uri_unknown_kind_raises():
    with pytest.raises(ValueError):
        R.parse_ref("saiverse://self/notakind/2")


# ---- 相互変換の不変条件 (I2) ----

@pytest.mark.parametrize("kind,key", [
    ("track", "2"), ("task", "7"), ("memopedia", "3"),
    ("message", "abc-uuid"), ("chronicle", "def-uuid"),
])
def test_roundtrip_persona_scoped_self(kind, key):
    # 短縮参照・自分 URI の三者が同じ (kind, key, self) に落ちる。
    from_short = R.parse_ref(R.to_short_ref(kind, key))
    from_uri = R.parse_ref(R.to_uri(kind, key))
    assert (from_short.kind, from_short.key, from_short.persona) == (kind, key, "self")
    assert (from_uri.kind, from_uri.key, from_uri.persona) == (kind, key, "self")


@pytest.mark.parametrize("kind,key", [("item", "5"), ("image", "pic.png"), ("building", "b1")])
def test_roundtrip_global(kind, key):
    from_uri = R.parse_ref(R.to_uri(kind, key))
    assert (from_uri.kind, from_uri.key, from_uri.persona) == (kind, key, None)


def test_persona_scoped_kinds_all_numeric_or_uuid_declared():
    # レジストリの健全性: ペルソナ依存 kind は URI にペルソナを含む前提。
    for word, k in R.KINDS.items():
        uri = R.to_uri(word, "1")
        if k.persona_scoped:
            assert uri.startswith("saiverse://self/"), word
        else:
            assert uri == f"saiverse://{word}/1", word
