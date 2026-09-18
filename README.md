# pg-explain-mcp

An MCP (Model Context Protocol) server for analyzing PostgreSQL query
execution plans. Built as a bridge between LLM-based coding assistants
(Continue.dev, Claude Desktop, Cursor) and a PostgreSQL database.

## What it does

The server exposes three MCP tools:

- **`ping`** — health check.
- **`list_tables`** — returns all user tables with their columns.
- **`explain`** — runs `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on a
  SELECT query and returns a structured report highlighting:
  - Sequential scans on large tables (likely missing indexes)
  - Large mismatches between planner estimates and actual rows
  - Disk spills in sorts and hash joins (work_mem issues)
  - Excessive Nested Loops

## Architecture

LLM (Continue.dev) ──MCP──> pg-explain-mcp ──psycopg3──> PostgreSQL
│
├── db.py (connection, EXPLAIN)
├── analyzer.py (plan analysis)
└── server.py (MCP tools)


All database access is **read-only** — the connection runs inside
`SET TRANSACTION READ ONLY`.

## Installation

```bash
git clone <repo-url>
cd pg-explain-mcp
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .

Example of usage:
Use the pg-explain tool to analyze: SELECT * FROM onek1 WHERE hundred BETWEEN 5 AND 55;
The pg_explain_explain analysis for your query shows that it executed extremely efficiently:

Execution Time: 0.904 ms
Planning Time: 1.228 ms
Total Time: 2.132 ms
Issues Found: None
Analysis:
The query is running very fast, likely because the onek1 table is relatively small (estimated at 1,000 rows), allowing the database to perform a Sequential Scan (reading the whole table) almost instantly.

Note on Performance Optimization: I noticed from the table schema that there is no index on the hundred column. While it isn't necessary for a small table like this, if the onek1 table were to grow to millions of rows, this query would become slower because the database would have to check every single row to see if it meets the BETWEEN 5 AND 55 condition.

If you expect this table to become large, you might consider adding an index:

CREATE INDEX idx_onek1_hundred ON onek1(hundred);

