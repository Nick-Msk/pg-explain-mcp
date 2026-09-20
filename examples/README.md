# Examples

Ready-to-use configurations for integrating `pg-explain-mcp` with
Continue.dev.

## Files

- [`mcpServers/pg-explain.yaml`](mcpServers/pg-explain.yaml) — MCP server
  configuration for Continue.dev.
- [`postgres-agent.md`](postgres-agent.md) — system prompt for an AI agent
  that knows how to use both `pg-explain` and a general-purpose PostgreSQL
  MCP server.

## Setup

1. Install `pg-explain-mcp` (see the top-level [`README.md`](../README.md)).
2. Copy `mcpServers/pg-explain.yaml` into your workspace's
   `.continue/mcpServers/` directory.
3. Replace the placeholders:
   - `command` — full path to the Python interpreter inside your `.venv`.
   - `PG_USER`, `PG_PASSWORD`, `PG_DATABASE` — your PostgreSQL credentials.
4. (Optional) Copy `postgres-agent.md` into `.continue/agents/` to use it
   as a custom agent prompt.
5. In VS Code: `Cmd+Shift+P` → **`Continue: Reload Config`**.
6. Open a new chat in **Agent Mode** and try:

   > Use the pg-explain tool to analyze:
   > `SELECT * FROM big_unclastered WHERE val LIKE '0%';`

## Security

The examples use placeholder credentials. Never commit real database
passwords to a public repository. Consider using environment variables
or a secrets manager in production.

