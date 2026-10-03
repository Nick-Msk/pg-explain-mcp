# `jit_decision` — JIT is worth it

**Scenario:** CPU-bound aggregate over 5M rows. The plan cost clears `jit_above_cost`, so PostgreSQL compiles the expression tree. Overhead is a small fraction of execution time — the check stays silent.

This is the **healthy** case: JIT is triggered, and it earns its keep.

## Setup

```sql
call mcp_explain_tool.fill_jit_decision(5000000);
```

`data_jit_decision_heavy` holds 5M rows, ~482 MB on disk, one `bigint` and one `double precision` column plus a short padding string. `data_jit_decision_cheap` is a small 100-row table used by the fires case — see [`sample_jit_decision_on_cheap.md`](sample_jit_decision_on_cheap.md).

## Query

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
SELECT sum(
    sqrt(val) * ln(val + 1)
  + power(val, 1.5)
  + sin(val / 100) * cos(val / 200)
  + exp(val / 10000)
  + atan(val / 1000)
  + log(val + 2)
  + cbrt(val)
  + tan(val / 5000)
  + asin(val / 5000)
  + length(repeat(pad, 2))
)
FROM mcp_explain_tool.data_jit_decision_heavy;
```

Eleven math/string functions per row × 5M rows ≈ 55M scalar function calls. The plan is irreducibly CPU-bound.

## Plan (trimmed)

```
Finalize Aggregate  (cost=260647.84..260647.85) (actual time=737.109..739.355 rows=1.00 loops=1)
  Buffers: shared hit=4642 read=57087
  I/O Timings: shared read=5.816
  ->  Gather  (cost=260647.63..260647.84) (actual time=737.022..739.343 rows=3.00 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        ->  Partial Aggregate  (cost=259647.63..259647.64) (actual time=727.342..727.342 rows=1.00 loops=3)
              ->  Parallel Seq Scan on data_jit_decision_heavy  (cost=0.00..82562.54 rows=2083354 width=59) (actual time=0.409..78.731 rows=1666666.67 loops=3)

Planning Time: 0.381 ms
JIT:
  Functions: 11
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 1.362 ms, Inlining 0.000 ms, Optimization 0.713 ms,
          Emission 10.708 ms, Total 12.783 ms
Execution Time: 751.685 ms
```

Key numbers:

| | ms | share |
|---|---|---|
| **JIT total** | 12.783 | **1.7 %** |
| Aggregate (root, actual) | 739.355 | 100 % |
| Partial Aggregate (per worker) | 727.342 | 98 % |
| Seq Scan (per worker) | 78.731 | 10 % |

The scan reads ~57k pages from disk (~92 % cold cache), but the disk I/O time is only 5.8 ms — I/O is not the bottleneck. The `Partial Aggregate` node, which evaluates the expression tree per row, dominates at ~727 ms per worker. This is exactly the workload JIT is designed for.

## Tool output

```json
{
  "checks_applied": [
    "SeqScanCheck", "EstimateMismatchCheck", "DiskSpillSortCheck",
    "DiskSpillHashCheck", "NestedLoopCheck", "BitmapHeapScanCheck",
    "IndexRegularScanCheck", "IndexOnlyScanCheck",
    "PartitionPruningCheck", "NonSargableCheck", "JitDecisionCheck"
  ],
  "execution_time_ms": 751.685,
  "planning_time_ms": 0.381,
  "total_time_ms": 752.066,
  "issues": [],
  "issue_count": 0,
  "summary": "No issues found. Execution time: 751.68 ms.",
  "root_meta": {
    "Planning Time": 0.381,
    "Triggers": [],
    "JIT": {
      "Functions": 11,
      "Options": {
        "Inlining": false, "Optimization": false,
        "Expressions": true, "Deforming": true
      },
      "Timing": {
        "Generation": { "Deform": 0.139, "Total": 1.362 },
        "Inlining": 0.0,
        "Optimization": 0.713,
        "Emission": 10.708,
        "Total": 12.783
      }
    },
    "Execution Time": 751.685
  }
}
```

## Why the check did not fire

`JitDecisionCheck` thresholds (from `config/seed.sql`):

- `min_jit_ms = 1.0` — JIT cost must be above 1 ms to bother reporting. 12.783 > 1, gate passes.
- `overhead_ratio = 0.3` — JIT / plan execution time must exceed 30 %. 12.783 / 739.355 = **1.7 %**, well below the threshold.

Both gates must trip; the second one does not. No issue raised.

Note the denominator: the check compares JIT cost against the **root node's `actual_total_time`** (`739.355 ms`), not against the top-level `Execution Time` (`751.685 ms`). The two are nearly identical here because the plan runs long enough that executor startup is negligible. On cheap queries, the same top-level number would include 130 ms of noise; the root-node timing is the honest baseline.

## Why JIT is a net positive here

The workload is the exact shape JIT is designed for:

- ~55M scalar function calls (`sqrt`, `ln`, `power`, `sin`, `cos`, `exp`, `atan`, `log`, `cbrt`, `tan`, `asin`, `length`);
- each call goes through PostgreSQL's expression interpreter — a tree-walking evaluator with per-node dispatch, `ExprState` dereferencing, and `TupleTableSlot` lookups;
- JIT compiles the expression tree into a single native function, eliminating per-node dispatch overhead.

The compiler spent 12.8 ms per process (≈38 ms across 3 processes). The query ran for 751 ms. Even a 3–5 % reduction in per-call overhead pays back the compile many times over.

## Why "Basic" JIT, not "Full"

| GUC | Value | Plan cost | Active? |
|---|---|---|---|
| `jit_above_cost` | 100,000 | 260,648 | ✅ |
| `jit_inline_above_cost` | 500,000 | 260,648 | ❌ |
| `jit_optimize_above_cost` | 500,000 | 260,648 | ❌ |

The plan cost clears the JIT threshold but falls short of the inlining and optimization thresholds. PostgreSQL compiled the expressions as a flat function — eliminating per-call overhead — but did not run LLVM's inliner or optimizer passes. For a single complex scalar expression evaluated 55M times, the flat-function compilation alone is worth it.

If this query were on a critical path and you wanted the extra 5–15 % from full LLVM optimization, you could lower the thresholds:

```sql
SET jit_inline_above_cost = 100000;
SET jit_optimize_above_cost = 100000;
```

That lets LLVM inline `sqrt`, `sin`, `cos`, and the rest into a single compiled block, accepting a slightly higher compile cost per statement. Whether that is worth it depends on query frequency, not on this single run.

## Possible (marginal) improvements

The plan shape is already optimal: parallel seq scan feeding three partial aggregates, then a trivial `Gather`. There is nothing structural to fix. If the query is on a critical path:

- **More workers.** Only 2 workers launched. With 5M rows of CPU-bound math, more workers would help — the expression evaluation is the bottleneck, not I/O:
  ```sql
  SET max_parallel_workers_per_gather = 4;
  ```
  Expected: near-linear speedup up to the CPU core limit.

- **Full JIT tier.** Lower `jit_inline_above_cost` / `jit_optimize_above_cost` to 100,000 so LLVM runs its optimizer on the compiled expression. Modest gain (5–15 %) on the hot loop.

- **Avoid recomputation.** For a full-table aggregate with no filter, if the table changes infrequently, a materialized view or a pre-computed column would eliminate the 5M-row re-computation entirely. That is a schema decision, not a query-tuning one.

## Verdict

| Check | Result |
|---|---|
| `seq_scan` | OK — full-table aggregate, no index helps |
| `estimate_mismatch` | OK — 2.08M estimated vs. 1.67M × 3 actual |
| `disk_spill_sort` / `disk_spill_hash` | N/A |
| `nested_loop` | N/A |
| `partition_pruning` | N/A (non-partitioned) |
| `non_sargable` | N/A (no `WHERE`) |
| **`jit_decision`** | **OK — 1.7 % overhead, well below 30 %** |

The plan is optimal for this workload. JIT helps and the decision to compile is correct.

## See also

- [`sample_jit_decision_on_cheap.md`](sample_jit_decision_on_cheap.md) — the opposite case: JIT overhead dominates on a tiny query and the check emits a warning.

## Test environment

- PostgreSQL 18.6 (self-built with `--with-llvm`, LLVM 19.1.7)
- macOS 15 (Apple Silicon, arm64)
- `jit = on`, `jit_provider = llvmjit`
- `jit_above_cost = 100000`, `jit_inline_above_cost = 500000`, `jit_optimize_above_cost = 500000`
- LLM: Qwen 3.8 27B (local, via LM Studio)
 
