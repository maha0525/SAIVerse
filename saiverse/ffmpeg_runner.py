"""ffmpeg ラッパー。

`imageio-ffmpeg` がプラットフォーム別の ffmpeg バイナリを pip wheel で同梱しているので、
ユーザーが個別に ffmpeg をインストールする必要はない。本モジュールはそのバイナリを subprocess
経由で呼び出すラッパーを提供する。

主な利用箇所:
    - api/routes/media.py: アップロードされた音声・動画の正規化
    - saiverse/media_summary.py: サマリ生成前のフォーマット検証
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple

LOGGER = logging.getLogger(__name__)

# ffmpeg の duration 出力パターン: "Duration: HH:MM:SS.ss" の形式
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")


def get_ffmpeg_path() -> Optional[str]:
    """imageio-ffmpeg が同梱する ffmpeg バイナリのパスを返す。

    Returns:
        ffmpeg 実行ファイルの絶対パス。imageio-ffmpeg が未インストールまたはバイナリ取得失敗時は None。
    """
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        LOGGER.warning("imageio_ffmpeg is not installed; ffmpeg-dependent features will be disabled")
        return None
    except Exception:
        LOGGER.exception("Failed to resolve ffmpeg path via imageio_ffmpeg")
        return None


def is_ffmpeg_available() -> bool:
    """ffmpeg バイナリが利用可能かを判定する。"""
    return get_ffmpeg_path() is not None


def run_ffmpeg(args: list[str], *, timeout: int = 120) -> Tuple[int, str, str]:
    """ffmpeg を subprocess で実行する。

    Args:
        args: ffmpeg に渡す引数のリスト (バイナリパス自体は含めない)
        timeout: タイムアウト秒数

    Returns:
        (returncode, stdout, stderr) のタプル。ffmpeg 未インストール時は (127, "", "ffmpeg not available")
    """
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return 127, "", "ffmpeg not available"

    cmd = [ffmpeg, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        LOGGER.warning("ffmpeg timed out after %ds: %s", timeout, " ".join(args))
        return 124, "", "ffmpeg timed out"
    except Exception as exc:
        LOGGER.exception("ffmpeg invocation failed")
        return 1, "", f"ffmpeg invocation failed: {exc}"


def probe_duration(path: Path) -> Optional[float]:
    """音声・動画ファイルの長さ (秒) を返す。

    imageio-ffmpeg は ffmpeg 本体のみ同梱で ffprobe は持たないため、
    `ffmpeg -i <input>` の stderr 出力から Duration をパースする方式を採る。

    Returns:
        ファイルの長さ (秒)、取得失敗時は None
    """
    if not path.exists():
        LOGGER.warning("probe_duration: file does not exist: %s", path)
        return None

    # ffmpeg -i input は本来エラー扱い (出力指定なし) だが、stderr に metadata が出る
    returncode, _stdout, stderr = run_ffmpeg(["-i", str(path)], timeout=30)
    # returncode は 1 になる (出力なし) が、stderr に Duration が含まれていれば成功
    match = _DURATION_RE.search(stderr)
    if not match:
        LOGGER.warning("probe_duration: could not parse duration from ffmpeg output for %s", path)
        return None

    hours = int(match.group(1))
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    return hours * 3600 + minutes * 60 + seconds


def normalize_audio(input_path: Path, output_path: Path, *, max_duration: float = 300.0) -> Tuple[bool, str]:
    """音声ファイルを opus 24kbps モノラル 16kHz ogg に正規化する。

    Args:
        input_path: 入力ファイル
        output_path: 出力ファイル (.ogg 推奨)
        max_duration: 上限秒数 (デフォルト 5 分)。超過時は失敗を返す

    Returns:
        (success, error_message) のタプル。success=True なら error_message は空
    """
    if not input_path.exists():
        return False, f"input file does not exist: {input_path}"

    duration = probe_duration(input_path)
    if duration is None:
        return False, "could not determine audio duration"
    if duration > max_duration:
        return False, f"audio duration ({duration:.1f}s) exceeds limit ({max_duration:.0f}s)"

    args = [
        "-y",  # overwrite output
        "-i", str(input_path),
        "-vn",  # no video stream
        "-c:a", "libopus",
        "-b:a", "24k",
        "-ac", "1",  # mono
        "-ar", "16000",  # 16kHz
        str(output_path),
    ]
    returncode, _stdout, stderr = run_ffmpeg(args, timeout=180)
    if returncode != 0:
        LOGGER.warning("normalize_audio failed (rc=%d): %s", returncode, stderr[-500:])
        return False, f"ffmpeg returned {returncode}: {stderr.strip()[-200:]}"
    if not output_path.exists() or output_path.stat().st_size == 0:
        return False, "ffmpeg produced no output file"
    return True, ""


def normalize_video(input_path: Path, output_path: Path, *, max_duration: float = 90.0) -> Tuple[bool, str]:
    """動画ファイルを 480p 1FPS + opus 24kbps モノラル 16kHz mp4 に正規化する。

    Args:
        input_path: 入力ファイル
        output_path: 出力ファイル (.mp4 推奨)
        max_duration: 上限秒数 (デフォルト 90 秒)。超過時は失敗を返す

    Returns:
        (success, error_message) のタプル
    """
    if not input_path.exists():
        return False, f"input file does not exist: {input_path}"

    duration = probe_duration(input_path)
    if duration is None:
        return False, "could not determine video duration"
    if duration > max_duration:
        return False, f"video duration ({duration:.1f}s) exceeds limit ({max_duration:.0f}s)"

    args = [
        "-y",  # overwrite output
        "-i", str(input_path),
        # video: 480p, 1FPS, h264
        "-vf", "scale=-2:480",
        "-r", "1",
        "-c:v", "libx264",
        "-crf", "28",
        "-preset", "fast",
        # audio: opus 24kbps mono 16kHz
        "-c:a", "libopus",
        "-b:a", "24k",
        "-ac", "1",
        "-ar", "16000",
        # mp4 container with faststart for streaming
        "-movflags", "+faststart",
        str(output_path),
    ]
    returncode, _stdout, stderr = run_ffmpeg(args, timeout=300)
    if returncode != 0:
        LOGGER.warning("normalize_video failed (rc=%d): %s", returncode, stderr[-500:])
        return False, f"ffmpeg returned {returncode}: {stderr.strip()[-200:]}"
    if not output_path.exists() or output_path.stat().st_size == 0:
        return False, "ffmpeg produced no output file"
    return True, ""


__all__ = [
    "get_ffmpeg_path",
    "is_ffmpeg_available",
    "run_ffmpeg",
    "probe_duration",
    "normalize_audio",
    "normalize_video",
]
