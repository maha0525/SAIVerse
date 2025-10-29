from discord_gateway.mapping import ChannelContext, ChannelMapping
from discord_gateway.permissions import InvitationRegistry, PermissionPolicy


def make_context(**overrides):
    base = ChannelContext(
        channel_id="123",
        city_id="CityA",
        building_id="Hall",
        host_user_id="host-1",
    )
    return base.__class__(**{**base.__dict__, **overrides})


def test_permission_allows_host():
    mapping = ChannelMapping([make_context()])
    policy = PermissionPolicy(mapping)
    context = mapping.get("123")
    decision = policy.evaluate(context, discord_user_id="host-1", roles=[])
    assert decision.allowed


def test_permission_requires_role():
    mapping = ChannelMapping([make_context(allowed_roles=("VIP",))])
    policy = PermissionPolicy(mapping)
    context = mapping.get("123")
    decision = policy.evaluate(context, discord_user_id="user", roles=["VIP"])
    assert decision.allowed
    denied = policy.evaluate(context, discord_user_id="user", roles=["NORMAL"])
    assert not denied.allowed


def test_permission_invitation_flow():
    registry = InvitationRegistry()
    mapping = ChannelMapping([make_context(invite_required=True)])
    policy = PermissionPolicy(mapping, registry)
    context = mapping.get("123")
    denied = policy.evaluate(context, discord_user_id="guest", roles=[])
    assert not denied.allowed
    registry.register("123", "guest")
    allowed = policy.evaluate(context, discord_user_id="guest", roles=[])
    assert allowed.allowed


def test_permission_accepts_role_dict():
    mapping = ChannelMapping([make_context(allowed_roles=("VIP",))])
    policy = PermissionPolicy(mapping)
    context = mapping.get("123")
    decision = policy.evaluate(
        context,
        discord_user_id="user",
        roles={"ids": [], "names": ["VIP"]},
    )
    assert decision.allowed
