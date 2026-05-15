"""Per-source annotation loaders.

Importing this subpackage triggers the per-module ``register_loader``
side effects so the registry is populated before any CLI dispatch
runs. The parent ``genome.annotate`` package imports this subpackage at
the bottom of its own ``__init__`` for the same reason.

Sub-phase 5.1a ships the PharmGKB loader (this file's only import).
Each subsequent sub-phase adds one module and one line below it.
"""

from __future__ import annotations

from genome.annotate.loaders import pharmgkb  # noqa: F401 — side-effect: registers PharmGKB
