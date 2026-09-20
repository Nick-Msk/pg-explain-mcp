"""Unit tests for the plan analyzer."""

from pg_explain_mcp.analyzer import (
    DEFAULT_CHECKS,
    BitmapHeapScanCheck,
    DiskSpillHashCheck,
    DiskSpillSortCheck,
    EstimateMismatchCheck,
    NestedLoopCheck,
    IndexScanCheck,
    SeqScanCheck,
    analyze_plan,
)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


class TestSeqScanCheck:
    def test_small_table_is_ok(self):
        node = {"Node Type": "Seq Scan", "Actual Rows": 100, "Relation Name": "t"}
        assert SeqScanCheck().check(node) == []

    def test_large_table_is_reported(self):
        node = {"Node Type": "Seq Scan", "Actual Rows": 5000, "Relation Name": "t"}
        issues = SeqScanCheck().check(node)
        assert len(issues) == 1
        assert issues[0].type == "seq_scan"
        assert issues[0].severity == "warning"
        assert "t" in issues[0].message

    def test_other_node_type_is_ignored(self):
        node = {"Node Type": "Index Scan", "Actual Rows": 999999}
        assert SeqScanCheck().check(node) == []


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
        node = {"Node Type": "Sort", "Sort Method": "external merge Disk: 2048kB"}
        issues = DiskSpillSortCheck().check(node)
        assert len(issues) == 1
        assert issues[0].type == "disk_spill_sort"


class TestDiskSpillHashCheck:
    def test_single_batch_is_ok(self):
        assert DiskSpillHashCheck().check({"Hash Batches": 1}) == []

    def test_multiple_batches_is_reported(self):
        issues = DiskSpillHashCheck().check({"Node Type": "Hash", "Hash Batches": 8})
        assert len(issues) == 1
        assert issues[0].type == "disk_spill_hash"


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
                    "Actual Rows": 5000,
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
                        {"Node Type": "Seq Scan", "Actual Rows": 9000, "Relation Name": "a"},
                        {"Node Type": "Seq Scan", "Actual Rows": 8000, "Relation Name": "b"},
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
