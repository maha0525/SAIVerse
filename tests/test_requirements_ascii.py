"""requirements*.txt が ASCII のみであることの回帰テスト。

pip は BOM の無い requirements ファイルを OS ロケールのエンコーディングで
読む (pip/_internal/utils/encoding.py の auto_decode)。日本語 Windows の
既定は cp932 なので、UTF-8 の日本語コメントが一行でもあると
`pip install -r requirements.txt` が UnicodeDecodeError で墜落し、
setup.bat の新規インストールと update.bat の更新が両方止まる
(2026-08-30 の v0.2.29 -> v0.3 アップグレード実機検証で発見)。
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_requirements_files_are_ascii_only():
    files = sorted(REPO_ROOT.glob("requirements*.txt"))
    assert files, "requirements*.txt not found at repo root"
    offenders = []
    for path in files:
        data = path.read_bytes()
        for lineno, line in enumerate(data.splitlines(), start=1):
            if any(byte > 0x7F for byte in line):
                offenders.append(f"{path.name}:{lineno}: {line.decode('utf-8', 'replace')}")
    assert not offenders, (
        "Non-ASCII bytes in requirements files break `pip install -r` on locales "
        "like cp932 (Japanese Windows):\n" + "\n".join(offenders)
    )
