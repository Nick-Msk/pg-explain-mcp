# mcp_explain_tool — test fixtures

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
CREATE EXTENSION mcp_explain_tool;
```

The schema `mcp_explain_tool` is created automatically (it is declared in
`mcp_explain_tool.control` via `schema = mcp_explain_tool` and
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

Every adapter has its own `fill_<adapter>(totalcount int)` procedure
that populates **two tables**:

| Table                       | Purpose                                    |
|-----------------------------|--------------------------------------------|
| `data_<adapter>_norm`       | Baseline — the check must **not** trigger  |
| `data_<adapter>_<failing>`  | Degraded — the check **must** trigger      |

The `<failing>` suffix is check-specific: `unclastered`, `large`, `spill`,
`stale`, `many`, etc.

### One adapter at a time

```sql
CALL mcp_explain_tool.fill_index_scan(5000000);
```

### All adapters at once

```sql
CALL mcp_explain_tool.fill_all(5000000);
```

### About VACUUM

`fill_*` procedures **do not** run `VACUUM` — it cannot be executed
inside a transaction or a procedure. For adapters that compare *fresh*
and *stale* visibility maps (like `index_scan`), run after filling:

```sql
VACUUM ANALYZE mcp_explain_tool.data_index_scan_norm;
```

For adapters that need a stale visibility map, `autovacuum_enabled = false`
is already set on the `_<failing>` table, so `fill_*` will leave it stale.

## Clear

```sql
CALL mcp_explain_tool.clear_all();
```

Or one adapter:

```sql
CALL mcp_explain_tool.clear_index_scan();
```

## Uninstall

```sql
DROP EXTENSION mcp_explain_tool CASCADE;
DROP SCHEMA mcp_explain_tool CASCADE;
```

Note: `DROP EXTENSION` removes all objects owned by the extension, but
the schema itself is **not** dropped automatically. To remove it as well,
run the second command.

## Adapters covered

| Adapter                    | Tables                                                  |
|----------------------------|---------------------------------------------------------|
| `index_scan`               | `data_index_scan_norm`, `data_index_scan_unclastered`   |
| `seq_scan`                 | *(planned)*                                             |
| `estimate_mismatch`        | *(planned)*                                             |
| `disk_spill_sort`          | *(planned)*                                             |
| `disk_spill_hash`          | *(planned)*                                             |
| `nested_loop`              | *(planned)*                                             |
| `bitmap_heap_scan`         | *(planned)*                                             |

## Adding a new adapter

1. Add two `CREATE TABLE` blocks — `data_<adapter>_norm` and
   `data_<adapter>_<failing>` — to `mcp_explain_tool--0.1.0.sql`.
2. Add a `fill_<adapter>(totalcount int)` procedure that populates both.
3. Add a `clear_<adapter>()` procedure that truncates both.
4. Register both in `fill_all` / `clear_all`.
5. Reinstall:
   ```bash
   make install
   ```
6. In `psql`:
   ```sql
   DROP EXTENSION mcp_explain_tool CASCADE;
   CREATE EXTENSION mcp_explain_tool;
   ```
7. Add a usage example under `../usage_examples/pg_<adapter>/`.

## Layout

```
fixtures/
├── mcp_explain_tool.control
├── mcp_explain_tool--0.1.0.sql
├── Makefile
└── README.md
```

## Static analysis

`mcp_explain_tool` procedures can be checked with [`plpgsql_check`](https://github.com/okbob/plpgsql_check):

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

