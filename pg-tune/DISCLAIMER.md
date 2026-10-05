# Disclaimer

`pg-tune` is a **write-capable** companion to `pg-explain-mcp`. It
runs on the same codebase and shares the analyzer, but unlike
`pg-explain-mcp` it can issue DDL, DML, and configuration changes
against the target database.

**Read this before running anything.**

## Intended use

`pg-tune` is strictly a **development and test** tool. It is designed
for:

- Local databases where mistakes are cheap to fix.
- Staging environments where a restore point exists.
- Throwaway instances created for the specific purpose of tuning.

It is **not** designed for, and must never be pointed at, a
production database.

## Write operations

`pg-tune` can, depending on configuration:

- take a `pg_dump` snapshot (read-only, safe),
- restore a database from a snapshot (**destructive**: drops and
  recreates the target database),
- apply session-level GUCs inside a rolled-back transaction,
- run `ANALYZE` on tables,
- create indexes,
- drop objects it created earlier.

Every write action is opt-in through the `tune_allows` table. On a
fresh install, only `backup` is enabled. Everything else requires an
explicit `pg-tune-config --allow <action>`.

## No warranty

Recommendations produced by `pg-tune` — or by an LLM using it — are
suggestions, not guarantees. The tuning loop picks a fix, applies it,
and re-measures. That is an empirical process, not a proof. A fix
that improves one query may hurt another. Always validate against
your own workload.

## Backups are your responsibility

`pg-tune` can create a backup before it writes, and the recommended
workflow does exactly that. But:

- The backup is local. It is not replicated, not verified, not
  off-site.
- If you delete the backup directory, you lose the restore point.
- `restore` itself is destructive: it drops the target database
  before restoring. If the restore fails midway, the database is
  left in an unknown state.

Before running `pg-tune` against any database you care about, either
run `backup` yourself, or take a snapshot through your normal
infrastructure.

## LLM involvement

Like `pg-explain-mcp`, `pg-tune` is designed to be driven by an
LLM assistant. The assistant can misread a plan, misapply a
recommendation, or generate a wrong DDL statement. The
`confirm=True` gate on destructive operations exists to keep a human
in the loop — do not automate it away.

## Read-only sibling

If you only need diagnosis, use `pg-explain-mcp`. It is read-only
by construction: `SET TRANSACTION READ ONLY` on every connection,
no DDL, no DML. It can be pointed at production replicas safely.
`pg-tune` cannot.

## Allow-list

Every write action is gated by `tune_allows`. Flipping a switch
from `0` to `1` grants pg-tune — and the LLM driving it — the
ability to perform that action. Enabling `restore` means the next
`restore(confirm=True)` will drop and recreate the target
database. Treat the allow-list as a security boundary, not a
convenience toggle.

## License

See [LICENSE](LICENSE). Provided "as is", without warranty of any
kind, express or implied.

