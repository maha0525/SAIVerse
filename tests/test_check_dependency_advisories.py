"""Tests for scripts/check_dependency_advisories.py (offline, no network calls)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import check_dependency_advisories as cda


def _write_minimal_repo(tmp_path: Path, *, node_version: str = "24.22.1") -> Path:
    ps1 = tmp_path / "scripts" / "install_node_portable.ps1"
    ps1.parent.mkdir(parents=True, exist_ok=True)
    ps1.write_text(
        f'param(\n    [string]$NodeVersion = "{node_version}"\n)\n',
        encoding="utf-8",
    )

    (tmp_path / "requirements.lock").write_text(
        "# comment\n"
        "cryptography==48.0.1\n"
        "fastapi==0.141.1 ; python_version >= '3.11'\n"
        "PyJWT[crypto]==2.15.1\n",
        encoding="utf-8",
    )

    fe = tmp_path / "frontend"
    fe.mkdir(parents=True, exist_ok=True)
    (fe / "package.json").write_text(
        json.dumps(
            {
                "name": "frontend",
                "dependencies": {
                    "next": "^16.4.6",
                    "recharts": "^2.15.0",
                    "eslint": "^9.39.2",
                    "eslint-config-next": "^16.4.6",
                },
            }
        ),
        encoding="utf-8",
    )
    (fe / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "frontend",
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "frontend",
                        "dependencies": {
                            "next": "^16.4.6",
                            "recharts": "^2.15.0",
                            "eslint": "^9.39.2",
                            "eslint-config-next": "^16.4.6",
                        },
                    },
                    "node_modules/next": {
                        "version": "16.4.6",
                        "dependencies": {"shared-util": "^1.0.0"},
                        "optionalDependencies": {"sharp": "^0.35.5"},
                    },
                    "node_modules/sharp": {"version": "0.35.5"},
                    "node_modules/recharts": {
                        "version": "2.15.0",
                        "dependencies": {"lodash": "^4.17.23"},
                    },
                    "node_modules/lodash": {"version": "4.17.23"},
                    "node_modules/shared-util": {"version": "1.0.0"},
                    "node_modules/eslint": {
                        "version": "9.39.2",
                        "dependencies": {
                            "lint-only-leaf": "^2.0.0",
                            "shared-util": "^1.0.0",
                        },
                    },
                    "node_modules/eslint-config-next": {
                        "version": "16.4.6",
                        "dependencies": {"lint-only-leaf": "^2.0.0"},
                    },
                    "node_modules/lint-only-leaf": {"version": "2.0.0"},
                },
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_parse_frontend_excludes_only_exclusive_lint_subgraph(tmp_path: Path) -> None:
    repo = _write_minimal_repo(tmp_path)
    pins = cda.parse_frontend_runtime_lock(
        repo / "frontend" / "package.json",
        repo / "frontend" / "package-lock.json",
    )
    names = {name for name, _ in pins}
    # Runtime roots, optionalDependencies (sharp), and shared transitive deps stay included.
    assert names == {"next", "sharp", "recharts", "lodash", "shared-util"}
    # Lint roots and their exclusive leaf are excluded.
    assert "eslint" not in names
    assert "eslint-config-next" not in names
    assert "lint-only-leaf" not in names


def test_parse_requirements_lock_normalizes_and_fails_on_garbage(tmp_path: Path) -> None:
    repo = _write_minimal_repo(tmp_path)
    pins = cda.parse_requirements_lock(repo / "requirements.lock")
    assert pins == [
        ("cryptography", "48.0.1"),
        ("fastapi", "0.141.1"),
        ("pyjwt", "2.15.1"),
    ]

    bad_lock = tmp_path / "bad.lock"
    bad_lock.write_text("fastapi>=0.141.1\n", encoding="utf-8")
    with pytest.raises(cda.AdvisoryCheckError):
        cda.parse_requirements_lock(bad_lock)


def test_check_portable_node_detects_only_newer_security_in_same_major() -> None:
    index = [
        {"version": "v25.1.0", "security": True, "date": "2026-11-15"},
        {"version": "v24.22.4", "security": False, "date": "2026-11-14"},
        {"version": "v24.22.3", "security": True, "date": "2026-11-10"},
        {"version": "v24.22.2", "security": False, "date": "2026-10-27"},
        {"version": "v24.22.1", "security": True, "date": "2026-10-20"},
    ]
    finding = cda.check_portable_node("24.22.1", index)
    assert finding is not None
    assert finding.pinned_version == "v24.22.1"
    assert finding.latest_in_major == "v24.22.4"
    assert [r.version for r in finding.security_releases] == ["v24.22.3"]

    # Up to date past all security releases in v24 (v25 security release does not trigger v24).
    assert cda.check_portable_node("24.22.3", index) is None
    assert cda.check_portable_node("24.22.4", index) is None

    # Fails closed if the pinned version does not exist in index.json at all.
    with pytest.raises(cda.AdvisoryCheckError):
        cda.check_portable_node("24.99.9", index)


def test_run_advisory_check_deduplicates_aliases_honors_accepted_and_ignores_withdrawn(
    tmp_path: Path,
) -> None:
    repo = _write_minimal_repo(tmp_path, node_version="24.22.1")

    vuln_docs = {
        # Accepted CVE via two mirrored OSV records (GHSA + PYSEC) -> 1 accepted finding.
        "GHSA-g6cj-pr64-35w5": {
            "id": "GHSA-g6cj-pr64-35w5",
            "aliases": ["CVE-2026-69247", "PYSEC-2026-3552"],
            "summary": "cryptography PKCS#7 issue",
            "affected": [
                {
                    "package": {"ecosystem": "PyPI", "name": "cryptography"},
                    "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "49.0.0"}]}],
                }
            ],
        },
        "PYSEC-2026-3552": {
            "id": "PYSEC-2026-3552",
            "aliases": ["CVE-2026-69247", "GHSA-g6cj-pr64-35w5"],
            "summary": "cryptography PKCS#7 issue mirror",
            "affected": [],
        },
        # Withdrawn advisory -> ignored completely.
        "GHSA-withdrawn-0001": {
            "id": "GHSA-withdrawn-0001",
            "withdrawn": "2026-11-01T00:00:00Z",
            "summary": "false alarm",
        },
        # Actionable npm advisory -> triggers exit 1.
        "GHSA-lodash-test": {
            "id": "GHSA-lodash-test",
            "aliases": ["CVE-2026-99999"],
            "summary": "lodash prototype pollution",
            "affected": [
                {
                    "package": {"ecosystem": "npm", "name": "lodash"},
                    "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "4.18.0"}]}],
                }
            ],
        },
    }

    def fake_fetcher(url: str, payload: dict[str, Any] | None) -> Any:
        if url == cda.NODEJS_INDEX_URL:
            return [{"version": "v24.22.1", "security": True, "date": "2026-10-20"}]
        if url == cda.OSV_QUERYBATCH_URL:
            assert payload is not None
            out = []
            for q in payload["queries"]:
                pkg = q["package"]["name"]
                if pkg == "cryptography":
                    out.append({"vulns": [{"id": "GHSA-g6cj-pr64-35w5"}, {"id": "PYSEC-2026-3552"}]})
                elif pkg == "fastapi":
                    out.append({"vulns": [{"id": "GHSA-withdrawn-0001"}]})
                elif pkg == "lodash":
                    out.append({"vulns": [{"id": "GHSA-lodash-test"}]})
                else:
                    out.append({})
            return {"results": out}
        for vid, doc in vuln_docs.items():
            if url.endswith(f"/{vid}"):
                return doc
        raise AssertionError(f"unexpected URL: {url}")

    report = cda.run_advisory_check(repo, fetcher=fake_fetcher)
    assert report.node_finding is None
    assert len(report.accepted_findings) == 1
    assert report.accepted_findings[0].canonical_id == "CVE-2026-69247"
    assert len(report.actionable_findings) == 1
    assert report.actionable_findings[0].package == "lodash"
    assert report.actionable_findings[0].canonical_id == "CVE-2026-99999"
    assert report.actionable_findings[0].fixed_versions == ("4.18.0",)
    assert report.has_actionable is True

    md = cda.format_markdown_report(report)
    assert "lodash" in md
    assert "4.18.0" in md
    assert "CVE-2026-69247" in md


def test_query_osv_fails_closed_on_mismatched_batch_results(tmp_path: Path) -> None:
    repo = _write_minimal_repo(tmp_path)

    def broken_fetcher(url: str, payload: dict[str, Any] | None) -> Any:
        if url == cda.NODEJS_INDEX_URL:
            return [{"version": "v24.22.1", "security": True, "date": "2026-10-20"}]
        return {"results": []}  # wrong length -> must fail closed

    with pytest.raises(cda.AdvisoryCheckError):
        cda.run_advisory_check(repo, fetcher=broken_fetcher)


def test_version_scoped_accepted_advisory_does_not_mask_newer_version() -> None:
    docs = [
        {
            "id": "GHSA-mmhr-3c5m-f64f",
            "aliases": ["CVE-2026-126190"],
            "summary": "soupsieve ReDoS",
            "affected": [],
        }
    ]
    # Exact version match (2.10 for <3.11.5) is accepted.
    act_old, acc_old = cda._group_overlapping_vulns(
        docs, "PyPI", "soupsieve", "2.10", cda.ACCEPTED_ADVISORIES
    )
    assert act_old == []
    assert len(acc_old) == 1

    # A different version (4.1.1) is NOT masked by the 2.10 rule.
    act_new, acc_new = cda._group_overlapping_vulns(
        docs, "PyPI", "soupsieve", "4.1.1", cda.ACCEPTED_ADVISORIES
    )
    assert len(act_new) == 1
    assert acc_new == []


def test_real_repository_files_parse_cleanly() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    node_ver = cda.parse_portable_node_version(
        repo_root / "scripts" / "install_node_portable.ps1"
    )
    assert node_ver.count(".") == 2

    pypi_pins = cda.parse_requirements_lock(repo_root / "requirements.lock")
    assert len(pypi_pins) >= 100

    npm_pins = cda.parse_frontend_runtime_lock(
        repo_root / "frontend" / "package.json",
        repo_root / "frontend" / "package-lock.json",
    )
    npm_names = {name for name, _ in npm_pins}
    assert "next" in npm_names
    assert "eslint" not in npm_names
    assert "eslint-config-next" not in npm_names
    assert len(npm_pins) >= 150
