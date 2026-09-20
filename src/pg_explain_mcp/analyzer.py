"""PostgreSQL execution plan analyzer: detect bottlenecks and issues.

The analyzer walks the JSON plan tree and applies a list of independent
checks (adapters) to every node. To add a new check, implement the
``PlanCheck`` protocol and register the instance in ``DEFAULT_CHECKS``.
"""

from dataclasses import asdict, dataclass
from typing import Any, Protocol


SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """A single detected problem in the execution plan."""

    severity: str
    type: str
    message: str
    node: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PlanCheck(Protocol):
    """Adapter interface: each check inspects a single plan node.

    Implementations must be stateless (or at least thread-safe) and
    must never mutate the ``node`` they receive.
    """

    name: str

    def check(self, node: dict[str, Any]) -> list[Issue]: ...


# ---------------------------------------------------------------------------
# Checks (adapters)
# ---------------------------------------------------------------------------


class SeqScanCheck:
    """Sequential scan on a large table — likely missing an index."""

    name = "seq_scan"
    THRESHOLD_ROWS = 1000

    def check(self, node: dict[str, Any]) -> list[Issue]:
        if node.get("Node Type") != "Seq Scan":
            return []
        rows = node.get("Actual Rows", 0)
        if rows <= self.THRESHOLD_ROWS:
            return []
        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.name,
                message=(
                    f"Sequential scan on '{node.get('Relation Name', '?')}' "
                    f"processed {rows} rows. Consider adding an index."
                ),
                node="Seq Scan",
            )
        ]


class EstimateMismatchCheck:
    """Large mismatch between planner estimate and actual row counts."""

    name = "estimate_mismatch"
    THRESHOLD_RATIO = 10.0

    def check(self, node: dict[str, Any]) -> list[Issue]:
        planned = node.get("Plan Rows", 0)
        actual = node.get("Actual Rows", 0)
        if planned <= 0 or actual <= 0:
            return []

        ratio = max(planned, actual) / min(planned, actual)
        if ratio <= self.THRESHOLD_RATIO:
            return []

        node_type = node.get("Node Type", "")
        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.name,
                message=(
                    f"Planner misestimated cardinality on '{node_type}': "
                    f"expected {planned}, got {actual} (ratio x{ratio:.1f}). "
                    "Consider running ANALYZE."
                ),
                node=node_type,
            )
        ]


class DiskSpillSortCheck:
    """Sort operation spilled to disk — work_mem is too small."""

    name = "disk_spill_sort"

    def check(self, node: dict[str, Any]) -> list[Issue]:
        if not node.get("Sort Method", "").startswith("external"):
            return []

        node_type = node.get("Node Type", "")
        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.name,
                message=(
                    f"Sort operation on '{node_type}' spilled to disk. "
                    "Increase work_mem or optimize the query."
                ),
                node=node_type,
            )
        ]


class DiskSpillHashCheck:
    """Hash Join used multiple batches — work_mem is too small."""

    name = "disk_spill_hash"

    def check(self, node: dict[str, Any]) -> list[Issue]:
        batches = node.get("Hash Batches", 1)
        if batches <= 1:
            return []

        node_type = node.get("Node Type", "")
        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.name,
                message=(f"Hash Join used multiple batches ({batches}). Increase work_mem."),
                node=node_type,
            )
        ]


class NestedLoopCheck:
    """Nested Loop with too many iterations — consider a Hash Join."""

    name = "nested_loop"
    THRESHOLD_LOOPS = 1000

    def check(self, node: dict[str, Any]) -> list[Issue]:
        if node.get("Node Type") != "Nested Loop":
            return []

        loops = node.get("Actual Loops", 1)
        if loops <= self.THRESHOLD_LOOPS:
            return []

        return [
            Issue(
                severity=SEVERITY_INFO,
                type=self.name,
                message=(
                    f"Nested Loop executed {loops} times. "
                    "Check whether a Hash Join would be more efficient."
                ),
                node="Nested Loop",
            )
        ]


class BitmapHeapScanCheck:
    """Bitmap Heap Scan on a large table — check bitmap efficiency.

    A Bitmap Heap Scan is often chosen when an index would return too many
    rows for a plain Index Scan. On large tables this can indicate a missing
    composite index, poor selectivity, or a bloated bitmap.
    """

    name = "bitmap_heap_scan"
    THRESHOLD_ROWS = 100_000

    def check(self, node: dict[str, Any]) -> list[Issue]:
        if node.get("Node Type") != "Bitmap Heap Scan":
            return []

        rows = node.get("Actual Rows", 0)
        if rows <= self.THRESHOLD_ROWS:
            return []

        relation = node.get("Relation Name", "?")
        return [
            Issue(
                severity=SEVERITY_INFO,
                type=self.name,
                message=(
                    f"Bitmap Heap Scan on '{relation}' processed {rows} rows. "
                    "Consider a composite index or partitioning to reduce "
                    "the number of heap fetches."
                ),
                node="Bitmap Heap Scan",
            )
        ]


class IndexScanCheck:
    """Index Scan / Index Only Scan with poor heap locality.

    Two scenarios are detected:

    1. **Index Only Scan** with a high number of ``Heap Fetches``. This
       means the visibility map is stale — the engine still has to visit
       the heap for most rows, defeating the purpose of an index-only scan.
       Fix: run ``VACUUM``, or consider ``CLUSTER`` to improve locality.

    2. **Index Scan** reading many blocks from disk relative to the number
       of rows returned. This indicates poor clustering: the heap rows are
       scattered, causing random I/O. Fix: ``CLUSTER`` on the index used.
    """

    name = "index_scan_heap_locality"

    # Minimum absolute threshold to avoid noise on small scans.
    MIN_ROWS = 1000

    # Index Only Scan: ratio of heap fetches to actual rows above which we warn.
    HEAP_FETCH_RATIO = 0.10  # 10%

    # Index Scan: minimum disk blocks read to trigger a warning.
    MIN_DISK_BLOCKS = 100

    def check(self, node: dict[str, Any]) -> list[Issue]:
        node_type = node.get("Node Type", "")

        if node_type == "Index Only Scan":
            return self._check_index_only(node)
        if node_type == "Index Scan":
            return self._check_regular_index(node)
        return []

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_index_only(self, node: dict[str, Any]) -> list[Issue]:
        heap_fetches = node.get("Heap Fetches", 0)
        actual_rows = node.get("Actual Rows", 0)

        if heap_fetches < self.MIN_ROWS:
            return []
        if actual_rows > 0 and heap_fetches / actual_rows < self.HEAP_FETCH_RATIO:
            return []

        index_name = node.get("Index Name", "?")
        relation = node.get("Relation Name", "?")
        ratio_pct = (heap_fetches / actual_rows * 100) if actual_rows else 0

        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.name,
                message=(
                    f"Index Only Scan on '{index_name}' ({relation}) "
                    f"performed {heap_fetches} heap fetches for {actual_rows} rows "
                    f"({ratio_pct:.1f}%). The visibility map is stale. "
                    "Run VACUUM, or consider CLUSTER to improve locality."
                ),
                node="Index Only Scan",
            )
        ]

    def _check_regular_index(self, node: dict[str, Any]) -> list[Issue]:
        actual_rows = node.get("Actual Rows", 0)
        read_blocks = node.get("Shared Read Blocks", 0)

        if actual_rows < self.MIN_ROWS:
            return []
        if read_blocks < self.MIN_DISK_BLOCKS:
            return []

        index_name = node.get("Index Name", "?")
        relation = node.get("Relation Name", "?")

        return [
            Issue(
                severity=SEVERITY_INFO,
                type=self.name,
                message=(
                    f"Index Scan on '{index_name}' ({relation}) read "
                    f"{read_blocks} blocks from disk for {actual_rows} rows. "
                    "Poor heap clustering may be causing random I/O. "
                    "Consider CLUSTER on this index."
                ),
                node="Index Scan",
            )
        ]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

DEFAULT_CHECKS: tuple[PlanCheck, ...] = (
    SeqScanCheck(),
    EstimateMismatchCheck(),
    DiskSpillSortCheck(),
    DiskSpillHashCheck(),
    NestedLoopCheck(),
    BitmapHeapScanCheck(),
    IndexScanCheck(),
)


# ---------------------------------------------------------------------------
# Traversal and reporting
# ---------------------------------------------------------------------------


def _walk_plan(
    node: dict[str, Any],
    issues: list[Issue],
    checks: tuple[PlanCheck, ...],
) -> None:
    """Recursively traverse the plan tree, applying every check to each node."""
    for check in checks:
        issues.extend(check.check(node))

    for child in node.get("Plans", []):
        _walk_plan(child, issues, checks)


def analyze_plan(
    plan_json: list[dict[str, Any]],
    checks: tuple[PlanCheck, ...] = DEFAULT_CHECKS,
) -> dict[str, Any]:
    """Analyze a JSON execution plan and return a structured report.

    Args:
        plan_json: The JSON plan returned by ``EXPLAIN (FORMAT JSON)``.
        checks:    A tuple of adapters to apply. Defaults to ``DEFAULT_CHECKS``.

    Returns:
        A dict with timing, a list of issues, and a short summary.
    """
    if not plan_json:
        return {"issues": [], "summary": "Empty plan"}

    root = plan_json[0]
    plan_tree = root.get("Plan", {})
    execution_time = root.get("Execution Time", 0)
    planning_time = root.get("Planning Time", 0)

    issues: list[Issue] = []
    _walk_plan(plan_tree, issues, checks)

    return {
        "execution_time_ms": execution_time,
        "planning_time_ms": planning_time,
        "total_time_ms": execution_time + planning_time,
        "issues": [issue.to_dict() for issue in issues],
        "issue_count": len(issues),
        "summary": _make_summary(issues, execution_time),
    }


def _make_summary(issues: list[Issue], exec_time: float) -> str:
    """Build a short human-readable summary for the LLM."""
    if not issues:
        return f"No issues found. Execution time: {exec_time:.2f} ms."

    warnings = [i for i in issues if i.severity == SEVERITY_WARNING]
    return (
        f"Found {len(issues)} issues ({len(warnings)} critical). "
        f"Execution time: {exec_time:.2f} ms."
    )
