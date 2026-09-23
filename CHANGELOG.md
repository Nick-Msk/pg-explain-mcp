# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/Nick-Msk/pg-explain-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Nick-Msk/pg-explain-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Nick-Msk/pg-explain-mcp/releases/tag/v0.1.0

