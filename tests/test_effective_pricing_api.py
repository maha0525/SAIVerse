"""Model-list HTTP responses use effective prices; editor responses stay raw.

Only isolated files and a router-only ASGI app are used. No manager, production
persona, provider client, external network, or live database is started.
"""

import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def pricing_api(monkeypatch, tmp_path):
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    monkeypatch.setenv("SAIVERSE_USER_DATA_DIR", str(tmp_path / "user_data"))

    from api.routes import config, usage
    from saiverse import data_paths, model_configs

    raw = {
        "model": "synthetic-api-model",
        "display_name": "Synthetic dated model",
        "provider": "synthetic",
        "pricing": {
            "currency": "USD",
            "input_per_1m_tokens": 1.5,
            "output_per_1m_tokens": 9.0,
            "pricing_note": "Estimate uses a conservative cutoff; provider timezone unspecified.",
            "periods": [{
                "starts_at": "2026-10-10T00:00:00Z",
                "ends_at": "2026-12-31T10:00:00Z",
                "rates": {"input_per_1m_tokens": 0.75, "output_per_1m_tokens": 4.5},
            }],
        },
    }
    configs = {"dated-a": raw, "dated-b": copy.deepcopy(raw), "unpriced": {"provider": "synthetic"}}
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", configs)
    monkeypatch.setattr(usage, "MODEL_CONFIGS", configs)
    monkeypatch.setattr(data_paths, "USER_DATA_DIR", tmp_path / "user_data")
    app = FastAPI()
    app.include_router(config.router, prefix="/api/config")
    app.include_router(usage.router, prefix="/api/usage")
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, config=config, usage=usage, raw=raw, home=tmp_path)


def _freeze_route(monkeypatch, route, instant):
    calls = []

    class Clock:
        @staticmethod
        def now(tz):
            assert tz is timezone.utc
            calls.append(instant)
            # A second read would cross the cutoff in the snapshot regression.
            return instant + timedelta(seconds=len(calls) - 1)

    monkeypatch.setattr(route, "datetime", Clock)
    return calls


@pytest.mark.parametrize("route_name", ["config", "usage"])
@pytest.mark.parametrize(("instant", "input_rate", "output_rate"), [
    ("2026-10-09T23:59:59+00:00", 1.5, 9.0),
    ("2026-10-10T00:00:00+00:00", 0.75, 4.5),
    ("2026-12-31T09:59:59+00:00", 0.75, 4.5),
    ("2026-12-31T10:00:00+00:00", 1.5, 9.0),
])
def test_model_lists_resolve_effective_prices(
    pricing_api, monkeypatch, route_name, instant, input_rate, output_rate,
):
    original = copy.deepcopy(pricing_api.raw)
    calls = _freeze_route(monkeypatch, getattr(pricing_api, route_name), datetime.fromisoformat(instant))
    response = pricing_api.client.get(f"/api/{route_name}/models")
    assert response.status_code == 200
    id_key = "id" if route_name == "config" else "model_id"
    input_key = "input_price" if route_name == "config" else "input_per_1m_tokens"
    output_key = "output_price" if route_name == "config" else "output_per_1m_tokens"
    models = {model[id_key]: model for model in response.json()}
    for key in ("dated-a", "dated-b"):
        assert models[key][input_key] == input_rate
        assert models[key][output_key] == output_rate
        assert models[key]["currency"] == "USD"
        assert models[key]["pricing_note"] == original["pricing"]["pricing_note"]
        assert "periods" not in models[key]
    assert models["unpriced"][input_key] is None
    assert models["unpriced"][output_key] is None
    assert models["unpriced"]["pricing_note"] is None
    assert len(calls) == 1, "all models must share one UTC snapshot per request"
    assert pricing_api.raw == original, "display resolution must not mutate editable settings"


def test_editor_model_response_preserves_raw_schedule(pricing_api):
    model_dir = pricing_api.home / "user_data" / "models"
    model_dir.mkdir(parents=True)
    original = copy.deepcopy(pricing_api.raw)
    (model_dir / "dated-a.json").write_text(json.dumps(original), encoding="utf-8")

    response = pricing_api.client.get("/api/config/models/dated-a")

    assert response.status_code == 200
    assert response.json() == {"key": "dated-a", "config": original, "source": "user_data"}
    assert response.json()["config"]["pricing"]["input_per_1m_tokens"] == 1.5
    assert response.json()["config"]["pricing"]["periods"] == original["pricing"]["periods"]
