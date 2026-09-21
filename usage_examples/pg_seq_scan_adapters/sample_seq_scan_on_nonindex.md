# sample: range query without an index

> scenario 2 of 2 for `SeqScanCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_seq_scan(1000000);
```

## context

`data_seq_scan_nonindex` contains 1M rows and has **no index** on `val`.
the same narrow range query as in the previous example now forces a
seq scan: postgres must read all 1M rows to find the ~100 that match.

## input query

```sql
select val
from mcp_explain_tool.data_seq_scan_nonindex
where val between 100 and 200;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain

```json
{
  "execution_time_ms": 166.364,
  "planning_time_ms": 1.055,
  "total_time_ms": 167.419,
  "issues": [
    {
      "severity": "warning",
      "type": "seq_scan",
      "message": "Sequential scan on 'data_seq_scan_nonindex' read 333333.0 rows (33.0 returned, 333300 filtered out). Consider adding an index on the filter column.",
      "node": "Seq Scan"
    }
  ],
  "issue_count": 1,
  "summary": "Found 1 issues (1 critical). Execution time: 166.36 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Gather",
      "actual_rows": 99.0,
      "plan_rows": 67,
      "shared_read_blocks": 29202
    },
    {
      "depth": 1,
      "node_type": "Seq Scan",
      "relation": "data_seq_scan_nonindex",
      "actual_rows": 33.0,
      "plan_rows": 28,
      "rows_removed_by_filter": 333300,
      "shared_read_blocks": 29202
    }
  ]
}
```

## analysis

| metric            | value     |
|-------------------|-----------|
| execution time    | 166.36 ms |
| rows returned     | 99        |
| rows read         | 333,333   |
| rows filtered out | 333,300   |
| shared read blocks| 29,202    |
| issues            | 1         |

the plan uses a **parallel seq scan** on `data_seq_scan_nonindex`. the
numbers in `plan_nodes` are **per-worker** values: `actual_rows: 33`
and `rows_removed_by_filter: 333300` — both multiplied by 3 workers give
the real totals (~99 returned, ~999,900 discarded).

`SeqScanCheck` sums `actual_rows + rows_removed_by_filter` (333,333) and
compares against its `threshold_rows = 1000`. the total is far above
the threshold, so the check fires with type `seq_scan`.

### recommendation

```sql
create index idx_data_seq_scan_nonindex_val
    on mcp_explain_tool.data_seq_scan_nonindex (val);
```

after adding the index, the planner switches to an index-only scan
(see `sample_seq_scan_on_norm.md`) and the warning disappears.

## what this proves

`SeqScanCheck` uses `actual_rows + rows_removed_by_filter`, so it catches
seq scans that read millions of rows even when the query returns only a
few. a check based on `actual_rows` alone would miss this case entirely —
33 rows would be far below any reasonable threshold.

