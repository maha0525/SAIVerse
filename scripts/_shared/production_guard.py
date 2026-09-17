"""テスト用スクリプトが本番の SAIVerse ホーム (~/.saiverse) へ書き込むのを拒否する番人。

会話ランナー・一日シム・世界複製・ペルソナ複製は、偽の会話や複製データを DB や
ペルソナのフォルダへ書く。書き先が本番の中なら、本番ペルソナの記憶と世界を汚す。

本番の場所は、環境変数 SAIVERSE_HOME ではなく、常に ``Path.home() / ".saiverse"`` で決める。
SAIVERSE_HOME はテスト環境を指すために書き換える変数なので、それを本番の基準にすると、
テスト環境を指した瞬間に番人が本番を見張らなくなる (2026-09-17 に run_conversation.py で
見つかった穴)。上書きフラグは用意しない (本番へ向けたい使い道が無い)。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional, Union

PathLike = Union[str, os.PathLike]


class ProductionPathError(RuntimeError):
    """書き込み先が本番のホームの中にある。メッセージをそのまま表示する。"""


def production_home() -> Path:
    """本番のホーム。SAIVERSE_HOME の値に関係なく ``~/.saiverse`` を解決したもの。"""
    return (Path.home() / ".saiverse").resolve()


def effective_home() -> Path:
    """このプロセスが実際に使うホーム (``saiverse.data_paths.get_saiverse_home`` と同じ導出)。"""
    return Path(os.getenv("SAIVERSE_HOME") or Path.home() / ".saiverse")


def effective_user_data_dir() -> Path:
    """このプロセスが実際に使う user_data (``saiverse.data_paths.USER_DATA_DIR`` と同じ導出)。"""
    env = os.getenv("SAIVERSE_USER_DATA_DIR")
    return Path(env) if env else effective_home() / "user_data"


def is_under_production(path: PathLike) -> bool:
    """path が本番のホームそのもの、またはその配下なら True。

    両辺を resolve するので、ジャンクションやシンボリックリンク経由の指定も捕まえる。
    ``~`` は展開して判定する (拒否する側に倒す)。
    """
    target = Path(path).expanduser().resolve()
    return target.is_relative_to(production_home())


def refuse_production_paths(
    targets: Mapping[str, Optional[PathLike]],
    *,
    reason: str,
    error_cls: type[Exception] = ProductionPathError,
) -> None:
    """targets のどれかが本番のホームの中なら error_cls を送出する。

    Args:
        targets: {表示名: パス}。値が None の項目は、この実行で書かないものとして飛ばす。
        reason: 本番に向けてはいけない理由 (エラーメッセージに入れる)。
        error_cls: 送出する例外の型。各スクリプトの既存のエラー型を渡す。
    """
    offenders = [
        f"{label}={Path(path)}"
        for label, path in targets.items()
        if path is not None and is_under_production(path)
    ]
    if not offenders:
        return
    raise error_cls(
        f"書き込み先が本番のホーム ({production_home()}) の中を指しています: "
        f"{', '.join(offenders)}。{reason}"
        " 本番の場所は SAIVERSE_HOME の値に関係なく ~/.saiverse で判定します。"
        " テスト環境 (test_data/ など) を指してください。"
    )
