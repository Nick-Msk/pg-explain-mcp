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

