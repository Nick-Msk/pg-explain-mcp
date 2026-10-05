# pg-tune tests

Unit tests run offline, in a sandbox under `tests/`. Integration
tests need a live PostgreSQL and are opt-in.

## Running

```bash
cd pg-tune

# Offline suite — fast, no PostgreSQL, no network
pytest -v

# Include integration tests (requires live PG)
PG_TEST_INTEGRATION=1 pytest -v

# Only integration
PG_TEST_INTEGRATION=1 pytest tests/test_integration_backup_restore.py -v
```

The default `pytest` run **skips** every test marked `integration`.
You will see them reported as `skipped` in the summary, which is
expected.

## What is covered offline

| File                    | What it tests                               |
|-------------------------|---------------------------------------------|
| `test_config_allows.py` | `list_allows` / `set_allow` / `reset_allow` |
| `test_cli_config.py`    | `pg-tune-config` argument parsing + output  |
| `test_backup.py`        | `make_backup` with mocked `subprocess.run`  |
| `test_vector.py`        | `extract_vector` — data-driven metric pull  |

Offline tests never touch a real database. `subprocess.run` is
replaced with a stub that writes a small file to a sandbox
directory, so `pg_dump` is never invoked. Nothing leaks into
`config/tune.db` or `config/backups/` — the fixtures redirect both
paths to `tests/test_tune.db` and `tests/test_backups/`.

`test_vector.py` is a pure-function test — it feeds synthetic plan
dicts to `extract_vector` and checks the returned metric map. It
also asserts that the seed rows in `tune_vec_params` are
well-formed (`scope` in the allowed set, `raw_key` non-empty), so
a typo in the seed is caught before it silently drops a metric out
of every future vector.

## What integration tests cover

`test_integration_backup_restore.py` creates a throwaway database
named `pg_tune_bkp_test_<random>`, runs a full cycle against it,
and drops it at teardown:

1. **Seed** — three users, four orders, no junk table.
2. **Backup** — `pg_dump -Fc --compress=zstd:19` writes to the
   sandbox backup dir.
3. **Mutate** — drops `orders`, truncates `users`, creates a
   `junk` table.
4. **Restore** — `pg_restore` from the backup.
5. **Verify** — `users = 3`, `orders = 4`, `junk` absent.

Plus four gate tests: restore must refuse without
`PG_TUNE_ALLOW_WRITES`, without `tune_allows['restore']`, when the
dump file is missing, and when the file's sha256 does not match.

## Requirements for integration tests

- A running PostgreSQL instance reachable via the standard `PG*`
  environment variables (`PGHOST`, `PGPORT`, `PGUSER`,
  `PGPASSWORD`, `PGDATABASE`).
- The connection user must be able to `CREATE DATABASE` and
  `DROP DATABASE`. The tests create and drop their own throwaway
  databases; nothing else on the server is touched.
- `pg_dump` and `pg_restore` on `PATH`, matching the server
  version closely enough for the custom format to round-trip.
  PostgreSQL 16 or newer, for `--compress=zstd:19`.

If any of these are missing, the integration tests will fail with
a clear error — not skip. The `PG_TEST_INTEGRATION` gate is the
only skip condition; everything past it is a real failure if the
environment is not ready.

## Sandbox files

The tests write to `tests/` next to themselves:

- `tests/test_tune.db` — SQLite config for the test run
- `tests/test_backups/` — fake and real `.dump.zstd` files

Both are removed by the fixtures at teardown and are listed in the
repository's `.gitignore`. If a run crashes hard (segfault, kill),
they may be left behind — safe to `rm -rf`:

```bash
rm -rf tests/test_tune.db* tests/test_backups/
```

The developer's real `config/tune.db` and `config/backups/` are
**never** touched by the test suite.

## Markers

Only one custom marker, registered in `pyproject.toml`:

- `integration` — the test needs a live PostgreSQL and is skipped
  unless `PG_TEST_INTEGRATION=1` is set.

Run `pytest --markers` to list all registered markers.

