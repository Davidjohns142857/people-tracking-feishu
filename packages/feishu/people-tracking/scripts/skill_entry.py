#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    configured = os.environ.get("PEOPLE_TRACKING_FEISHU_CLI")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path.home() / ".local" / "bin" / "people-tracking-feishu",
    ]
    launcher = next((path for path in candidates if path and path.is_file()), None)
    if launcher is None:
        print(
            '{"ok":false,"error":{"type":"NotInstalled","message":"run the verified bundle installer first"}}',
            file=sys.stderr,
        )
        return 2
    completed = subprocess.run([str(launcher), *sys.argv[1:]], check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
