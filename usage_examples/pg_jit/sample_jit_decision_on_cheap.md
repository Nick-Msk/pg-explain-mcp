# `jit_decision` — JIT overhead dominates

**Scenario:** `SELECT sum(val)` over a 100-row table. JIT fires anyway (because `jit_above_cost` was lowered), and the compile time exceeds the plan's entire execution time. The check reports a warning.

This is the **pathological** case: JIT costs more than it saves.

## Setup

```sql
call mcp_explain_tool.fill_jit_decision(5000000);
```

`data_jit_decision_cheap` is a small table (100 rows, 1 page). `data_jit_decision_heavy` holds the 5M-row aggregate workload — see [`sample_jit_decision_on_heavy.md`](sample_jit_decision_on_heavy.md).

To reproduce the fires case, the session GUC has to be lowered so JIT is chosen for a plan whose cost is well below the default threshold:

```sql
SET jit_above_cost = 1;
```

## Query

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
SELECT sum(val) FROM mcp_explain_tool.data_jit_decision_cheap;
```

## Plan (trimmed)

```
Aggregate  (cost=2.25..2.26 rows=1 width=8) (actual time=38.366..38.366 rows=1.00 loops=1)
  Buffers: shared hit=1
  ->  Seq Scan on data_jit_decision_cheap  (cost=0.00..2.00 rows=100 width=4) (actual time=0.019..0.025 rows=100.00 loops=1)
        Buffers: shared hit=1

Planning Time: 0.666 ms
JIT:
  Functions: 3
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 1.500 ms, Inlining 0.000 ms, Optimization 7.774 ms,
          Emission 30.549 ms, Total 39.824 ms
Execution Time: 172.627 ms
```

Key numbers:

| | ms | share |
|---|---|---|
| **JIT total** | 39.824 | **103.8 %** |
| Aggregate (root, actual) | 38.366 | 100 % |
| Seq Scan | 0.025 | 0.07 % |

The plan itself does 38 ms of work. JIT spends 39.8 ms compiling the expression tree for it. The compiler costs more than the query.

## Tool output

```json
{
  "checks_applied": [
    "SeqScanCheck", "EstimateMismatchCheck", "DiskSpillSortCheck",
    "DiskSpillHashCheck", "NestedLoopCheck", "BitmapHeapScanCheck",
    "IndexRegularScanCheck", "IndexOnlyScanCheck",
    "PartitionPruningCheck", "NonSargableCheck", "JitDecisionCheck"
  ],
  "execution_time_ms": 172.627,
  "planning_time_ms": 0.666,
  "total_time_ms": 173.293,
  "issues": [
    {
      "severity": "warning",
      "type": "jit_decision",
      "message": "JIT compiled 3 function(s) in 39.82 ms on a plan that ran in 38.37 ms — 103.8% of the plan's execution time was JIT overhead. JIT pays off on long-running analytical queries, not on cheap OLTP queries. Check jit_above_cost (SHOW jit_above_cost); the default is 100000. Raise it, or disable JIT for this workload with SET jit = off.",
      "node": "JIT",
      "depth": 0,
      "parent_node": ""
    }
  ],
  "issue_count": 1,
  "summary": "Found 1 issues (1 critical). Execution time: 172.63 ms.",
  "root_meta": {
    "Planning": { "Shared Hit Blocks": 58, "Shared Read Blocks": 0 },
    "Planning Time": 0.666,
    "Triggers": [],
    "JIT": {
      "Functions": 3,
      "Options": {
        "Inlining": false, "Optimization": false,
        "Expressions": true, "Deforming": true
      },
      "Timing": {
        "Generation": { "Deform": 0.194, "Total": 1.5 },
        "Inlining": 0.0,
        "Optimization": 7.774,
        "Emission": 30.549,
        "Total": 39.824
      }
    },
    "Execution Time": 172.627
  }
}
```

## Why the check fired

`JitDecisionCheck` thresholds (from `config/seed.sql`):

- `min_jit_ms = 1.0` — JIT cost must be above 1 ms to bother reporting. 39.824 > 1, gate passes.
- `overhead_ratio = 0.3` — JIT / plan execution time must exceed 30 %. 39.824 / 38.366 = **1.038** (103.8 %), well above the threshold.

Both gates trip. The check emits a `WARNING`.

Note the denominator: the check compares JIT cost against the **root node's `actual_total_time`** (`38.366 ms`), not against the top-level `Execution Time` (`172.627 ms`). The top-level number includes executor startup, JIT context init, and result marshalling — noise that on a sub-millisecond plan dwarfs the query itself. The root node's own timing is the honest baseline.

## What went wrong

The plan's `total_cost` is **2.26**. The default `jit_above_cost` is **100000**. JIT should never have been chosen.

The fact that it was, means one of:

- `jit_above_cost` has been lowered for the session or role;
- `plan_cache_mode = force_generic_plan` is reusing a generic plan from a context where JIT was appropriate;
- a role-level or database-level `ALTER ... SET jit_above_cost` is in effect.

## Recommended fixes

**1. Verify the GUC.** Check what is actually in effect:

```sql
SHOW jit_above_cost;
SHOW jit;
SHOW plan_cache_mode;
```

If `jit_above_cost` is something small (like `1`), restore the default:

```sql
-- session-scoped
RESET jit_above_cost;

-- or, for a persistent fix at the role / database level
ALTER ROLE <role> RESET jit_above_cost;
ALTER DATABASE <db> RESET jit_above_cost;

-- if it was set in postgresql.conf
ALTER SYSTEM RESET jit_above_cost;
SELECT pg_reload_conf();
```

**2. Disable JIT for the workload.** For OLTP connections where queries are cheap, JIT is rarely worth it:

```sql
-- session
SET jit = off;

-- or, persistently
ALTER ROLE oltp_role SET jit = off;
```

**3. Leave JIT on, but raise the threshold.** If other queries in the same session are heavy and benefit from JIT, keep `jit = on` and make sure `jit_above_cost` is at the default 100000 (or higher, tuned to the workload). The per-query cost gate is what prevents this scenario.

## Expected impact

After fixing `jit_above_cost` (or disabling JIT for this workload), the same query should run in roughly:

```
planning        0.7 ms
JIT             gone
seq scan        0.025 ms
aggregate       ~0.01 ms
```

Sub-millisecond wall time. Versus the current 172.6 ms (machine-reported), a ~200× speedup on a trivial query.

## Verdict

| Check | Result |
|---|---|
| `seq_scan` | OK — 100 rows, one page, all cached |
| `estimate_mismatch` | OK |
| `disk_spill_sort` / `disk_spill_hash` | N/A |
| `nested_loop` | N/A |
| `partition_pruning` | N/A |
| `non_sargable` | N/A |
| **`jit_decision`** | **WARNING — 103.8 % JIT overhead** |

The plan shape is fine. The problem is a misconfigured GUC, not the query. Fix `jit_above_cost` (or `jit = off`) and re-run.

## See also

- [`sample_jit_decision_on_heavy.md`](sample_jit_decision_on_heavy.md) — the opposite case: JIT overhead is 2.7 % of a 5M-row aggregate and the check stays silent.

## Test environment

- PostgreSQL 18.6 (self-built with `--with-llvm`, LLVM 19.1.7)
- macOS 15 (Apple Silicon, arm64)
- `jit = on`, `jit_provider = llvmjit`
- Session override for this test: `SET jit_above_cost = 1;`
- LLM: Qwen 3.8 27B (local, via LM Studio)

