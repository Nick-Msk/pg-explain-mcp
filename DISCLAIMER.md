# Disclaimer

## Summary

`pg-explain-mcp` is a **diagnostic and educational tool**. It is provided
"as is", without warranty of any kind. The recommendations produced by
the analyzer or by an LLM assistant on top of it are **suggestions, not
guarantees**.

**Always validate any recommendation against your own database before
applying it to a production system.**

## 1. Read-Only by Design

The MCP server enforces `SET TRANSACTION READ ONLY` on every connection.
It cannot modify your data. However:

- The server executes whatever `SELECT` / `WITH` query you send it,
  including queries that may be **slow** or **resource-intensive** on
  large tables.
- Running `EXPLAIN (ANALYZE, ...)` **actually executes** the query. On a
  production database this can cause load, locks, or I/O spikes.
- Do not run `explain` against production databases during peak hours.
  Use a replica or a staging environment whenever possible.

## 2. Recommendations Are Heuristic

The checks in `analyzer.py` are based on general PostgreSQL best practices:

- `Seq Scan` on a large table is *often* — but not always — a problem.
- `Heap Fetches` in an Index Only Scan *usually* mean a stale visibility
  map, but can also indicate other issues.
- `Estimate Mismatch` thresholds are tuned for typical workloads and may
  not fit yours.

Real performance depends on your schema, data distribution, hardware,
concurrency, and PostgreSQL version. **No automated tool can replace a
human DBA.**

## 3. LLM Output May Be Wrong

The assistant built on top of `pg-explain-mcp` (for example, Continue.dev
with a local or remote LLM) may:

- Misinterpret the plan.
- Recommend an index that already exists.
- Suggest a fix that does not apply to your workload.
- Hallucinate fields or statistics not present in the actual plan.

Always cross-check the assistant's answer against the raw JSON returned
by the MCP tool, and against the output of `EXPLAIN (ANALYZE, BUFFERS)`
run directly in `psql`.

Error messages shown to the LLM must be descriptive. Do not embed shell commands in exceptions raised by MCP tools — the assistant will try to execute them. Put usage instructions in the README instead.

## 3a. Write Access to the Config Database

Three MCP tools (`set_checker_value`, `reset_checker_value`, and
`show_params`) read and write the SQLite config database at
`config/checks.db`. This is the tool's **own** configuration store,
not user data. It is separate from the read-only contract with
PostgreSQL.

The assistant is instructed to ask before calling
`set_checker_value` — but the tool itself does not enforce this.
If you deploy `pg-explain-mcp` in a shared environment, restrict
filesystem permissions on `config/checks.db` accordingly.

## 4. No Liability

The authors and contributors of `pg-explain-mcp` accept **no
responsibility or liability** for:

- Data loss, corruption, or unavailability.
- Performance regressions or outages.
- Incorrect diagnostic conclusions or recommendations.
- Any direct, indirect, incidental, or consequential damages arising
  from the use of this software.

Use of this software is entirely at your own risk. See [`LICENSE`](LICENSE)
for the full legal text.

## 5. Credentials and Secrets

Never commit real database credentials to a public repository. Use
environment variables, a secrets manager, or per-user configuration.
The example configs in [`examples/`](examples/) use placeholders only.

## 6. Scope

This tool is intended for:

- Development and staging environments.
- Read-only replicas.
- Educational exploration.

It is **not** intended as a production monitoring or auto-tuning system.

