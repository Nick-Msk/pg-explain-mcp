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
    depth: int = 0
    parent_node: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_context(self, depth: int, parent_node: str) -> "Issue":
        return replace(self, depth=depth, parent_node=parent_node)

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

    name = "SeqScanCheck"
    type = "seq_scan"

    def __init__(
        self,
        threshold_rows: int = 1000,
        min_filter_ratio: float = 0.9,
    ) -> None:
        self.threshold_rows = threshold_rows
        self.min_filter_ratio = min_filter_ratio

    def check(self, node: dict[str, Any]) -> list[Issue]:
        if node.get("Node Type") != "Seq Scan":
            return []

        actual = node.get("Actual Rows", 0)
        removed = node.get("Rows Removed by Filter", 0)
        total_read = actual + removed

        if total_read <= self.threshold_rows:
            return []

        if removed == 0:
            return []

        if removed / total_read < self.min_filter_ratio:
            return []

        relation = node.get("Relation Name", "?")
        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.type,
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

    name = "EstimateMismatchCheck"
    type = "estimate_mismatch"

    def __init__(
        self,
        threshold_ratio: float = 10.0,
        min_rows: int = 1000,
    ) -> None:
        self.threshold_ratio = threshold_ratio
        self.min_rows = min_rows

    def check(self, node: dict[str, Any]) -> list[Issue]:
        planned = node.get("Plan Rows", 0)
        actual = node.get("Actual Rows", 0)

        if planned <= 0 or actual <= 0:
            return []
        if max(planned, actual) < self.min_rows:
            return []

        ratio = max(planned, actual) / min(planned, actual)
        if ratio <= self.threshold_ratio:
            return []

        node_type = node.get("Node Type", "")
        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.type,
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

    name = "DiskSpillSortCheck"
    type = "disk_spill_sort"

    def __init__(self, min_spill_kb: int = 0) -> None:
        self.min_spill_kb = min_spill_kb

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
                    type=self.type,
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
                type=self.type,
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

    name = "DiskSpillHashCheck"
    type = "disk_spill_hash"

    def __init__(self, min_batches: int = 2) -> None:
        self.min_batches = min_batches

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
                type=self.type,
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
    """Nested Loop with a high number of inner iterations.

    PostgreSQL reports the loop count on the **inner** child
    (``Plans[1]``), not on the Nested Loop node itself — the outer
    node always reports ``Actual Loops = 1``.

    A Nested Loop with an indexed inner side and ~1 row per lookup is
    optimal for a small outer table; there is nothing to fix. The
    check is emitted at ``INFO`` level as a heads-up for future
    growth, not as a warning.

    Related inefficiencies caused by misestimated cardinality are
    reported separately by ``EstimateMismatchCheck``.
    """

    name = "NestedLoopCheck"
    type = "nested_loop"

    def __init__(
        self,
        threshold_loops: int = 1000,
        threshold_rows: int = 100000,
    ) -> None:
        self.threshold_loops = threshold_loops
        self.threshold_rows = threshold_rows

    def check(self, node: dict[str, Any]) -> list[Issue]:
        if node.get("Node Type") != "Nested Loop":
            return []

        plans = node.get("Plans", [])
        if len(plans) < 2:
            return []

        inner = plans[1]
        loops = inner.get("Actual Loops", 1)
        if loops <= self.threshold_loops:
            return []

        inner_type = inner.get("Node Type", "?")
        inner_relation = inner.get("Relation Name", "?")
        inner_avg = inner.get("Actual Rows", 0)

        return [
            Issue(
                severity=SEVERITY_INFO,
                type=self.type,
                message=(
                    f"Nested Loop ran the inner side {loops} times "
                    f"('{inner_type}' on '{inner_relation}', ~{inner_avg:.2f} "
                    "rows per loop). This is optimal for the current data, "
                    "but execution time grows linearly with the outer row "
                    "count — re-check if the outer side becomes much larger."
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

    name = "BitmapHeapScanCheck"
    type = "bitmap_heap_scan"

    def __init__(self, threshold_rows: int = 100000) -> None:
        self.threshold_rows = threshold_rows

    def check(self, node: dict[str, Any]) -> list[Issue]:
        if node.get("Node Type") != "Bitmap Heap Scan":
            return []

        rows = node.get("Actual Rows", 0)
        if rows <= self.threshold_rows:
            return []

        relation = node.get("Relation Name", "?")
        return [
            Issue(
                severity=SEVERITY_INFO,
                type=self.type,
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

    name = "IndexScanCheck"
    type = "index_scan_heap_locality"

    def __init__(
        self,
        min_rows: int = 1000,
        heap_fetch_ratio: float = 0.10,
        min_disk_blocks: int = 100,
    ) -> None:
        self.min_rows = min_rows
        self.heap_fetch_ratio = heap_fetch_ratio
        self.min_disk_blocks = min_disk_blocks

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

        if heap_fetches < self.min_rows:
            return []
        if actual_rows > 0 and heap_fetches / actual_rows < self.heap_fetch_ratio:
            return []

        index_name = node.get("Index Name", "?")
        relation = node.get("Relation Name", "?")
        ratio_pct = (heap_fetches / actual_rows * 100) if actual_rows else 0

        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.type,
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

        if actual_rows < self.min_rows:
            return []
        if read_blocks < self.min_disk_blocks:
            return []

        index_name = node.get("Index Name", "?")
        relation = node.get("Relation Name", "?")

        return [
            Issue(
                severity=SEVERITY_INFO,
                type=self.type,
                message=(
                    f"Index Scan on '{index_name}' ({relation}) read "
                    f"{read_blocks} blocks from disk for {actual_rows} rows. "
                    "Poor heap clustering may be causing random I/O. "
                    "Consider CLUSTER on this index."
                ),
                node="Index Scan",
            )
        ]

class PartitionPruningCheck:
    """Append / Merge Append over many partitions — pruning may have failed.

    On a partitioned table the planner prunes partitions that cannot
    match the query's predicate. When pruning succeeds, ``Append`` has
    few children. When it fails, ``Append`` covers every partition.

    The check cannot know the total partition count from the plan
    alone, so it uses an absolute threshold. When it fires, it sets
    ``skip_children=True`` — the individual partition scans are a
    *consequence* of the pruning failure, not separate problems, and
    reporting them would drown the real signal.
    """

    name = "PartitionPruningCheck"
    type = "partition_pruning"

    def __init__(self, max_children: int = 3) -> None:
        self.max_children = max_children

    def check(self, node: dict[str, Any]) -> list[Issue]:
        if node.get("Node Type") not in ("Append", "Merge Append"):
            return []

        children = node.get("Plans", [])
        count = len(children)
        if count <= self.max_children:
            return []

        names = [
            c.get("Relation Name")
            for c in children
            if c.get("Relation Name")
        ]
        preview = ", ".join(names[:3])
        if len(names) > 3:
            preview += f", … (+{len(names) - 3} more)"

        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.type,
                message=(
                    f"Append over {count} partitions "
                    f"(threshold: {self.max_children}). "
                    "Partition pruning may have failed. "
                    "Check that the predicate on the partition key is "
                    "sargable — no function calls, no casts, no "
                    "expressions. Scanned: " + preview
                ),
                node=node.get("Node Type"),
                skip_children=True,
            )
        ]

# ---------------------------------------------------------------------------
# Traversal and reporting
# ---------------------------------------------------------------------------


def _walk_plan(
    node: dict[str, Any],
    issues: list[Issue],
    checks: tuple[PlanCheck, ...],
    depth: int = 0,
    parent_node: str = ""
) -> None:
    """Recursively traverse the plan tree, applying every check to each node."""
    for check in checks:
        for issue in check.check(node):
            issues.append(issue.with_context(depth, parent_node))

    node_type = node.get("Node Type", "?")
    for child in node.get("Plans", []):
        _walk_plan(child, issues, checks, depth + 1, node_type)

def analyze_plan(
    plan_json: list[dict[str, Any]],
    checks: tuple[PlanCheck, ...],
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
        "checks_applied": [c.name for c in checks],
        "execution_time_ms": execution_time,
        "planning_time_ms": planning_time,
        "total_time_ms": execution_time + planning_time,
        "issues": [issue.to_dict() for issue in issues],
        "issue_count": len(issues),
        "summary": _make_summary(issues, execution_time)
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

def summarize_plan_node(
    node: dict[str, Any],
    fields: dict[str, str],
    depth: int = 0,
) -> list[dict[str, Any]]:
    """Flatten a plan tree into a compact list of nodes.

    ``fields`` maps EXPLAIN JSON field names to compact output keys
    (for example ``"Relation Name" -> "relation"``). The mapping is
    loaded from the ``plan_fields`` table in the SQLite config, so
    users can toggle or rename fields without touching the code.

    Only fields present in both the mapping and the node are kept.
    Unknown fields are ignored — the goal is to give the LLM the small
    set of values that matter for reasoning, not the entire plan.
    """
    entry: dict[str, Any] = {
        "depth": depth,
        "node_type": node.get("Node Type", "?"),
    }
    for raw, key in fields.items():
        if raw in node:
            entry[key] = node[raw]

    result = [entry]
    for child in node.get("Plans", []):
        result.extend(summarize_plan_node(child, fields, depth + 1))
    return result

