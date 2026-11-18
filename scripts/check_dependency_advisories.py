"""SAIVerse が配布する3系統の固定バージョンを、公開の脆弱性情報と照合する。

対象:
1. Windows 向け同梱 Node.js 本体 (``scripts/install_node_portable.ps1`` の ``$NodeVersion``)
   — ``https://nodejs.org/dist/index.json`` で同じメジャー系列に ``security: true`` の
   より新しいリリースが出ていないかを確かめる。
2. フロントエンドの本番実行パッケージ (``frontend/package.json`` + ``frontend/package-lock.json``)
   — 開発者専用の lint ツール (``eslint`` / ``eslint-config-next``) からしか到達しない
   パッケージを除き、本番ビルド・本番サーバーで動く全パッケージを OSV に問う。
3. バックエンドの Python パッケージ (``requirements.lock`` の全 pin)
   — プラットフォーム別の環境マーカー行も含めて全 pin を OSV に問う。

終了コード:
- 0: 未対応の脆弱性・セキュリティ更新なし
- 1: 対応が必要な脆弱性または Node.js セキュリティ更新が見つかった
- 2: ファイルの読み取り失敗、または OSV / nodejs.org への照会失敗 (fail-closed:
     照会できなかったときに「問題なし」として通さない)

GitHub Actions 上 (``GITHUB_STEP_SUMMARY`` が設定されている環境) では、メールから開いた
実行画面の最上部に日本語の要約表を出す。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

OSV_QUERYBATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL_TEMPLATE = "https://api.osv.dev/v1/vulns/{vuln_id}"
NODEJS_INDEX_URL = "https://nodejs.org/dist/index.json"
OSV_BATCH_SIZE = 500
USER_AGENT = "SAIVerse-AdvisoryCheck/1.0"

# frontend/package.json は dependencies 一本で書かれているが、eslint と eslint-config-next は
# 開発者の `npm run lint` 専用で、利用者の `npm run build` / `npm start` では使われない
# (Next.js 16 は next build から eslint 実行を外している)。他の本番パッケージからも到達する
# 共通依存は除外されず、この 2 つからしか到達しない専用の部分木だけを除外する。
DEV_ONLY_FRONTEND_ROOTS: frozenset[str] = frozenset({"eslint", "eslint-config-next"})

_PY_PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==([^\s;]+)(?:\s*;\s*(.+))?$")
_NODE_PS1_VERSION_RE = re.compile(r'\[string\]\$NodeVersion\s*=\s*"(\d+\.\d+\.\d+)"')
_SEMVER_TRIPLE_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class AdvisoryCheckError(RuntimeError):
    """入力ファイルの破損や外部 API 照会失敗など、判定自体が完了できなかった状態。"""


@dataclass(frozen=True)
class AcceptedAdvisory:
    """理由付きで固定を維持している既知の勧告 (docs/intent/dependency_management.md §3-2)。"""

    ecosystem: str
    package: str
    advisory_id: str
    reason: str


# 「なんとなく除外」は禁止。docs/intent/dependency_management.md に理由と解除条件が
# 記録されているものだけをここに置く。
ACCEPTED_ADVISORIES: tuple[AcceptedAdvisory, ...] = (
    AcceptedAdvisory(
        ecosystem="PyPI",
        package="cryptography",
        advisory_id="CVE-2026-69247",
        reason="cryptography>=49 に macOS x86_64 wheel が無く、該当機能 (PKCS#7 復号) を SAIVerse は未使用 (dependency_management.md §3-2)",
    ),
    AcceptedAdvisory(
        ecosystem="PyPI",
        package="cryptography",
        advisory_id="CVE-2026-69248",
        reason="cryptography>=49 に macOS x86_64 wheel が無く、該当機能 (x509 経路検証器) を SAIVerse は未使用 (dependency_management.md §3-2)",
    ),
    AcceptedAdvisory(
        ecosystem="PyPI",
        package="cryptography",
        advisory_id="CVE-2026-69249",
        reason="cryptography>=49 に macOS x86_64 wheel が無く、該当機能 (x509 経路検証器) を SAIVerse は未使用 (dependency_management.md §3-2)",
    ),
)


@dataclass(frozen=True)
class PackageAdvisoryFinding:
    ecosystem: str
    package: str
    version: str
    canonical_id: str
    all_ids: tuple[str, ...]
    summary: str
    fixed_versions: tuple[str, ...]
    accepted_reason: str | None = None


@dataclass(frozen=True)
class NodeSecurityRelease:
    version: str
    date: str


@dataclass(frozen=True)
class NodeAdvisoryFinding:
    pinned_version: str
    latest_in_major: str
    security_releases: tuple[NodeSecurityRelease, ...]


@dataclass(frozen=True)
class CheckReport:
    pinned_node_version: str
    pypi_pin_count: int
    npm_runtime_pin_count: int
    node_finding: NodeAdvisoryFinding | None
    actionable_findings: tuple[PackageAdvisoryFinding, ...]
    accepted_findings: tuple[PackageAdvisoryFinding, ...]

    @property
    def has_actionable(self) -> bool:
        return self.node_finding is not None or bool(self.actionable_findings)


JsonFetcher = Callable[[str, dict[str, Any] | None], Any]


def normalize_pypi_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_semver_triple(raw: str) -> tuple[int, int, int] | None:
    match = _SEMVER_TRIPLE_RE.match(raw.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def default_http_json(
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
    retries: int = 3,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    last_error: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            last_error = exc
            # 429 や一時的な 5xx だけ再試行し、404 などは即座に失敗させる。
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt + 1 >= retries:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = exc
            if attempt + 1 >= retries:
                break
        sleep_fn(1.5 * (2**attempt))

    raise AdvisoryCheckError(f"{url} の取得に失敗しました: {last_error}")


def parse_portable_node_version(ps1_path: Path) -> str:
    try:
        text = ps1_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AdvisoryCheckError(f"{ps1_path} を読めませんでした: {exc}") from exc
    matches = _NODE_PS1_VERSION_RE.findall(text)
    if len(matches) != 1:
        raise AdvisoryCheckError(
            f"{ps1_path} から $NodeVersion を一意に読み取れませんでした (一致数: {len(matches)})"
        )
    return matches[0]


def check_portable_node(pinned_version: str, index_entries: Any) -> NodeAdvisoryFinding | None:
    pinned_tuple = _parse_semver_triple(pinned_version)
    if pinned_tuple is None:
        raise AdvisoryCheckError(f"Node.js 固定版の形式が不正です: {pinned_version!r}")
    if not isinstance(index_entries, list) or not index_entries:
        raise AdvisoryCheckError("nodejs.org の index.json が空または配列ではありません")

    major = pinned_tuple[0]
    seen_pinned = False
    latest_tuple = pinned_tuple
    latest_version = f"v{pinned_version}"
    security_newer: list[ tuple[tuple[int, int, int], NodeSecurityRelease] ] = []

    for entry in index_entries:
        if not isinstance(entry, dict):
            continue
        ver_str = str(entry.get("version") or "")
        ver_tuple = _parse_semver_triple(ver_str)
        if ver_tuple is None or ver_tuple[0] != major:
            continue
        if ver_tuple == pinned_tuple:
            seen_pinned = True
        if ver_tuple > latest_tuple:
            latest_tuple = ver_tuple
            latest_version = ver_str if ver_str.startswith("v") else f"v{ver_str}"
        if ver_tuple > pinned_tuple and entry.get("security") is True:
            security_newer.append(
                (
                    ver_tuple,
                    NodeSecurityRelease(
                        version=ver_str if ver_str.startswith("v") else f"v{ver_str}",
                        date=str(entry.get("date") or "unknown"),
                    ),
                )
            )

    if not seen_pinned:
        raise AdvisoryCheckError(
            f"nodejs.org の index.json に固定版 v{pinned_version} が見つかりません (形式変更または誤記の疑い)"
        )
    if not security_newer:
        return None

    security_newer.sort(key=lambda item: item[0], reverse=True)
    return NodeAdvisoryFinding(
        pinned_version=f"v{pinned_version}",
        latest_in_major=latest_version,
        security_releases=tuple(item[1] for item in security_newer),
    )


def parse_requirements_lock(lock_path: Path) -> list[tuple[str, str]]:
    try:
        lines = lock_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AdvisoryCheckError(f"{lock_path} を読めませんでした: {exc}") from exc

    pins: set[tuple[str, str]] = set()
    for lineno, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PY_PIN_RE.match(stripped)
        if not match:
            raise AdvisoryCheckError(
                f"{lock_path}:{lineno}: requirements.lock の行を解釈できませんでした: {stripped!r}"
            )
        pins.add((normalize_pypi_name(match.group(1)), match.group(2)))

    if not pins:
        raise AdvisoryCheckError(f"{lock_path} にパッケージ pin が 1 件もありません")
    return sorted(pins)


def _resolve_lock_package_key(packages: dict[str, Any], parent_key: str, dep_name: str) -> str | None:
    current = parent_key
    while True:
        candidate = f"{current}/node_modules/{dep_name}" if current else f"node_modules/{dep_name}"
        if candidate in packages:
            return candidate
        if not current or "/node_modules/" not in current:
            top = f"node_modules/{dep_name}"
            return top if top in packages else None
        current = current.rsplit("/node_modules/", 1)[0]


def parse_frontend_runtime_lock(
    package_json_path: Path,
    package_lock_path: Path,
    *,
    dev_only_roots: frozenset[str] = DEV_ONLY_FRONTEND_ROOTS,
) -> list[tuple[str, str]]:
    try:
        manifest = json.loads(package_json_path.read_text(encoding="utf-8"))
        lock = json.loads(package_lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdvisoryCheckError(f"フロントエンドの lock ファイルを解釈できませんでした: {exc}") from exc

    dependencies = manifest.get("dependencies")
    packages = lock.get("packages")
    if not isinstance(dependencies, dict) or not isinstance(packages, dict):
        raise AdvisoryCheckError("package.json または package-lock.json の構造が想定外です")

    runtime_roots = [name for name in dependencies if name not in dev_only_roots]
    if not runtime_roots:
        raise AdvisoryCheckError("フロントエンドの本番依存ルートが 0 件です")

    visited_keys: set[str] = set()
    stack: list[str] = []
    for root_name in runtime_roots:
        resolved = _resolve_lock_package_key(packages, "", root_name)
        if resolved is None:
            raise AdvisoryCheckError(
                f"package.json の本番依存 {root_name!r} が package-lock.json に見つかりません"
            )
        stack.append(resolved)

    while stack:
        key = stack.pop()
        if key in visited_keys:
            continue
        visited_keys.add(key)
        entry = packages.get(key)
        if not isinstance(entry, dict):
            continue
        # optionalDependencies には @next/swc-* や sharp など実行時バイナリが入るため含める。
        for field in ("dependencies", "optionalDependencies"):
            children = entry.get(field)
            if not isinstance(children, dict):
                continue
            for child_name in children:
                child_key = _resolve_lock_package_key(packages, key, child_name)
                if child_key is not None and child_key not in visited_keys:
                    stack.append(child_key)

    pins: set[tuple[str, str]] = set()
    for key in visited_keys:
        entry = packages.get(key)
        if not isinstance(entry, dict):
            continue
        version = entry.get("version")
        if not isinstance(version, str) or not version:
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            name = key.split("node_modules/")[-1]
        pins.add((name, version))

    if not pins:
        raise AdvisoryCheckError("フロントエンドの本番パッケージが 0 件でした")
    return sorted(pins)


def _extract_fixed_versions(vuln_doc: dict[str, Any], ecosystem: str, package: str) -> tuple[str, ...]:
    target = normalize_pypi_name(package) if ecosystem == "PyPI" else package.lower()
    fixed: set[str] = set()
    for affected in vuln_doc.get("affected") or []:
        if not isinstance(affected, dict):
            continue
        pkg_info = affected.get("package") or {}
        pkg_eco = str(pkg_info.get("ecosystem") or "")
        pkg_name_raw = str(pkg_info.get("name") or "")
        pkg_name = (
            normalize_pypi_name(pkg_name_raw) if pkg_eco == "PyPI" else pkg_name_raw.lower()
        )
        if pkg_eco != ecosystem or pkg_name != target:
            continue
        for rng in affected.get("ranges") or []:
            if not isinstance(rng, dict) or rng.get("type") not in ("ECOSYSTEM", "SEMVER"):
                continue
            for event in rng.get("events") or []:
                if isinstance(event, dict) and isinstance(event.get("fixed"), str):
                    fixed.add(event["fixed"])
    return tuple(sorted(fixed))


def _preferred_canonical_id(ids: set[str]) -> str:
    for prefix in ("CVE-", "GHSA-", "PYSEC-"):
        matching = sorted(item for item in ids if item.startswith(prefix))
        if matching:
            return matching[0]
    return sorted(ids)[0]


def _group_overlapping_vulns(
    vuln_docs: list[dict[str, Any]],
    ecosystem: str,
    package: str,
    version: str,
    accepted: tuple[AcceptedAdvisory, ...],
) -> tuple[list[PackageAdvisoryFinding], list[PackageAdvisoryFinding]]:
    active_docs = [doc for doc in vuln_docs if not doc.get("withdrawn")]
    if not active_docs:
        return [], []

    clusters: list[tuple[set[str], list[dict[str, Any]]]] = []
    for doc in active_docs:
        vid = str(doc.get("id") or "").strip()
        if not vid:
            continue
        aliases = {
            str(alias).strip()
            for alias in (doc.get("aliases") or [])
            if isinstance(alias, str) and alias.strip()
        }
        doc_ids = {vid, *aliases}

        merged_ids = set(doc_ids)
        merged_docs = [doc]
        remaining: list[tuple[set[str], list[dict[str, Any]]]] = []
        for existing_ids, existing_docs in clusters:
            if existing_ids & merged_ids:
                merged_ids |= existing_ids
                merged_docs.extend(existing_docs)
            else:
                remaining.append((existing_ids, existing_docs))
        remaining.append((merged_ids, merged_docs))
        clusters = remaining

    norm_pkg = normalize_pypi_name(package) if ecosystem == "PyPI" else package.lower()
    accepted_lookup: dict[str, str] = {}
    for rule in accepted:
        rule_pkg = (
            normalize_pypi_name(rule.package)
            if rule.ecosystem == "PyPI"
            else rule.package.lower()
        )
        if rule.ecosystem == ecosystem and rule_pkg == norm_pkg:
            accepted_lookup[rule.advisory_id] = rule.reason

    actionable: list[PackageAdvisoryFinding] = []
    accepted_out: list[PackageAdvisoryFinding] = []

    for cluster_ids, cluster_docs in clusters:
        canonical = _preferred_canonical_id(cluster_ids)
        summary = ""
        fixed_all: set[str] = set()
        for doc in cluster_docs:
            if not summary:
                raw_summary = str(doc.get("summary") or "").strip()
                if not raw_summary:
                    raw_details = str(doc.get("details") or "").strip().splitlines()
                    raw_summary = raw_details[0].strip() if raw_details else ""
                summary = raw_summary
            fixed_all.update(_extract_fixed_versions(doc, ecosystem, package))

        reason = next(
            (accepted_lookup[aid] for aid in sorted(cluster_ids) if aid in accepted_lookup),
            None,
        )
        finding = PackageAdvisoryFinding(
            ecosystem=ecosystem,
            package=package,
            version=version,
            canonical_id=canonical,
            all_ids=tuple(sorted(cluster_ids)),
            summary=summary or "(概要なし)",
            fixed_versions=tuple(sorted(fixed_all)),
            accepted_reason=reason,
        )
        if reason is not None:
            accepted_out.append(finding)
        else:
            actionable.append(finding)

    actionable.sort(key=lambda item: item.canonical_id)
    accepted_out.sort(key=lambda item: item.canonical_id)
    return actionable, accepted_out


def query_osv_advisories(
    pypi_pins: list[tuple[str, str]],
    npm_pins: list[tuple[str, str]],
    *,
    fetcher: JsonFetcher = default_http_json,
    accepted: tuple[AcceptedAdvisory, ...] = ACCEPTED_ADVISORIES,
) -> tuple[tuple[PackageAdvisoryFinding, ...], tuple[PackageAdvisoryFinding, ...]]:
    targets: list[tuple[str, str, str]] = [
        *[("PyPI", name, ver) for name, ver in pypi_pins],
        *[("npm", name, ver) for name, ver in npm_pins],
    ]
    raw_hits: list[tuple[str, str, str, list[str]]] = []

    for offset in range(0, len(targets), OSV_BATCH_SIZE):
        chunk = targets[offset : offset + OSV_BATCH_SIZE]
        payload = {
            "queries": [
                {"package": {"ecosystem": eco, "name": name}, "version": ver}
                for eco, name, ver in chunk
            ]
        }
        response = fetcher(OSV_QUERYBATCH_URL, payload)
        results = response.get("results") if isinstance(response, dict) else None
        if not isinstance(results, list) or len(results) != len(chunk):
            raise AdvisoryCheckError(
                f"OSV querybatch の応答件数が一致しません (期待 {len(chunk)}, 実際 {len(results) if isinstance(results, list) else '不正'})"
            )
        for (eco, name, ver), res in zip(chunk, results):
            if not isinstance(res, dict):
                raise AdvisoryCheckError("OSV querybatch の要素がオブジェクトではありません")
            vulns = res.get("vulns") or []
            ids = [
                str(v["id"])
                for v in vulns
                if isinstance(v, dict) and isinstance(v.get("id"), str)
            ]
            if ids:
                raw_hits.append((eco, name, ver, ids))

    vuln_cache: dict[str, dict[str, Any]] = {}
    for _, _, _, ids in raw_hits:
        for vid in ids:
            if vid not in vuln_cache:
                doc = fetcher(OSV_VULN_URL_TEMPLATE.format(vuln_id=vid), None)
                if not isinstance(doc, dict) or not doc.get("id"):
                    raise AdvisoryCheckError(f"OSV 勧告詳細 {vid} の応答が不正です")
                vuln_cache[vid] = doc

    actionable_all: list[PackageAdvisoryFinding] = []
    accepted_all: list[PackageAdvisoryFinding] = []
    for eco, name, ver, ids in raw_hits:
        docs = [vuln_cache[vid] for vid in ids]
        act, acc = _group_overlapping_vulns(docs, eco, name, ver, accepted)
        actionable_all.extend(act)
        accepted_all.extend(acc)

    return tuple(actionable_all), tuple(accepted_all)


def run_advisory_check(
    project_dir: Path,
    *,
    fetcher: JsonFetcher = default_http_json,
    accepted: tuple[AcceptedAdvisory, ...] = ACCEPTED_ADVISORIES,
) -> CheckReport:
    pinned_node = parse_portable_node_version(
        project_dir / "scripts" / "install_node_portable.ps1"
    )
    pypi_pins = parse_requirements_lock(project_dir / "requirements.lock")
    npm_pins = parse_frontend_runtime_lock(
        project_dir / "frontend" / "package.json",
        project_dir / "frontend" / "package-lock.json",
    )

    node_index = fetcher(NODEJS_INDEX_URL, None)
    node_finding = check_portable_node(pinned_node, node_index)
    actionable, accepted_out = query_osv_advisories(
        pypi_pins,
        npm_pins,
        fetcher=fetcher,
        accepted=accepted,
    )

    return CheckReport(
        pinned_node_version=f"v{pinned_node}",
        pypi_pin_count=len(pypi_pins),
        npm_runtime_pin_count=len(npm_pins),
        node_finding=node_finding,
        actionable_findings=actionable,
        accepted_findings=accepted_out,
    )


def format_markdown_report(report: CheckReport) -> str:
    lines: list[str] = []
    if report.has_actionable:
        lines.append("## ⚠️ SAIVerse 依存関係のセキュリティ点検: 要確認の勧告があります")
    else:
        lines.append("## ✅ SAIVerse 依存関係のセキュリティ点検: 未対応の勧告はありません")

    lines.append("")
    lines.append(
        f"- 点検対象: 同梱 Node.js (`{report.pinned_node_version}`) / "
        f"フロントエンド本番 npm (`{report.npm_runtime_pin_count}` 件) / "
        f"Python `requirements.lock` (`{report.pypi_pin_count}` 件)"
    )
    lines.append("")

    if report.node_finding is not None:
        nf = report.node_finding
        sec_list = ", ".join(f"{r.version} ({r.date})" for r in nf.security_releases)
        lines.append("### 1. 同梱 Node.js (`scripts/install_node_portable.ps1`)")
        lines.append("")
        lines.append(
            f"- 現在の固定版: `{nf.pinned_version}` → 同メジャー最新: `{nf.latest_in_major}`"
        )
        lines.append(f"- 公開されているセキュリティ更新: {sec_list}")
        lines.append("")

    if report.actionable_findings:
        lines.append("### 2. パッケージの未対応勧告 (OSV)")
        lines.append("")
        lines.append("| 区分 | パッケージ | 現在の版 | 修正版 | 勧告 ID | 概要 |")
        lines.append("|---|---|---|---|---|---|")
        for item in report.actionable_findings:
            fixed_str = ", ".join(item.fixed_versions) if item.fixed_versions else "未公開/要確認"
            safe_summary = item.summary.replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {item.ecosystem} | `{item.package}` | `{item.version}` | "
                f"`{fixed_str}` | `{item.canonical_id}` | {safe_summary} |"
            )
        lines.append("")

    if report.accepted_findings:
        lines.append("<details><summary>承知済み・理由付き除外 (" + str(len(report.accepted_findings)) + " 件)</summary>")
        lines.append("")
        for item in report.accepted_findings:
            lines.append(
                f"- `{item.ecosystem}` `{item.package}=={item.version}` (`{item.canonical_id}`): {item.accepted_reason}"
            )
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines)


def _write_step_summary(markdown: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    except OSError as exc:
        print(f"[WARN] GITHUB_STEP_SUMMARY への書き込みに失敗しました: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="SAIVerse の固定バージョンを公開の脆弱性情報 (OSV / nodejs.org) と照合する。"
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="SAIVerse リポジトリのルートディレクトリ",
    )
    args = parser.parse_args(argv)

    try:
        report = run_advisory_check(args.project_dir.resolve())
    except AdvisoryCheckError as exc:
        msg = f"## ❌ SAIVerse 依存関係のセキュリティ点検: 照会または解析に失敗しました\n\n- 理由: {exc}\n"
        print(msg, file=sys.stderr)
        _write_step_summary(msg)
        return 2

    markdown = format_markdown_report(report)
    print(markdown)
    _write_step_summary(markdown)
    return 1 if report.has_actionable else 0


if __name__ == "__main__":
    raise SystemExit(main())
