"""PostgreSQL execution plan analyzer: detect bottlenecks and issues.

The analyzer walks the JSON plan tree and applies a list of independent
checks (adapters) to every node. To add a new check, implement the
``PlanCheck`` protocol and register the instance in ``DEFAULT_CHECKS``.
"""

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from pg_explain_mcp.db import get_indexes

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
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_context(self, depth: int, parent_node: str) -> "Issue":
        return replace(self, depth=depth, parent_node=parent_node)

@dataclass
class PlanNode:
    """A single node in a parsed plan.

    Navigation is by object reference — ``parent`` and ``children``
    point to other PlanNode instances, not ids. Comparable to
    ``struct PlanNode { PlanNode *parent; PlanNode **children; }``
    in C, plus the EXPLAIN fields attached.
    """

    depth: int
    path: str
    parent: "PlanNode | None" = None
    children: list["PlanNode"] = field(default_factory=list)
    fields: dict[str, Any] = field(default_factory=dict)
    root_meta: dict[str, Any] = field(default_factory=dict)

    # --- field access (behave like the old dict) --------------------

    def __getitem__(self, key: str) -> Any:
        return self.fields[key]

    def __contains__(self, key: str) -> bool:
        return key in self.fields

    def get(self, key: str, default: Any = None) -> Any:
        return self.fields.get(key, default)

    def items(self):
        return self.fields.items()

    @property
    def node_type(self) -> str:
        return self.fields.get("Node Type", "?")

    # --- navigation -------------------------------------------------

    def ancestors(self) -> Iterator["PlanNode"]:
        """Immediate parent → … → root."""
        cur = self.parent
        while cur is not None:
            yield cur
            cur = cur.parent

    def descendants(self) -> Iterator["PlanNode"]:
        """All descendants in DFS pre-order."""
        stack = list(reversed(self.children))
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed(n.children))

    def root(self) -> "PlanNode":
        n = self
        while n.parent is not None:
            n = n.parent
        return n

    def is_under(self, node_type: str) -> bool:
        """True if any ancestor (or self) has the given Node Type."""
        return self.node_type == node_type or any(
            a.node_type == node_type for a in self.ancestors()
        )

class CheckBase(ABC):
    """Common base for all checks.

    Subclasses declare:

      - ``name``   — registry key, matches SQLite ``checks.name``.
      - ``type``   — issue type, matches JSON ``issues[].type``.
      - ``PARAMS`` — ``{param_name: type}`` — a schema for coercion.

    Actual values come from ``check_params`` and are passed to the
    constructor as ``dict[str, str]``. Each value is coerced to the
    declared type. A missing parameter is an error — the database is
    the source of truth, and a check should never fall back to an
    implicit default.
    """

    name: str
    type: str
    PARAMS: dict[str, type] = {}

    def __init__(
        self,
        params: dict[str, str] | None = None,
    ) -> None:
        raw = params or {}
        self.params: dict[str, Any] = {}
        for key, typ in self.PARAMS.items():
            if key not in raw:
                raise ValueError(
                    f"{self.name}: missing required param '{key}'"
                )
            try:
                self.params[key] = typ(raw[key])
            except (ValueError, TypeError) as e:
                raise ValueError(
                    f"{self.name}.{key}: invalid value {raw[key]!r} ({e})"
                ) from e

class ParsedPlanCheckBase(CheckBase):
    """Checks that inspect a single node with free navigation.

    The analyzer calls ``check(node)`` for every node in pre-order.
    ``node.parent`` and ``node.children`` are real object references —
    navigation to any ancestor or descendant is direct, no path
    parsing involved.
    """

    def check(self, node: PlanNode) -> list[Issue]:
        info = self.gather_info(node)
        if info is None or not self.validate_rule(info):
            return []
        issues = self.generate_msg(info)
        # Attach the check's own working dict as context on every
        # issue. The same info that drove validate_rule is now
        # available to pg-tune / the LLM as structured facts —
        # no need to parse the human-readable message.
        return [replace(i, context=info) for i in issues]

    @abstractmethod
    def gather_info(self, node: PlanNode) -> dict[str, Any] | None:
        ...

    @abstractmethod
    def validate_rule(self, info: dict[str, Any]) -> bool:
        ...

    @abstractmethod
    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        ...

def new_parse_plan(plan_json: list[dict[str, Any]]) -> PlanNode | None:
    """Build a linked tree from EXPLAIN (FORMAT JSON) output.

    Returns the root PlanNode, or None for an empty plan.
    """
    if not plan_json:
        return None

    root_raw = plan_json[0]
    root_meta = {k: v for k, v in root_raw.items() if k != "Plan"}

    def build(
        raw: dict[str, Any],
        parent: PlanNode | None,
        depth: int,
        parent_path: str,
        sibling_index: int,
    ) -> PlanNode:
        segment = f"{depth}:{sibling_index}"
        path = f"{parent_path}/{segment}" if parent_path else segment
        fields = {k: v for k, v in raw.items() if k != "Plans"}
        node = PlanNode(
            depth=depth,
            path=path,
            parent=parent,
            fields=fields,
            root_meta=root_meta
        )
        for i, child_raw in enumerate(raw.get("Plans", [])):
            node.children.append(build(child_raw, node, depth + 1, path, i))
        return node

    return build(root_raw.get("Plan", {}), None, 0, "", 0)

def new_parse_root(plan_json: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract top-level EXPLAIN metadata.

    ``plan_json[0]`` carries, alongside ``Plan``:

    - ``Planning``      — planning-time buffer/IO counters
    - ``Planning Time`` — planning wall time, ms
    - ``Execution Time``— execution wall time, ms
    - ``JIT``           — JIT stats (present only when JIT ran)
    - ``Triggers``      — trigger firing info

    Returns everything except ``Plan``. Empty dict for empty input.
    """
    if not plan_json:
        return {}
    root = plan_json[0]
    return {k: v for k, v in root.items() if k != "Plan"}

def _render_meta(value: Any, indent: int) -> list[str]:
    pad = "  " * indent

    if isinstance(value, dict):
        if not value:
            return [f"{pad}{{}}"]
        lines: list[str] = []
        for k, v in value.items():
            if isinstance(v, dict) and v:
                lines.append(f"{pad}{k}:")
                lines.extend(_render_meta(v, indent + 1))
            elif isinstance(v, list) and v:
                lines.append(f"{pad}{k}:")
                lines.extend(_render_meta(v, indent + 1))
            else:
                # scalar, or empty dict / empty list — inline
                lines.append(f"{pad}{k}: {v}")
        return lines

    if isinstance(value, list):
        if not value:
            return [f"{pad}[]"]
        lines = []
        for i, item in enumerate(value):
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}[{i}]:")
                lines.extend(_render_meta(item, indent + 1))
            else:
                lines.append(f"{pad}[{i}]: {item}")
        return lines

    return [f"{pad}{value}"]

def new_format_root(meta: dict[str, Any]) -> str:
    """Render top-level EXPLAIN metadata as indented text."""
    if not meta:
        return ""
    return "\n".join(_render_meta(meta, 0))

def _auto_key(raw: str) -> str:
    """Fallback snake_case key for fields not listed in plan_fields."""
    return raw.lower().replace(" ", "_").replace("/", "_")

def new_plan_to_list(
    root: PlanNode | None,
    field_config: dict[str, tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Serialize the tree to a flat, JSON-friendly list.

    ``field_config`` maps ``raw_explain_field → (compact_key, mode)``.
    Modes: 0=hide, 1=keep zeros, 999=drop zeros. If ``None``,
    every field is kept under its original name with no filtering.
    """
    if root is None:
        return []

    result: list[dict[str, Any]] = []

    def visit(node: PlanNode, parent_id: int | None) -> int:
        node_id = len(result)
        entry: dict[str, Any] = {
            "id": node_id,
            "parent_id": parent_id,
            "depth": node.depth,
            "path": node.path,
            "children_ids": [],
        }
        for raw, value in node.fields.items():
            if field_config is not None:
                spec = field_config.get(raw)
                if spec is None:
                    # Unknown field — treat as mode 999.
                    if _is_zero(value):
                        continue
                    entry[_auto_key(raw)] = value
                else:
                    key, mode = spec
                    if mode == _FIELD_HIDE:
                        continue
                    if mode != _FIELD_KEEP_ZEROS and _is_zero(value):
                        continue
                    entry[key] = value
            else:
                entry[raw] = value

        result.append(entry)
        for child in node.children:
            child_id = visit(child, node_id)
            entry["children_ids"].append(child_id)
        return node_id

    visit(root, None)
    return result

def new_format_plan_tree(
    root: PlanNode | None,
    field_config: dict[str, tuple[str, int]] | None = None,
    marker_tabs: int = 5,
) -> str:
    """Render a PlanNode tree as indented text.

    Same visual layout as ``format_plan_tree``, but takes a PlanNode
    root instead of a flat list.
     Applies the same field policy as ``new_plan_to_list``:
    0 = hide, 1 = keep zeros, 999 = drop zeros; None = keep all.
    """
    if root is None:
        return "(empty plan)"

    pad = "\t" * marker_tabs
    lines: list[str] = []

    def render(node: PlanNode) -> None:
        base = "  " * node.depth
        marker = node.path.rsplit("/", 1)[-1]
        lines.append(f"{base}{node.node_type}{pad}[{marker}]")
        for raw, value in node.fields.items():
            if field_config is not None:
                spec = field_config.get(raw)
                if spec is None:
                    if _is_zero(value):
                        continue
                else:
                    key, mode = spec
                    if mode == _FIELD_HIDE:
                        continue
                    if mode != _FIELD_KEEP_ZEROS and _is_zero(value):
                        continue
            lines.append(f"{base}  {raw}: {value}")
        for child in node.children:
            render(child)

    render(root)
    return "\n".join(lines)

def new_analyze_plan(
    plan_json: list[dict[str, Any]],
    checks: tuple[CheckBase, ...],
) -> dict[str, Any]:
    """Analyze a plan using PlanNode-based checks.

    Walks every node in pre-order, calls ``check(node)``, and enriches
    each resulting Issue with the node's depth and parent type — so
    the LLM can group child issues under their root cause.
    """
    if not plan_json:
        return {"issues": [], "summary": "Empty plan"}

    root_meta = plan_json[0]
    execution_time = root_meta.get("Execution Time", 0)
    planning_time = root_meta.get("Planning Time", 0)

    tree = new_parse_plan(plan_json)
    if tree is None:
        return {"issues": [], "summary": "Empty plan"}

    issues: list[Issue] = []
    for node in [tree, *tree.descendants()]:
        parent_type = node.parent.node_type if node.parent else ""
        for check in checks:
            for issue in check.check(node):
                issues.append(issue.with_context(node.depth, parent_type))

    return {
        "checks_applied": [c.name for c in checks],
        "execution_time_ms": execution_time,
        "planning_time_ms": planning_time,
        "total_time_ms": execution_time + planning_time,
        "issues": [issue.to_dict() for issue in issues],
        "issue_count": len(issues),
        "summary": _make_summary(issues, execution_time),
    }

# ---------------------------------------------------------------------------
# Checks (adapters)
# ---------------------------------------------------------------------------

class SeqScanCheck(ParsedPlanCheckBase):
    """Sequential scan that discards most of what it reads.

    A Seq Scan is not a problem by itself. It becomes one when the
    planner reads many rows only to throw most of them away — usually
    a sign that an index on the filter column might help.

    The check ignores:

    - small scans (below ``threshold_rows``);
    - scans with no filter (``Rows Removed by Filter = 0``) — reading
      the whole table is the only reasonable strategy here;
    - scans where the filter rejects less than ``min_filter_ratio`` of
      the rows read — an index rarely beats a Seq Scan at moderate
      selectivity.

    ``gather_info`` returns:

        {
            "actual":     float,
            "removed":    float,
            "total_read": float,
            "ratio":      float,   # removed / total_read
            "relation":   str,
        }

    Fires at ``WARNING`` level. The message is deliberately neutral:
    it asks the caller to verify whether an index already exists,
    rather than instructing to create one.
    """

    name = "SeqScanCheck"
    type = "seq_scan"
    PARAMS = {
        "threshold_rows":   int,
        "min_filter_ratio": float,
    }

    def gather_info(self, node: PlanNode) -> dict[str, Any] | None:
        if node.node_type != "Seq Scan":
            return None

        actual, removed = _loop_adjusted_rows(node)
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
        if info["total_read"] <= self.params["threshold_rows"]:
            return False
        if info["removed"] == 0:
            return False
        return info["ratio"] >= self.params["min_filter_ratio"]

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
                    "Verify whether an index on the filter column "
                    "exists; if it does, investigate why the planner "
                    "ignored it (stale statistics, low correlation, or "
                    "high random_page_cost). If no index exists, "
                    "consider adding one."
                ),
                node="Seq Scan",
            )
        ]

class EstimateMismatchCheck(ParsedPlanCheckBase):
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
    PARAMS = {
        "threshold_ratio": float,
        "min_rows":        int,
    }

    def gather_info(self, node: PlanNode) -> dict[str, Any] | None:
        # plan_rows under a Limit is the full-scan estimate, not the
        # truncated one — comparing it to actual yields a spurious
        # ratio.
        if node.is_under("Limit"):
            return None

        planned = node.get("Plan Rows", 0)
        actual = node.get("Actual Rows", 0)
        if planned <= 0 or actual <= 0:
            return None

        return {
            "planned": planned,
            "actual": actual,
            "ratio": max(planned, actual) / min(planned, actual),
            "node_type": node.node_type,
            "relation": node.get("Relation Name", ""),
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        if info["planned"] < self.params["min_rows"]:
            return False
        if info["actual"] < self.params["min_rows"]:
            return False
        return info["ratio"] > self.params["threshold_ratio"]

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        where = f" on '{info['relation']}'" if info["relation"] else ""
        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.type,
                message=(
                    f"Planner misestimated cardinality on "
                    f"'{info['node_type']}'{where}: expected "
                    f"{info['planned']}, got {info['actual']} "
                    f"(ratio x{info['ratio']:.1f}). "
                    "Investigate why: stale statistics, a non-sargable "
                    "predicate on the column, or a distribution not "
                    "covered by the column histogram. ANALYZE helps "
                    "only in the first case."
                ),
                node=info["node_type"],
            )
        ]

class DiskSpillSortCheck(ParsedPlanCheckBase):
    """Sort spilled to disk — work_mem is too small.

    Fires when ``Sort Method`` starts with ``external``. If the plan
    reports the spill size (``Sort Space Type = "Disk"`` and ``Sort
    Space Used > 0``), the message includes the size and a rounded-up
    ``work_mem`` recommendation. Otherwise a generic message is
    emitted.

    ``gather_info`` returns:

        {
            "size_kb": int,    # 0 when the plan does not report it
            "size_mb": float,  # 0.0 when the plan does not report it
        }
    """

    name = "DiskSpillSortCheck"
    type = "disk_spill_sort"
    PARAMS = {
        "min_spill_kb":     int,
        "min_work_mem_mb":  int,     # bottom of the standard series
        "headroom_ratio":   float,   # safety margin before rounding up
    }

    def gather_info(self, node: PlanNode) -> dict[str, Any] | None:
        method = node.get("Sort Method", "")
        if not method.startswith("external"):
            return None
        used = node.get("Sort Space Used", 0)
        return {
            "size_kb": used,
            "size_mb": used / 1024 if used else 0.0,
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        return info["size_kb"] >= self.params["min_spill_kb"]

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        if not info["size_kb"]:
            return [
                Issue(
                    severity=SEVERITY_WARNING,
                    type=self.type,
                    message=(
                        "Sort spilled to disk. Increase work_mem. "
                        "Note: hash_mem_multiplier does NOT apply to "
                        "sorts — only work_mem counts."
                    ),
                    node="Sort",
                )
            ]

        size_kb = info["size_kb"]
        size_mb = info["size_mb"]

        # round up to the next power of two: 32/64/128/256/512/1024
        # Round up to the next power-of-two standard value from the
        # series {min_work_mem_mb, 2x, 4x, 8x, ...}.
        target = self.params["min_work_mem_mb"]
        ceiling = size_mb * self.params["headroom_ratio"]

        while target < ceiling:
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

class DiskSpillHashCheck(ParsedPlanCheckBase):
    """Hash operation spilled to disk — work_mem is too small.

    When the hash table no longer fits in ``work_mem``, PostgreSQL
    partitions it into multiple batches and writes the excess to
    temporary files on disk.

    ``gather_info`` returns:

        {
            "batches":      int,
            "peak_kb":      int,      # 0 if absent
            "estimated_mb": float,    # 0.0 if peak absent
            "disk_kb":      int,      # 0 if absent
            "parallel":     bool,
            "loops":        int,
            "node_type":    str,
        }

    The effective hash budget is ``work_mem × hash_mem_multiplier`` —
    the multiplier is not part of the plan, so the message points at
    ``list_parameters`` for the caller to look it up.
    """

    name = "DiskSpillHashCheck"
    type = "disk_spill_hash"
    PARAMS = {"min_batches": int}

    def gather_info(self, node: PlanNode) -> dict[str, Any] | None:
        batches = node.get("Hash Batches", 1)
        if batches <= 0:
            return None

        peak_kb = node.get("Peak Memory Usage", 0)
        return {
            "batches": batches,
            "peak_kb": peak_kb,
            "estimated_mb": (
                round(peak_kb * batches / 1024, 1) if peak_kb else 0.0
            ),
            "disk_kb": node.get("Disk Usage", 0),
            "parallel": node.get("Parallel Aware", False),
            "loops": node.get("Actual Loops", 1),
            "node_type": node.node_type,
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        return info["batches"] >= self.params["min_batches"]

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        parts = [f"{info['batches']} batches"]
        if info["peak_kb"]:
            parts.append(f"peak {info['peak_kb']}kB per batch")
            parts.append(
                f"estimated full size ≈ {info['estimated_mb']} MB"
            )
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
                    f"Hash operation spilled to disk{parallel_note}: "
                    f"{details}. "
                    "To keep the hash table in memory, set work_mem such "
                    "that work_mem × hash_mem_multiplier > estimated full "
                    "size. Call list_parameters for the current "
                    "hash_mem_multiplier."
                ),
                node=info["node_type"],
            )
        ]

class NestedLoopCheck(ParsedPlanCheckBase):
    """Nested Loop with a high number of inner iterations.

    PostgreSQL reports the loop count on the **inner** child
    (``children[1]``), not on the Nested Loop node itself — the outer
    node always reports ``Actual Loops = 1``.

    A Nested Loop with an indexed inner side and ~1 row per lookup is
    optimal for a small outer table; there is nothing to fix. The
    check is emitted at ``INFO`` level as a heads-up for future
    growth, not as a warning.

    ``gather_info`` returns:

        {
            "loops":          int,
            "inner_type":     str,
            "inner_relation": str,
            "inner_avg_rows": float,
        }
    """

    name = "NestedLoopCheck"
    type = "nested_loop"
    PARAMS = {"threshold_loops": int}

    def gather_info(self, node: PlanNode) -> dict[str, Any] | None:
        if node.node_type != "Nested Loop":
            return None
        if len(node.children) < 2:
            return None

        inner = node.children[1]
        return {
            "loops": inner.get("Actual Loops", 1),
            "inner_type": inner.node_type,
            "inner_relation": inner.get("Relation Name", "?"),
            "inner_avg_rows": inner.get("Actual Rows", 0),
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        return info["loops"] > self.params["threshold_loops"]

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        return [
            Issue(
                severity=SEVERITY_INFO,
                type=self.type,
                message=(
                    f"Nested Loop ran the inner side {info['loops']} times "
                    f"('{info['inner_type']}' on "
                    f"'{info['inner_relation']}', "
                    f"~{info['inner_avg_rows']:.2f} rows per loop). "
                    "This is optimal for the current data, but execution "
                    "time grows linearly with the outer row count — "
                    "re-check if the outer side becomes much larger."
                ),
                node="Nested Loop",
            )
        ]

class BitmapHeapScanCheck(ParsedPlanCheckBase):
    """Large Bitmap Heap Scan.

    A Bitmap Heap Scan is the right strategy for medium selectivity:
    too many rows for an index scan, too few for a full sequential
    scan. When the scan processes a very large number of rows, the
    heap fetches dominate — often a sign that a composite index or
    partitioning would reduce I/O.

    ``gather_info`` returns:

        {
            "rows":     float,
            "relation": str,
        }

    Fires at ``INFO`` level — the scan itself is not wrong, it is
    simply worth investigating at scale.
    """

    name = "BitmapHeapScanCheck"
    type = "bitmap_heap_scan"
    PARAMS = {"threshold_rows": int}

    def gather_info(self, node: PlanNode) -> dict[str, Any] | None:
        if node.node_type != "Bitmap Heap Scan":
            return None
        return {
            "rows": node.get("Actual Rows", 0),
            "relation": node.get("Relation Name", "?"),
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        return info["rows"] > self.params["threshold_rows"]

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

class IndexOnlyScanCheck(ParsedPlanCheckBase):
    """Index Only Scan with a stale visibility map.

    The planner chose an Index Only Scan because the index covers the
    predicate. But the visibility map is stale — the engine cannot
    trust the index to determine tuple visibility, so it falls back
    to the heap for each row. The result is worse than a plain Index
    Scan: index traversal plus random heap reads.

    Typical cause: heavy UPDATE/DELETE churn without VACUUM.

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
    PARAMS = {
        "min_rows":         int,
        "heap_fetch_ratio": float,
    }

    def gather_info(self, node: PlanNode) -> dict[str, Any] | None:
        if node.node_type != "Index Only Scan":
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
        if info["actual_rows"] < self.params["min_rows"]:
            return False
        return info["ratio"] >= self.params["heap_fetch_ratio"]

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

class IndexRegularScanCheck(ParsedPlanCheckBase):
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
    PARAMS = {
        "min_rows":        int,
        "min_disk_blocks": int,
    }

    def gather_info(self, node: PlanNode) -> dict[str, Any] | None:
        if node.node_type != "Index Scan":
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
        if info["actual_rows"] < self.params["min_rows"]:
            return False
        return info["read_blocks"] >= self.params["min_disk_blocks"]

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

class PartitionPruningCheck(ParsedPlanCheckBase):
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
    PARAMS = {"max_children": int}

    def gather_info(self, node: PlanNode) -> dict[str, Any] | None:
        if node.node_type not in ("Append", "Merge Append"):
            return None

        return {
            "count": len(node.children),
            "names": [
                c.get("Relation Name")
                for c in node.children
                if c.get("Relation Name")
            ],
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        return info["count"] > self.params["max_children"]

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
                    f"(threshold: {self.params['max_children']}). "
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

class NonSargableCheck(ParsedPlanCheckBase):
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

    PARAMS = {
        "threshold_rows": int,
    }
    # Lazy cache: filled on first gather_info, shared only within
    # this instance. The class-level None is a shared read-only
    # default — instance assignment shadows it.
    _indexes: dict[str, list[dict[str, Any]]] | None = None

    def _load_indexes(self) -> dict[str, list[dict[str, Any]]]:
        """Lazy-load all user indexes, grouped by relation name.

        Called once per check instance. gather_info runs for every
        plan node, so a per-call get_indexes() would produce N queries
        per explain.
        """
        if self._indexes is None:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in get_indexes():
                grouped.setdefault(row["table_name"], []).append(row)
            self._indexes = grouped
        return self._indexes

    def gather_info(self, node: PlanNode) -> dict[str, Any] | None:
        if self._indexes is None:
            self._indexes = self._load_indexes()

        filter_str = node.get("Filter", "")
        if not filter_str:
            return None

        relation = node.get("Relation Name")
        if not relation:
            return None

        indexed_columns = self._plain_indexed_columns(relation)
        if not indexed_columns:
            return None

        for column in indexed_columns:
            func = self._wrapped_in_function(filter_str, column)
            if func is None:
                continue
            rows_read = (
                node.get("Actual Rows", 0)
                + node.get("Rows Removed by Filter", 0)
            )
            return {
                "relation": relation,
                "column": column,
                "func": func,
                "filter": filter_str,
                "rows_read": rows_read,
            }

        return None

    def validate_rule(self, info: dict[str, Any]) -> bool:
        return info["rows_read"] >= self.params["threshold_rows"]

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

    def _plain_indexed_columns(self, relation: str) -> set[str]:
        indexes = self._load_indexes().get(relation, [])
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

class JitDecisionCheck(ParsedPlanCheckBase):
    """JIT compilation dominated the query's runtime.

    Reads the top-level ``JIT`` block from EXPLAIN JSON, available
    on every node via ``node.root_meta``. Fires once per query — on
    the root node only — when JIT overhead exceeds
    ``overhead_ratio`` of ``Execution Time`` and the absolute JIT
    cost is above ``min_jit_ms``.

    A query like ``SELECT count(*) FROM flights`` (0.4 ms of real
    work) with JIT overhead of 3.2 ms is the canonical case — JIT
    costs more than it saves.

    ``gather_info`` returns::

        {
            "functions": int,
            "total_jit": float,   # ms
            "exec_time": float,   # ms
            "ratio":     float,   # total_jit / exec_time
        }
    """

    name = "JitDecisionCheck"
    type = "jit_decision"
    PARAMS = {
        "min_jit_ms":     float,
        "overhead_ratio": float,
    }

    def gather_info(self, node: PlanNode) -> dict[str, Any] | None:
        # Root-meta checks run once per query. Skip descendants.
        if node.parent is not None:
            return None

        jit = node.root_meta.get("JIT")
        if not jit:
            return None

        total_jit = jit.get("Timing", {}).get("Total", 0.0)

        # Compare against the root node's own execution time, not the
        # top-level "Execution Time". The latter includes executor
        # startup/shutdown, JIT context init, and result transfer — on
        # cheap queries that overhead dwarfs the query itself and
        # drowns out the JIT signal.
        node_time = node.get("Actual Total Time", 0.0)
        if node_time <= 0:
            return None

        return {
            "functions": jit.get("Functions", 0),
            "total_jit": total_jit,
            "node_time": node_time,
            "ratio": total_jit / node_time,
        }

    def validate_rule(self, info: dict[str, Any]) -> bool:
        if info["total_jit"] < self.params["min_jit_ms"]:
            return False
        return info["ratio"] >= self.params["overhead_ratio"]

    def generate_msg(self, info: dict[str, Any]) -> list[Issue]:
        pct = info["ratio"] * 100
        return [
            Issue(
                severity=SEVERITY_WARNING,
                type=self.type,
                message=(
                    f"JIT compiled {info['functions']} function(s) in "
                    f"{info['total_jit']:.2f} ms on a plan that ran in "
                    f"{info['node_time']:.2f} ms — {pct:.1f}% of the plan's "
                    "execution time was JIT overhead. JIT pays off on "
                    "long-running analytical queries, not on cheap OLTP "
                    "queries. Check jit_above_cost (SHOW jit_above_cost); "
                    "the default is 100000. Raise it, or disable JIT "
                    "for this workload with SET jit = off."
                ),
                node="JIT",
            )
        ]


# ---------------------------------------------------------------------------
# Traversal and reporting
# ---------------------------------------------------------------------------


def _make_summary(issues: list[Issue], exec_time: float) -> str:
    """Build a short human-readable summary for the LLM."""
    if not issues:
        return f"No issues found. Execution time: {exec_time:.2f} ms."

    warnings = [i for i in issues if i.severity == SEVERITY_WARNING]
    return (
        f"Found {len(issues)} issues ({len(warnings)} critical). "
        f"Execution time: {exec_time:.2f} ms."
    )

# Field modes used by filtered_parse_plan.
_FIELD_HIDE = 0          # drop the field entirely
_FIELD_KEEP_ZEROS = 1    # keep, even when zero
_FIELD_UNKNOWN = 999     # unknown — keep, but drop numeric zeros


def _is_zero(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == 0
    )

def _loop_adjusted_rows(node: PlanNode) -> tuple[float, float]:
    """Return ``(actual_rows, rows_removed_by_filter)`` × ``Actual Loops``.

    In a parallel plan each worker reports its own per-loop counters.
    Multiplying by ``Actual Loops`` reconstructs the total for the
    node — what the check actually cares about.

    ``Actual Loops`` is 1 for non-parallel nodes, so this is a no-op
    in that case.
    """
    loops = node.get("Actual Loops", 1) or 1
    actual = node.get("Actual Rows", 0) * loops
    removed = node.get("Rows Removed by Filter", 0) * loops
    return actual, removed

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

