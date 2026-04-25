"""アドオン external/ 配下ライブラリの名前空間隔離ローダ。

## 解決したい問題

アドオン (拡張パック) は ``expansion_data/<addon>/external/`` 配下に、
上流リポジトリ (例: GPT-SoVITS, Whisper 等) を直接 clone して取り込む。
これらの上流コードはトップレベルに ``tools/`` ``utils/`` などの汎用名で
パッケージを持つことが多く、SAIVerse 本体側の ``tools/`` などと **同名衝突**
する。

従来の対策(各パックが ``sys.modules['tools']`` を一時的に剥がす context
manager)はシリアル実行では動くが、**並列スレッドで本体側 import が走ると
パック側の external/ tools を掴んでしまう**(2026-04 時点で
``addon_spell_help`` で実際に発生)。

## このモジュールの方針

ホスト側で1個だけ実装し、各パックの ``external/`` を**呼び出し元コードの
ファイルパスベース**で振り分ける。

1. パック登録時に ``external/<lib>/`` 配下から「ホスト top-level と同名」の
   パッケージを検出し、``addons.<sanitized_addon>.<name>`` として
   ``sys.modules`` に事前ロードする。
2. ``builtins.__import__`` をパッチして、絶対 import が呼ばれた際に:
   - 名前のトップ要素が「いずれかの登録パックでの衝突候補」なら
   - 呼び出し元の ``__file__`` を見て、それが登録 external/ 配下なら
   - その登録の ``addons.<sanitized_addon>.<name>`` 名前空間にリダイレクト
3. ホスト側コードからの import は呼び出し元が external/ 配下ではないため
   常にホスト本来の名前空間に解決される。

呼び出し元判定はファイルパス(``__file__``)ベースなので、複数スレッドが
並列実行しても**どのスレッドが import しているかではなく、どのコードが
import しているか**で名前解決が決まる。スレッド間の汚染が原理的に発生しない。

## パック作者向けの利点

パック側で ``sys.modules['tools']`` を剥がす hack は不要。``external/`` を
普通に sys.path に追加して上流コードを import するだけで、衝突は本機構が
透過的に解決する。
"""
from __future__ import annotations

import builtins
import importlib.util
import logging
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Host top-level package detection
# ---------------------------------------------------------------------------


def _saiverse_root() -> Path:
    """SAIVerse リポジトリの root を返す (saiverse/ の親)。"""
    return Path(__file__).resolve().parent.parent


_HOST_TOP_LEVEL_CACHE: Optional[Set[str]] = None


def _detect_host_top_level() -> Set[str]:
    """SAIVerse 直下の top-level Python パッケージ/モジュール名を列挙する。

    addon の external 内コードがこれらの名前を絶対 import すると、
    ホスト側パッケージとの衝突候補となる。
    """
    global _HOST_TOP_LEVEL_CACHE
    if _HOST_TOP_LEVEL_CACHE is not None:
        return _HOST_TOP_LEVEL_CACHE

    root = _saiverse_root()
    names: Set[str] = set()
    if not root.exists():
        _HOST_TOP_LEVEL_CACHE = names
        return names

    skip_dirs = {".git", ".venv", "node_modules", "__pycache__",
                 "expansion_data", "external", "frontend", "sbert",
                 "user_data", "test_data"}
    for entry in root.iterdir():
        if entry.name.startswith(".") or entry.name in skip_dirs:
            continue
        if entry.is_dir() and (entry / "__init__.py").exists():
            names.add(entry.name)
        elif entry.is_file() and entry.suffix == ".py" and entry.stem != "__init__":
            names.add(entry.stem)
    _HOST_TOP_LEVEL_CACHE = names
    LOGGER.debug("addon_external_loader: host top-level=%s", sorted(names))
    return names


# ---------------------------------------------------------------------------
# Registration data structures
# ---------------------------------------------------------------------------


@dataclass
class _Registration:
    """1個のアドオンに対する登録情報。"""

    addon_name: str  # オリジナル名 (ハイフンを含むことがある)
    sanitized_name: str  # Python 識別子に正規化した名前
    external_root: Path  # external/ ディレクトリ (絶対パス、解決済み)
    namespace_prefix: str  # ``addons.<sanitized>``
    # external/<lib>/<name> として実在し、かつホスト top-level と衝突する名前
    conflicting_names: Set[str] = field(default_factory=set)
    # name -> 物理パス (__init__.py または .py)
    name_to_path: Dict[str, Path] = field(default_factory=dict)


_lock = threading.Lock()
_registrations: Dict[str, _Registration] = {}  # sanitized_name -> registration
_import_patched = False
_original_import = builtins.__import__


# ---------------------------------------------------------------------------
# __import__ patching
# ---------------------------------------------------------------------------


def _is_under(file_path: Path, root: Path) -> bool:
    """``file_path`` が ``root`` 以下にあるか。例外なしで判定する。"""
    try:
        file_path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_caller_file(caller_globals: Optional[Dict[str, Any]]) -> Optional[Path]:
    if not caller_globals:
        return None
    fname = caller_globals.get("__file__")
    if not fname:
        return None
    try:
        return Path(fname).resolve()
    except (OSError, ValueError):
        return None


def _find_redirect(name: str, caller_globals: Optional[Dict[str, Any]]) -> Optional[str]:
    """absolute import (level=0) の name が、登録パックの external/ 配下から
    呼ばれた衝突名なら、リダイレクト先名を返す。該当しなければ None。
    """
    if not _registrations or not name:
        return None
    top = name.split(".", 1)[0]

    # まず top が「いずれかの登録に属する衝突名」かどうかをホット判定
    candidates = [
        reg for reg in _registrations.values() if top in reg.conflicting_names
    ]
    if not candidates:
        return None

    caller_file = _resolve_caller_file(caller_globals)
    if not caller_file:
        return None

    for reg in candidates:
        if _is_under(caller_file, reg.external_root):
            return f"{reg.namespace_prefix}.{name}"
    return None


def _patched_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name:
        redirect = _find_redirect(name, globals)
        if redirect is not None:
            LOGGER.debug(
                "addon_external_loader: redirect %r -> %r (caller=%s)",
                name, redirect,
                globals.get("__file__") if globals else None,
            )
            return _original_import(redirect, globals, locals, fromlist, level)
    return _original_import(name, globals, locals, fromlist, level)


def _ensure_import_patched() -> None:
    global _import_patched
    if _import_patched:
        return
    builtins.__import__ = _patched_import
    _import_patched = True
    LOGGER.info("addon_external_loader: __import__ patched for namespace isolation")


def _ensure_addons_namespace() -> None:
    """``addons`` および ``addons.<sanitized>`` 仮想パッケージを sys.modules に登録。"""
    if "addons" not in sys.modules:
        addons_module = type(sys)("addons")
        # 名前空間パッケージとして空のパスリストを持たせる
        addons_module.__path__ = []  # type: ignore[attr-defined]
        sys.modules["addons"] = addons_module


def _ensure_addon_namespace(reg: _Registration) -> None:
    if reg.namespace_prefix in sys.modules:
        return
    addon_module = type(sys)(reg.namespace_prefix)
    addon_module.__path__ = [str(reg.external_root)]  # type: ignore[attr-defined]
    sys.modules[reg.namespace_prefix] = addon_module


# ---------------------------------------------------------------------------
# Pre-loading conflicting packages under namespaced names
# ---------------------------------------------------------------------------


def _preload_conflicting(reg: _Registration) -> None:
    """external/<lib>/<conflict_name> を addons.<sanitized>.<conflict_name> として
    sys.modules に事前ロードする。

    これにより、リダイレクト先 ``addons.<sanitized>.tools.audio_sr`` のような
    深い名前を ``__import__`` が解決できる(リダイレクト後のパスでパッケージを
    探す際、トップ要素 ``addons.<sanitized>.tools`` が既に登録済みであれば
    その submodule_search_locations 経由でサブモジュールを発見できる)。
    """
    for conflict_name, source_path in reg.name_to_path.items():
        full_name = f"{reg.namespace_prefix}.{conflict_name}"
        if full_name in sys.modules:
            continue
        try:
            if source_path.is_dir():
                init_file = source_path / "__init__.py"
                if not init_file.exists():
                    continue
                spec = importlib.util.spec_from_file_location(
                    full_name,
                    str(init_file),
                    submodule_search_locations=[str(source_path)],
                )
            else:
                spec = importlib.util.spec_from_file_location(
                    full_name, str(source_path)
                )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[full_name] = module
            spec.loader.exec_module(module)
            LOGGER.debug(
                "addon_external_loader: pre-loaded %s from %s",
                full_name, source_path,
            )
        except Exception:
            # ロード失敗時は cache を引き上げて、後続の import 時に再試行できる
            # ようにする(本機構の責任範囲を超えるので警告のみ)。
            sys.modules.pop(full_name, None)
            LOGGER.warning(
                "addon_external_loader: failed to pre-load %s from %s",
                full_name, source_path, exc_info=True,
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register_addon_external(addon_name: str, external_root: Path) -> None:
    """指定アドオンの external/ ディレクトリを名前空間隔離下に登録する。

    Args:
        addon_name: アドオン名 (パックの addon.json の name)。ハイフンを含んでも可。
        external_root: ``expansion_data/<addon>/external/`` のような Path。

    呼ばれた後の挙動:
        - ``external_root/<lib>/<name>`` のうちホスト top-level と衝突する名前を検出
        - 検出した各パッケージを ``addons.<sanitized>.<name>`` として sys.modules に事前ロード
        - ``builtins.__import__`` をパッチ(初回のみ)
        - 以降、external/ 配下のコードが ``import <name>`` した際、自動的に
          名前空間版にリダイレクトされる

    呼び出し元コードからの ``import <name>`` は引き続きホスト本来の名前空間に解決される。
    """
    if not external_root.exists():
        LOGGER.debug(
            "addon_external_loader: %s/external does not exist, skipping (%s)",
            addon_name, external_root,
        )
        return

    sanitized = addon_name.replace("-", "_").replace(".", "_")
    if not sanitized.isidentifier():
        LOGGER.warning(
            "addon_external_loader: addon name %r cannot be sanitized to identifier",
            addon_name,
        )
        return

    namespace_prefix = f"addons.{sanitized}"

    with _lock:
        if sanitized in _registrations:
            LOGGER.debug(
                "addon_external_loader: %s already registered, skipping",
                addon_name,
            )
            return

        host_top = _detect_host_top_level()
        external_root_resolved = external_root.resolve()

        # external_root/<lib>/<name> を走査し、ホスト top-level と衝突する name を
        # その物理パスとともに収集
        name_to_path: Dict[str, Path] = {}
        for lib_dir in external_root_resolved.iterdir():
            if not lib_dir.is_dir() or lib_dir.name.startswith("."):
                continue
            for entry in lib_dir.iterdir():
                if entry.is_dir() and (entry / "__init__.py").exists():
                    if entry.name in host_top and entry.name not in name_to_path:
                        name_to_path[entry.name] = entry
                elif entry.is_file() and entry.suffix == ".py":
                    stem = entry.stem
                    if stem in host_top and stem not in name_to_path and stem != "__init__":
                        name_to_path[stem] = entry

        if not name_to_path:
            LOGGER.debug(
                "addon_external_loader: %s has no host name conflicts under %s",
                addon_name, external_root_resolved,
            )
            # 衝突がなければパッチ自体不要
            return

        reg = _Registration(
            addon_name=addon_name,
            sanitized_name=sanitized,
            external_root=external_root_resolved,
            namespace_prefix=namespace_prefix,
            conflicting_names=set(name_to_path.keys()),
            name_to_path=name_to_path,
        )

        _ensure_addons_namespace()
        _ensure_addon_namespace(reg)
        _preload_conflicting(reg)
        _registrations[sanitized] = reg
        _ensure_import_patched()

        LOGGER.info(
            "addon_external_loader: registered %s -> %s "
            "(conflicting_names=%s)",
            namespace_prefix, external_root_resolved,
            sorted(reg.conflicting_names),
        )


def get_registered_addons() -> Iterable[str]:
    """登録済みアドオン名 (sanitized) のイテレータ。診断/テスト用。"""
    return tuple(_registrations.keys())


def get_namespace_prefix(addon_name: str) -> Optional[str]:
    """``register_addon_external`` で登録されたアドオンの名前空間プレフィックスを返す。

    パック側で `importlib.import_module(prefix + '.GPT_SoVITS....')` などをする際に使う。
    """
    sanitized = addon_name.replace("-", "_").replace(".", "_")
    reg = _registrations.get(sanitized)
    return reg.namespace_prefix if reg else None
