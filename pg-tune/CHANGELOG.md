# Changelog

All notable changes to `pg-tune` are documented in this file.

`pg-tune` is a subproject of `pg-explain-mcp`; see the parent
[`../CHANGELOG.md`](../CHANGELOG.md) for the read-only server's
history.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`tune_settings` registry.** `tune_allows` merged into a single
  table with a `category` column: `ALLOWS` for write permissions,
  `SETTINGS` for `tune()` defaults (`default_cold_run`,
  `default_max_iters`, `default_dry_run`). Every row carries
  `value` and `default_value` as text, so `reset` has a defined
  target.

- **Unified settings API.** `list_settings()`, `get_setting()`,
  `set_setting()`, `reset_setting()` in `config.py`, plus MCP tools
  `list_allows`, `set_allow`, and `list_settings`.

- **`pg-tune-config` console script.** CLI for the SQLite registry,
  mirroring `pg-explain-config` on the read-only side. Supports
  `--init`, `--show [CAT]`, `--set CAT NAME VALUE`, `--allow`,
  `--deny`, `--reset CAT [NAME]`, `--reset-all`.

- **`tune_vec_params` registry.** Metrics for `tune_audit_vector`
  are declared in SQLite, not hardcoded. Each row carries `name`,
  `desc`, `measure`, `scope` (`root_meta` or `root_plan`), and
  `raw_key`. Fresh install ships seven metrics: `ela_time`,
  `shared_hit_blocks`, `shared_read_blocks`, `temp_read_blocks`,
  `temp_written_blocks`, `shared_i_o_read_time`,
  `temp_i_o_write_time`.

- **`pg_tune.vector.extract_vector`.** Reads a raw EXPLAIN ANALYZE
  payload and returns `{metric_name: value}`, driven entirely by
  `tune_vec_params`. Adding a metric is one `INSERT` — no code
  change. Missing metrics are omitted rather than recorded as 0.

- **MCP tools `list_backups` and `status`.** `list_backups` reads
  the local registry; `status` connects fresh to the target and
  lists user tables with row counts.

- **zstd → gzip fallback in `make_backup`.** `pg_dump --help` does
  not advertise available compression methods, and
  `pg_config --configure` describes the server install, not the
  client binary on `PATH`. `make_backup` tries `--compress=zstd:19`
  and falls back to `--compress=gzip:9` only when the binary
  explicitly reports zstd unsupported. The file extension reflects
  what was used (`.dump.zstd` or `.dump.gz`).

- **Agent prompt rules for pg-tune.** `set_allow` is marked as
  security-relevant (confirm before enabling); restores must be
  verified with `pg-tune.status()`, not `postgres-test1`.

- **`Makefile` and Docker workflow.** `make sync` (uv workspace
  sync), `make test` / `test-tune-*` for local runs,
  `make docker-test-*` for container runs against PostgreSQL 18.
  `Dockerfile.dev` and `docker-compose.yml` bring up a reproducible
  environment with `pg_dump` / `pg_restore` 18 from PGDG.

### Changed

- **`tune_allows` merged into `tune_settings`.** The old table is
  gone; every reference in code, docs, and tests now uses the
  `ALLOWS` category. `enabled = 1` became `value = '1'`.

- **`pg-tune-config --show` prints both categories.** Output starts
  with `ALLOWS:` then `SETTINGS:`, or just one when a category
  argument is given. `--show-allows` and `--show-params` were
  replaced by `--show ALLOWS` / `--show SETTINGS` and
  `--show-params` (metrics only).

- **`--show-params` includes `scope` and `raw_key`.** The full
  definition of each metric is now visible in one place.

- **Agent prompt no longer lists available tools.** The
  `## Available tools` section was removed — MCP tool discovery
  already supplies docstrings and signatures to the LLM in every
  request. The prompt carries only rules, workflow, and response
  style.

- **Restore workflow includes a disconnect step.** Rule 7 was
  tightened after observing that `universal-db-mcp`
  (postgres-test1) hangs permanently after `DROP DATABASE ...
  WITH (FORCE)`. The dry-run output now instructs disconnecting
  that server before `restore(confirm=True)`.

- **`license` metadata uses SPDX strings.** `license = { text =
  "MIT" }` became `license = "MIT"`, and the superseded
  `License :: OSI Approved :: MIT License` classifier was removed.
  Required for PEP 639 and setuptools >= 77.

- **Test sandbox moved under `pg-tune/tests/`.** SQLite and dump
  files live in `tests/test_tune.db` and `tests/test_backups/`,
  removed by fixtures at teardown, listed in `.gitignore`. The
  developer's real `config/tune.db` is never touched.

### Fixed

- **`pg-tune-backup` reports a clean `pg_version`.** Previously the
  value was read via `psql -tA`, which honored `~/.psqlrc` and
  mixed `\timing on` output into the result. Now reads
  `current_setting('server_version')` via psycopg with an explicit
  `dbname`.

- **`pg-tune-restore @latest` works without `PGDATABASE`.**
  `@latest` now falls back to the most recent backup across all
  databases when no target is set, instead of erroring out.

- **`pg-dump` failures are not retried as gzip.** `_pg_dump_once`
  converts `FileNotFoundError` and `OSError` into `BackupError`;
  the gzip fallback triggers only on an explicit "zstd not
  supported" message. Connection errors, permission errors, and
  disk-full conditions fail immediately.

- **`_pg_server_version` returns a tuple.** `(display, num)`
  replaces the bare string. `make_backup` uses the display value
  for `tune_backups.pg_version`; the numeric form is available for
  future version-based decisions.

[Unreleased]: https://github.com/Nick-Msk/pg-explain-mcp/compare/v0.5.2...HEAD

