import logging
import requests
from typing import Dict, List, Optional, Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from saiverse_manager import SAIVerseManager

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

        home_city_config = self.cities_config.get(self.home_city_id)
        if not home_city_config:
            raise ValueError(f"Home city '{self.home_city_id}' not found in configuration.")
        
        self.think_api_url = f"http://localhost:{home_city_config['api_port']}/persona-proxy/{self.persona_id}/think"
        self.session = requests.Session()

    def run_pulse(self, occupants: List[str], user_online: bool) -> List[str]:
        """
        Gets a response from the home city's "think" API.
        This method is called by the ConversationManager in the host city.
        """
        logging.info(f"[ProxyPulse] {self.persona_id} starting pulse in remote city.")
        
        # 1. Gather context from the host city
        # Get last 10 messages as context
        recent_history = self.manager.building_histories.get(self.current_building_id, [])[-10:]
        
        context_payload = {
            "building_id": self.current_building_id,
            "occupants": occupants,
            "recent_history": recent_history,
            "user_online": user_online,
        }
        
        # 2. Call home city's think API
        try:
            response = self.session.post(self.think_api_url, json=context_payload, timeout=35)
            response.raise_for_status()
            data = response.json()
            response_text = data.get("response_text")
            
            if response_text:
                logging.info(f"[ProxyPulse] Received thought from home city for {self.persona_id}: '{response_text[:50]}...'")
                return [response_text]
            else:
                logging.warning(f"[ProxyPulse] Received empty response from home city for {self.persona_id}.")
                return []
                
        except requests.exceptions.RequestException as e:
            logging.error(f"[ProxyPulse] Failed to get thought from home city for {self.persona_id}: {e}")
        except Exception as e:
            logging.error(f"[ProxyPulse] An unexpected error occurred for {self.persona_id}: {e}", exc_info=True)
        
        return []