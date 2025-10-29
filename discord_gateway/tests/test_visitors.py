from discord_gateway.visitors import VisitorProfile, VisitorRegistry


def test_register_and_lookup():
    registry = VisitorRegistry()
    profile = VisitorProfile(
        discord_user_id="999",
        persona_id="persona-1",
        owner_user_id="owner-1",
        current_city_id="CityA",
        current_building_id="Hall",
    )
    registry.register(profile)
    assert registry.get_by_discord("999") is profile
    assert registry.get_by_persona("persona-1") is profile


def test_update_and_unregister():
    profile = VisitorProfile(
        discord_user_id="999",
        persona_id="persona-1",
        owner_user_id="owner-1",
        current_city_id="CityA",
        current_building_id="Hall",
    )
    registry = VisitorRegistry([profile])
    registry.update_location("persona-1", city_id="CityB", building_id="Garden")
    assert profile.current_city_id == "CityB"
    assert profile.current_building_id == "Garden"
    registry.unregister_by_persona("persona-1")
    assert registry.get_by_discord("999") is None
