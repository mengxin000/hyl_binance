from __future__ import annotations

import asyncio
import os

from .app import run


def main() -> None:
    """Start the interactive public-market screener."""
    if os.name == "nt":
        # Enable ANSI escape handling in Windows Terminal and modern consoles.
        os.system("")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
