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
    IndexOnlyScanCheck,
    IndexRegularScanCheck,
    NestedLoopCheck,
    NonSargableCheck,
    PartitionPruningCheck,
    SeqScanCheck,
)

ALL_CHECKS = (
    SeqScanCheck(params={
        "threshold_rows": "1000",
        "min_filter_ratio": "0.9",
    }),
    EstimateMismatchCheck(params={
        "threshold_ratio": "10.0",
        "min_rows": "1000",
    }),
    DiskSpillSortCheck(params={
        "min_spill_kb": "0",
        "min_work_mem_mb": "32",
        "headroom_ratio": "1.1",
    }),
    DiskSpillHashCheck(params={"min_batches": "2"}),
    NestedLoopCheck(params={"threshold_loops": "1000"}),
    BitmapHeapScanCheck(params={"threshold_rows": "100000"}),
    IndexOnlyScanCheck(params={
        "min_rows": "100",
        "heap_fetch_ratio": "0.10",
    }),
    IndexRegularScanCheck(params={
        "min_rows": "1000",
        "min_disk_blocks": "100",
    }),
    PartitionPruningCheck(params={"max_children": "3"}),
    NonSargableCheck(params={"threshold_rows": "1000"})
)

