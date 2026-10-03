"""Chronicle / Sluice の中止要求を実ルート経由で検証する (LLM・本番 DB なし)。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(params=["arasuji", "sluice"])
def cancel_api(request, monkeypatch, tmp_path):
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    from api.deps import get_manager
    from api.routes.people import arasuji, sluice

    if request.param == "arasuji":
        module = arasuji
        store_name = "_generation_jobs"
        worker_name = "_run_chronicle_generation"
        path = "arasuji/generate"
    else:
        module = sluice
        store_name = "_capture_jobs"
        worker_name = "_run_capture_job"
        path = "sluice/capture"

    jobs = {}
    monkeypatch.setattr(module, store_name, jobs)
    worker = Mock(side_effect=AssertionError("Cancel must not start background work"))
    monkeypatch.setattr(module, worker_name, worker)
    job_id = module._create_job("synthetic-persona")
    app = FastAPI()
    app.dependency_overrides[get_manager] = lambda: SimpleNamespace(personas={})
    app.include_router(module.router, prefix="/api/people")
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            module=module,
            jobs=jobs,
            job_id=job_id,
            url=f"/api/people/synthetic-persona/{path}/{job_id}",
        )
    worker.assert_not_called()


@pytest.mark.parametrize("status", ["pending", "running", "started"])
def test_first_and_repeated_cancel_preserve_existing_start_states(cancel_api, status):
    api = cancel_api
    api.module._update_job(api.job_id, status=status, progress=3, total=8)
    before = api.module._get_job(api.job_id)
    accepted = status != "started" or api.module.__name__.endswith(".arasuji")

    first = api.client.post(f"{api.url}/cancel")
    assert first.status_code == 200
    if not accepted:
        # Sluice never accepted "started" as a cancellable state.
        assert first.json() == {"cancelled": False, "reason": "Job is not running"}
        assert api.module._get_job(api.job_id) == before
        return

    assert first.json() == {"cancelled": True}
    cancelling = {**before, "cancel_requested": True, "status": "cancelling"}
    assert api.module._get_job(api.job_id) == cancelling
    poll = api.client.get(api.url)
    assert poll.status_code == 200
    assert poll.json()["status"] == "cancelling"

    second = api.client.post(f"{api.url}/cancel")
    assert second.status_code == 200
    assert second.json() == {"cancelled": True}
    assert api.jobs == {api.job_id: cancelling}
    assert api.client.get(api.url).json() == poll.json()


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("cancel_requested", [False, True])
def test_terminal_cancel_does_not_change_outcome_or_monotonic_flag(
    cancel_api, status, cancel_requested,
):
    api = cancel_api
    api.module._update_job(
        api.job_id, status=status, cancel_requested=cancel_requested,
        progress=3, total=8, entries_created=3, message="Worker finished",
    )
    before = api.module._get_job(api.job_id)

    response = api.client.post(f"{api.url}/cancel")

    assert response.status_code == 200
    assert response.json() == {"cancelled": False, "reason": "Job is not running"}
    assert api.jobs == {api.job_id: before}
    poll = api.client.get(api.url)
    assert poll.status_code == 200
    assert poll.json()["status"] == status


def test_missing_job_returns_404_without_creating_work(cancel_api):
    api = cancel_api
    before = api.module._get_job(api.job_id)
    missing_url = api.url.replace(api.job_id, "missing-job")

    response = api.client.post(f"{missing_url}/cancel")

    assert response.status_code == 404
    assert response.json() == {"detail": "Job missing-job not found"}
    assert api.jobs == {api.job_id: before}


@pytest.mark.parametrize(
    "status", ["pending", "running", "started", "cancelling", "completed", "failed", "cancelled"],
)
def test_other_persona_cannot_cancel_even_when_already_cancelling(cancel_api, status):
    api = cancel_api
    api.module._update_job(
        api.job_id, status=status, cancel_requested=status in ("cancelling", "cancelled"),
    )
    before = api.module._get_job(api.job_id)
    other_url = api.url.replace("synthetic-persona", "other-persona")

    response = api.client.post(f"{other_url}/cancel")

    assert response.status_code == 404
    assert api.jobs == {api.job_id: before}
