"""Make the existing backend importable from the frontend.

The backend lives under ``src/`` (see pyproject ``pythonpath = ["src"]``).
Importing this module once, before any ``utils``/``universe``/``connectivity``
import, guarantees the real backend packages resolve when the app is launched
with ``uvicorn app.main:app``.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
