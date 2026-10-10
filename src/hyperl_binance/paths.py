from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    """Find the directory containing this application's config and runtime data."""
    configured = os.environ.get("HYPERL_BINANCE_HOME")
    if configured:
        return Path(configured).expanduser().resolve()

    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "config" / "market.json").is_file():
            return candidate

    # Source checkout fallback; installed deployments should set the environment
    # variable or run from a directory containing the config directory.
    return Path(__file__).resolve().parents[2]


def project_path(path: str | Path) -> Path:
    """Resolve a configured path relative to the application root."""
    value = Path(path).expanduser()
    return value.resolve() if value.is_absolute() else (project_root() / value).resolve()
