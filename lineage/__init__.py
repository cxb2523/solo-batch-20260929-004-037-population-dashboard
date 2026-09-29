"""Compatibility bootstrap for the src layout.

The implementation lives in ``src/lineage``. Extending this package's
``__path__`` makes ``lineage.serve`` (and its relative imports) resolve to the
canonical modules under ``src/lineage`` while ``python -m lineage.serve`` still
works from the repo root without an editable install. Under pytest the
top-level ``conftest.py`` puts ``src`` on ``sys.path`` directly.
"""
from __future__ import annotations

from pathlib import Path

_SRC_PACKAGE = Path(__file__).resolve().parent.parent / "src" / "lineage"
__path__.append(str(_SRC_PACKAGE))  # noqa: F821
