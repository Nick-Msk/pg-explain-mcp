# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-09-20

### Added

- MCP server with three tools: `ping`, `list_tables`, `explain`.
- Read-only PostgreSQL connection layer (`db.py`) with `SET TRANSACTION READ ONLY`.
- Plan analyzer (`analyzer.py`) with a pluggable `PlanCheck` adapter registry.
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

[Unreleased]: https://github.com/Nick-Msk/pg-explain-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Nick-Msk/pg-explain-mcp/releases/tag/v0.1.0

