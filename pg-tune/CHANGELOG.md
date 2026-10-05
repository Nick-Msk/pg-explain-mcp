# Changelog

All notable changes to `pg-tune` are documented in this file.

`pg-tune` is a subproject of `pg-explain-mcp`; see the parent
[`../CHANGELOG.md`](../CHANGELOG.md) for the read-only server's
history.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Allow-list management API.** `tune_allows` now carries
  `default_enabled` alongside `enabled`, so `reset` has a defined
  target. Three new functions in `config.py` — `list_allows()`,
  `set_allow(action, enabled)`, `reset_allow(action="")` — plus
  two MCP tools (`list_allows`, `set_allow`) and a new
  `pg-tune-config` CLI with `--init`, `--show`, `--allow`,
  `--deny`, and `--reset`. Tests cover the CRUD API and the CLI.

- **`pg-tune-config` console script.** Entry point for the SQLite
  registry, mirroring `pg-explain-config` on the read-only side.

- **Agent prompt rules 8 and 9.** `set_allow` is marked as
  security-relevant (confirm before enabling); restores must be
  verified with `pg-tune.status()`, not `postgres-test1`.

### Changed

- **Agent prompt no longer lists available tools.** The
  `## Available tools` section was removed — MCP tool discovery
  already supplies docstrings and signatures to the LLM in every
  request. The prompt now carries only rules, workflow, and
  response style.

- **`pg-tune.md` workflow for restore includes disconnect step.**
  Rule 7 was tightened after observing that `universal-db-mcp`
  (postgres-test1) hangs permanently after `DROP DATABASE ...
  WITH (FORCE)`. The dry-run output now instructs disconnecting
  that server before `restore(confirm=True)`.

### Fixed

- **`pg-tune-backup` reports a clean `pg_version`.** Previously
  the value was read via `psql -tA`, which honored `~/.psqlrc`
  and mixed `\timing on` output into the result. Now reads
  `SHOW server_version` via psycopg with an explicit `dbname`.

- **`pg-tune-restore @latest` works without `PGDATABASE`.**
  `@latest` now falls back to the most recent backup across all
  databases when no target is set, instead of erroring out.

[Unreleased]: https://github.com/Nick-Msk/pg-explain-mcp/compare/v0.5.2...HEA
D
