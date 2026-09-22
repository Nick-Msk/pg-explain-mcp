"""Unit tests for the plan analyzer."""

from pg_explain_mcp.analyzer import (
    DEFAULT_CHECKS,
    BitmapHeapScanCheck,
    DiskSpillHashCheck,
    DiskSpillSortCheck,
    EstimateMismatchCheck,
    IndexScanCheck,
    NestedLoopCheck,
    SeqScanCheck,
    analyze_plan,
    summarize_plan_node,
)
from pg_explain_mcp.server import _format_indexes, _format_params

# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


class TestSeqScanCheck:
    def test_small_table_is_ok(self):
        node = {"Node Type": "Seq Scan", "Actual Rows": 100, "Relation Name": "t"}
        assert SeqScanCheck().check(node) == []

    def test_large_table_is_reported(self):
        node = {
            "Node Type": "Seq Scan",
            "Relation Name": "t",
            "Actual Rows": 100,
            "Rows Removed by Filter": 9900,
        }
        issues = SeqScanCheck().check(node)
        assert len(issues) == 1
        assert issues[0].type == "seq_scan"
        assert "10000 rows" in issues[0].message

    def test_boundary_value_is_ok(self):
        node = {
            "Node Type": "Seq Scan",
            "Actual Rows": 500,
            "Rows Removed by Filter": 500,
            "Relation Name": "t",
        }
        assert SeqScanCheck().check(node) == []

    def test_other_node_type_is_ignored(self):
        node = {"Node Type": "Index Scan", "Actual Rows": 999999}
        assert SeqScanCheck().check(node) == []

    def test_message_is_neutral(self):
        node = {
            "Node Type": "Seq Scan",
            "Relation Name": "big",
            "Actual Rows": 100,
            "Rows Removed by Filter": 999_900,
        }
        issues = SeqScanCheck().check(node)
        assert "Verify whether an index" in issues[0].message
        assert "if it does, investigate" in issues[0].message.lower()

    def test_moderate_selectivity_is_ok(self):
        """Filter discards ~50 % — not enough to justify an index."""
        node = {
            "Node Type": "Seq Scan",
            "Relation Name": "t",
            "Actual Rows": 5000,
            "Rows Removed by Filter": 5000,
        }
        assert SeqScanCheck().check(node) == []

    def test_no_filter_is_ok(self):
        """Self-join or full scan — index won't help."""
        node = {
            "Node Type": "Seq Scan",
            "Relation Name": "t",
            "Actual Rows": 1_000_000,
        }
        assert SeqScanCheck().check(node) == []

    def test_high_selectivity_filter_is_reported(self):
        node = {
            "Node Type": "Seq Scan",
            "Relation Name": "big",
            "Actual Rows": 100,
            "Rows Removed by Filter": 999_900,
        }
        issues = SeqScanCheck().check(node)
        assert len(issues) == 1
        assert "Verify whether an index" in issues[0].message

class TestEstimateMismatchCheck:
    def test_close_estimate_is_ok(self):
        node = {"Node Type": "Hash Join", "Plan Rows": 100, "Actual Rows": 120}
        assert EstimateMismatchCheck().check(node) == []

    def test_large_mismatch_is_reported(self):
        node = {"Node Type": "Hash Join", "Plan Rows": 100, "Actual Rows": 5000}
        issues = EstimateMismatchCheck().check(node)
        assert len(issues) == 1
        assert issues[0].type == "estimate_mismatch"

    def test_missing_values_do_not_crash(self):
        assert EstimateMismatchCheck().check({"Node Type": "X"}) == []

    def test_small_absolute_numbers_are_ignored(self):
        """High ratio on tiny numbers is noise, not a problem."""
        node = {"Node Type": "Bitmap Index Scan", "Plan Rows": 4, "Actual Rows": 83}
        assert EstimateMismatchCheck().check(node) == []

    def test_large_absolute_mismatch_is_reported(self):
        """A real mismatch on a large scan is reported."""
        node = {"Node Type": "Hash Join", "Plan Rows": 5_000, "Actual Rows": 200_000}
        issues = EstimateMismatchCheck().check(node)
        assert len(issues) == 1
        assert issues[0].type == "estimate_mismatch"


class TestDiskSpillSortCheck:
    def test_in_memory_sort_is_ok(self):
        node = {"Node Type": "Sort", "Sort Method": "quicksort"}
        assert DiskSpillSortCheck().check(node) == []

    def test_external_sort_is_reported(self):
        node = {
            "Node Type": "Sort",
            "Sort Method": "external merge",
            "Sort Space Type": "Disk",
            "Sort Space Used": 221208,
        }
        issues = DiskSpillSortCheck().check(node)
        assert len(issues) == 1
        assert issues[0].type == "disk_spill_sort"
        assert "221208kB" in issues[0].message
        assert "216.0 MB" in issues[0].message

    def test_message_mentions_work_mem_formula(self):
        node = {
            "Node Type": "Sort",
            "Sort Method": "external merge",
            "Sort Space Type": "Disk",
            "Sort Space Used": 221208,
        }
        issues = DiskSpillSortCheck().check(node)
        msg = issues[0].message
        assert "set work_mem to at least" in msg
        assert "hash_mem_multiplier does not apply" in msg
        assert "Current work_mem is not part of this calculation" in msg

    def test_missing_fields_do_not_crash(self):
        node = {"Node Type": "Sort", "Sort Method": "external merge"}
        issues = DiskSpillSortCheck().check(node)
        assert len(issues) == 1

    def test_key_order_is_stable(self):
        node = {"Node Type": "Seq Scan", "Relation Name": "t"}
        result = summarize_plan_node(node)
        keys = list(result[0].keys())
        assert keys[:2] == ["depth", "node_type"]
    def test_message_contains_absolute_value(self):
        node = {
            "Node Type": "Sort",
            "Sort Method": "external merge",
            "Sort Space Type": "Disk",
            "Sort Space Used": 221208,
        }
        issues = DiskSpillSortCheck().check(node)
        msg = issues[0].message
        assert "221208kB" in msg
        assert "216.0 MB" in msg
        assert "256 MB" in msg
        assert "Current work_mem is not part of" in msg

class TestDiskSpillHashCheck:
    def test_single_batch_is_ok(self):
        assert DiskSpillHashCheck().check({"Hash Batches": 1}) == []

    def test_multiple_batches_is_reported(self):
        node = {
            "Node Type": "Hash",
            "Hash Batches": 8,
            "Peak Memory Usage": 20000,
        }
        issues = DiskSpillHashCheck().check(node)
        assert len(issues) == 1
        assert "8 batches" in issues[0].message
        assert "estimated full size" in issues[0].message

    def test_message_mentions_multiplier_formula(self):
        node = {"Hash Batches": 4, "Peak Memory Usage": 37536}
        issues = DiskSpillHashCheck().check(node)
        assert "hash_mem_multiplier" in issues[0].message
        assert "list_parameters" in issues[0].message

    def test_parallel_hash_is_annotated(self):
        node = {
            "Node Type": "Hash",
            "Hash Batches": 4,
            "Peak Memory Usage": 37536,
            "Disk Usage": 10720,
            "Parallel Aware": True,
            "Actual Loops": 3,
        }
        issues = DiskSpillHashCheck().check(node)
        msg = issues[0].message
        assert "3 workers" in msg
        assert "37536kB per batch" in msg
        assert "146.6 MB" in msg
        assert "disk 10720kB" in msg

    def test_missing_fields_do_not_crash(self):
        node = {"Hash Batches": 4}
        issues = DiskSpillHashCheck().check(node)
        assert len(issues) == 1
        assert "4 batches" in issues[0].message

class TestNestedLoopCheck:
    def test_few_loops_is_ok(self):
        node = {"Node Type": "Nested Loop", "Actual Loops": 10}
        assert NestedLoopCheck().check(node) == []

    def test_many_loops_is_reported(self):
        node = {"Node Type": "Nested Loop", "Actual Loops": 100_000}
        issues = NestedLoopCheck().check(node)
        assert len(issues) == 1
        assert issues[0].severity == "info"


class TestBitmapHeapScanCheck:
    def test_small_bitmap_is_ok(self):
        node = {
            "Node Type": "Bitmap Heap Scan",
            "Actual Rows": 1000,
            "Relation Name": "t",
        }
        assert BitmapHeapScanCheck().check(node) == []

    def test_large_bitmap_is_reported(self):
        node = {
            "Node Type": "Bitmap Heap Scan",
            "Actual Rows": 500_000,
            "Relation Name": "events",
        }
        issues = BitmapHeapScanCheck().check(node)
        assert len(issues) == 1
        assert issues[0].type == "bitmap_heap_scan"
        assert issues[0].severity == "info"
        assert "events" in issues[0].message

    def test_boundary_value_is_ok(self):
        node = {
            "Node Type": "Bitmap Heap Scan",
            "Actual Rows": 100_000,
            "Relation Name": "t",
        }
        assert BitmapHeapScanCheck().check(node) == []

    def test_other_node_type_is_ignored(self):
        node = {"Node Type": "Index Scan", "Actual Rows": 999_999}
        assert BitmapHeapScanCheck().check(node) == []

    def test_missing_relation_name_does_not_crash(self):
        node = {"Node Type": "Bitmap Heap Scan", "Actual Rows": 200_000}
        issues = BitmapHeapScanCheck().check(node)
        assert len(issues) == 1
        assert "?" in issues[0].message


class TestIndexScanCheck:
    # --- Index Only Scan -------------------------------------------------

    def test_index_only_scan_with_few_heap_fetches_is_ok(self):
        node = {
            "Node Type": "Index Only Scan",
            "Index Name": "idx_a",
            "Relation Name": "t",
            "Actual Rows": 10_000,
            "Heap Fetches": 50,
        }
        assert IndexScanCheck().check(node) == []

    def test_index_only_scan_below_min_threshold_is_ok(self):
        node = {
            "Node Type": "Index Only Scan",
            "Actual Rows": 500,
            "Heap Fetches": 400,  # ratio 80%, but below MIN_ROWS
        }
        assert IndexScanCheck().check(node) == []

    def test_index_only_scan_with_many_heap_fetches_is_reported(self):
        node = {
            "Node Type": "Index Only Scan",
            "Index Name": "idx_orders_status",
            "Relation Name": "orders",
            "Actual Rows": 50_000,
            "Heap Fetches": 45_000,  # 90% — bad
        }
        issues = IndexScanCheck().check(node)
        assert len(issues) == 1
        assert issues[0].type == "index_scan_heap_locality"
        assert issues[0].severity == "warning"
        assert "idx_orders_status" in issues[0].message
        assert "VACUUM" in issues[0].message

    # --- Regular Index Scan ---------------------------------------------

    def test_index_scan_from_cache_is_ok(self):
        node = {
            "Node Type": "Index Scan",
            "Actual Rows": 5000,
            "Shared Read Blocks": 0,  # всё из кэша
        }
        assert IndexScanCheck().check(node) == []

    def test_index_scan_with_many_disk_reads_is_reported(self):
        node = {
            "Node Type": "Index Scan",
            "Index Name": "idx_users_email",
            "Relation Name": "users",
            "Actual Rows": 20_000,
            "Shared Read Blocks": 8_000,
        }
        issues = IndexScanCheck().check(node)
        assert len(issues) == 1
        assert issues[0].type == "index_scan_heap_locality"
        assert issues[0].severity == "info"
        assert "CLUSTER" in issues[0].message

    # --- Edge cases ------------------------------------------------------

    def test_seq_scan_is_ignored(self):
        node = {"Node Type": "Seq Scan", "Actual Rows": 999_999, "Shared Read Blocks": 9999}
        assert IndexScanCheck().check(node) == []

    def test_missing_fields_do_not_crash(self):
        assert IndexScanCheck().check({"Node Type": "Index Scan"}) == []
        assert IndexScanCheck().check({"Node Type": "Index Only Scan"}) == []
        assert IndexScanCheck().check({}) == []


class TestSummarizePlanNode:
    def test_simple_node(self):
        node = {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Actual Rows": 5000,
            "Plan Rows": 4800,
        }
        result = summarize_plan_node(node)
        assert len(result) == 1
        assert result[0]["node_type"] == "Seq Scan"
        assert result[0]["relation"] == "orders"
        assert result[0]["actual_rows"] == 5000
        assert result[0]["depth"] == 0

    def test_nested_tree_is_flattened(self):
        node = {
            "Node Type": "Hash Join",
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "a", "Actual Rows": 100},
                {"Node Type": "Index Scan", "Index Name": "idx_b", "Actual Rows": 50},
            ],
        }
        result = summarize_plan_node(node)
        assert len(result) == 3
        assert result[0]["node_type"] == "Hash Join"
        assert result[0]["depth"] == 0
        assert result[1]["depth"] == 1
        assert result[2]["depth"] == 1
        assert result[2]["index"] == "idx_b"

    def test_optional_fields_are_skipped(self):
        node = {"Node Type": "Limit", "Actual Rows": 10}
        result = summarize_plan_node(node)
        assert "relation" not in result[0]
        assert "index" not in result[0]
        assert "heap_fetches" not in result[0]

class TestFormatParams:
    def test_empty_list(self):
        assert _format_params([]) == "No parameters found."

    def test_with_unit(self):
        rows = [{
            "name": "work_mem",
            "setting": "20480",
            "unit": "kB",
            "source": "default",
            "short_desc": "Sets the maximum memory to be used for query workspaces.",
        }]
        result = _format_params(rows)
        assert "work_mem = 20480 kB (default)" in result

    def test_without_unit(self):
        rows = [{
            "name": "hash_mem_multiplier",
            "setting": "2",
            "unit": None,
            "source": "default",
            "short_desc": "Multiple of work_mem to use for hash tables.",
        }]
        result = _format_params(rows)
        assert "hash_mem_multiplier = 2 (default)" in result


# ---------------------------------------------------------------------------
# analyze_plan — end-to-end
# ---------------------------------------------------------------------------


class TestAnalyzePlan:
    def test_empty_plan(self):
        result = analyze_plan([])
        assert result["issues"] == []
        assert result["summary"] == "Empty plan"

    def test_healthy_plan(self):
        plan = [
            {
                "Plan": {"Node Type": "Index Scan", "Actual Rows": 10},
                "Execution Time": 0.5,
                "Planning Time": 0.1,
            }
        ]
        result = analyze_plan(plan)
        assert result["issue_count"] == 0
        assert "No issues found" in result["summary"]
        assert result["total_time_ms"] == 0.6

    def test_plan_with_seq_scan(self):
        plan = [
            {
                "Plan": {
                    "Node Type": "Seq Scan",
                    "Relation Name": "orders",
                    "Actual Rows": 100,
                    "Rows Removed by Filter": 9900,
                    "Plans": [],
                },
                "Execution Time": 42.0,
                "Planning Time": 1.0,
            }
        ]
        result = analyze_plan(plan)
        assert result["issue_count"] == 1
        assert result["issues"][0]["type"] == "seq_scan"

    def test_nested_plan_is_traversed(self):
        plan = [
            {
                "Plan": {
                    "Node Type": "Hash Join",
                    "Plans": [
                        {
                            "Node Type": "Seq Scan",
                            "Relation Name": "a",
                            "Actual Rows": 100,
                            "Rows Removed by Filter": 9900,
                        },
                        {
                            "Node Type": "Seq Scan",
                            "Relation Name": "b",
                            "Actual Rows": 100,
                            "Rows Removed by Filter": 9900,
                        },
                    ],
                },
                "Execution Time": 10.0,
                "Planning Time": 0.5,
            }
        ]
        result = analyze_plan(plan)
        assert result["issue_count"] == 2

    def test_custom_checks_registry(self):
        plan = [
            {
                "Plan": {"Node Type": "Seq Scan", "Actual Rows": 5000, "Relation Name": "t"},
                "Execution Time": 1.0,
                "Planning Time": 0.1,
            }
        ]
        result = analyze_plan(plan, checks=())
        assert result["issue_count"] == 0
        assert len(DEFAULT_CHECKS) > 0

    def test_plan_with_bitmap_heap_scan(self):
        plan = [
            {
                "Plan": {
                    "Node Type": "Bitmap Heap Scan",
                    "Relation Name": "events",
                    "Actual Rows": 500_000,
                    "Plans": [],
                },
                "Execution Time": 120.0,
                "Planning Time": 2.0,
            }
        ]
        result = analyze_plan(plan)
        assert result["issue_count"] == 1
        assert result["issues"][0]["type"] == "bitmap_heap_scan"

    def test_plan_with_index_scan_heap_locality_issue(self):
        plan = [
            {
                "Plan": {
                    "Node Type": "Index Only Scan",
                    "Index Name": "idx_big_n",
                    "Relation Name": "big",
                    "Actual Rows": 100_000,
                    "Heap Fetches": 95_000,
                    "Plans": [],
                },
                "Execution Time": 250.0,
                "Planning Time": 1.5,
            }
        ]
        result = analyze_plan(plan)
        assert result["issue_count"] == 1
        assert result["issues"][0]["type"] == "index_scan_heap_locality"


# ---------------------------------------------------------------------------
# list_indexes formatting
# ---------------------------------------------------------------------------


class TestFormatIndexes:
    def test_empty_list(self):
        assert _format_indexes([]) == "No indexes found."

    def test_regular_index(self):
        rows = [
            {
                "schema_name": "public",
                "table_name": "orders",
                "index_name": "idx_orders_status",
                "is_unique": False,
                "is_primary": False,
                "columns": ["status"],
            }
        ]
        result = _format_indexes(rows)
        assert "idx_orders_status" in result
        assert "[INDEX]" in result
        assert "status" in result

    def test_unique_index(self):
        rows = [
            {
                "schema_name": "public",
                "table_name": "users",
                "index_name": "idx_users_email",
                "is_unique": True,
                "is_primary": False,
                "columns": ["email"],
            }
        ]
        result = _format_indexes(rows)
        assert "[UNIQUE]" in result

    def test_primary_key(self):
        rows = [
            {
                "schema_name": "public",
                "table_name": "users",
                "index_name": "users_pkey",
                "is_unique": True,
                "is_primary": True,
                "columns": ["id"],
            }
        ]
        result = _format_indexes(rows)
        assert "[PRIMARY KEY]" in result

    def test_composite_index(self):
        rows = [
            {
                "schema_name": "public",
                "table_name": "orders",
                "index_name": "idx_orders_status_created",
                "is_unique": False,
                "is_primary": False,
                "columns": ["status", "created_at"],
            }
        ]
        result = _format_indexes(rows)
        assert "status, created_at" in result
