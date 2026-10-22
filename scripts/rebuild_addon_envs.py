"""setup.bat / setup.sh が呼ぶ: アドオン専用の Python 環境を、いまの Python で作り直す。

アドオン専用の環境 (``~/.saiverse/addon_install/<id>/envs/<name>/``) は、作ったときの
本体の Python の系列 (3.12 など) に固定される。setup が ``.venv`` を Python 3.13 で
作り直したあと、古い系列の環境は使えなくなる (``saiverse.addon_paths.get_addon_env_python``
が断る)。このスクリプトは、そういう環境を保存された答えのまま作り直す
(``saiverse.addon_installer.rebuild_unusable_addon_envs``)。使えない環境が無ければ
何もしない。設計: ``docs/intent/dependency_management.md`` §2-7。

``.venv`` の python で実行する (setup は ``.venv`` を有効にしてから呼ぶ)。

- 作り直しに失敗したアドオンがあっても、終了コードは 0 (setup を止めない。SAIVerse
  本体は動く)。失敗したアドオンと、どうすればよいかを表示し、
  ``.addon_env_rebuild_failures.json`` (SAIVerse のフォルダ) に書き残す。失敗が無ければ
  このファイルを消す。
- SAIVerse が動いている (または動いているか分からない) ときは作り直さない。動いている
  SAIVerse がそのアドオンを操作しているかもしれないため。作り直しが要るアドオンを
  失敗として記録し、SAIVerse を閉じて setup をもう一度実行するよう表示する。
- 終了コード 1 は、このスクリプト自体が動けなかったとき (想定外の例外) だけ。

Usage:
    python scripts/rebuild_addon_envs.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow running as `python scripts/rebuild_addon_envs.py` from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from saiverse.addon_installer import (  # noqa: E402
    EnvRebuildResult,
    ProgressEvent,
    find_unusable_addon_envs,
    rebuild_unusable_addon_envs,
    record_env_rebuild_failures,
)
from saiverse.data_paths import EXPANSION_DATA_DIR  # noqa: E402


def _print_progress(event: ProgressEvent) -> None:
    prefix = "  "
    if event.step_index is not None and event.step_total is not None:
        prefix += f"({event.step_index}/{event.step_total}) "
    print(f"{prefix}{event.message}", flush=True)


def _saiverse_running() -> tuple[bool, str]:
    """SAIVerse のプロセスが動いている (または動いているか分からない) か。"""
    from saiverse.runtime_marker import marker_status

    state, reason = marker_status()
    return state != "stopped", reason


def _print_failures(failures: list[EnvRebuildResult], record_path: Path | None) -> None:
    print("", flush=True)
    print("[WARN] Some addon Python environments could not be rebuilt. SAIVerse itself works.")
    print("[WARN] アドオンの専用の Python 環境を作り直せないものがありました。SAIVerse 本体は動きます。")
    for r in failures:
        print(f"  - {r.addon_id} ({', '.join(r.envs)}): {r.error}")
    print("  These addons cannot use their own Python environment until it is rebuilt.")
    print("  Run setup again, or reinstall the addon from the addon manager:")
    print("  uninstall it, then install it again. Its data is kept.")
    print("  作り直すまで、これらのアドオンは専用の Python 環境を使う機能が動きません。")
    print("  setup をもう一度実行するか、アドオン管理でそのアドオンを入れ直してください")
    print("  (アンインストールしてから、もう一度インストールします。アドオンのデータは残ります)。")
    if record_path is not None:
        print(f"  The failures are recorded in {record_path.name}.")
    sys.stdout.flush()


def main(expansion: Path | None = None) -> int:
    # 標準出力がパイプのとき、日本語を書けない文字コードでも止まらないように
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    expansion = expansion or EXPANSION_DATA_DIR

    pending = find_unusable_addon_envs(expansion)
    if not pending:
        record_env_rebuild_failures([])
        print("[OK] Addon Python environments match the current Python", flush=True)
        return 0

    running, reason = _saiverse_running()
    if running:
        failures = [
            EnvRebuildResult(
                addon_id=addon_id,
                envs=envs,
                error=f"SAIVerse may be running ({reason}); close SAIVerse and run setup again",
            )
            for addon_id, envs in pending.items()
        ]
        path = record_env_rebuild_failures(failures)
        print("", flush=True)
        print("[WARN] SAIVerse may be running, so the addon Python environments were not rebuilt.")
        print("  Close every SAIVerse window, then run setup again.")
        print("[WARN] SAIVerse が動いている可能性があるため、アドオンの専用の Python 環境を作り直していません。")
        print("  SAIVerse の窓をすべて閉じてから、setup をもう一度実行してください。")
        for r in failures:
            print(f"  - {r.addon_id} ({', '.join(r.envs)})")
        if path is not None:
            print(f"  Recorded in {path.name}.")
        sys.stdout.flush()
        return 0

    print("", flush=True)
    print("[SETUP] Rebuilding addon Python environments made with another Python...", flush=True)
    results = rebuild_unusable_addon_envs(expansion, progress_callback=_print_progress)
    path = record_env_rebuild_failures(results)
    failures = [r for r in results if not r.ok]
    for r in results:
        if r.ok:
            print(f"[OK] Rebuilt the Python environment of {r.addon_id} ({', '.join(r.envs)})", flush=True)
    if failures:
        _print_failures(failures, path)
    return 0


def _main_recording_crashes() -> int:
    """``main`` that leaves a record for the banner even when it crashes, so an
    unexpected error does not leave an addon unusable with nothing on screen."""
    try:
        return main()
    except Exception as exc:  # the record is the point; the traceback is printed too
        import traceback

        traceback.print_exc()
        try:
            record_env_rebuild_failures([
                EnvRebuildResult(addon_id="(setup)", envs=[], error=f"{type(exc).__name__}: {exc}")
            ])
        except Exception:
            traceback.print_exc()
        print("[WARN] The addon Python environments could not be checked or rebuilt (see above).")
        print("[WARN] アドオンの専用の Python 環境を確かめられない・作り直せないまま終わりました (上の表示を見てください)。")
        sys.stdout.flush()
        return 1


if __name__ == "__main__":
    sys.exit(_main_recording_crashes())
