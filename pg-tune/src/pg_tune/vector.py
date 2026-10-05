"""Extract execution metrics from an EXPLAIN ANALYZE plan.

The metric-to-source mapping lives in the tune SQLite config
(``tune_vec_params``), not in this module. Each row declares a
``scope`` (``root_meta`` for top-level fields, ``root_plan`` for
fields on the root Plan node) and a ``raw_key`` — the EXPLAIN
field name to read.

Only the root is ever read. Descendants have their own per-node
counters, and in parallel plans summing them would double-count.
"""

from typing import Any


def _to_float(value: Any) -> float | None:
    """Return float(value), or None if it cannot be coerced."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_vector(
    plan_json: list[dict[str, Any]],
    params: list[dict[str, Any]],
) -> dict[str, float]:
    """Return {metric_name: value} for one EXPLAIN ANALYZE run.

    ``plan_json`` is the raw ``QUERY PLAN`` payload from
    ``EXPLAIN (ANALYZE, FORMAT JSON)``.

    ``params`` is the list of rows from ``tune_vec_params`` — the
    caller loads it once and can reuse it across many plans in the
    same tuning session. Each row must have ``name``, ``scope``,
    and ``raw_key``.

    Metrics that are absent from the plan are omitted rather than
    stored as 0: a missing counter is not the same as a measured
    zero.
    """
    if not plan_json:
        return {}

    root_meta = plan_json[0]
    plan = root_meta.get("Plan") or {}

    result: dict[str, float] = {}
    for p in params:
        scope = p["scope"]
        if scope == "root_meta":
            source = root_meta
        elif scope == "root_plan":
            source = plan
        else:
            # Unknown scope — schema check should prevent this.
            # Skip rather than guess.
            continue

        value = _to_float(source.get(p["raw_key"]))
        if value is not None:
            result[p["name"]] = value

    return result

