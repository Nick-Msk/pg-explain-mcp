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
    """Sequential scan that discards most of what it reads.

    A Seq Scan is not a problem by itself. It becomes one when the
    planner reads many rows only to throw most of them away — usually
    a sign that an index on the filter column might help.

    The check therefore ignores:

    - small scans (below ``THRESHOLD_ROWS``);
    - scans with **no** filter (``Rows Removed by Filter`` is 0) —
      reading the whole table is the only reasonable strategy here,
      and an index would not change the plan;
    - scans where the filter rejects less than ``MIN_FILTER_RATIO`` of
      the rows read — an index rarely beats a Seq Scan at moderate
      selectivity.

    The message is intentionally neutral: it points the reader (or the
    LLM) at the filter column and asks them to verify whether an index
    already exists, rather than blindly recommending one.
    """

    name = "seq_scan"
    THRESHOLD_ROWS = 1000
    MIN_FILTER_RATIO = 0.9   # 90 % of read rows must be discarded

    def check(self, node: dict[str, Any]) -> list[Issue]:
        if node.get("Node Type") != "Seq Scan":
            return []

        actual = node.get("Actual Rows", 0)
        removed = node.get("Rows Removed by Filter", 0)
        total_read = actual + removed

        if total_read <= self.THRESHOLD_ROWS:
            return []

        if removed == 0:
            return []

        if removed / total_read < self.MIN_FILTER_RATIO:
            return []

        relation = node.get("Relation Name", "?")
        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.name,
                message=(
                    f"Sequential scan on '{relation}' read {total_read} rows "
                    f"({actual} returned, {removed} filtered out, "
                    f"{removed / total_read * 100:.1f}% discarded). "
                    "Verify whether an index on the filter column exists; "
                    "if it does, investigate why the planner ignored it "
                    "(stale statistics, low correlation, or high "
                    "random_page_cost). If no index exists, consider "
                    "adding one."
                ),
                node="Seq Scan",
            )
        ]

class EstimateMismatchCheck:
    """Large mismatch between planner estimate and actual row counts.

    The check has two thresholds:
    - a relative one (``THRESHOLD_RATIO``) — the misestimate ratio, and
    - an absolute one (``MIN_ROWS``) — the minimum number of actual rows
      for the ratio to be meaningful.

    Small absolute numbers (e.g. 4 vs 83) produce high ratios but are
    irrelevant for planning and usually indicate stale statistics on
    tiny subsets, not a real problem.
    """

    name = "estimate_mismatch"
    THRESHOLD_RATIO = 10.0
    MIN_ROWS = 1000

    def check(self, node: dict[str, Any]) -> list[Issue]:
        planned = node.get("Plan Rows", 0)
        actual = node.get("Actual Rows", 0)

        if planned <= 0 or actual <= 0:
            return []
        if max(planned, actual) < self.MIN_ROWS:
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
    """Sort operation spilled to disk — work_mem is too small.

    The check produces a self-contained recommendation: it rounds the
    spill size up to the next power-of-two MB value and states it
    directly, so the caller does not have to subtract, divide, or look
    up runtime parameters.
    """

    name = "disk_spill_sort"

    def check(self, node: dict[str, Any]) -> list[Issue]:
        method = node.get("Sort Method", "")
        if not method.startswith("external"):
            return []

        used = node.get("Sort Space Used", 0)
        space_type = node.get("Sort Space Type", "")

        if not (used and space_type == "Disk"):
            return [
                Issue(
                    severity=SEVERITY_WARNING,
                    type=self.name,
                    message="Sort spilled to disk. Increase work_mem.",
                    node="Sort",
                )
            ]

        size_mb = used / 1024
        # round up to the next power of two: 32/64/128/256/512/1024 MB
        target = 32
        while target < size_mb * 1.1:
            target *= 2

        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.name,
                message=(
                    f"Sort spilled to disk ({used}kB ≈ {size_mb:.1f} MB). "
                    f"Recommended fix: set work_mem to at least {size_mb:.0f} MB; "
                    f"a safe round value is {target} MB. "
                    "Note: sorts use work_mem directly — hash_mem_multiplier "
                    "does not apply. Current work_mem is not part of this "
                    "calculation."
                ),
                node="Sort",
            )
        ]

class DiskSpillHashCheck:
    """Hash operation spilled to disk — work_mem is too small.

    When the hash table no longer fits in ``work_mem``, PostgreSQL
    partitions it into multiple batches and writes the excess to
    temporary files on disk.

    Two fields matter when interpreting the message:

    - ``Peak Memory Usage`` — memory used by **one** batch, not the
      whole hash table. When spilling, the real size is roughly
      ``peak_memory × batches``.
    - ``Disk Usage`` — actual bytes written to temporary files, when
      PostgreSQL reports them.

    The recommendation formula is intentionally spelled out in the
    message: PostgreSQL allocates ``work_mem × hash_mem_multiplier``
    to hash operations, and the multiplier is not visible in the plan.
    The caller is expected to look it up via ``list_parameters``.
    """

    name = "disk_spill_hash"

    def check(self, node: dict[str, Any]) -> list[Issue]:
        batches = node.get("Hash Batches", 1)
        if batches <= 1:
            return []

        memory = node.get("Peak Memory Usage", 0)
        disk = node.get("Disk Usage", 0)
        parallel = node.get("Parallel Aware", False)
        loops = node.get("Actual Loops", 1)

        parts = [f"{batches} batches"]
        if memory:
            estimated_mb = round(memory * batches / 1024, 1)
            parts.append(f"peak {memory}kB per batch")
            parts.append(f"estimated full size ≈ {estimated_mb} MB")
        if disk:
            parts.append(f"disk {disk}kB")

        details = ", ".join(parts)
        parallel_note = f" (per worker, {loops} workers)" if parallel and loops > 1 else ""

        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.name,
                message=(
                    f"Hash operation spilled to disk{parallel_note}: {details}. "
                    "To keep the hash table in memory, set work_mem such that "
                    "work_mem × hash_mem_multiplier > estimated full size. "
                    "Call list_parameters for the current hash_mem_multiplier."
                ),
                node=node.get("Node Type", "Hash"),
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


# Mapping: EXPLAIN JSON field → key in our compact output.
# Order matters only for readability; lookup is by key.
_PLAN_FIELDS: dict[str, str] = {
    "Relation Name":          "relation",
    "Index Name":             "index",
    "Actual Rows":            "actual_rows",
    "Actual Loops":           "actual_loops",
    "Plan Rows":              "plan_rows",
    "Rows Removed by Filter": "rows_removed_by_filter",
    "Heap Fetches":           "heap_fetches",
    "Shared Read Blocks":     "shared_read_blocks",
    "Sort Method":            "sort_method",
    "Sort Space Type":        "sort_space_type",
    "Sort Space Used":        "sort_space_used_kb",
    "Hash Buckets":           "hash_buckets",
    "Hash Batches":           "hash_batches",
    "Peak Memory Usage":      "peak_memory_usage_kb",
    "Disk Usage":             "disk_usage_kb",
    "Parallel Aware":         "parallel_aware",
}

def summarize_plan_node(node: dict[str, Any], depth: int = 0) -> list[dict[str, Any]]:
    """Flatten a plan tree into a compact list of nodes.

    Each entry keeps only the fields listed in ``_PLAN_FIELDS`` plus
    ``depth`` and ``node_type``. Unknown fields are ignored — the goal
    is to give the LLM the small set of values that matter for
    reasoning, not the entire plan.
    """
    entry: dict[str, Any] = {
        "depth": depth,
        "node_type": node.get("Node Type", "?"),
    }
    for src, dst in _PLAN_FIELDS.items():
        if src in node:
            entry[dst] = node[src]

    result = [entry]
    for child in node.get("Plans", []):
        result.extend(summarize_plan_node(child, depth + 1))
    return result
