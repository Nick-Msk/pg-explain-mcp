"""PostgreSQL execution plan analyzer: detect bottlenecks and issues."""

from typing import Any


SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


def _walk_plan(node: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    """Recursively traverse the plan tree and collect issues."""
    node_type = node.get("Node Type", "")

    # 1. Sequential scan on a large table — likely missing an index
    if node_type == "Seq Scan":
        rows = node.get("Actual Rows", 0)
        if rows > 1000:
            issues.append({
                "severity": SEVERITY_WARNING,
                "type": "seq_scan",
                "message": (
                    f"Sequential scan on '{node.get('Relation Name', '?')}' "
                    f"processed {rows} rows. Consider adding an index."
                ),
                "node": node_type,
            })

    # 2. Large mismatch between planner estimate and actual rows
    planned = node.get("Plan Rows", 0)
    actual = node.get("Actual Rows", 0)
    if planned > 0 and actual > 0:
        ratio = max(planned, actual) / min(planned, actual)
        if ratio > 10:
            issues.append({
                "severity": SEVERITY_WARNING,
                "type": "estimate_mismatch",
                "message": (
                    f"Planner misestimated cardinality on '{node_type}': "
                    f"expected {planned}, got {actual} (ratio x{ratio:.1f}). "
                    "Consider running ANALYZE."
                ),
                "node": node_type,
            })

    # 3. Disk spills — work_mem is too small
    if node.get("Sort Method", "").startswith("external"):
        issues.append({
            "severity": SEVERITY_WARNING,
            "type": "disk_spill_sort",
            "message": (
                f"Sort operation on '{node_type}' spilled to disk. "
                "Increase work_mem or optimize the query."
            ),
            "node": node_type,
        })

    if node.get("Hash Batches", 1) > 1:
        issues.append({
            "severity": SEVERITY_WARNING,
            "type": "disk_spill_hash",
            "message": (
                f"Hash Join used multiple batches "
                f"({node.get('Hash Batches')}). Increase work_mem."
            ),
            "node": node_type,
        })

    # 4. Nested Loop with too many iterations
    if node_type == "Nested Loop":
        loops = node.get("Actual Loops", 1)
        if loops > 1000:
            issues.append({
                "severity": SEVERITY_INFO,
                "type": "nested_loop",
                "message": (
                    f"Nested Loop executed {loops} times. "
                    "Check whether a Hash Join would be more efficient."
                ),
                "node": node_type,
            })

    for child in node.get("Plans", []):
        _walk_plan(child, issues)


def analyze_plan(plan_json: list[dict[str, Any]]) -> dict[str, Any]:
    """Analyze a JSON execution plan and return a structured report."""
    if not plan_json:
        return {"issues": [], "summary": "Empty plan"}

    root = plan_json[0]
    plan_tree = root.get("Plan", {})
    execution_time = root.get("Execution Time", 0)
    planning_time = root.get("Planning Time", 0)

    issues: list[dict[str, Any]] = []
    _walk_plan(plan_tree, issues)

    return {
        "execution_time_ms": execution_time,
        "planning_time_ms": planning_time,
        "total_time_ms": execution_time + planning_time,
        "issues": issues,
        "issue_count": len(issues),
        "summary": _make_summary(issues, execution_time),
    }


def _make_summary(issues: list[dict[str, Any]], exec_time: float) -> str:
    """Build a short human-readable summary for the LLM."""
    if not issues:
        return f"No issues found. Execution time: {exec_time:.2f} ms."

    warnings = [i for i in issues if i["severity"] == SEVERITY_WARNING]
    return (
        f"Found {len(issues)} issues ({len(warnings)} critical). "
        f"Execution time: {exec_time:.2f} ms."
    )

