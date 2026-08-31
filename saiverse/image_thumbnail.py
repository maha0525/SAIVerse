"""画像アイテムの軽量サムネイル (webp) 生成とキャッシュ。

右サイドバーのアイテム一覧など「小さく大量に並べる」用途向けに、オリジナル
画像から一辺 ``max_size`` px に収まる webp サムネイルを生成する。生成物は
``<home>/image/.thumbnails/`` にキャッシュし、二度目以降は使い回す。

キャッシュキーはオリジナルの絶対パス・mtime・サイズ・max_size から作る。
オリジナルが差し替わる (mtime か size が変わる) と別キーになるため、古い
サムネイルを掴み続けることはない。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps

LOGGER = logging.getLogger(__name__)

# サイドバーのカードは実表示で ~40-64px。retina を見込んで 256 を上限に取る。
DEFAULT_MAX_SIZE = 256


def _cache_dir(src_path: Path, home: Optional[Path]) -> Path:
    """サムネイルの保存先ディレクトリ。home があればそこの image/.thumbnails、
    無ければオリジナルと同階層の .thumbnails に置く。"""
    base = (home / "image") if home is not None else src_path.parent
    return base / ".thumbnails"


def get_or_create_thumbnail(
    src_path: Path,
    *,
    max_size: int = DEFAULT_MAX_SIZE,
    home: Optional[Path] = None,
) -> Path:
    """``src_path`` の webp サムネイルパスを返す。未生成ならその場で生成する。

    生成に失敗した場合は例外を送出する (呼び出し側でオリジナルへフォールバック
    する想定)。
    """
    st = src_path.stat()
    raw_key = f"{src_path.resolve()}|{st.st_mtime_ns}|{st.st_size}|{max_size}"
    key = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()

    cache_dir = _cache_dir(src_path, home)
    thumb_path = cache_dir / f"{key}.webp"

    if thumb_path.exists():
        return thumb_path

    cache_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(src_path) as im:
        # 写真の EXIF 回転を反映してから縮小する。
        im = ImageOps.exif_transpose(im)

        # webp は alpha を扱えるので、透過があれば残す。
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")

        im.thumbnail((max_size, max_size), Image.LANCZOS)

        # 途中で失敗しても壊れた .webp を残さないよう、一時ファイル経由で置く。
        tmp_path = thumb_path.with_suffix(".webp.tmp")
        im.save(tmp_path, "WEBP", quality=80, method=6)
        tmp_path.replace(thumb_path)

    LOGGER.debug("Generated thumbnail: %s -> %s", src_path, thumb_path)
    return thumb_path
