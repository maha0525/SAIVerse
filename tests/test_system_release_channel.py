"""Version comparison, channel-aware update check and the channel-switch route.

docs/intent/early_access_release.md §3-4 / §3-2. What is pinned:

- versions are ordered by PEP 440, the same standard the startup upgrade chain
  uses: ``0.4.0rc1 < 0.4.0rc2 < 0.4.0`` (the old dotted-integer comparison put
  ``0.4.0rc1`` *above* ``0.4.0``). An unparseable tag answers "no update"
  instead of raising;
- on main (and any branch that is not early-access) the check is the unchanged
  ``releases/latest`` one and never looks at the prerelease list;
- on early-access the newest release *including* prereleases is announced,
  ordered by version rather than publish date, drafts excluded, and the
  response says whether stable has caught up (stable >= current);
- the switch route refuses while SAIVerse is still running -- bad channel,
  joining without consent, and every engine pre-check -- and only a request
  that passes writes the updater config, carrying ``switch_channel``.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import system as system_routes
from saiverse import app_state
from scripts import update_engine


# --- version comparison ------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "latest", "newer"),
    [
        ("0.4.0rc1", "0.4.0", True),
        ("0.4.0", "0.4.0rc1", False),
        ("0.4.0rc1", "0.4.0rc2", True),
        ("0.4.0rc2", "0.4.0rc1", False),
        ("0.3.14", "0.4.0rc1", True),
        ("0.1.6", "0.1.10", True),
        ("0.1.10", "0.1.6", False),
        ("0.3.14", "v0.3.15", True),
        ("0.3.14", "0.3.14", False),
        ("0.0.0+unknown", "0.3.14", True),
    ],
)
def test_compare_versions_uses_pep440_order(current: str, latest: str, newer: bool) -> None:
    assert system_routes._compare_versions(current, latest) is newer


@pytest.mark.parametrize(("current", "latest"), [("0.3.14", "0.4.0-ea.1"), ("", "0.3.14"), ("0.3.14", "nightly")])
def test_compare_versions_degrades_to_no_update_on_unparseable_input(current: str, latest: str) -> None:
    assert system_routes._compare_versions(current, latest) is False


def _release(tag: str, *, prerelease: bool = False, draft: bool = False) -> dict:
    return {
        "tag_name": tag,
        "html_url": f"https://example.invalid/{tag}",
        "name": f"SAIVerse {tag}",
        "published_at": "",
        "prerelease": prerelease,
        "draft": draft,
    }


def test_newest_release_orders_by_version_and_skips_drafts() -> None:
    releases = [
        # Newest first by publish date, as GitHub lists them: a stable hotfix
        # published after the early-access releases must not outrank them.
        _release("v0.3.15"),
        _release("v0.5.0", prerelease=True, draft=True),
        _release("v0.4.0rc2", prerelease=True),
        _release("v0.4.0rc1", prerelease=True),
        _release("not-a-version"),
        _release("v0.3.14"),
    ]
    newest = system_routes._newest_release(releases, include_prereleases=True)
    stable = system_routes._newest_release(releases, include_prereleases=False)
    assert newest is not None and newest[1]["tag_name"] == "v0.4.0rc2"
    assert stable is not None and stable[1]["tag_name"] == "v0.3.15"


# --- /api/system/version -----------------------------------------------------


class _AppStateCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        saved = (app_state.project_dir, app_state.version, app_state.city_name)
        self.addCleanup(self._restore, saved)
        app_state.project_dir = str(self.project)
        app_state.city_name = "city_a"
        app = FastAPI()
        app.include_router(system_routes.router, prefix="/api/system")
        self.client = TestClient(app)
        # The one-launch-per-backend guard is module state; reset it so each
        # test starts from "nothing launched yet" and leaves it that way.
        system_routes._updater_launched = False
        self.addCleanup(setattr, system_routes, "_updater_launched", False)

    @staticmethod
    def _restore(saved: tuple) -> None:
        app_state.project_dir, app_state.version, app_state.city_name = saved

    def _on_branch(self, head: str) -> None:
        git_dir = self.project / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "HEAD").write_text(head + "\n", encoding="utf-8")


class VersionEndpointChannelTest(_AppStateCase):
    def test_stable_checkout_keeps_the_releases_latest_check(self) -> None:
        self._on_branch("ref: refs/heads/main")
        app_state.version = "0.3.14"
        latest = {"tag_name": "v0.3.15", "html_url": "u", "name": "n", "published_at": ""}
        with patch.object(system_routes, "_fetch_latest_release", return_value=latest), patch.object(
            system_routes, "_fetch_release_list", side_effect=AssertionError("stable must not list prereleases")
        ):
            body = self.client.get("/api/system/version").json()

        self.assertEqual(body["channel"], "stable")
        self.assertEqual(body["branch"], "main")
        self.assertEqual(body["latest_version"], "0.3.15")
        self.assertTrue(body["update_available"])
        self.assertIsNone(body["can_return_to_stable"])
        self.assertIsNone(body["stable_latest_version"])

    def test_development_branch_and_detached_head_are_stable(self) -> None:
        app_state.version = "0.3.14"
        for head, branch in (("ref: refs/heads/develop", "develop"), ("0123456789abcdef", None)):
            self._on_branch(head)
            with patch.object(system_routes, "_fetch_latest_release", return_value=None), patch.object(
                system_routes, "_fetch_release_list", side_effect=AssertionError("not early access")
            ):
                body = self.client.get("/api/system/version").json()
            self.assertEqual(body["channel"], "stable")
            self.assertEqual(body["branch"], branch)
            self.assertIsNone(body["update_available"])

    def test_early_access_announces_prereleases_and_reports_no_return_yet(self) -> None:
        self._on_branch("ref: refs/heads/early-access")
        app_state.version = "0.4.0rc1"
        releases = [
            _release("v0.3.15"),
            _release("v0.4.0rc2", prerelease=True),
            _release("v0.4.0rc1", prerelease=True),
        ]
        with patch.object(system_routes, "_fetch_release_list", return_value=releases), patch.object(
            system_routes, "_fetch_latest_release", side_effect=AssertionError("EA uses the list")
        ):
            body = self.client.get("/api/system/version").json()

        self.assertEqual(body["channel"], "early_access")
        self.assertEqual(body["branch"], "early-access")
        self.assertEqual(body["latest_version"], "0.4.0rc2")
        self.assertTrue(body["update_available"])
        self.assertEqual(body["stable_latest_version"], "0.3.15")
        self.assertFalse(body["can_return_to_stable"])

    def test_early_access_can_return_once_stable_catches_up(self) -> None:
        self._on_branch("ref: refs/heads/early-access")
        app_state.version = "0.4.0rc2"
        releases = [_release("v0.4.0"), _release("v0.4.0rc2", prerelease=True)]
        with patch.object(system_routes, "_fetch_release_list", return_value=releases):
            body = self.client.get("/api/system/version").json()

        self.assertEqual(body["latest_version"], "0.4.0")
        self.assertTrue(body["update_available"])
        self.assertEqual(body["stable_latest_version"], "0.4.0")
        self.assertTrue(body["can_return_to_stable"])

    def test_early_access_without_github_reports_unknown(self) -> None:
        self._on_branch("ref: refs/heads/early-access")
        app_state.version = "0.4.0rc1"
        with patch.object(system_routes, "_fetch_release_list", return_value=None):
            body = self.client.get("/api/system/version").json()

        self.assertEqual(body["channel"], "early_access")
        self.assertIsNone(body["latest_version"])
        self.assertIsNone(body["update_available"])
        self.assertIsNone(body["can_return_to_stable"])


# --- /api/system/channel -------------------------------------------------------


class ChannelSwitchRouteTest(_AppStateCase):
    def setUp(self) -> None:
        super().setUp()
        script = self.project / "scripts" / "update_engine.py"
        script.parent.mkdir(parents=True)
        script.write_text("", encoding="utf-8")
        self.config_path = self.project / ".update_config.json"

    def test_unknown_channel_is_refused(self) -> None:
        res = self.client.post("/api/system/channel", json={"channel": "nightly", "consent": True})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.config_path.exists())

    def test_joining_without_consent_is_refused(self) -> None:
        res = self.client.post("/api/system/channel", json={"channel": "early_access"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("consent", res.json()["detail"])
        self.assertFalse(self.config_path.exists())

    def test_engine_precheck_refusal_happens_before_shutdown(self) -> None:
        refusal = update_engine.UpdateError("The early-access branch has not been published on origin yet")
        with patch.object(system_routes.subprocess, "run", return_value=Mock(returncode=0)), patch.object(
            update_engine, "preflight_switch", side_effect=refusal
        ), patch.object(system_routes.threading, "Timer") as timer:
            res = self.client.post("/api/system/channel", json={"channel": "early_access", "consent": True})

        self.assertEqual(res.status_code, 409)
        self.assertIn("has not been published", res.json()["detail"])
        self.assertFalse(self.config_path.exists())
        timer.assert_not_called()

    def test_accepted_switch_hands_the_channel_to_the_detached_updater(self) -> None:
        plan = update_engine.SwitchPlan("early_access", "early-access", "main", "abc123")
        with patch.object(system_routes.subprocess, "run", return_value=Mock(returncode=0)), patch.object(
            update_engine, "preflight_switch", return_value=plan
        ) as preflight, patch.object(system_routes.subprocess, "Popen") as popen, patch.object(
            system_routes.threading, "Timer"
        ) as timer:
            res = self.client.post("/api/system/channel", json={"channel": "early_access", "consent": True})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "switching", "channel": "early_access", "branch": "early-access"})
        preflight.assert_called_once_with(self.project, "early_access")
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["switch_channel"], "early_access")
        self.assertEqual(config["project_dir"], str(self.project))
        popen.assert_called_once()
        self.assertIn("--config", popen.call_args.args[0])
        timer.return_value.start.assert_called_once()

    def test_second_launch_request_is_refused_while_one_is_in_progress(self) -> None:
        # Both /channel and /update stop the backend right after spawning
        # their updater; a second request in that window must not spawn a
        # second updater racing the first over the same checkout and venv.
        plan = update_engine.SwitchPlan("early_access", "early-access", "main", "abc123")
        with patch.object(system_routes.subprocess, "run", return_value=Mock(returncode=0)), patch.object(
            update_engine, "preflight_switch", return_value=plan
        ), patch.object(system_routes.subprocess, "Popen") as popen, patch.object(
            system_routes.threading, "Timer"
        ):
            first = self.client.post("/api/system/channel", json={"channel": "early_access", "consent": True})
            second = self.client.post("/api/system/channel", json={"channel": "early_access", "consent": True})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertIn("already in progress", second.json()["detail"])
        popen.assert_called_once()

    def test_refused_preflight_keeps_the_launch_retryable(self) -> None:
        # The launch is claimed only after every refusal: a 409 from the
        # preflight must not burn the one launch this backend may make.
        plan = update_engine.SwitchPlan("early_access", "early-access", "main", "abc123")
        with patch.object(system_routes.subprocess, "run", return_value=Mock(returncode=0)), patch.object(
            update_engine,
            "preflight_switch",
            side_effect=[update_engine.UpdateError("tree is dirty"), plan],
        ), patch.object(system_routes.subprocess, "Popen"), patch.object(system_routes.threading, "Timer"):
            refused = self.client.post("/api/system/channel", json={"channel": "early_access", "consent": True})
            accepted = self.client.post("/api/system/channel", json={"channel": "early_access", "consent": True})

        self.assertEqual(refused.status_code, 409)
        self.assertEqual(accepted.status_code, 200)

    def test_failed_launch_releases_the_claim_so_a_retry_can_succeed(self) -> None:
        # A launch that dies before the updater runs (contract write, spawn)
        # must give the claim back: the backend is still alive, and a retry
        # after fixing the environment has to be possible without a restart.
        # The launcher is failed as a whole rather than through Popen, so the
        # test does not depend on how many spawn attempts a platform makes
        # (Windows retries once without the job-breakaway flag).
        plan = update_engine.SwitchPlan("early_access", "early-access", "main", "abc123")
        with patch.object(system_routes.subprocess, "run", return_value=Mock(returncode=0)), patch.object(
            update_engine, "preflight_switch", return_value=plan
        ), patch.object(
            system_routes, "_launch_detached_updater", side_effect=OSError("launch failed")
        ), patch.object(system_routes.threading, "Timer"):
            with self.assertRaises(OSError):
                self.client.post("/api/system/channel", json={"channel": "early_access", "consent": True})

        with patch.object(system_routes.subprocess, "run", return_value=Mock(returncode=0)), patch.object(
            update_engine, "preflight_switch", return_value=plan
        ), patch.object(system_routes.subprocess, "Popen"), patch.object(system_routes.threading, "Timer"):
            retry = self.client.post("/api/system/channel", json={"channel": "early_access", "consent": True})

        self.assertEqual(retry.status_code, 200)

    def test_returning_to_stable_needs_no_consent_flag(self) -> None:
        plan = update_engine.SwitchPlan("stable", "main", "early-access", "abc123")
        with patch.object(system_routes.subprocess, "run", return_value=Mock(returncode=0)), patch.object(
            update_engine, "preflight_switch", return_value=plan
        ), patch.object(system_routes.subprocess, "Popen"), patch.object(system_routes.threading, "Timer"):
            res = self.client.post("/api/system/channel", json={"channel": "stable"})

        self.assertEqual(res.status_code, 200)
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["switch_channel"], "stable")
