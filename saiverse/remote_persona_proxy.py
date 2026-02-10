import logging
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .saiverse_manager import SAIVerseManager

class RemotePersonaProxy:
    """
    A lightweight proxy for a persona residing in another city.
    This object does not think on its own. Instead, it calls the "think" API
    of its home city to get responses.
    """
    def __init__(
        self,
        persona_id: str,
        persona_name: str,
        avatar_image: Optional[str],
        home_city_id: str,
        cities_config: Dict,
        saiverse_manager: 'SAIVerseManager',
        current_building_id: str,
    ):
        self.is_proxy = True
        self.persona_id = persona_id
        self.persona_name = persona_name
        self.avatar_image = avatar_image
        self.home_city_id = home_city_id
        self.cities_config = cities_config
        self.manager = saiverse_manager
        self.current_building_id = current_building_id
        self.emotion = {}  # Dummy attribute for interface consistency

        # cities_config may be used for future inter-city communication
        _ = self.cities_config.get(self.home_city_id)