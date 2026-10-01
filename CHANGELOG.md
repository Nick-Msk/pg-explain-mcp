# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

(no changes yet)

## [0.5.0] — 2026-10-01

### Added

- **`PlanNode` and the `new_*` migration layer.**
  `new_parse_plan` returns a linked tree of `PlanNode` objects with
  direct `parent` / `children` references — the Python equivalent of
  `struct PlanNode` in C. The pipeline is now `new_parse_plan` →
  `new_plan_to_list` / `new_format_plan_tree` / `new_analyze_plan`;
  the legacy `parse_plan`, `filtered_parse_plan`, `format_plan_tree`,
  `analyze_plan`, `_walk_plan`, `PlanCheckBase`, and the `PlanCheck`
  protocol are gone (see *Removed* below).
- **`ParsedPlanCheckBase`.** New base class for checks that receive
  a `PlanNode` and can navigate the whole tree. Same three-phase
  shape as the legacy base: `gather_info` → `validate_rule` →
  `generate_msg`. `gather_info` gets a `PlanNode` instead of a
  dict and a `parent_type` string.

- **`CheckBase` and per-check `PARAMS`.** Every check declares
  `PARAMS = {name: type}`. Values come from `check_params` as
  `dict[str, str]` and are coerced by `CheckBase.__init__`. A
  missing parameter raises `ValueError` at construction time.
  `_REGISTRY` is now a flat `dict[str, type[CheckBase]]` — no
  per-check type table.

- **Three new MCP tools:**
  - `list_relation_info` — `pg_class` metadata (sizes, row/page
    counts, column/index counts, owner, persistence, tablespace,
    comment).
  - `list_relation_stats` — `pg_stat_user_tables` counters combined
    with `pg_class` estimates (`reltuples`, `relpages`,
    `relallvisible`).
  - `list_column_stats` — per-column planner statistics from
    `pg_stats`: `null_frac`, `avg_width`, `n_distinct`,
    `correlation`, `most_common_vals`, `most_common_freqs`,
    `histogram_bounds`.

- **`field_config` in `plan_fields`.** The `enabled` column becomes
  a three-state mode with a `CHECK (enabled in (0, 1, 999))`
  constraint:
  - `0` — hide the field (`Parallel Aware: false`, `Disabled:
    false`, `Async Capable: false`).
  - `1` — keep the field including zero values (`Heap Fetches`,
    `Rows Removed by Filter`, `Shared Read Blocks`, `Temp Read
    Blocks`, `Temp Written Blocks`).
  - `999` — unknown: keep, drop numeric zeros (the default).
  `new_plan_to_list` applies the policy and auto-derives `snake_case`
  keys for fields not present in `plan_fields`; explicit `key`
  overrides where a short name matters.

- **`pg-explain-parse` CLI.** Runs EXPLAIN itself on a SQL query
  (argument, file, or stdin), or parses a saved JSON plan via
  `--plan`. Flags: `--json`, `--no-analyze`, `--no-buffers`,
  `--all-fields`, `--marker-tabs N`.

- **`pg-explain-config` CLI.** Console script for `--init` and
  `--show`. Display modes for `plan_fields` are `[hide]` / `[keep]`
  / `[auto]`, matching the three-state semantics.

- **Node path.** Every parsed node carries a `path` field
  (`0:0/1:0/2:1`) — the root-to-node route. `format_plan_tree`
  prints the last segment only (`[4:1]`) with configurable tab
  padding; depth is already visible from indentation.

- **Per-check statistics integration.** `NonSargableCheck` loads
  indexes lazily on first `gather_info` via `get_indexes()`; the
  cache is a per-instance class-default attribute, no `__init__`
  override. Other checks will follow the same pattern as they
  migrate.

### Changed

- **`get_indexes` accepts `schema.table`.** Reuses the same
  `_relation_filter` helper as `get_relation_info`, so a qualified
  filter now matches instead of returning an empty list.

- **`RelationNotFoundError` and `StatisticsNotAvailableError`.**
  `get_relation_info` and `get_relation_stat_info` no longer return
  an empty list for a missing relation. The first signals "does not
  exist"; the second distinguishes "exists but is not a table"
  (view, matview, foreign table, sequence, index). MCP tools render
  the distinction.

- **`new_analyze_plan` enriches issues with `with_context`.** Every
  Issue from a `ParsedPlanCheckBase` check carries `depth` and
  `parent_node` — the LLM can group child issues under their root
  cause.

- **`non_sargable` message includes the wrapped function name.**
  "wraps 'email' in 'lower(...)'" instead of the generic "wraps
  'email' in a function".

- **All 10 checks migrated to `ParsedPlanCheckBase`.**
  `PartitionPruningCheck` — the last holdout — now takes
  `max_children` through `PARAMS` (SQLite-driven, coerced by
  `CheckBase.__init__`), reads `node.node_type` / `node.children`
  instead of a raw dict, and drops the `parent_type` argument from
  `gather_info`. The analyzer no longer needs the legacy
  dict-walking base class; see *Removed*.

### Fixed

- **`get_indexes` on schema-qualified names** — the previous
  `relname = 'schema.table'` comparison never matched. Now uses
  the same `_relation_filter` helper as `get_relation_info`.

- **`new_plan_to_list` auto-derives snake_case keys** for fields
  missing from `plan_fields`, so output is no longer a mix of
  `snake_case` and raw EXPLAIN names.

- **`explain_tree` and `explain_tree_text` use the new pipeline.**
  Both were still on the legacy `parse_plan` path and emitted raw
  field names.

- **`new_format_plan_tree` respects `field_config`.** Text output
  no longer leaks `Parallel Aware: false` / `Disabled: false` /
  `Async Capable: false`.

- **`pg-explain-config --show` displays `[hide]/[keep]/[auto]`**
  for `plan_fields` instead of collapsing `999` to `off`.

- **`--init` is idempotent** — drops and rebuilds `checks.db`
  instead of failing on UNIQUE violations.

### Removed

- **Legacy plan-analysis API.** With every check on
  `ParsedPlanCheckBase`, the old dict-walking infrastructure is
  removed: `parse_plan`, `filtered_parse_plan`, `format_plan_tree`,
  `analyze_plan`, `_walk_plan`, `_parse_plan_impl`,
  `_STRUCTURAL_KEYS`, `PlanCheckBase`, and the `PlanCheck` protocol.
  Consumers switch to `new_parse_plan`, `new_plan_to_list`,
  `new_format_plan_tree`, `new_analyze_plan`, and
  `ParsedPlanCheckBase`. `summarize_plan_node` stays — it is
  orthogonal to the analyze pipeline and feeds the summarizer path.

## [0.4.0] — 2026-09-27

### Added

- **Tags replace `check_class`.** The `check_classes` catalog and the
  `checks.check_class` column are removed. A new `tags` catalog and
  `checks_tags` junction table allow a check to belong to multiple
  categories. A `checks_with_tags` view provides a compact list for
  `--show` and external clients. Seed tags: `SCAN`, `SPILL`, `SORT`,
  `HASH`, `JOIN`, `INDEX`, `ESTIMATE`, `PARTITION`.

### Changed

- **All 10 checks migrated to `PlanCheckBase` ABC.** Each check is
  split into three phases: `gather_info`, `validate_rule`,
  `generate_msg`. The public `check(node)` entry point is unchanged;
  behaviour is identical for existing calls. `PlanCheckBase` uses
  `@abstractmethod` — incomplete subclasses fail at instantiation
  rather than at call time.

- **`IndexScanCheck` split into two classes.**
  `IndexRegularScanCheck` (`type = index_scan_poor_clustering`) and
  `IndexOnlyScanCheck` (`type = index_only_scan_stale_vm`) are
  separate checks with distinct node types, thresholds, and severities.
  Registered as `num = 7` and `num = 8` in `seed.sql`.

- **`EstimateMismatchCheck` skips nodes under `Limit`.** The planner's
  `plan_rows` on a node below a `Limit` is the full-scan estimate, not
  the truncated one. Comparing it against `actual_rows` produced a
  spurious 200× ratio on every `ORDER BY … LIMIT N` query. The check
  now receives `parent_type` from the walker and returns `None` when
  the parent is `Limit`.

- **`IndexOnlyScanCheck` applies `min_rows` to `actual_rows`, not to
  `heap_fetches`.** A stale visibility map on a small scan (440 rows,
  594 heap fetches) was previously below the absolute floor of 1000
  heap fetches and stayed silent. The floor now reflects the scan's
  size, not the symptom's magnitude. `min_rows` in `seed.sql` is
  lowered from 1000 to 100.

### Removed

- **`DiskSpillSortUnsizedCheck`.** PostgreSQL 14+ always reports
  `Sort Space Type` and `Sort Space Used` for external sorts. The
  defensive branch never fired on real plans.

- **`IndexScanCheck`.** Replaced by the two specialised classes above.

### Fixed

- `DiskSpillHashCheck`: message simplified after several iterations.
  The extended wording (before/after pair, standard-set rounding)
  caused the model to skip `list_parameters` and hedge with
  hypothetical values. The plain form reliably triggers the correct
  call and produces a valid recommendation.

### Documentation

- `usage_examples/` reorganised. The old
  `pg_index_scan_adapters/` directory is replaced by:
  - `pg_index_only_scan_adapters/` — fresh vs. stale visibility map.
  - `pg_index_regular_scan_adapters/` — warm vs. cold cache.
- All new examples include the LLM used during capture
  (`qwen 3.8 27b-splash` via LM Studio) in a `## test environment`
  section.

## [0.3.0] — 2026-09-23

### Added

- **SQLite-backed check configuration.**
  `CheckRegistry.load()` reads the list of enabled checks and their
  parameters from `config/checks.db`. Edits to the database take effect
  on the next `explain` call — no MCP server restart is required.
  - `config/schema.sql` declares the tables: `databases`, `checks`,
    `check_params`, `plan_fields`.
  - `config/seed.sql` populates the default registry for `postgres`.
  - `python -m pg_explain_mcp.config --init` rebuilds the database
    from schema and seed; `--show` prints the current state.
  - The database is created automatically on first `load()` if
    missing, and rebuilt automatically on schema mismatch (missing
    table or missing column).
  - Checks and plan fields are keyed by `(num, database)` and
    `(database, raw)` — the schema is multi-database ready,
    currently populated only for `postgres`.

- **`TARGET_DB_TYPE` environment variable.** Selects which target
  scope the server reads from SQLite. Default: `postgres`. All
  queries filter by this value, so a future MySQL adapter with the
  same check names will not collide with PostgreSQL's.

- **`plan_fields` — configurable plan node serialization.**
  `summarize_plan_node` now takes its raw→key mapping from the
  `plan_fields` table instead of a hard-coded `_PLAN_FIELDS`
  dictionary. Fields can be toggled or renamed from SQL without
  touching the code. The new `summarize_plan_node(node, fields)`
  signature requires the mapping as an explicit argument.

- **Runtime check management via MCP tools.**
  - `show_params(checker=None)` — lists check parameters with current
    and default values. A leading `*` marks params that differ from
    the default.
  - `set_checker_value(checker, param, value)` — changes a parameter.
    Accepts `str | int | float` for `value`; validated against the
    declared type before saving.
  - `reset_checker_value(checker, param=None)` — restores one parameter
    or all parameters of a check to their defaults.

- **`checks_applied` in the `explain` output.**
  Every report now lists the names of the checks that actually ran.
  This lets the assistant distinguish "no issue found" from "the
  relevant check is disabled in the configuration" instead of
  speculating about thresholds.

- **`check_params.default_value` column.**
  Stores the seed value for each parameter. `reset_checker_value`
  reads from it to restore a parameter.

### Changed

- **`PlanCheck` constructors take thresholds as arguments.**
  Module-level constants (`THRESHOLD_ROWS`, `MIN_FILTER_RATIO`,
  `MIN_ROWS`, `HEAP_FETCH_RATIO`, ...) have been removed. Defaults
  in `__init__` preserve v0.2.0 behaviour for callers that
  instantiate checks directly.
- **`DEFAULT_CHECKS` removed** from `analyzer.py`. The SQLite
  registry is now the single source of truth. Tests use
  `tests/conftest.ALL_CHECKS` instead.
- **`analyze_plan` requires `checks` explicitly.** The argument is no
  longer optional — there is no fallback registry.
- **Check classes carry both `name` and `type`:**
  - `name` (e.g. `"SeqScanCheck"`) matches the SQLite key and the
    Python class name.
  - `type` (e.g. `"seq_scan"`) is what appears in the JSON
    `issues[].type` field. Existing documentation and examples use
    the `snake_case` form.
- **`server.py` exposes three new tools** (`show_params`,
  `set_checker_value`, `reset_checker_value`). All three are scoped
  to `TARGET_DB_TYPE`.

### Fixed

- `python -m pg_explain_mcp.config --init` is now idempotent —
  re-running drops and rebuilds `checks.db` instead of failing on
  UNIQUE constraint violations.
- `_ensure_db` now validates column presence, not just table
  presence. A stale database created before a schema migration is
  rebuilt automatically instead of failing at query time.
- `set_checker_value` accepts `int` and `float` scalars in addition
  to strings, so an LLM passing `5000` instead of `"5000"` no longer
  trips client-side schema validation.

## [0.2.0] — 2026-09-23

### Added

- **MCP tools**
  - `list_indexes` — returns existing indexes for a table (or all
    tables), so the assistant can avoid recommending an index that
    already exists.
  - `list_parameters` — returns runtime parameters relevant to plan
    analysis: `work_mem`, `hash_mem_multiplier`, `shared_buffers`,
    `effective_cache_size`, `random_page_cost`, `seq_page_cost`,
    parallel worker limits, `jit`.

- **Fixture extension** `mcp_explain_tool`
  - A PostgreSQL extension that ships empty tables and `fill_*` /
    `clear_*` procedures for every `PlanCheck`.
  - `schema = mcp_explain_tool` declared in the control file, so
    `create extension mcp_explain_tool;` creates the schema
    automatically.
  - `fill_all(totalcount)` / `clear_all()` aggregate procedures.
  - All seven adapters covered: `index_scan`, `seq_scan`,
    `disk_spill_sort`, `disk_spill_hash`, `nested_loop`,
    `bitmap_heap_scan`, `estimate_mismatch`.
  - `data_estimate_mismatch_skewed` uses
    `pg_restore_attribute_stats()` to fabricate statistics, with
    autovacuum disabled so the fake values survive.

- **Usage examples** in `usage_examples/` — every adapter now has a
  reproducible set of `.md` files with real tool output:
  - `pg_index_scan_adapters/` — healthy vs. stale visibility map.
  - `pg_seq_scan_adapters/` — with and without an index.
  - `pg_disk_spill_sort/` — in-memory vs. external merge sort, plus a
    *precise* variant with derived `work_mem`.
  - `pg_disk_spill_hash/` — single-batch vs. multi-batch spill, plus
    a *precise* variant with derived `work_mem`.
  - `pg_nested_loop/` — 100 vs. 5000 inner iterations.
  - `pg_bitmap_heap_scan/` — narrow vs. wide range on the same table.
  - `pg_estimate_mismatch/` — norm, norm+index, skewed, and
    skewed-other-value.
  - `usage_examples/README.md` — test environment, prompting tips,
    and per-check sections.

- **Continue.dev configuration examples** in `config_example/`
  - `postgres-agent.md` — system prompt for an assistant that knows
    how to use `pg-explain` and a generic PostgreSQL MCP server,
    including per-check guidance for `seq_scan`, `disk_spill_sort`,
    and `disk_spill_hash`.
  - `mcpServers/pg-explain.yaml` — MCP server registration with
    placeholder credentials.

- **Documentation**
  - `DISCLAIMER.md` — read-only guarantees, LLM caveats, no
    liability, credentials policy.
  - `fixtures/README.md` — install, populate, VACUUM policy,
    autovacuum exceptions, static analysis.
  - `CHANGELOG.md` — this file.

### Changed

- **`SeqScanCheck`** — refined to avoid false positives:
  - requires a filter (`Rows Removed by Filter > 0`),
  - requires ≥ 90 % of the read rows to be discarded by the filter,
  - message is now neutral: it asks the caller to verify whether an
    index already exists, rather than instructing to create one.

- **`DiskSpillSortCheck`** — the message now contains a concrete,
  ready-to-use `work_mem` value (rounded up from the reported spill
  size) and explicitly states that `hash_mem_multiplier` does not
  apply to sorts. This removed a persistent LLM misinterpretation
  (see the design note in
  `usage_examples/pg_disk_spill_sort/sample_disk_spill_sort_on_spill_adv.md`).

- **`DiskSpillHashCheck`** — the message now reports:
  - batch count,
  - peak memory per batch,
  - estimated full hash size (`peak × batches`),
  - parallel worker count,
  - disk usage when available.
  It also states the correct formula
  (`work_mem × hash_mem_multiplier > estimated full size`) and
  points at `list_parameters`.

- **`NestedLoopCheck`** — rewritten:
  - reads the loop count from the **inner** child (`Plans[1]`), not
    from the Nested Loop node itself (which always reports
    `Actual Loops = 1`),
  - emitted at `INFO` level, not `WARNING`,
  - message is a future-looking note about linear growth, not a
    fix-me instruction.

- **`summarize_plan_node`** — refactored to use a `_PLAN_FIELDS`
  mapping instead of a chain of `if` statements. New fields exposed
  in `plan_nodes`:
  - `actual_loops`
  - `hash_buckets`, `hash_batches`, `peak_memory_usage_kb`
  - `sort_space_type`, `sort_space_used_kb`
  - `parallel_aware`
  - `disk_usage_kb`

- **`get_params`** in `db.py` — reads the curated set of parameters
  from `pg_settings` rather than hard-coding values.

### Fixed

- `DiskSpillSortCheck` power-of-two rounding — a 216 MB spill now
  recommends `256 MB` (previously `512 MB`).
- `summarize_plan_node` no longer shadows `Sort Space Type` behind
  the `Sort Method` key, which previously masked the `Disk` /
  `Memory` distinction.
- `fill_index_scan` — loop variable no longer shadows the
  `generate_series` alias.

### Notes

- The analyzer enforces read-only access at the transaction level
  (`SET TRANSACTION READ ONLY`).
- `mcp` dependency is pinned to `<2.0.0` for SDK compatibility.
- All fixture procedures declare
  `SET search_path = mcp_explain_tool, pg_catalog`, so they work
  regardless of the caller's `search_path`.

## [0.1.0] — 2026-09-20

### Added

- MCP server with three tools: `ping`, `list_tables`, `explain`.
- Read-only PostgreSQL connection layer (`db.py`) with
  `SET TRANSACTION READ ONLY`.
- Plan analyzer (`analyzer.py`) with a pluggable `PlanCheck` adapter
  registry.
- Checks:
  - `SeqScanCheck` — sequential scans on large tables.
  - `EstimateMismatchCheck` — planner cardinality misestimates.
  - `DiskSpillSortCheck` — sorts spilling to disk.
  - `DiskSpillHashCheck` — hash joins using multiple batches.
  - `NestedLoopCheck` — excessive Nested Loop iterations.
  - `BitmapHeapScanCheck` — large Bitmap Heap Scans.
  - `IndexScanCheck` — stale visibility map / poor heap locality.
- `summarize_plan_node` — flattens the plan tree into a compact,
  LLM-friendly list of nodes.
- `list_indexes` tool — inspects existing indexes before recommending
  new ones.
- Unit tests for all checks and the plan summarizer.
- Usage examples in `usage_examples/`, including a reproducible SQL
  scenario for `IndexScanCheck`.
- Example Continue.dev configuration in `examples/`.
- `DISCLAIMER.md` with usage warnings.

### Notes

- The analyzer enforces read-only access at the transaction level.
- `mcp` dependency is pinned to `<2.0.0` for SDK compatibility.

[0.1.0]: https://github.com/Nick-Msk/pg-explain-mcp/releases/tag/v0.1.0
[0.2.0]: https://github.com/Nick-Msk/pg-explain-mcp/compare/v0.1.0...v0.2.0
[0.3.0]: https://github.com/Nick-Msk/pg-explain-mcp/compare/v0.2.0...v0.3.0
[0.4.0]: https://github.com/Nick-Msk/pg-explain-mcp/compare/v0.3.0...v0.4.0
[0.5.0]: https://github.com/Nick-Msk/pg-explain-mcp/compare/v0.4.0...v0.5.0
[Unreleased]: https://github.com/Nick-Msk/pg-explain-mcp/compare/v0.5.0...HEAD
