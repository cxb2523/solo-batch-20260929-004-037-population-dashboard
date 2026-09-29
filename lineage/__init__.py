"""Path shim: the real package implementation lives under ``src/lineage``.

This package only redirects imports so ``python -m lineage.serve`` works from
the repository root without an editable install.
"""

from pathlib import Path

_SRC_LINEAGE = Path(__file__).resolve().parent.parent / "src" / "lineage"
__path__ = [str(_SRC_LINEAGE)]
