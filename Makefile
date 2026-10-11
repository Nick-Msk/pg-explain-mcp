# pg-explain-mcp + pg-tune — developer shortcuts.
#
# Everything uses `uv run`, so no `source .venv/bin/activate` needed.
# Docker targets bring up PostgreSQL 18 on :5433 and run tests inside
# the pg-tune image.

SHELL   := /bin/bash
COMPOSE := docker compose
UV      := uv

.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

.PHONY: sync
sync:  ## install/refresh both packages into .venv
	$(UV) sync --all-packages

# ---------------------------------------------------------------------------
# Local — lint and tests, no database
# ---------------------------------------------------------------------------

.PHONY: lint test test-explain test-tune
lint:  ## ruff over both projects
	$(UV) run ruff check src tests
	cd pg-tune && $(UV) run ruff check src tests

test:  ## all unit tests, both projects
	$(UV) run pytest tests -v
	cd pg-tune && $(UV) run pytest -v

test-explain:  ## pg-explain-mcp unit tests only
	$(UV) run pytest tests -v

test-tune:  ## pg-tune unit tests only
	cd pg-tune && $(UV) run pytest -v

# ---------------------------------------------------------------------------
# Docker — bring the stack up/down
# ---------------------------------------------------------------------------

.PHONY: docker-build docker-up docker-down docker-logs
docker-build:  ## rebuild the pg-tune image
	$(COMPOSE) build pg-tune

docker-up:  ## start PostgreSQL 18 in the background
	$(COMPOSE) up -d postgres

docker-down:  ## stop and remove containers (keeps volumes)
	$(COMPOSE) down

docker-logs:  ## tail logs from the postgres container
	$(COMPOSE) logs -f postgres

# ---------------------------------------------------------------------------
# Docker — tests inside the container
# ---------------------------------------------------------------------------

.PHONY: docker-test docker-test-unit docker-test-integration
docker-test:  ## all tests inside the container (unit + integration)
	$(COMPOSE) run --rm -e PG_TEST_INTEGRATION=1 pg-tune \
	    pytest tests/ -v

docker-test-unit:  ## unit tests only, inside the container
	$(COMPOSE) run --rm pg-tune pytest tests/ -v

docker-test-integration:  ## integration tests only, inside the container
	$(COMPOSE) run --rm -e PG_TEST_INTEGRATION=1 pg-tune \
	    pytest tests/test_integration_backup_restore.py -v

# ---------------------------------------------------------------------------
# Docker — interactive
# ---------------------------------------------------------------------------

.PHONY: docker-shell
docker-shell:  ## open a shell inside the pg-tune container
	$(COMPOSE) run --rm pg-tune bash

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

.PHONY: clean
clean:  ## remove caches and local test artifacts
	find . -type d -name '__pycache__' -not -path './.git/*' \
	    -exec rm -rf {} + 2>/dev/null || true
	find . -type d \( -name '.pytest_cache' -o -name '.ruff_cache' \) \
	    -not -path './.git/*' -exec rm -rf {} + 2>/dev/null || true
	rm -f pg-tune/tests/test_tune.db*
	rm -rf pg-tune/tests/test_backups/

# ---------------------------------------------------------------------------
# Help — the default goal
# ---------------------------------------------------------------------------

.PHONY: help
help:  ## list all targets
	@awk 'BEGIN {FS = ":.*?## "} \
	     /^[a-zA-Z_-]+:.*?## / { \
	         printf "  \033[36m%-26s\033[0m %s\n", $$1, $$2 \
	     }' $(MAKEFILE_LIST)

