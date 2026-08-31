"""Phase 2-E: addon catalog API の E2E 検証スクリプト。

起動中の SAIVerse に対して uninstall → install を叩いて SSE 進捗を表示する。
registry.json はローカルの ``temp/saiverse-addon-registry/registry.json`` を
指す環境変数を SAIVerse 起動時に渡しておく必要がある:

    set SAIVERSE_ADDON_REGISTRY_URL=C:\\Users\\shuhe\\workspace\\SAIVerse\\temp\\saiverse-addon-registry\\registry.json
    python main.py city_a

その上で:
    python scripts/test_addon_catalog_api.py

オプション:
    --base http://127.0.0.1:8000  (デフォルト)
    --addon saiverse-elyth-addon  (デフォルト)
    --skip-uninstall              (uninstall せず install のみ試す)
    --skip-install                (uninstall のみ)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Iterator
from urllib.parse import urljoin

import urllib.error
import urllib.request


def _http_get(base: str, path: str) -> dict:
    url = urljoin(base, path)
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _http_post_sse(base: str, path: str, body: dict) -> Iterator[dict]:
    """POST → SSE ストリームを 1 event ずつ yield。"""
    url = urljoin(base, path)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {resp.read().decode('utf-8')}")
        buffer = ""
        # SSE は data: ...\n\n の繰り返し
        for raw_chunk in resp:
            chunk = raw_chunk.decode("utf-8", errors="replace")
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                for line in block.splitlines():
                    if line.startswith("data: "):
                        payload = line[len("data: "):]
                        try:
                            yield json.loads(payload)
                        except json.JSONDecodeError:
                            print(f"[malformed SSE] {payload}", file=sys.stderr)


def _print_event(event: dict) -> None:
    phase = event.get("phase", "?")
    msg = event.get("message", "")
    if event.get("step_index"):
        print(f"  [{phase}] ({event['step_index']}/{event['step_total']}) {msg}")
    elif phase == "finished":
        ok = event.get("ok")
        restart = event.get("restart_required")
        err = event.get("error")
        manifest = event.get("manifest")
        tail = f"ok={ok} restart_required={restart}"
        if manifest:
            tail += f" version={manifest.get('version')}"
        if err:
            tail += f" error={err}"
        print(f"  [{phase}] {tail}")
    else:
        print(f"  [{phase}] {msg}")


def run_test(base: str, addon_id: str, do_uninstall: bool, do_install: bool) -> int:
    print(f"=== base: {base}")
    print(f"=== addon: {addon_id}\n")

    print("--- GET /api/addon-catalog/debug/registry-url ---")
    info = _http_get(base, "/api/addon-catalog/debug/registry-url")
    print(f"  effective_url: {info['effective_url']}")
    print(f"  env_override:  {info['env_override']}")
    print()

    print("--- GET /api/addon-catalog/registry ---")
    reg = _http_get(base, "/api/addon-catalog/registry")
    addons = reg["registry"]["addons"]
    print(f"  {len(addons)} addon(s):")
    for a in addons:
        print(f"    - {a['id']} (latest {a['latest']})")
    print()

    print("--- GET /api/addon-catalog/installed (BEFORE) ---")
    before = _http_get(base, "/api/addon-catalog/installed")
    for a in before:
        print(f"    - {a['addon_id']} v{a['version']} (mv={a['manifest_version']})")
    print()

    if do_uninstall and any(a["addon_id"] == addon_id for a in before):
        print(f"--- POST /api/addon-catalog/uninstall {addon_id} ---")
        for ev in _http_post_sse(base, "/api/addon-catalog/uninstall",
                                  {"addon_id": addon_id, "delete_data": False}):
            _print_event(ev)
        time.sleep(0.5)
        print()
    elif do_uninstall:
        print(f"  (skip uninstall: {addon_id} is not installed)\n")

    if do_install:
        print(f"--- POST /api/addon-catalog/install {addon_id} ---")
        for ev in _http_post_sse(base, "/api/addon-catalog/install",
                                  {"addon_id": addon_id}):
            _print_event(ev)
        time.sleep(0.5)
        print()

    print("--- GET /api/addon-catalog/installed (AFTER) ---")
    after = _http_get(base, "/api/addon-catalog/installed")
    for a in after:
        print(f"    - {a['addon_id']} v{a['version']} (mv={a['manifest_version']})")
    print()

    target = [a for a in after if a["addon_id"] == addon_id]
    if do_install and not target:
        print(f"FAIL: {addon_id} not in installed list after install")
        return 1
    if not do_install and do_uninstall and target:
        print(f"FAIL: {addon_id} still in installed list after uninstall")
        return 1

    print("[OK] all checks passed")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8000")
    p.add_argument("--addon", default="saiverse-elyth-addon")
    p.add_argument("--skip-uninstall", action="store_true")
    p.add_argument("--skip-install", action="store_true")
    args = p.parse_args()

    try:
        return run_test(
            base=args.base.rstrip("/"),
            addon_id=args.addon,
            do_uninstall=not args.skip_uninstall,
            do_install=not args.skip_install,
        )
    except urllib.error.URLError as e:
        print(f"ERROR: cannot connect to {args.base}: {e}", file=sys.stderr)
        print(
            "    SAIVerse が起動してることと、ポートが正しいことを確認してください。",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
