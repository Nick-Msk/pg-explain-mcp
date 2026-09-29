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

    def check(
        self,
        node: dict[str, Any],
        parent_type: str = "",
    ) -> list[Issue]:
        info = self.gather_info(node, parent_type)
        if info is None or not self.validate_rule(info):
            return []
        return self.generate_msg(info)

    @abstractmethod
    def gather_info(
        self,
        node: dict[str, Any],
        parent_type: str = "",
    ) -> dict[str, Any] | None:
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

    def gather_info(
        self,
        node: dict[str, Any],
        parent_type: str = ""
    ) -> dict[str, Any] | None:
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

    def gather_info(
        self,
        node: dict[str, Any],
        parent_type: str = ""
    ) -> dict[str, Any] | None:
        # plan_rows under Limit is the full-scan estimate, not the
        # truncated one. Comparing it to actual produces a spurious ratio.
        if parent_type == "Limit":
            return None
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

class DiskSpillSortCheck(PlanCheckBase):
    """Sort spilled to disk, size known.

    Fires when ``Sort Method`` starts with ``external`` and the plan
    reports the spill size (``Sort Space Type = "Disk"`` and
    ``Sort Space Used > 0``). The message includes the spill size and
    a recommended ``work_mem`` value rounded up to the next power of
    two.

    ``gather_info`` returns:

        {
            "size_kb": int,
            "size_mb": float,
        }

    ``hash_mem_multiplier`` does **not** apply to sorts — only
    ``work_mem`` counts.
    """

    name = "DiskSpillSortCheck"
    type = "disk_spill_sort"

    def __init__(self, min_spill_kb: int = 0) -> None:
        self.min_spill_kb = min_spill_kb

    def gather_info(
        self,
        node: dict[str, Any],
        parent_type: str = ""
    ) -> dict[str, Any] | None:
        method = node.get("Sort Method", "")
        if not method.startswith("external"):
            return None
        used = node.get("Sort Space Used", 0)
        space_type = node.get("Sort Space Type", "")
        if not (used and space_type == "Disk"):
            return None
        return {
            "size_kb": used,
            "size_mb": used / 1024,
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        return info["size_kb"] >= self.min_spill_kb

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        size_kb = info["size_kb"]
        size_mb = info["size_mb"]

        # round up to the next power of two: 32/64/128/256/512/1024 MB
        target = 32
        while target < size_mb * 1.1:
            target *= 2

        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.type,
                message=(
                    f"Sort spilled to disk ({size_kb}kB ≈ "
                    f"{size_mb:.1f} MB). "
                    f"Recommended fix: set work_mem to at least "
                    f"{size_mb:.0f} MB; a safe round value is "
                    f"{target} MB. "
                    "Note: sorts use work_mem directly — "
                    "hash_mem_multiplier does not apply. "
                    "Current work_mem is not part of this calculation."
                ),
                node="Sort",
            )
        ]

class DiskSpillHashCheck(PlanCheckBase):
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

    ``gather_info`` returns:

        {
            "batches":       int,
            "peak_kb":       int,    # 0 if absent
            "estimated_mb":  float,  # 0.0 if peak absent
            "disk_kb":       int,    # 0 if absent
            "parallel":      bool,
            "loops":         int,
            "node_type":     str,
        }

    The effective hash budget is ``work_mem × hash_mem_multiplier`` —
    the multiplier is not part of the plan, so the message points at
    ``list_parameters`` for the caller to look it up.
    """

    name = "DiskSpillHashCheck"
    type = "disk_spill_hash"

    def __init__(self, min_batches: int = 2) -> None:
        self.min_batches = min_batches

    def gather_info(
        self,
        node: dict[str, Any],
        parent_type: str = ""
    ) -> dict[str, Any] | None:
        batches = node.get("Hash Batches", 1)
        if batches <= 0:
            return None

        peak_kb = node.get("Peak Memory Usage", 0)
        return {
            "batches": batches,
            "peak_kb": peak_kb,
            "estimated_mb": round(peak_kb * batches / 1024, 1) if peak_kb else 0.0,
            "disk_kb": node.get("Disk Usage", 0),
            "parallel": node.get("Parallel Aware", False),
            "loops": node.get("Actual Loops", 1),
            "node_type": node.get("Node Type", "Hash"),
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        return info["batches"] >= self.min_batches

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        parts = [f"{info['batches']} batches"]
        if info["peak_kb"]:
            parts.append(f"peak {info['peak_kb']}kB per batch")
            parts.append(f"estimated full size ≈ {info['estimated_mb']} MB")
        if info["disk_kb"]:
            parts.append(f"disk {info['disk_kb']}kB")

        details = ", ".join(parts)
        parallel_note = (
            f" (per worker, {info['loops']} workers)"
            if info["parallel"] and info["loops"] > 1
            else ""
        )

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
                node=info["node_type"],
            )
        ]

class NestedLoopCheck(PlanCheckBase):
    """Nested Loop with a high number of inner iterations.

    PostgreSQL reports the loop count on the **inner** child
    (``Plans[1]``), not on the Nested Loop node itself — the outer
    node always reports ``Actual Loops = 1``.

    A Nested Loop with an indexed inner side and ~1 row per lookup is
    optimal for a small outer table; there is nothing to fix. The
    check is emitted at ``INFO`` level as a heads-up for future
    growth, not as a warning.

    ``gather_info`` returns:

        {
            "loops":          int,    # inner-side iteration count
            "inner_type":     str,
            "inner_relation": str,
            "inner_avg_rows": float,
        }

    Fires at ``INFO`` level.
    """

    name = "NestedLoopCheck"
    type = "nested_loop"

    def __init__(
        self,
        threshold_loops: int = 1000,
        **_ignored: Any,
    ) -> None:
        self.threshold_loops = threshold_loops

    def gather_info(
        self,
        node: dict[str, Any],
        parent_type: str = ""
    ) -> dict[str, Any] | None:
        if node.get("Node Type") != "Nested Loop":
            return None

        plans = node.get("Plans", [])
        if len(plans) < 2:
            return None

        inner = plans[1]
        return {
            "loops": inner.get("Actual Loops", 1),
            "inner_type": inner.get("Node Type", "?"),
            "inner_relation": inner.get("Relation Name", "?"),
            "inner_avg_rows": inner.get("Actual Rows", 0),
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        return info["loops"] > self.threshold_loops

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        return [
            Issue(
                severity=SEVERITY_INFO,
                type=self.type,
                message=(
                    f"Nested Loop ran the inner side {info['loops']} times "
                    f"('{info['inner_type']}' on '{info['inner_relation']}', "
                    f"~{info['inner_avg_rows']:.2f} rows per loop). "
                    "This is optimal for the current data, but execution "
                    "time grows linearly with the outer row count — "
                    "re-check if the outer side becomes much larger."
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

    def gather_info(
        self,
        node: dict[str, Any],
        parent_type: str = ""
    ) -> dict[str, Any] | None:
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

class IndexOnlyScanCheck(PlanCheckBase):
    """Index Only Scan with a stale visibility map.

    The planner chose an Index Only Scan because the index covers the
    predicate. But the visibility map is stale — the engine cannot
    trust the index to determine tuple visibility, so it falls back
    to the heap for each row. The result is worse than a plain Index
    Scan: index traversal plus random heap reads.

    ``gather_info`` returns:

        {
            "heap_fetches": int,
            "actual_rows":  float,
            "ratio":        float,   # heap_fetches / actual_rows
            "index_name":   str,
            "relation":     str,
        }

    Fires at ``WARNING`` level.
    """

    name = "IndexOnlyScanCheck"
    type = "index_only_scan_stale_vm"

    def __init__(
        self,
        min_rows: int = 1000,
        heap_fetch_ratio: float = 0.10,
        **_ignored: Any,
    ) -> None:
        self.min_rows = min_rows
        self.heap_fetch_ratio = heap_fetch_ratio

    def gather_info(
        self,
        node: dict[str, Any],
        parent_type: str = ""
    ) -> dict[str, Any] | None:
        if node.get("Node Type") != "Index Only Scan":
            return None
        heap_fetches = node.get("Heap Fetches", 0)
        actual_rows = node.get("Actual Rows", 0)
        if heap_fetches <= 0 or actual_rows <= 0:
            return None
        return {
            "heap_fetches": heap_fetches,
            "actual_rows": actual_rows,
            "ratio": heap_fetches / actual_rows,
            "index_name": node.get("Index Name", "?"),
            "relation": node.get("Relation Name", "?"),
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        if info["actual_rows"] < self.min_rows:
            return False
        if info["heap_fetches"] == 0:
            return False
        return info["ratio"] >= self.heap_fetch_ratio

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        pct = info["ratio"] * 100
        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.type,
                message=(
                    f"Index Only Scan on '{info['index_name']}' "
                    f"({info['relation']}) performed "
                    f"{info['heap_fetches']} heap fetches for "
                    f"{info['actual_rows']} rows ({pct:.1f}%). "
                    "The visibility map is stale. Run VACUUM, or "
                    "consider CLUSTER to improve locality."
                ),
                node="Index Only Scan",
            )
        ]

class IndexRegularScanCheck(PlanCheckBase):
    """Index Scan reading too many disk blocks for the rows returned.

    A plain Index Scan always visits the heap for each matching row.
    When the heap is poorly clustered by the indexed column, those
    visits are scattered across many blocks, and the scan reads far
    more pages than the number of rows suggests. ``CLUSTER`` on the
    index physically reorders the heap so subsequent scans read fewer
    blocks.

    ``gather_info`` returns:

        {
            "actual_rows": float,
            "read_blocks": int,
            "index_name":  str,
            "relation":    str,
        }

    Fires at ``INFO`` level — this is a hint, not a defect.
    """

    name = "IndexRegularScanCheck"
    type = "index_scan_poor_clustering"

    def __init__(
        self,
        min_rows: int = 1000,
        min_disk_blocks: int = 100,
        **_ignored: Any,
    ) -> None:
        self.min_rows = min_rows
        self.min_disk_blocks = min_disk_blocks

    def gather_info(
        self,
        node: dict[str, Any],
        parent_type: str = ""
    ) -> dict[str, Any] | None:
        if node.get("Node Type") != "Index Scan":
            return None
        actual_rows = node.get("Actual Rows", 0)
        if actual_rows <= 0:
            return None
        return {
            "actual_rows": actual_rows,
            "read_blocks": node.get("Shared Read Blocks", 0),
            "index_name": node.get("Index Name", "?"),
            "relation": node.get("Relation Name", "?"),
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        if info["actual_rows"] < self.min_rows:
            return False
        return info["read_blocks"] >= self.min_disk_blocks

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        return [
            Issue(
                severity=SEVERITY_INFO,
                type=self.type,
                message=(
                    f"Index Scan on '{info['index_name']}' "
                    f"({info['relation']}) read {info['read_blocks']} "
                    f"blocks from disk for {info['actual_rows']} rows. "
                    "Poor heap clustering may be causing random I/O. "
                    "Consider CLUSTER on this index."
                ),
                node="Index Scan",
            )
        ]

class PartitionPruningCheck(PlanCheckBase):
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

    ``gather_info`` returns:

        {
            "count":    int,
            "names":    list[str],   # partition relation names, may be empty
        }

    Fires at ``WARNING`` level.
    """

    name = "PartitionPruningCheck"
    type = "partition_pruning"

    def __init__(self, max_children: int = 3) -> None:
        self.max_children = max_children

    def gather_info(
        self,
        node: dict[str, Any],
        parent_type: str = ""
    ) -> dict[str, Any] | None:
        if node.get("Node Type") not in ("Append", "Merge Append"):
            return None

        children = node.get("Plans", [])
        return {
            "count": len(children),
            "names": [
                c.get("Relation Name")
                for c in children
                if c.get("Relation Name")
            ],
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        return info["count"] > self.max_children

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        names = info["names"]
        preview = ", ".join(names[:3])
        if len(names) > 3:
            preview += f", … (+{len(names) - 3} more)"

        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.type,
                message=(
                    f"Append over {info['count']} partitions "
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
                node="Append",
            )
        ]


class NonSargableCheck(PlanCheckBase):
    """Non-sargable predicate on a column that has a plain index.

    A predicate like ``lower(email) = 'x'`` wraps the column in a
    function. The planner cannot use a plain index on ``email`` for
    such a predicate — it has to evaluate the function for every row.
    If the column is indexed, the index exists but is unusable for
    this query.

    The check does not use a whitelist of function names — that would
    miss user-defined functions. Instead it looks for the pattern
    ``<word>(...<column>...)`` inside the ``Filter`` string, using the
    list of columns covered by plain indexes on the relation.

    ``relation_indexes`` is supplied by the caller (``server.py``)
    because the check must not query the database itself. Each entry
    is ``{leading_attnum: int, plain_columns: list[str]}``.

    ``gather_info`` returns:

        {
            "relation":   str,
            "column":     str,   # wrapped in a function
            "func":       str,   # the wrapping function name
            "filter":     str,   # full Filter string
            "rows_read":  float, # actual + removed
        }

    Fires at ``INFO`` level — the predicate is functionally correct,
    just not index-friendly.
    """

    name = "NonSargableCheck"
    type = "non_sargable"

    def __init__(
        self,
        threshold_rows: int = 1000,
        relation_indexes: dict[str, list[dict[str, Any]]] | None = None,
        **_ignored: Any,
    ) -> None:
        self.threshold_rows = threshold_rows
        self.relation_indexes = relation_indexes or {}

    def gather_info(
        self,
        node: dict[str, Any],
        parent_type: str = ""
    ) -> dict[str, Any] | None:
        filter_str = node.get("Filter", "")
        if not filter_str:
            return None

        relation = node.get("Relation Name", "?")
        indexed_columns = self._plain_indexed_columns(relation)
        if not indexed_columns:
            return None

        for column in indexed_columns:
            func = self._wrapped_in_function(filter_str, column)
            if func is None:
                continue
            return {
                "relation": relation,
                "column": column,
                "func": func,
                "filter": filter_str,
                "rows_read": (
                    node.get("Actual Rows", 0)
                    + node.get("Rows Removed by Filter", 0)
                ),
            }

        return None

    def validate_rule(self, info: dict[str, Any]) -> bool:
        return info["rows_read"] >= self.threshold_rows

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        return [
            Issue(
                severity=SEVERITY_INFO,
                type=self.type,
                message=(
                    f"Non-sargable predicate on '{info['relation']}': "
                    f"the filter wraps '{info['column']}' in "
                    f"'{info['func']}(...)', so the index on "
                    f"'{info['column']}' cannot be used. Consider a "
                    "functional index on the exact expression, or "
                    "rewriting the predicate. "
                    f"Filter: {info['filter']}"
                ),
                node="Seq Scan",
            )
        ]

    # ----- helpers -------------------------------------------------------

    def _plain_indexed_columns(self, relation: str) -> set[str]:
        """Columns that a plain ``col = value`` predicate can use.

        Only the leading slot of an index qualifies, and only if that
        slot is a real column (not an expression). Non-leading columns
        of composite indexes are excluded — a rewrite to ``col = value``
        would not use such an index.
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

        Scans every ``word(`` occurrence in the filter and, for each
        one, inspects the contents up to the matching ``)``. If the
        column appears as a whole word anywhere inside, the predicate
        is non-sargable on that column.
        """
        for m in re.finditer(r"\b(\w+)\s*\(", filter_str, re.IGNORECASE):
            func = m.group(1)
            if func.lower() in ("array", "row", "values"):
                continue

            start = m.end()
            depth = 1
            i = start
            while i < len(filter_str) and depth > 0:
                if filter_str[i] == "(":
                    depth += 1
                elif filter_str[i] == ")":
                    depth -= 1
                i += 1

            inner = (
                filter_str[start : i - 1]
                if depth == 0
                else filter_str[start:]
            )
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
        for issue in check.check(node, parent_node):
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


def _parse_plan_impl(
    plan_json: list[dict[str, Any]],
    *,
    compact: bool,
) -> list[dict[str, Any]]:
    if not plan_json:
        return []

    root = plan_json[0].get("Plan", {})
    nodes: list[dict[str, Any]] = []

    def _is_zero(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value == 0
        )

    def visit(
        node: dict[str, Any],
        parent_id: int | None,
        depth: int,
        parent_path: str,
        sibling_index: int,
    ) -> int:
        node_id = len(nodes)
        segment = f"{depth}:{sibling_index}"
        path = f"{parent_path}/{segment}" if parent_path else segment

        entry: dict[str, Any] = {
            "id": node_id,
            "parent_id": parent_id,
            "depth": depth,
            "path": path,
            "children_ids": [],
        }
        for key, value in node.items():
            if key == "Plans":
                continue
            if compact and _is_zero(value):
                continue
            entry[key] = value
        nodes.append(entry)

        for i, child in enumerate(node.get("Plans", [])):
            child_id = visit(child, node_id, depth + 1, path, i)
            entry["children_ids"].append(child_id)

        return node_id

    visit(root, None, 0, "", 0)
    return nodes

def parse_plan(plan_json: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse an EXPLAIN (FORMAT JSON) plan into a flat, navigable tree.

    Every field from the original JSON is preserved, including numeric
    zeros. Use this when you need the raw, faithful representation —
    for example, when diffing plans or inspecting individual block
    counters.

    Use ``filtered_parse_plan`` when the output is going to a human or
    an LLM and zero-noise is undesirable.
    """
    return _parse_plan_impl(plan_json, compact=False)


def filtered_parse_plan(plan_json: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same as ``parse_plan``, but drops zero-valued numeric fields.

    PostgreSQL's JSON output always includes every field, even when
    empty. On a two-node plan that is roughly thirty fields per node,
    most of them ``0``. Filtering brings the payload down to the
    fields that actually carry signal.

    Kept regardless of value:

    - booleans (``false`` is meaningful: ``Parallel Aware: false``),
    - empty lists (``children_ids: []`` — structural),
    - nulls (``parent_id: null`` for the root).

    Dropped: integers and floats equal to exactly zero.
    """
    return _parse_plan_impl(plan_json, compact=True)


_STRUCTURAL_KEYS = frozenset(
    {"id", "parent_id", "depth", "path", "children_ids", "Node Type"}
)


def format_plan_tree(nodes: list[dict[str, Any]]) -> str:
    """Render a parsed plan tree as indented text.

    Format:

        Limit
          Plan Rows: 100000
          Actual Rows: 100000
          Actual Loops: 1
          Index Scan
            Plan Rows: 1000000
            Relation Name: data_index_scan_norm
            ...

    Each node starts with its ``Node Type`` on its own line; its
    fields follow, indented by two spaces; each child is rendered
    indented by two more spaces per depth level.
    """
    if not nodes:
        return "(empty plan)"

    by_id = {n["id"]: n for n in nodes}
    lines: list[str] = []

    def render(node_id: int) -> None:
        node = by_id[node_id]
        base = "  " * node["depth"]
        lines.append(
            f"{base}{node['Node Type']} "
            f" [depth={node['depth']}]"
            f" [path={node['path']}]"
        )
        for key, value in node.items():
            if key in _STRUCTURAL_KEYS:
                continue
            lines.append(f"{base}  {key}: {value}")
        for child_id in node["children_ids"]:
            render(child_id)

    render(0)
    return "\n".join(lines)

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

