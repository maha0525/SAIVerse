"""Windows で Visual C++ の部品が無いときに、読める案内を出して起動を止める。

記憶の検索に使う onnxruntime は、Microsoft の「Visual C++ 再頒布可能パッケージ」の
部品 (msvcp140.dll など) を必要とする。この部品は Windows に最初から入っているもの
ではなく、入れたばかりの Windows には無い。無いまま起動すると、import の途中で
「DLL load failed while importing onnxruntime_pybind11_state」という長いエラーで落ち、
利用者には原因が分からない (docs/issues/archive/clean_windows_missing_vc_runtime_blocks_startup.md)。

setup.bat は scripts/install_vc_redist.ps1 でこの部品を自動で入れる。ここは、それでも
無いまま起動されたとき (導入を断った、古い setup で入れた) の受け皿。
"""
from __future__ import annotations

import sys
from typing import Callable, List

VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

# onnxruntime が読み込む部品 (onnxruntime_pybind11_state.pyd の import 表で確認)。
REQUIRED_DLLS = ("msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll")


def _can_load_dll(name: str) -> bool:
    import ctypes

    try:
        ctypes.WinDLL(name)
    except OSError:
        return False
    return True


def missing_vc_runtime_dlls(can_load: Callable[[str], bool] = _can_load_dll) -> List[str]:
    """読み込めない部品の名前。Windows 以外では常に空。"""
    if sys.platform != "win32":
        return []
    return [name for name in REQUIRED_DLLS if not can_load(name)]


def _onnxruntime_loads() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def build_message(missing: List[str]) -> str:
    names = ", ".join(missing)
    return (
        "\n"
        "[ERROR] SAIVerse を起動できません: Microsoft Visual C++ 再頒布可能パッケージが入っていません。\n"
        f"  記憶の検索に使う部品が、このパッケージを必要とします (見つからない部品: {names})。\n"
        "  次のどちらかで入れてから、もう一度起動してください。\n"
        "    - setup.bat をもう一度実行する (自動で入れます)\n"
        f"    - Microsoft の配布元から入れる: {VC_REDIST_URL}\n"
        "\n"
        "[ERROR] SAIVerse cannot start: the Microsoft Visual C++ Redistributable is not installed.\n"
        f"  The component used for memory search needs it (missing: {names}).\n"
        "  Install it one of these ways, then start SAIVerse again.\n"
        "    - Run setup.bat again (it installs the package automatically)\n"
        f"    - Install it from Microsoft: {VC_REDIST_URL}\n"
    )


def _print_for_any_console(text: str) -> None:
    # 英語の Windows のコンソールは日本語を表せない。表せない文字で起動時の案内ごと
    # 落ちないように、置き換えて出す。
    encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
    sys.stderr.write(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))
    sys.stderr.flush()


def exit_if_vc_runtime_missing(
    missing_dlls: Callable[[], List[str]] = missing_vc_runtime_dlls,
    onnxruntime_loads: Callable[[], bool] = _onnxruntime_loads,
) -> None:
    """部品が無くて onnxruntime が読み込めないときだけ、案内を出して終了する。

    部品の有無の見立てだけでは止めない。見立てが外れていて onnxruntime が読み込める
    PC を、ここで起動できなくしてはいけないので、実際に読み込めないことまで確かめる。
    """
    missing = missing_dlls()
    if not missing:
        return
    if onnxruntime_loads():
        return
    _print_for_any_console(build_message(missing))
    sys.exit(1)
