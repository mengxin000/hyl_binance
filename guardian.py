from __future__ import annotations

import sys
from pathlib import Path

source_root = Path(__file__).resolve().parent / "src"
if str(source_root) not in sys.path:
    sys.path.insert(0, str(source_root))

if __name__ == "__main__":
    from hyperl_binance.guardian import main

    main()
