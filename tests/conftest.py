"""Shared test fixtures.

``ALL_CHECKS`` is a *test-only* registry — production loads checks from
SQLite. Duplicating the list here keeps tests independent of the
config database and its seed state.
"""

from pg_explain_mcp.analyzer import (
    BitmapHeapScanCheck,
    DiskSpillHashCheck,
    DiskSpillSortCheck,
    EstimateMismatchCheck,
    IndexScanCheck,
    NestedLoopCheck,
    PartitionPruningCheck,
    SeqScanCheck,
)

ALL_CHECKS = (
    SeqScanCheck(),
    EstimateMismatchCheck(),
    DiskSpillSortCheck(),
    DiskSpillHashCheck(),
    NestedLoopCheck(),
    BitmapHeapScanCheck(),
    IndexScanCheck(),
    PartitionPruningCheck()
)

