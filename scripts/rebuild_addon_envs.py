"""setup.bat / setup.sh と更新が呼ぶ: アドオンのパッケージを、いまの ``.venv`` と Python で入れ直す。

更新 (``scripts/update_engine.py`` の ``rebuild_addon_envs``、``--core-venv-recreated`` なし) が
呼ぶのは、v0.3.26 より前の setup や手で ``.venv`` を作り直した環境で、古い系列のまま残った
専用の環境を、setup を待たずに作り直すため。使える環境には触らない。

二つのことをこの順にする。設計: ``docs/intent/dependency_management.md`` §2-7。

1. 本体の ``.venv`` に入れる step の入れ直し (``--core-venv-recreated`` のときと、前の回の
   入れ直しが失敗していたとき)。setup が新しい ``.venv`` を作ると、そこには
   requirements.lock の部品しか入っていない。アドオンの setup の step のうち ``env`` の
   無い ``pip_install`` が本体の ``.venv`` に入れていたパッケージを、保存された答えのまま
   入れ直す (``saiverse.addon_installer.reinstall_core_venv_steps``)。setup は
   ``scripts/setup_venv.py prepare`` が ``create`` / ``renamed`` / ``resume`` を返した回
   (新しい ``.venv`` を作った回と、その続き) にだけ ``--core-venv-recreated`` を付ける。前の回の記録 (``.addon_env_rebuild_failures.json``)
   に本体の ``.venv`` への入れ直しの失敗があれば、付かない回でもそのアドオンをやり直す。
2. アドオン専用の Python 環境の作り直し。専用の環境 (``~/.saiverse/addon_install/<id>/envs/<name>/``)
   は、作ったときの本体の Python の系列 (3.12 など) に固定される。古い系列の環境は
   使えなくなる (``saiverse.addon_paths.get_addon_env_python`` が断る) ので、保存された
   答えのまま作り直す (``saiverse.addon_installer.rebuild_unusable_addon_envs``)。

``.venv`` の python で実行する (setup は ``.venv`` を有効にしてから呼ぶ)。

- 失敗したアドオンがあっても、終了コードは 0 (setup を止めない。SAIVerse 本体は動く)。
  失敗したアドオンと、どうすればよいかを表示し、1 と 2 の失敗を一つの
  ``.addon_env_rebuild_failures.json`` (SAIVerse のフォルダ) に書き残す。失敗が無ければ
  このファイルを消す。
- SAIVerse が動いている (または動いているか分からない) ときは、どちらもしない。動いている
  SAIVerse がそのアドオンを操作しているかもしれないため。入れ直しが要るアドオンを
  失敗として記録し、SAIVerse を閉じて setup をもう一度実行するよう表示する。
- 終了コード 1 は、このスクリプト自体が動けなかったとき (想定外の例外) だけ。setup は
  0 で終わったときだけ ``setup_venv.py finish`` を呼ぶ (0 以外なら ``.venv`` のやり直しの
  記録が残り、次の setup がパッケージとアドオンの入れ直しをやり直す)。

Usage:
    python scripts/rebuild_addon_envs.py [--core-venv-recreated]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running as `python scripts/rebuild_addon_envs.py` from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The same .env main.py loads, before any saiverse path is resolved: a
# SAIVERSE_HOME set there must point this script at the same saved answers and
# addon environments as SAIVerse itself (load_saiverse_dotenv).
from scripts.update_engine import load_saiverse_dotenv  # noqa: E402

load_saiverse_dotenv(REPO_ROOT)

from saiverse.addon_installer import (  # noqa: E402
    SETUP_CRASH_ADDON_ID,
    EnvRebuildResult,
    ProgressEvent,
    core_venv_failures_in_record,
    find_unusable_addon_envs,
    plan_core_venv_reinstalls,
    rebuild_unusable_addon_envs,
    record_env_rebuild_failures,
    reinstall_core_venv_steps,
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


def _where(r: EnvRebuildResult) -> str:
    """失敗の表示で、そのアドオンのどこに入れようとしたか。"""
    if r.core_venv:
        return "SAIVerse .venv"
    return f"addon environment: {', '.join(r.envs)}"


def _print_failures(failures: list[EnvRebuildResult], record_path: Path | None) -> None:
    print("", flush=True)
    print("[WARN] Some addon packages could not be installed again. SAIVerse itself works.")
    print("[WARN] アドオンのパッケージを入れ直せないものがありました。SAIVerse 本体は動きます。")
    for r in failures:
        steps = f" [{', '.join(r.steps)}]" if r.steps else ""
        print(f"  - {r.addon_id} ({_where(r)}){steps}: {r.error}")
    print("  These addons do not work until their packages are installed again.")
    print("  Run setup again, or reinstall the addon from the addon manager:")
    print("  uninstall it, then install it again. Its data is kept.")
    print("  これらのアドオンは、パッケージを入れ直すまで動きません。")
    print("  setup をもう一度実行するか、アドオン管理でそのアドオンを入れ直してください")
    print("  (アンインストールしてから、もう一度インストールします。アドオンのデータは残ります)。")
    if record_path is not None:
        print(f"  The failures are recorded in {record_path.name}.")
    sys.stdout.flush()


def _core_venv_targets(core_venv_recreated: bool) -> tuple[bool, set[str] | None]:
    """本体の ``.venv`` に入れる step を入れ直すか、入れ直すならどのアドオンか (None は全部)。"""
    if core_venv_recreated:
        return True, None
    previous = core_venv_failures_in_record()
    if SETUP_CRASH_ADDON_ID in previous:
        return True, None
    if previous:
        return True, previous
    return False, None


def main(expansion: Path | None = None, *, core_venv_recreated: bool = False) -> int:
    # 標準出力がパイプのとき、日本語を書けない文字コードでも止まらないように
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    expansion = expansion or EXPANSION_DATA_DIR

    run_core, core_ids = _core_venv_targets(core_venv_recreated)
    core_plans, core_plan_failures = (
        plan_core_venv_reinstalls(expansion, core_ids) if run_core else ([], [])
    )
    pending = find_unusable_addon_envs(expansion)
    if not core_plans and not core_plan_failures and not pending:
        record_env_rebuild_failures([])
        print("[OK] Addon packages match the current .venv and Python", flush=True)
        return 0

    running, reason = _saiverse_running()
    if running:
        error = f"SAIVerse may be running ({reason}); close SAIVerse and run setup again"
        failures = [
            EnvRebuildResult(
                addon_id=plan.addon_id, envs=[], steps=[s.name for s in plan.steps],
                error=error, core_venv=True,
            )
            for plan in core_plans
        ]
        failures += core_plan_failures
        failures += [
            EnvRebuildResult(addon_id=addon_id, envs=envs, error=error)
            for addon_id, envs in pending.items()
        ]
        path = record_env_rebuild_failures(failures)
        print("", flush=True)
        print("[WARN] SAIVerse may be running, so the addon packages were not installed again.")
        print("  Close every SAIVerse window, then run setup again.")
        print("[WARN] SAIVerse が動いている可能性があるため、アドオンのパッケージを入れ直していません。")
        print("  SAIVerse の窓をすべて閉じてから、setup をもう一度実行してください。")
        for r in failures:
            print(f"  - {r.addon_id} ({_where(r)})")
        if path is not None:
            print(f"  Recorded in {path.name}.")
        sys.stdout.flush()
        return 0

    results: list[EnvRebuildResult] = []
    if core_plans or core_plan_failures:
        print("", flush=True)
        print("[SETUP] Installing addon packages into the SAIVerse .venv again...", flush=True)
        results += reinstall_core_venv_steps(
            expansion, progress_callback=_print_progress, addon_ids=core_ids,
        )
    if pending:
        print("", flush=True)
        print("[SETUP] Rebuilding addon Python environments made with another Python...", flush=True)
        results += rebuild_unusable_addon_envs(expansion, progress_callback=_print_progress)
    # 両方の結果を一度に書く (片方だけで書くと、もう片方の失敗の記録が消える)
    path = record_env_rebuild_failures(results)
    failures = [r for r in results if not r.ok]
    for r in results:
        if not r.ok:
            continue
        if r.core_venv:
            print(
                f"[OK] Installed the packages of {r.addon_id} into the SAIVerse .venv again "
                f"({', '.join(r.steps)})",
                flush=True,
            )
        else:
            print(f"[OK] Rebuilt the Python environment of {r.addon_id} ({', '.join(r.envs)})", flush=True)
    if failures:
        _print_failures(failures, path)
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--core-venv-recreated",
        action="store_true",
        help="setup created a new .venv in this run; install the addon packages that go into it again",
    )
    return parser.parse_args(argv)


def _main_recording_crashes(argv: list[str] | None = None) -> int:
    """``main`` that leaves a record for the banner even when it crashes, so an
    unexpected error does not leave an addon unusable with nothing on screen."""
    args = _parse_args(argv or [])
    # 落ちたときの記録が、本体の .venv への入れ直しが済んでいないことを失わないように、
    # main が記録を書き換える前に読んでおく
    try:
        core_pending = args.core_venv_recreated or bool(core_venv_failures_in_record())
    except Exception:  # 記録を読めなくても、落ちたときの記録は書く
        core_pending = True
    try:
        return main(core_venv_recreated=args.core_venv_recreated)
    except Exception as exc:  # the record is the point; the traceback is printed too
        import traceback

        traceback.print_exc()
        try:
            record_env_rebuild_failures([
                EnvRebuildResult(
                    addon_id=SETUP_CRASH_ADDON_ID, envs=[], error=f"{type(exc).__name__}: {exc}",
                    core_venv=core_pending,
                )
            ])
        except Exception:
            traceback.print_exc()
        print("[WARN] The addon packages could not be checked or installed again (see above).")
        print("[WARN] アドオンのパッケージを確かめられない・入れ直せないまま終わりました (上の表示を見てください)。")
        sys.stdout.flush()
        return 1


if __name__ == "__main__":
    sys.exit(_main_recording_crashes(sys.argv[1:]))
