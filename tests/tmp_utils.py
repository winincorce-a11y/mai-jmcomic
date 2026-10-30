"""测试临时目录工具。

Windows 上 ``tempfile.TemporaryDirectory()`` 会用 ``0o700`` 建目录，在当前
沙箱/ACL 环境里会导致子目录无法创建；这里统一用默认权限在工作区内建目录。
"""

from __future__ import annotations

from pathlib import Path

import shutil
import uuid

TMP_ROOT = Path(__file__).resolve().parent / ".tmp"


def make_temp_dir(prefix: str = "jm-") -> Path:
    """在 ``tests/.tmp`` 下创建一个可写的临时目录。"""

    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / f"{prefix}{uuid.uuid4().hex[:8]}"
    path.mkdir()
    return path


def remove_temp_dir(path: Path) -> None:
    """尽力删除临时目录。"""

    shutil.rmtree(path, ignore_errors=True)
