# test queries

A collection of SQL files used to test the parser, the CLI, and the
check registry against realistic plans. Each `.sql` file has a
matching `.parsed` file — the indented tree produced by
`pg-explain-parse`, committed for reference and for diffing when the
output format changes.

## naming

```
q_<topic>_<number>.sql      SQL query
q_<topic>_<number>.parsed   rendered tree (generated, committed)
```

`<topic>` matches the check name where possible (`seq_scan`,
`index_scan`, `nested_loop`, `estimate_mismatch`, ...). This makes it
easy to find a sample for a given check:

```bash
ls q_estimate_mismatch_*.sql
```

Multiple queries on the same topic get sequential numbers.

## regeneration

The `.parsed` file is regenerated with the CLI. Any of these work:

```bash
# from stdin
cat q_index_scan_1.sql | pg-explain-parse > q_index_scan_1.parsed

# from a file argument — auto-detected by file existence
pg-explain-parse q_index_scan_1.sql > q_index_scan_1.parsed

# same, with a wider marker column
pg-explain-parse --marker-tabs 8 q_index_scan_1.sql > q_index_scan_1.parsed

# JSON instead of the tree (rarely useful, but available)
pg-explain-parse --json q_index_scan_1.sql > q_index_scan_1.json
```

Connection parameters come from environment variables, same as the
MCP server:

```bash
export PG_USER=skelet PG_DATABASE=test1 PG_PASSWORD=123
```

## usage

Two reasons to keep these files around:

1. **Manual checks.** After changing `format_plan_tree` or
   `parse_plan`, re-run one query and diff the `.parsed` file:

   ```bash
   pg-explain-parse q_index_scan_1.sql | diff - q_index_scan_1.parsed
   ```

   A clean diff means the format is stable. Unexpected changes are
   visible immediately.

2. **Documentation.** The `.parsed` files show what the tool produces
   on real plans — useful when explaining the project to someone who
   does not have a PostgreSQL instance handy.

## adding a new query

1. Write the SQL in `q_<topic>_<number>.sql`.
2. Generate the tree:
   ```bash
   pg-explain-parse q_<topic>_<number>.sql > q_<topic>_<number>.parsed
   ```
3. Commit both files.

Prefer queries that produce interesting plans — multiple levels,
varied node types, parallel sections, `Append` with several
children. A trivial `select 1` is not worth a file.

## current files

| file | what it exercises |
|---|---|
| `q_nested_loop_1.sql` | `Nested Loop` + `Aggregate` + `Sort` + `Limit` — 5 levels |

