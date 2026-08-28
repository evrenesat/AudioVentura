from __future__ import annotations

import sys
from pathlib import Path

HOME_INGEST_SOURCE = Path(__file__).resolve().parents[2] / "home_ingest" / "src"
if str(HOME_INGEST_SOURCE) not in sys.path:
    sys.path.insert(0, str(HOME_INGEST_SOURCE))
