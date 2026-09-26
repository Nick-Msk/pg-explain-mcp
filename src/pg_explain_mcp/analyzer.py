"""PostgreSQL execution plan analyzer: detect bottlenecks and issues.

The analyzer walks the JSON plan tree and applies a list of independent
checks (adapters) to every node. To add a new check, implement the
``PlanCheck`` protocol and register the instance in ``DEFAULT_CHECKS``.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, replace
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

# ---------------------------------------------------------------------------
# Checks (adapters)
# ---------------------------------------------------------------------------

class PlanCheck(Protocol):
    """Structural type for anything the analyzer can run.

    Any object with ``name``, ``type``, and ``check(node)`` satisfies
    this protocol — no inheritance required. Used for typing in
    ``analyze_plan`` and ``_walk_plan``.
    """

    name: str
    type: str

    def check(self, node: dict[str, Any]) -> list[Issue]:
        ...

class PlanCheckBase(ABC):
    """Base for single-rule checks split into three phases.

    - ``gather_info(node)`` extracts values needed to decide. Return
      ``None`` if the node is not applicable (wrong node type,
      missing fields, etc.). The returned dict is a private contract
      of the check — document its keys in the subclass docstring.
    - ``validate_rule(info)`` returns True if the check should fire.
    - ``generate_msg(info)`` builds the issues for a positive match.

    ``check()`` runs the phases in order and short-circuits on the
    first ``None`` or ``False``. Subclasses must implement all three
    ``@abstractmethod`` — ``ABC`` prevents instantiation otherwise.
    """

    name: str
    type: str

    def check(self, node: dict[str, Any]) -> list[Issue]:
        info = self.gather_info(node)
        if info is None or not self.validate_rule(info):
            return []
        return self.generate_msg(info)

    @abstractmethod
    def gather_info(self, node: dict[str, Any]) -> dict[str, Any] | None:
        ...

    @abstractmethod
    def validate_rule(self, info: dict[str, Any]) -> bool:
        ...

    @abstractmethod
    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        ...


class SeqScanCheck(PlanCheckBase):
    """Sequential scan that discards most of what it reads.

    A Seq Scan is not a problem by itself. It becomes one when the
    planner reads many rows only to throw most of them away — usually
    a sign that an index on the filter column might help.

    The check ignores:

    - small scans (below ``threshold_rows``);
    - scans with no filter (``Rows Removed by Filter = 0``) — reading
      the whole table is the only reasonable strategy here, and an
      index would not change the plan;
    - scans where the filter rejects less than ``min_filter_ratio`` of
      the rows read — an index rarely beats a Seq Scan at moderate
      selectivity.

    ``gather_info`` returns:

        {
            "actual":     float,   # rows returned by the scan
            "removed":    float,   # rows discarded by the filter
            "total_read": float,   # actual + removed
            "ratio":      float,   # removed / total_read, in [0, 1]
            "relation":   str,
        }

    Fires at ``WARNING`` level. The message is deliberately neutral:
    it asks the caller to verify whether an index already exists,
    rather than instructing to create one.
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

    def gather_info(self, node: dict[str, Any]) -> dict[str, Any] | None:
        if node.get("Node Type") != "Seq Scan":
            return None

        actual = node.get("Actual Rows", 0)
        removed = node.get("Rows Removed by Filter", 0)
        total_read = actual + removed
        if total_read <= 0:
            return None

        return {
            "actual": actual,
            "removed": removed,
            "total_read": total_read,
            "ratio": removed / total_read,
            "relation": node.get("Relation Name", "?"),
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        if info["total_read"] <= self.threshold_rows:
            return False
        if info["removed"] == 0:
            return False
        return info["ratio"] >= self.min_filter_ratio

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        pct = info["ratio"] * 100
        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.type,
                message=(
                    f"Sequential scan on '{info['relation']}' read "
                    f"{info['total_read']} rows "
                    f"({info['actual']} returned, "
                    f"{info['removed']} filtered out, "
                    f"{pct:.1f}% discarded). "
                    "Verify whether an index on the filter column exists; "
                    "if it does, investigate why the planner ignored it "
                    "(stale statistics, low correlation, or high "
                    "random_page_cost). If no index exists, consider "
                    "adding one."
                ),
                node="Seq Scan",
            )
        ]

class EstimateMismatchCheck(PlanCheckBase):
    """Planner cardinality misestimate.

    ``gather_info`` returns:

        {
            "planned":   int,
            "actual":    int,
            "ratio":     float,
            "node_type": str,
            "relation":  str,
        }

    Fires when both ``planned`` and ``actual`` exceed ``min_rows`` and
    the ratio between them exceeds ``threshold_ratio``. The floor on
    both sides suppresses low-signal ratios on tiny absolute numbers
    (5000 vs. 1), where a perfect estimate would not have changed the
    plan anyway.
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

    def gather_info(self, node: dict[str, Any]) -> dict[str, Any] | None:
        planned = node.get("Plan Rows", 0)
        actual = node.get("Actual Rows", 0)
        if planned <= 0 or actual <= 0:
            return None
        return {
            "planned": planned,
            "actual": actual,
            "ratio": max(planned, actual) / min(planned, actual),
            "node_type": node.get("Node Type", ""),
            "relation": node.get("Relation Name", ""),
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        if info["planned"] < self.min_rows:
            return False
        if info["actual"] < self.min_rows:
            return False
        return info["ratio"] > self.threshold_ratio

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        where = f" on '{info['relation']}'" if info["relation"] else ""
        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.type,
                message=(
                    f"Planner misestimated cardinality on "
                    f"'{info['node_type']}'{where}: "
                    f"expected {info['planned']}, got {info['actual']} "
                    f"(ratio x{info['ratio']:.1f}). "
                    "Investigate why: stale statistics, a non-sargable "
                    "predicate on the column, or a distribution not "
                    "covered by the column histogram. ANALYZE helps "
                    "only in the first case."
                ),
                node=info["node_type"],
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

class BitmapHeapScanCheck(PlanCheckBase):
    """Large Bitmap Heap Scan.

    A Bitmap Heap Scan is the right strategy for medium selectivity:
    too many rows for an index scan, too few for a sequential scan.
    But when the scan processes a very large number of rows, the heap
    fetches dominate — often a sign that a composite index or
    partitioning would reduce the amount of heap I/O.

    ``gather_info`` returns:

        {
            "rows":     float,
            "relation": str,
        }

    Fires at ``INFO`` level: the scan itself is not wrong, it is
    simply worth investigating at scale.
    """

    name = "BitmapHeapScanCheck"
    type = "bitmap_heap_scan"

    def __init__(self, threshold_rows: int = 100_000) -> None:
        self.threshold_rows = threshold_rows

    def gather_info(self, node: dict[str, Any]) -> dict[str, Any] | None:
        if node.get("Node Type") != "Bitmap Heap Scan":
            return None
        return {
            "rows": node.get("Actual Rows", 0),
            "relation": node.get("Relation Name", "?"),
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        return info["rows"] > self.threshold_rows

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        return [
            Issue(
                severity=SEVERITY_INFO,
                type=self.type,
                message=(
                    f"Bitmap Heap Scan on '{info['relation']}' processed "
                    f"{info['rows']} rows. Consider a composite index or "
                    "partitioning to reduce the number of heap fetches."
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
    alone, so it uses an absolute threshold. The message also warns
    that any cardinality misestimates inside the partitions are a
    *consequence* of the pruning failure — not stale statistics. When
    the predicate is non-sargable, the planner has no statistics to
    attribute rows to specific partitions, so per-partition estimates
    default to a uniform split. ``ANALYZE`` will not fix that; only
    rewriting the predicate will.
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
                    "Partition pruning may have failed — check that the "
                    "predicate on the partition key is sargable: no "
                    "function calls, no casts, no expressions. "
                    "Note: any cardinality misestimates inside the "
                    "partitions are a consequence, not stale statistics. "
                    "The planner cannot attribute rows to partitions "
                    "when the predicate is non-sargable, so ANALYZE will "
                    "not help — rewrite the predicate. "
                    "Scanned: " + preview
                ),
                node=node.get("Node Type"),
            )
        ]

class NonSargableCheck:
    """Non-sargable predicate on a column that has a plain index.

    A predicate like ``lower(email) = 'x'`` wraps the column in a
    function. The planner cannot use a plain index on ``email`` for
    such a predicate — it has to evaluate the function for every row.
    If the column is indexed, the index exists but is unusable for
    this query.

    The check does not use a whitelist of function names — that would
    miss user-defined functions and any builtin we forgot. Instead it
    looks for the pattern ``<word>(<column>`` in the ``Filter``
    string, using the list of columns covered by plain (non-functional)
    indexes on the relation.

    Functional indexes themselves are not our concern: if a functional
    index exists on the exact expression, the planner uses it, and the
    predicate appears in ``Index Cond`` rather than ``Filter``. Such
    cases are ignored.
    """

    name = "NonSargableCheck"
    type = "non_sargable"

    def __init__(
        self,
        threshold_rows: int = 1000,
        relation_indexes: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.threshold_rows = threshold_rows
        self.relation_indexes = relation_indexes or {}

    def check(self, node: dict[str, Any]) -> list[Issue]:
        filter_str = node.get("Filter", "")
        if not filter_str:
            return []

        actual = node.get("Actual Rows", 0)
        removed = node.get("Rows Removed by Filter", 0)
        if actual + removed < self.threshold_rows:
            return []

        relation = node.get("Relation Name", "?")
        indexed_columns = self._plain_indexed_columns(relation)
        if not indexed_columns:
            return []

        for column in indexed_columns:
            func = self._wrapped_in_function(filter_str, column)
            if func is None:
                continue

            return [
                Issue(
                    severity=SEVERITY_INFO,
                    type=self.type,
                    message=(
                        f"Non-sargable predicate on '{relation}': the "
                        f"filter wraps '{column}' in '{func}(...)', so "
                        f"the index on '{column}' cannot be used. "
                        f"Consider a functional index on the exact "
                        f"expression, or rewriting the predicate. "
                        f"Filter: {filter_str}"
                    ),
                    node=node.get("Node Type"),
                )
            ]

        return []

    def _plain_indexed_columns(self, relation: str) -> set[str]:
        """Return columns that a plain ``col = value`` predicate can use.

        A column qualifies only if it is the **leading** column of an
        index and that slot is a plain column, not an expression. Two
        cases are excluded:

        - functional indexes (``lower(email)``) — planner would not put
          the predicate in ``Filter`` if such an index existed for the
          expression, so those never reach this check in practice;
        - non-leading columns of composite indexes (``email`` in
          ``(status, email)``) — a rewrite to ``email = value`` would not
          use the index.
        """
        indexes = self.relation_indexes.get(relation, [])
        columns: set[str] = set()
        for idx in indexes:
            leading = idx.get("leading_attnum")
            if leading in (None, 0):
                continue
            plain = idx.get("plain_columns") or []
            if not plain:
                continue
            columns.add(plain[0])

        return columns

    def _wrapped_in_function(
        self, filter_str: str, column: str,
    ) -> str | None:
        """Return a function name if ``column`` appears inside its parens.

        Scans every ``word(`` occurrence in the filter and, for each one,
        inspects the contents up to the matching ``)``. If the column
        appears as a whole word anywhere inside, the predicate is
        non-sargable on that column.

        This catches:

        - ``lower(email)`` — column is the first argument;
        - ``date_trunc('month', ts)`` — column is not the first argument;
        - ``extract(month from ts)`` — special FROM syntax;
        - ``lower(users.email)`` — table-qualified name;
        - ``lower(coalesce(email, ''))`` — nested calls, since the inner
          ``coalesce`` will also be visited.

        It does not flag:

        - ``email = 'x'`` — no function wrapping;
        - ``email IS NOT NULL`` — no ``word(`` pattern;
        - ``email = ANY(ARRAY[...])`` — ``array`` is on the right-hand
          side; the column does not appear inside ``any(...)``.
        """
        for m in re.finditer(r"\b(\w+)\s*\(", filter_str, re.IGNORECASE):
            func = m.group(1)
            if func.lower() in ("array", "row", "values"):
                continue

            # Scan forward to the matching close paren.
            start = m.end()
            depth = 1
            i = start
            while i < len(filter_str) and depth > 0:
                if filter_str[i] == "(":
                    depth += 1
                elif filter_str[i] == ")":
                    depth -= 1
                i += 1

            inner = filter_str[start:i - 1] if depth == 0 else filter_str[start:]

            if re.search(rf"\b{re.escape(column)}\b", inner, re.IGNORECASE):
                return func

        return None

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

