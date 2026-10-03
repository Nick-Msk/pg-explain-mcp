"""Shared test fixtures.

``ALL_CHECKS`` is a *test-only* registry — production loads checks from
SQLite. Duplicating the list here keeps tests independent of the
config database and its seed state.
The ``_no_db`` fixture patches ``analyzer.get_indexes`` so that
``NonSargableCheck`` never attempts a real connection during unit
tests. Tests that need specific indexes (``TestNonSargableCheck``)
override it with their own ``monkeypatch.setattr``, which wins because
it is applied later.
"""

import pytest

import pg_explain_mcp.analyzer as analyzer
from pg_explain_mcp.analyzer import (
    BitmapHeapScanCheck,
    DiskSpillHashCheck,
    DiskSpillSortCheck,
    EstimateMismatchCheck,
    IndexOnlyScanCheck,
    IndexRegularScanCheck,
    JitDecisionCheck,
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
    DiskSpillHashCheck(params={
        "min_batches": "2"
    }),
    NestedLoopCheck(params={
        "threshold_loops": "1000"
    }),
    BitmapHeapScanCheck(params={
        "threshold_rows": "100000"
    }),
    IndexOnlyScanCheck(params={
        "min_rows": "100",
        "heap_fetch_ratio": "0.10",
    }),
    IndexRegularScanCheck(params={
        "min_rows": "1000",
        "min_disk_blocks": "100",
    }),
    PartitionPruningCheck(params={
        "max_children": "3"
    }),
    NonSargableCheck(params={
        "threshold_rows": "1000"
    }),
    JitDecisionCheck(params={
        "min_jit_ms":     "1.0",
        "overhead_ratio": "0.3",
    }),
)

@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """Keep the unit-test suite offline.

    ``NonSargableCheck`` lazily loads indexes via ``get_indexes`` the
    first time ``gather_info`` runs. Left alone, any test that
    exercises it — including ``TestAnalyzePlan`` via ``ALL_CHECKS`` —
    would open a real PostgreSQL connection.

    Patching the symbol in the ``analyzer`` namespace is enough: it is
    the same object ``NonSargableCheck._load_indexes`` resolves at call
    time. Tests that need specific index rows override this with their
    own ``monkeypatch.setattr`` (see ``TestNonSargableCheck``); their
    patch is applied after this fixture and therefore wins.
    """
    monkeypatch.setattr(analyzer, "get_indexes", lambda *a, **kw: [])

