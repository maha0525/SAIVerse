"""Client wrapper around the SDS REST endpoints."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

import requests


class SDSClientError(RuntimeError):
    """Error raised when SDS communication fails."""


@dataclass
class SDSClient:
    """A thin wrapper that encapsulates SDS interactions."""

    city_name: str
    city_id: int
    api_port: int
    sds_url: str
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self) -> None:
        self.sds_url = self.sds_url.rstrip("/")

    def register_city(self) -> bool:
        """Attempt to register the city with the SDS."""
        register_url = f"{self.sds_url}/register"
        payload = {
            "city_name": self.city_name,
            "city_id_pk": self.city_id,
            "api_port": self.api_port,
        }
        try:
            response = self.session.post(register_url, json=payload, timeout=5)
            response.raise_for_status()
            logging.info("Successfully registered with SDS at %s", self.sds_url)
            return True
        except requests.exceptions.RequestException as exc:
            logging.error("Could not register with SDS: %s", exc)
            return False

    def send_heartbeat(self) -> bool:
        """Send a heartbeat to the SDS."""
        heartbeat_url = f"{self.sds_url}/heartbeat"
        payload = {"city_name": self.city_name}
        try:
            response = self.session.post(heartbeat_url, json=payload, timeout=2)
            response.raise_for_status()
            logging.debug("Heartbeat sent to SDS for %s", self.city_name)
            return True
        except requests.exceptions.RequestException as exc:
            logging.warning("Could not send heartbeat to SDS: %s", exc)
            return False

    def fetch_city_directory(self) -> Dict[str, Dict[str, object]]:
        """Fetch currently registered cities from the SDS."""
        cities_url = f"{self.sds_url}/cities"
        try:
            response = self.session.get(cities_url, timeout=5)
            response.raise_for_status()
            cities_data = response.json()
            if self.city_name in cities_data:
                del cities_data[self.city_name]
            return cities_data
        except (requests.exceptions.RequestException, json.JSONDecodeError) as exc:
            raise SDSClientError(str(exc)) from exc

    def clone_with_session(self, session: Optional[requests.Session]) -> "SDSClient":
        """Return a new instance that uses the provided session."""
        if session is None:
            return self
        return SDSClient(
            city_name=self.city_name,
            city_id=self.city_id,
            api_port=self.api_port,
            sds_url=self.sds_url,
            session=session,
        )
