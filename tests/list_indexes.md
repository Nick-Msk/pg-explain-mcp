# Sample: Using `list_indexes` Before Recommending a New Index

## Context

A common pitfall when analyzing query plans: the LLM sees a Sequential
Scan and immediately recommends `CREATE INDEX`. But what if a suitable
index already exists and the planner simply chose not to use it?

The `list_indexes` tool solves this — it lets the assistant check the
existing indexes for a table before making a recommendation.

## Prompt to the Assistant

> List all indexes on the `big_unclastered` table.

## Raw Output from pg-explain

```
public.big_unclastered → idx_big_unclastered_val [INDEX] (val)
public.big_unclastered → big_unclastered_pkey [PRIMARY KEY] (id)
```

## Why This Matters

Without `list_indexes`, the assistant might suggest:

```sql
CREATE INDEX idx_big_unclastered_val ON big_unclastered (val);
```

…which would fail — the index already exists.

With `list_indexes`, the assistant can instead ask:

> The index `idx_big_unclastered_val` already exists on `val`.
> The planner chose a Sequential Scan anyway — likely because the filter
> `LIKE '0%'` matches too many rows for an index scan. Consider
> `text_pattern_ops` or a partial index if the pattern is fixed.

This turns a generic recommendation into a **context-aware diagnosis**.

