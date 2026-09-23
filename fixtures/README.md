# mcp_explain_tool — test fixtures

> **Versioning note.** `mcp_explain_tool` is versioned independently
> from the `pg-explain-mcp` Python package. Its version tracks the
> fixture API (table names, procedure signatures), not the analyzer
> release. The current extension version is `0.1.0`.

A PostgreSQL extension that creates **empty tables** and **`fill_*` /
`clear_*` procedures** for every `PlanCheck` in
[`pg-explain-mcp`](../).

The extension itself is tiny — it contains no data. Data is loaded on
demand, so you decide how many rows to generate and which adapters to
exercise.

## Install

Requires PostgreSQL 14+ and `pg_config` in `PATH`.

```bash
cd fixtures
make install
```

If `pg_config` is not in `PATH` (e.g. on macOS with Homebrew):

```bash
export PATH="$(brew --prefix postgresql@18)/bin:$PATH"
make install
```

This copies two files into your PostgreSQL extension directory:

- `mcp_explain_tool.control`
- `mcp_explain_tool--0.1.0.sql`

## Create in a database

```sql
create extension mcp_explain_tool;
```

The schema `mcp_explain_tool` is created automatically (it is declared
in `mcp_explain_tool.control` via `schema = mcp_explain_tool` and
`relocatable = false`).

The `mcp_` prefix is used instead of `pg_` because PostgreSQL reserves
`pg_` for system schemas.

Verify:

```sql
\dn mcp_explain_tool
\dt mcp_explain_tool.*
\df mcp_explain_tool.*
```

## Populate

Every adapter has its own `fill_<adapter>(totalcount int)` procedure.
Most adapters use a **pair** of tables:

| table                       | purpose                                      |
|-----------------------------|----------------------------------------------|
| `data_<adapter>_norm`       | baseline — the check must **not** trigger    |
| `data_<adapter>_<variant>`  | degraded — the check **must** trigger        |

The `<variant>` suffix is adapter-specific:

| adapter            | variant       | meaning                              |
|--------------------|---------------|--------------------------------------|
| `index_scan`       | `unclastered` | stale visibility map                 |
| `seq_scan`         | `nonindex`    | no index on the filter column        |
| `disk_spill_sort`  | `spill`       | sort payload exceeds `work_mem`      |
| `disk_spill_hash`  | `spill`       | hash table exceeds `work_mem`        |
| `estimate_mismatch`| `skewed`      | fake statistics via `pg_restore_*`   |

Some adapters differ from the pair pattern:

- **`nested_loop`** — three tables: `_norm`, `_many`, `_inner`. The
  inner table is the indexed lookup target shared by both queries.
- **`bitmap_heap_scan`** — a **single** table. The check fires based on
  the selectivity of the query, not the state of the table.

### One adapter at a time

```sql
call mcp_explain_tool.fill_index_scan(5000000);
```

### All adapters at once

```sql
call mcp_explain_tool.fill_all(5000000);
```

### About VACUUM

`fill_*` procedures **do not** run `VACUUM` — it cannot be executed
inside a transaction or a procedure. After filling, run `VACUUM
ANALYZE` from `psql` for tables that need a fresh visibility map:

```sql
vacuum analyze mcp_explain_tool.data_index_scan_norm;
```

For adapters that rely on a **stale** visibility map or **fake
statistics**, autovacuum is disabled at the table level so the
`fill_*` procedures can leave the table in the desired state:

| table                              | why autovacuum is off                          |
|------------------------------------|------------------------------------------------|
| `data_index_scan_unclastered`      | a fresh visibility map would hide the problem  |
| `data_estimate_mismatch_skewed`    | autovacuum's `ANALYZE` would overwrite fake stats |

## Clear

```sql
call mcp_explain_tool.clear_all();
```

Or one adapter:

```sql
call mcp_explain_tool.clear_index_scan();
```

## Uninstall

```sql
drop extension mcp_explain_tool cascade;
drop schema mcp_explain_tool cascade;
```

`drop extension` removes all objects owned by the extension, but the
schema itself is **not** dropped automatically. To remove it as well,
run the second command.

## Adapters covered

| adapter              | tables                                                                    | status |
|----------------------|---------------------------------------------------------------------------|--------|
| `index_scan`         | `data_index_scan_norm`, `data_index_scan_unclastered`                     | done   |
| `seq_scan`           | `data_seq_scan_norm`, `data_seq_scan_nonindex`                            | done   |
| `disk_spill_sort`    | `data_disk_spill_sort_norm`, `data_disk_spill_sort_spill`                 | done   |
| `disk_spill_hash`    | `data_disk_spill_hash_norm`, `data_disk_spill_hash_spill`                 | done   |
| `nested_loop`        | `data_nested_loop_norm`, `data_nested_loop_many`, `data_nested_loop_inner`| done   |
| `bitmap_heap_scan`   | `data_bitmap_heap_scan`                                                   | done   |
| `estimate_mismatch`  | `data_estimate_mismatch_norm`, `data_estimate_mismatch_skewed`            | done   |

## Adding a new adapter

1. Add the tables (`data_<adapter>_*`) to
   `mcp_explain_tool--0.1.0.sql`. Follow the pair convention
   (`_norm` + `_<variant>`) unless the check is query-driven, in which
   case a single table is fine.
2. Add a `fill_<adapter>(totalcount int)` procedure that populates all
   tables for the adapter, then runs `analyze` on them.
3. Add a `clear_<adapter>()` procedure that truncates them with
   `restart identity`.
4. Register both in `fill_all` / `clear_all`.
5. Reinstall:
   ```bash
   make install
   ```
6. Recreate in `psql`:
   ```sql
   drop extension mcp_explain_tool cascade;
   create extension mcp_explain_tool;
   ```
7. Add a usage example under `../usage_examples/pg_<adapter>/`.
8. Update the **Adapters covered** table above.

## Layout

```
fixtures/
├── mcp_explain_tool.control
├── mcp_explain_tool--0.1.0.sql
├── Makefile
└── README.md
```

## Static analysis

`mcp_explain_tool` procedures can be checked with
[`plpgsql_check`](https://github.com/okbob/plpgsql_check):

```sql
create extension if not exists plpgsql_check;

select p.proname,
       plpgsql_check_function(
           p.oid::regprocedure,
           performance_warnings := true
       ) as issues
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'mcp_explain_tool'
  and p.prokind = 'p'
order by p.proname;
```

Expected: no rows (clean output).

## See also

- [`../README.md`](../README.md) — main project README
- [`../usage_examples/`](../usage_examples/) — real-world runs with
  `pg-explain-mcp`

