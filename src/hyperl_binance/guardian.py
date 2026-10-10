from __future__ import annotations

import os
import subprocess
import sys
import time

from .paths import project_root


def main() -> None:
    """Restart the CLI process after an unexpected non-zero exit."""
    root = project_root()
    command = [sys.executable, "-m", "hyperl_binance"]
    environment = os.environ.copy()
    source_root = root / "src"
    if source_root.is_dir():
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(source_root), existing) if part
        )

    while True:
        try:
            result = subprocess.run(command, cwd=root, env=environment, check=False)
        except KeyboardInterrupt:
            return
        if result.returncode == 0:
            return
        print(f"主程序异常退出（{result.returncode}），3 秒后重启；按 Ctrl+C 停止守护程序。")
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            return
