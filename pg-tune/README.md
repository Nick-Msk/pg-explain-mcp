# pg-tune

A **write-capable** tuning companion to
[`pg-explain-mcp`](../). It reuses the same analyzer and check
catalogue, but unlike the read-only server it can apply
recommendations — session GUCs, `ANALYZE`, indexes — and re-measure
the result.

`pg-tune` lives in the same repository as `pg-explain-mcp` but is a
separate MCP server (`pg-tune-mcp`) with its own SQLite config
(`config/tune.db`) and its own safety model. The split exists because
the two servers have different access requirements: one is
read-only by design, the other must write.

> ⚠️ **Dev and test only.** See [DISCLAIMER.md](DISCLAIMER.md).
> Never point `pg-tune-mcp` at a production database.

## Status

Early skeleton. `v0.1.0-alpha` exposes only two tools — `backup` and
`restore`. The tuning loop itself lands in a later release and is
described below so the shape is on record.

## What it does

Given a SQL query, `pg-tune` runs the full diagnosis → fix → verify
cycle:

1. **Warm-up.** Run the query 2–3 times before measuring. The first
   run pays for cold cache, cold planner, cold JIT context; the
   third run is representative. The warm-up is done via
   `EXPLAIN ANALYZE` (the query is actually executed).
2. **Diagnose.** Call `pg-explain-mcp:explain` on the same query.
   Get `issues[]` — the list of detected problems, each with `type`,
   `message`, and the node that produced it.
3. **No issues → report.** If `issues` is empty, the query is
   healthy. The report still contains a **statistics vector**
   (`elapsed_ms`, disk read bytes, temp written bytes, …) recorded
   for later comparison.
4. **Issues → apply fixes.** For each issue, look up a fix in
   `tune_allows`. If the corresponding action is enabled, apply it
   and re-run `explain`. Which fixes to apply and in what order is
   the user's call — `pg-tune` suggests an ordering by risk (session
   GUC → `ANALYZE` → index) but does not enforce it.
5. **Verify.** If at least one issue disappeared between the
   pre-fix and post-fix runs, keep the change and continue. If none
   disappeared, roll back and try the next fix (or stop).
6. **Report.** Every iteration is logged in `tune_audit` with the
   statistics vector before and after, the issues before and after,
   and whether the fix was kept or rolled back. The final report
   lists: what was tried, what stuck, what remained, and — if the
   session ends with unresolved issues — the path to the backup
   taken at step 0.

The statistics vector is the same shape for baseline and every
iteration. It is produced by `pg-explain-mcp` alongside the issues;
`pg-tune` stores it and diffs it.

## Tuning loop — an example


```
$ pg-tune tune "SELECT sum(val) FROM t WHERE ts > now() - interval '7 days'"

Warm-up: 3 runs (elapsed 245 / 121 / 118 ms)

Baseline:
  issues: [seq_scan on t, estimate_mismatch on t]
  vector: {ela_time: 118.4, disk_read_bytes: 12_345_678, ...}

Iteration 1:
  fix:    ANALYZE t
  result: estimate_mismatch gone, seq_scan still present
  vector: {ela_time: 96.2, disk_read_bytes: 12_345_678, ...}
  kept

Iteration 2:
  fix:    CREATE INDEX CONCURRENTLY idx_t_ts ON t (ts)
  result: seq_scan gone
  vector: {ela_time: 4.1, disk_read_bytes: 21_233, ...}
  kept

Iteration 3:
  no applicable fix for remaining issues
  stop

Report:
  applied: 2 (analyze, create_index_concurrent)
  rolled_back: 0
  remaining issues: none
  managed objects: idx_t_ts (created by pg-tune)
  backup: backups/testdb_20261003T120000Z_a1b2c3d4.dump.zst
```

## Configuration

`pg-tune` reads a small SQLite database at `config/tune.db`,
independent of `checks.db` used by `pg-explain-mcp`.

### `tune_allows`

Which write actions are permitted. Defaults on a fresh install:

| Action                    | Default | What it does                            |
|---------------------------|---------|-----------------------------------------|
| `backup`                  | on      | `pg_dump \| zstd` snapshot (read-only)  |
| `restore`                 | off     | Drop and restore from a backup          |
| `set_session_guc`         | off     | `SET LOCAL` inside a rolled-back txn    |
| `analyze`                 | off     | `ANALYZE` on a table                    |
| `create_index`            | off     | `CREATE INDEX` (blocking)               |
| `create_index_concurrent` | off     | `CREATE INDEX CONCURRENTLY`             |
| `drop_own_objects`        | off     | `DROP` objects pg-tune created earlier  |

Each row carries `enabled` and `default_enabled`. The second one
is what `reset` returns to — it never changes after `--init`, so
"reset to factory" always has a defined target.

**Via CLI:**

```bash
pg-tune-config --init                   # create tune.db from schema + seed
pg-tune-config --show                   # print the allow-list
pg-tune-config --allow restore          # enable one action
pg-tune-config --deny  restore          # disable one action
pg-tune-config --reset restore          # reset one action to default
pg-tune-config --reset                  # reset everything to defaults

**Via MCP (from an agent chat):**

list_allows()                            # show all actions and states
set_allow("restore", True)               # enable
set_allow("restore", False)              # disable
Turning an action on is security-relevant — it grants the LLM
the ability to modify the database. Confirm with the user before
calling set_allow(..., True). Disabling is always safe.

### Statistics vector

Metrics recorded alongside every run are declared in
`tune_vec_params`. A fresh install ships with `ela_time` (elapsed
time, ms). More land as they are wired up (`disk_read_bytes`,
`disk_write_bytes`, `temp_read_bytes`, `temp_write_bytes`,
`plan_cost`, `plan_rows`, …). Adding a metric is a row insert — no
schema change.

Values for a given audit row live in `tune_audit_vector`, one row
per `(audit_id, phase, vec_name)`. `phase` is `'B'` (before) or
`'A'` (after).

## Safety model

Three layers, in order of precedence:

1. **Environment flag.** `pg-tune-mcp` refuses to start unless
   `PG_TUNE_ALLOW_WRITES=yes` is set. Without it, the process exits.
2. **Host blacklist.** The startup check refuses to run against
   hosts matching any pattern in `tune_blacklist` (`prod`,
   `*.prod.internal`, …). Edit the table directly.
3. **`tune_allows`.** Even with both of the above in place, no
   action runs unless its row in `tune_allows` has `enabled = 1`.

Destructive tools (`restore`, and later `DROP` of managed objects)
require an explicit `confirm=True` argument on the MCP call. That
flag exists so that an LLM cannot trigger them by accident.

## Managed objects

Anything `pg-tune` creates — indexes, views, temporary tables — is
recorded in `tune_objects` with the DDL that produced it. `DROP`
is allowed **only** for objects with `status = 'active'` in that
table. Objects created outside `pg-tune` are never dropped by it,
even if they look unused.

`pg-tune` will not touch the schema of a database it does not
recognise as its own.

## Non-goals

- **Not for production.** Ever. See DISCLAIMER.
- **Not an autopilot.** It applies fixes one at a time and reports;
  a human decides whether to keep them.
- **Not a substitute for `pg-explain-mcp`.** If you only need
  diagnosis, use the read-only server.
- **No PITR, no WAL archival, no `pg_basebackup`.** Backups are
  local `pg_dump` files. Disaster recovery is out of scope.

## Installation

Same repository as `pg-explain-mcp`:

```bash
pip install -e .
```

Additional console scripts after install:

- `pg-tune-mcp` — the write-capable tuning server.
- `pg-tune-config` — the tuning registry CLI.

## Tests

```bash
cd pg-tune
pytest -v                                  # offline suite, ~1s
PG_TEST_INTEGRATION=1 pytest -v            # + live-PostgreSQL tests

## Roadmap

- **Apply fixes.** The allow-list already gates `set_session_guc`,
  `analyze`, `create_index`, and `drop_own_objects`, but only
  `backup` and `restore` are wired to tools. The tuning loop
  (diagnose → fix → re-measure) lands next.
- **Audit for `tune_allows` changes.** Every flip of an enable
  switch should land in `tune_audit`, so a rogue `set_allow`
  in the middle of the night is visible afterwards.
- **`tune_blacklist` enforcement.** The safety model mentions a
  host blacklist; the table exists in schema but startup does
  not yet refuse to run against matching hosts.

## License

MIT. See [LICENSE](../LICENSE).

