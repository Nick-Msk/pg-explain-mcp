"""Unit tests for the plan analyzer."""

import pytest

from pg_explain_mcp.analyzer import (
    BitmapHeapScanCheck,
    DiskSpillHashCheck,
    DiskSpillSortCheck,
    EstimateMismatchCheck,
    IndexOnlyScanCheck,
    IndexRegularScanCheck,
    NestedLoopCheck,
    NonSargableCheck,
    PartitionPruningCheck,
    PlanNode,
    SeqScanCheck,
    analyze_plan,
    filtered_parse_plan,
    format_plan_tree,
    parse_plan,
    summarize_plan_node,
)
from pg_explain_mcp.server import _format_indexes, _format_params
from tests.conftest import ALL_CHECKS

# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


class TestSeqScanCheck:
    DEFAULTS = {"threshold_rows": "1000", "min_filter_ratio": "0.9"}

    def _check(self, **overrides) -> SeqScanCheck:
        return SeqScanCheck(params={**self.DEFAULTS, **overrides})

    def _node(self, **fields) -> PlanNode:
        return PlanNode(depth=0, path="0:0",
                        fields={"Node Type": "Seq Scan", **fields})

    def test_small_scan_is_ok(self):
        node = self._node(**{"Actual Rows": 100, "Relation Name": "t"})
        assert self._check().check(node) == []

    def test_no_filter_is_ok(self):
        node = self._node(**{"Actual Rows": 1_000_000, "Relation Name": "t"})
        assert self._check().check(node) == []

    def test_moderate_selectivity_is_ok(self):
        node = self._node(**{
            "Actual Rows": 5000,
            "Rows Removed by Filter": 5000,
            "Relation Name": "t",
        })
        assert self._check().check(node) == []

    def test_high_selectivity_is_reported(self):
        node = self._node(**{
            "Actual Rows": 100,
            "Rows Removed by Filter": 999_900,
            "Relation Name": "big",
        })
        issues = self._check().check(node)
        assert len(issues) == 1
        assert issues[0].type == "seq_scan"
        assert issues[0].severity == "warning"
        assert "1000000 rows" in issues[0].message
        assert "999900 filtered out" in issues[0].message
        assert "big" in issues[0].message

    def test_other_node_type_is_ignored(self):
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Index Scan", "Actual Rows": 999_999,
        })
        assert self._check().check(node) == []

    def test_custom_thresholds(self):
        node = self._node(**{
            "Actual Rows": 500,
            "Rows Removed by Filter": 500,
            "Relation Name": "t",
        })
        # default: total 1000 <= threshold 1000 → silent
        assert self._check().check(node) == []
        # custom: threshold 100 → fires (ratio 0.5 < 0.9 — so still silent)
        assert self._check(threshold_rows="100").check(node) == []
        # relax ratio → fires
        issues = self._check(
            threshold_rows="100", min_filter_ratio="0.4"
        ).check(node)
        assert len(issues) == 1

    def test_parallel_seq_scan_counts_all_loops(self):
        """Parallel Seq Scan reports per-worker counters; multiply by
        Actual Loops to get the real total."""
        node = self._node(**{
            "Actual Rows": 29,
            "Rows Removed by Filter": 333_304,
            "Actual Loops": 3,
            "Relation Name": "big",
        })
        issues = self._check().check(node)
        assert len(issues) == 1
        # total = 3 × (29 + 333304) = 999_999
        assert "999999 rows" in issues[0].message
        assert "87 returned" in issues[0].message
        assert "999912 filtered out" in issues[0].message

class TestEstimateMismatchCheck:
    DEFAULTS = {"threshold_ratio": "10.0", "min_rows": "1000"}

    def _check(self, **overrides) -> EstimateMismatchCheck:
        return EstimateMismatchCheck(
            params={**self.DEFAULTS, **overrides}
        )

    def _node(self, node_type="Hash Join", parent=None, **fields):
        return PlanNode(
            depth=0, path="0:0", parent=parent,
            fields={"Node Type": node_type, **fields},
        )

    def test_small_actual_is_ignored(self):
        node = self._node(**{"Plan Rows": 5000, "Actual Rows": 1})
        assert self._check().check(node) == []

    def test_small_planned_is_ignored(self):
        node = self._node(**{"Plan Rows": 1, "Actual Rows": 5000})
        assert self._check().check(node) == []

    def test_zero_actual_is_ignored(self):
        node = self._node(**{"Plan Rows": 5000, "Actual Rows": 0})
        assert self._check().check(node) == []

    def test_large_mismatch_is_reported(self):
        node = self._node(**{"Plan Rows": 1000, "Actual Rows": 50_000})
        issues = self._check().check(node)
        assert len(issues) == 1
        assert issues[0].type == "estimate_mismatch"
        assert "Investigate why" in issues[0].message
        assert "non-sargable" in issues[0].message

    def test_relation_name_is_included(self):
        node = self._node(
            node_type="Seq Scan",
            **{"Plan Rows": 1000, "Actual Rows": 50_000,
               "Relation Name": "orders"},
        )
        msg = self._check().check(node)[0].message
        assert "on 'orders'" in msg

    def test_ratio_below_threshold_is_ignored(self):
        node = self._node(**{"Plan Rows": 1000, "Actual Rows": 5000})
        assert self._check().check(node) == []

    def test_direct_limit_parent_is_ignored(self):
        limit = self._node(node_type="Limit")
        scan = PlanNode(
            depth=1, path="0:0/1:0", parent=limit,
            fields={
                "Node Type": "Index Scan",
                "Plan Rows": 999_996,
                "Actual Rows": 5000,
            },
        )
        assert self._check().check(scan) == []

    def test_limit_two_levels_above(self):
        """Limit -> Sort -> Seq Scan — ratio must be suppressed."""
        limit = PlanNode(depth=0, path="0:0", fields={"Node Type": "Limit"})
        sort = PlanNode(depth=1, path="0:0/1:0", parent=limit,
                        fields={"Node Type": "Sort"})
        scan = PlanNode(depth=2, path="0:0/1:0/2:0", parent=sort, fields={
            "Node Type": "Seq Scan",
            "Plan Rows": 395_833,
            "Actual Rows": 3_389,
        })
        assert self._check().check(scan) == []

    def test_limit_three_levels_above(self):
        """Limit -> Gather Merge -> Sort -> Seq Scan (the real reproducer)."""
        limit = PlanNode(depth=0, path="0:0", fields={"Node Type": "Limit"})
        gm = PlanNode(depth=1, path="0:0/1:0", parent=limit,
                      fields={"Node Type": "Gather Merge"})
        sort = PlanNode(depth=2, path="0:0/1:0/2:0", parent=gm,
                        fields={"Node Type": "Sort"})
        scan = PlanNode(depth=3, path="0:0/1:0/2:0/3:0", parent=sort, fields={
            "Node Type": "Seq Scan",
            "Plan Rows": 395_833,
            "Actual Rows": 3_389,
        })
        assert self._check().check(scan) == []

    def test_no_limit_ancestor_still_fires(self):
        """Sanity: without Limit above, the check still works."""
        sort = PlanNode(depth=0, path="0:0", fields={"Node Type": "Sort"})
        scan = PlanNode(depth=1, path="0:0/1:0", parent=sort, fields={
            "Node Type": "Seq Scan",
            "Plan Rows": 395_833,
            "Actual Rows": 3_389,
        })
        issues = self._check().check(scan)
        assert len(issues) == 1

class TestDiskSpillSortCheck:
    def _check(self, min_spill_kb: int = 0) -> DiskSpillSortCheck:
        return DiskSpillSortCheck(params={
            "min_spill_kb":    str(min_spill_kb),
            "min_work_mem_mb": "32",
            "headroom_ratio":  "1.1",
        })

    def test_in_memory_sort_is_ok(self):
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Sort", "Sort Method": "quicksort",
        })
        assert self._check().check(node) == []

    def test_sized_spill_is_reported(self):
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Sort",
            "Sort Method": "external merge",
            "Sort Space Type": "Disk",
            "Sort Space Used": 221208,
        })
        issues = self._check().check(node)
        assert len(issues) == 1
        assert issues[0].type == "disk_spill_sort"
        assert "216.0 MB" in issues[0].message
        assert "256 MB" in issues[0].message

    def test_unsized_spill_uses_generic_message(self):
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Sort",
            "Sort Method": "external merge",
        })
        issues = self._check().check(node)
        assert len(issues) == 1
        assert "Increase work_mem" in issues[0].message
        assert "hash_mem_multiplier" in issues[0].message

    def test_min_spill_threshold_suppresses_small(self):
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Sort",
            "Sort Method": "external merge",
            "Sort Space Type": "Disk",
            "Sort Space Used": 512,
        })
        assert self._check(min_spill_kb=1000).check(node) == []

class TestDiskSpillHashCheck:
    DEFAULTS = {"min_batches": "2"}

    def _check(self, **overrides) -> DiskSpillHashCheck:
        return DiskSpillHashCheck(
            params={**self.DEFAULTS, **overrides}
        )

    def test_single_batch_is_ok(self):
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Hash", "Hash Batches": 1,
        })
        assert self._check().check(node) == []

    def test_missing_batches_is_ok(self):
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Hash",
        })
        assert self._check().check(node) == []

    def test_multiple_batches_is_reported(self):
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Hash",
            "Hash Batches": 4,
            "Peak Memory Usage": 37536,
        })
        issues = self._check().check(node)
        assert len(issues) == 1
        assert issues[0].type == "disk_spill_hash"
        assert "4 batches" in issues[0].message
        assert "37536kB" in issues[0].message
        assert "146.6 MB" in issues[0].message

    def test_parallel_hash_is_annotated(self):
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Hash",
            "Hash Batches": 4,
            "Peak Memory Usage": 37536,
            "Parallel Aware": True,
            "Actual Loops": 3,
        })
        msg = self._check().check(node)[0].message
        assert "3 workers" in msg

    def test_custom_threshold_suppresses_small(self):
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Hash",
            "Hash Batches": 2,
            "Peak Memory Usage": 100,
        })
        assert self._check(min_batches="4").check(node) == []

class TestNestedLoopCheck:
    def _check(self, threshold_loops: int = 1000) -> NestedLoopCheck:
        return NestedLoopCheck(
            params={"threshold_loops": str(threshold_loops)}
        )

    @staticmethod
    def _nl(outer, inner) -> PlanNode:
        return PlanNode(
            depth=0, path="0:0",
            fields={"Node Type": "Nested Loop"},
            children=[outer, inner],
        )

    def test_few_loops_is_ok(self):
        outer = PlanNode(depth=1, path="0:0/1:0",
                         fields={"Node Type": "Seq Scan"})
        inner = PlanNode(depth=1, path="0:0/1:1",
                         fields={"Node Type": "Index Scan",
                                 "Actual Loops": 10})
        assert self._check().check(self._nl(outer, inner)) == []

    def test_many_loops_is_reported(self):
        outer = PlanNode(depth=1, path="0:0/1:0",
                         fields={"Node Type": "Seq Scan"})
        inner = PlanNode(depth=1, path="0:0/1:1", fields={
            "Node Type": "Index Only Scan",
            "Relation Name": "inner_t",
            "Actual Loops": 5000,
            "Actual Rows": 0.98,
        })
        issues = self._check().check(self._nl(outer, inner))
        assert len(issues) == 1
        assert issues[0].severity == "info"
        assert issues[0].type == "nested_loop"
        assert "5000 times" in issues[0].message
        assert "inner_t" in issues[0].message
        assert "Index Only Scan" in issues[0].message

    def test_missing_inner_child_is_ignored(self):
        outer = PlanNode(depth=1, path="0:0/1:0",
                         fields={"Node Type": "Seq Scan"})
        node = PlanNode(depth=0, path="0:0",
                        fields={"Node Type": "Nested Loop"},
                        children=[outer])
        assert self._check().check(node) == []

    def test_wrong_node_type_is_ignored(self):
        node = PlanNode(depth=0, path="0:0",
                        fields={"Node Type": "Hash Join"})
        assert self._check().check(node) == []

    def test_boundary_value_is_ok(self):
        outer = PlanNode(depth=1, path="0:0/1:0",
                         fields={"Node Type": "Seq Scan"})
        inner = PlanNode(depth=1, path="0:0/1:1", fields={
            "Node Type": "Index Scan",
            "Actual Loops": 1000,
        })
        assert self._check().check(self._nl(outer, inner)) == []

    def test_custom_threshold(self):
        outer = PlanNode(depth=1, path="0:0/1:0",
                         fields={"Node Type": "Seq Scan"})
        inner = PlanNode(depth=1, path="0:0/1:1", fields={
            "Node Type": "Index Scan",
            "Actual Loops": 500,
        })
        assert self._check(threshold_loops=100).check(
            self._nl(outer, inner)
        )
        assert self._check(threshold_loops=1000).check(
            self._nl(outer, inner)
        ) == []

class TestBitmapHeapScanCheck:
    def test_small_bitmap_is_ok(self):
        check = BitmapHeapScanCheck(params={"threshold_rows": "100000"})
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Bitmap Heap Scan",
            "Actual Rows": 1000,
            "Relation Name": "t",
        })
        assert check.check(node) == []

    def test_large_bitmap_is_reported(self):
        check = BitmapHeapScanCheck(params={"threshold_rows": "100000"})
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Bitmap Heap Scan",
            "Actual Rows": 500_000,
            "Relation Name": "events",
        })
        issues = check.check(node)
        assert len(issues) == 1
        assert issues[0].type == "bitmap_heap_scan"
        assert issues[0].severity == "info"
        assert "events" in issues[0].message

    def test_boundary_value_is_ok(self):
        check = BitmapHeapScanCheck(params={"threshold_rows": "100000"})
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Bitmap Heap Scan",
            "Actual Rows": 100_000,
            "Relation Name": "t",
        })
        assert check.check(node) == []

    def test_other_node_type_is_ignored(self):
        check = BitmapHeapScanCheck(params={"threshold_rows": "100000"})
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Index Scan",
            "Actual Rows": 999_999,
        })
        assert check.check(node) == []

    def test_missing_relation_name_does_not_crash(self):
        check = BitmapHeapScanCheck(params={"threshold_rows": "100000"})
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Bitmap Heap Scan",
            "Actual Rows": 200_000,
        })
        issues = check.check(node)
        assert len(issues) == 1
        assert "?" in issues[0].message

class TestIndexOnlyScanCheck:
    DEFAULTS = {"min_rows": "100", "heap_fetch_ratio": "0.10"}

    def _check(self, **overrides) -> IndexOnlyScanCheck:
        return IndexOnlyScanCheck(params={**self.DEFAULTS, **overrides})

    def _node(self, **fields) -> PlanNode:
        return PlanNode(depth=0, path="0:0",
                        fields={"Node Type": "Index Only Scan", **fields})

    def test_zero_heap_fetches_is_ok(self):
        node = self._node(**{
            "Index Name": "idx_a",
            "Relation Name": "t",
            "Actual Rows": 10_000,
            "Heap Fetches": 0,
        })
        assert self._check().check(node) == []

    def test_below_min_rows_is_ok(self):
        node = self._node(**{
            "Actual Rows": 50,
            "Heap Fetches": 45,
        })
        assert self._check().check(node) == []

    def test_low_fetch_ratio_is_ok(self):
        """10k rows, 500 fetches → ratio 0.05 < 0.10 → silent."""
        node = self._node(**{
            "Actual Rows": 10_000,
            "Heap Fetches": 500,
        })
        assert self._check().check(node) == []

    def test_stale_vm_is_reported(self):
        node = self._node(**{
            "Index Name": "idx_orders_status",
            "Relation Name": "orders",
            "Actual Rows": 50_000,
            "Heap Fetches": 45_000,
        })
        issues = self._check().check(node)
        assert len(issues) == 1
        assert issues[0].type == "index_only_scan_stale_vm"
        assert issues[0].severity == "warning"
        assert "idx_orders_status" in issues[0].message
        assert "VACUUM" in issues[0].message
        assert "45000 heap fetches" in issues[0].message

    def test_ratio_above_one_is_reported(self):
        """440 rows, 594 fetches → ratio 1.35."""
        node = self._node(**{
            "Index Name": "idx_x",
            "Relation Name": "t",
            "Actual Rows": 440,
            "Heap Fetches": 594,
        })
        issues = self._check().check(node)
        assert len(issues) == 1
        assert "135.0%" in issues[0].message

    def test_index_scan_is_ignored(self):
        node = PlanNode(depth=0, path="0:0", fields={
            "Node Type": "Index Scan",
            "Actual Rows": 20_000,
            "Shared Read Blocks": 8_000,
        })
        assert self._check().check(node) == []

    def test_zero_actual_rows_is_ignored(self):
        node = self._node(**{
            "Actual Rows": 0,
            "Heap Fetches": 500,
        })
        assert self._check().check(node) == []

class TestIndexRegularScanCheck:
    def test_few_blocks_is_ok(self):
        check = IndexRegularScanCheck()
        node = {
            "Node Type": "Index Scan",
            "Actual Rows": 5000,
            "Shared Read Blocks": 0,
        }
        assert check.check(node) == []

    def test_below_min_rows_is_ok(self):
        check = IndexRegularScanCheck()
        node = {
            "Node Type": "Index Scan",
            "Actual Rows": 500,
            "Shared Read Blocks": 9000,
        }
        assert check.check(node) == []

    def test_poor_clustering_is_reported(self):
        check = IndexRegularScanCheck()
        node = {
            "Node Type": "Index Scan",
            "Index Name": "idx_users_email",
            "Relation Name": "users",
            "Actual Rows": 20_000,
            "Shared Read Blocks": 8_000,
        }
        issues = check.check(node)
        assert len(issues) == 1
        assert issues[0].type == "index_scan_poor_clustering"
        assert issues[0].severity == "info"
        assert "CLUSTER" in issues[0].message

    def test_index_only_scan_is_ignored(self):
        check = IndexRegularScanCheck()
        node = {
            "Node Type": "Index Only Scan",
            "Actual Rows": 20_000,
            "Heap Fetches": 18_000,
        }
        assert check.check(node) == []

    def test_gather_info_returns_none_without_rows(self):
        check = IndexRegularScanCheck()
        assert check.gather_info({"Node Type": "Index Scan"}) is None

    def test_validate_rule_threshold(self):
        check = IndexRegularScanCheck(min_rows=100, min_disk_blocks=50)
        assert check.validate_rule({"actual_rows": 100, "read_blocks": 50}) is True
        assert check.validate_rule({"actual_rows": 99, "read_blocks": 999}) is False
        assert check.validate_rule({"actual_rows": 1000, "read_blocks": 49}) is False

class TestPartitionPruningCheck:
    def test_append_with_few_children_is_ok(self):
        node = {
            "Node Type": "Append",
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "t_p06"},
                {"Node Type": "Seq Scan", "Relation Name": "t_p07"},
                {"Node Type": "Seq Scan", "Relation Name": "t_p08"},
            ],
        }
        assert PartitionPruningCheck().check(node) == []

    def test_append_at_threshold_is_ok(self):
        node = {
            "Node Type": "Append",
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": f"t_p{i:02d}"}
                for i in range(1, 4)
            ],
        }
        assert PartitionPruningCheck().check(node) == []

    def test_append_with_many_children_is_reported(self):
        node = {
            "Node Type": "Append",
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": f"t_p{i:02d}"}
                for i in range(1, 13)
            ],
        }
        issues = PartitionPruningCheck().check(node)
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert issues[0].type == "partition_pruning"
        assert "12 partitions" in issues[0].message
        assert "t_p01" in issues[0].message
        assert "consequence, not stale statistics" in issues[0].message
        assert "ANALYZE will not help" in issues[0].message

    def test_merge_append_is_also_checked(self):
        node = {
            "Node Type": "Merge Append",
            "Plans": [{"Node Type": "Seq Scan"} for _ in range(10)],
        }
        issues = PartitionPruningCheck().check(node)
        assert len(issues) == 1
        assert issues[0].type == "partition_pruning"

    def test_non_append_node_is_ignored(self):
        node = {
            "Node Type": "Seq Scan",
            "Plans": [{} for _ in range(50)],
        }
        assert PartitionPruningCheck().check(node) == []

    def test_custom_threshold(self):
        node = {
            "Node Type": "Append",
            "Plans": [{"Node Type": "Seq Scan"} for _ in range(5)],
        }
        assert PartitionPruningCheck(max_children=10).check(node) == []
        assert len(PartitionPruningCheck(max_children=3).check(node)) == 1

    def test_children_without_relation_name(self):
        node = {
            "Node Type": "Append",
            "Plans": [{"Node Type": "Seq Scan"} for _ in range(12)],
        }
        issues = PartitionPruningCheck().check(node)
        assert len(issues) == 1
        assert "Scanned: " in issues[0].message

    # ----- phase tests --------------------------------------------------

    def test_gather_info_returns_none_for_wrong_node(self):
        check = PartitionPruningCheck()
        assert check.gather_info({"Node Type": "Seq Scan"}) is None

    def test_gather_info_for_empty_append(self):
        check = PartitionPruningCheck()
        info = check.gather_info({"Node Type": "Append", "Plans": []})
        assert info == {"count": 0, "names": []}

    def test_gather_info_collects_relation_names(self):
        check = PartitionPruningCheck()
        node = {
            "Node Type": "Merge Append",
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "t_p01"},
                {"Node Type": "Seq Scan", "Relation Name": "t_p02"},
                {"Node Type": "Seq Scan"},
            ],
        }
        info = check.gather_info(node)
        assert info["count"] == 3
        assert info["names"] == ["t_p01", "t_p02"]

    def test_validate_rule_threshold(self):
        check = PartitionPruningCheck(max_children=3)
        assert check.validate_rule({"count": 4, "names": []}) is True
        assert check.validate_rule({"count": 3, "names": []}) is False

    def test_generate_msg_short_list(self):
        check = PartitionPruningCheck(max_children=3)
        info = {"count": 5, "names": ["a", "b", "c"]}
        msg = check.generate_msg(info)[0].message
        assert "Append over 5 partitions" in msg
        assert "Scanned: a, b, c" in msg

    def test_generate_msg_long_list_is_truncated(self):
        check = PartitionPruningCheck(max_children=3)
        info = {
            "count": 12,
            "names": [f"t_p{i:02d}" for i in range(1, 13)],
        }
        msg = check.generate_msg(info)[0].message
        assert "Append over 12 partitions" in msg
        assert "t_p01, t_p02, t_p03" in msg
        assert "… (+9 more)" in msg

class TestNonSargableCheck:
    """Tests for NonSargableCheck.

    Indexes are loaded lazily from the database via ``get_indexes``.
    Tests monkey-patch that function so they don't need a live DB.
    """

    @staticmethod
    def _patch_indexes(monkeypatch, rows: list[dict]) -> None:
        import pg_explain_mcp.analyzer as analyzer
        monkeypatch.setattr(analyzer, "get_indexes", lambda: rows)

    @staticmethod
    def _node(**fields) -> PlanNode:
        return PlanNode(
            depth=0,
            path="0:0",
            fields={"Node Type": "Seq Scan", **fields},
        )

    # ----- negative cases --------------------------------------------------

    def test_sargable_predicate_is_ok(self, monkeypatch):
        self._patch_indexes(monkeypatch, [
            {"table_name": "users",
             "leading_attnum": 2, "plain_columns": ["email"]},
        ])
        check = NonSargableCheck(params={"threshold_rows": "1000"})
        node = self._node(**{
            "Relation Name": "users",
            "Actual Rows": 100,
            "Rows Removed by Filter": 9900,
            "Filter": "(email = 'x'::text)",
        })
        assert check.check(node) == []

    def test_no_index_on_column_is_ok(self, monkeypatch):
        """No index on the column — SeqScanCheck's job, not ours."""
        self._patch_indexes(monkeypatch, [
            {"table_name": "users",
             "leading_attnum": 1, "plain_columns": ["id"]},
        ])
        check = NonSargableCheck(params={"threshold_rows": "1000"})
        node = self._node(**{
            "Relation Name": "users",
            "Actual Rows": 100,
            "Rows Removed by Filter": 9900,
            "Filter": "(lower(email) = 'x'::text)",
        })
        assert check.check(node) == []

    def test_non_leading_column_is_ignored(self, monkeypatch):
        """Index (status, email) — email is second; rewrite won't help."""
        self._patch_indexes(monkeypatch, [
            {"table_name": "users",
             "leading_attnum": 1, "plain_columns": ["status", "email"]},
        ])
        check = NonSargableCheck(params={"threshold_rows": "1000"})
        node = self._node(**{
            "Relation Name": "users",
            "Actual Rows": 100,
            "Rows Removed by Filter": 9900,
            "Filter": "(lower(email) = 'x'::text)",
        })
        assert check.check(node) == []

    def test_functional_index_is_ignored(self, monkeypatch):
        self._patch_indexes(monkeypatch, [
            {"table_name": "users",
             "leading_attnum": 0, "plain_columns": []},
        ])
        check = NonSargableCheck(params={"threshold_rows": "1000"})
        node = self._node(**{
            "Relation Name": "users",
            "Actual Rows": 100,
            "Rows Removed by Filter": 9900,
            "Filter": "(lower(email) = 'x'::text)",
        })
        assert check.check(node) == []

    def test_small_scan_is_ignored(self, monkeypatch):
        self._patch_indexes(monkeypatch, [
            {"table_name": "users",
             "leading_attnum": 2, "plain_columns": ["email"]},
        ])
        check = NonSargableCheck(params={"threshold_rows": "1000"})
        node = self._node(**{
            "Relation Name": "users",
            "Actual Rows": 5,
            "Rows Removed by Filter": 5,
            "Filter": "(lower(email) = 'x'::text)",
        })
        assert check.check(node) == []

    def test_no_filter_is_ignored(self, monkeypatch):
        self._patch_indexes(monkeypatch, [
            {"table_name": "users",
             "leading_attnum": 2, "plain_columns": ["email"]},
        ])
        check = NonSargableCheck(params={"threshold_rows": "1000"})
        node = self._node(**{
            "Relation Name": "users",
            "Actual Rows": 5000,
        })
        assert check.check(node) == []

    # ----- positive cases --------------------------------------------------

    def test_lower_with_index_is_reported(self, monkeypatch):
        self._patch_indexes(monkeypatch, [
            {"table_name": "users",
             "leading_attnum": 2, "plain_columns": ["email"]},
        ])
        check = NonSargableCheck(params={"threshold_rows": "1000"})
        node = self._node(**{
            "Relation Name": "users",
            "Actual Rows": 100,
            "Rows Removed by Filter": 9900,
            "Filter": "(lower(email) = 'x'::text)",
        })
        issues = check.check(node)
        assert len(issues) == 1
        assert issues[0].type == "non_sargable"
        assert issues[0].severity == "info"
        assert "lower" in issues[0].message
        assert "email" in issues[0].message

class TestSummarizePlanNode:
    FIELDS = {
        "Relation Name":  "relation",
        "Index Name":     "index",
        "Actual Rows":    "actual_rows",
        "Actual Loops":   "actual_loops",
        "Plan Rows":      "plan_rows",
        "Parallel Aware": "parallel_aware",
        "Sort Method":    "sort_method",
    }

    def test_simple_node(self):
        node = {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Actual Rows": 5000,
            "Plan Rows": 4800,
        }
        result = summarize_plan_node(node, self.FIELDS)
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
        result = summarize_plan_node(node, self.FIELDS)
        assert len(result) == 3
        assert result[0]["node_type"] == "Hash Join"
        assert result[1]["depth"] == 1
        assert result[2]["depth"] == 1
        assert result[2]["index"] == "idx_b"

    def test_optional_fields_are_skipped(self):
        node = {"Node Type": "Limit", "Actual Rows": 10}
        result = summarize_plan_node(node, self.FIELDS)
        assert "relation" not in result[0]
        assert "index" not in result[0]
        assert "heap_fetches" not in result[0]

    def test_unknown_fields_in_mapping_are_ignored(self):
        """Fields in the mapping that aren't in the node don't appear."""
        node = {"Node Type": "Seq Scan"}
        fields = {"This Field Does Not Exist": "nonexistent"}
        result = summarize_plan_node(node, fields)
        assert result[0] == {"depth": 0, "node_type": "Seq Scan"}

    def test_empty_mapping_returns_only_header(self):
        node = {"Node Type": "Seq Scan", "Actual Rows": 5000}
        result = summarize_plan_node(node, {})
        assert result == [{"depth": 0, "node_type": "Seq Scan"}]

    def test_key_order_is_stable(self):
        node = {"Node Type": "Seq Scan", "Relation Name": "t"}
        result = summarize_plan_node(node, self.FIELDS)
        keys = list(result[0].keys())
        assert keys[:2] == ["depth", "node_type"]

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
        result = analyze_plan([], checks=())
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
        result = analyze_plan(plan, checks=ALL_CHECKS)
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
        result = analyze_plan(plan, checks=ALL_CHECKS)
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
        result = analyze_plan(plan, checks=ALL_CHECKS)
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
        result = analyze_plan(plan, checks=ALL_CHECKS)
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
        result = analyze_plan(plan, checks=ALL_CHECKS)
        assert result["issue_count"] == 1
        assert result["issues"][0]["type"] == "index_only_scan_stale_vm"

    def test_checks_applied_is_present(self):
        plan = [{
            "Plan": {"Node Type": "Seq Scan", "Actual Rows": 100},
            "Execution Time": 1.0,
            "Planning Time": 0.1,
        }]
        result = analyze_plan(plan, checks=(SeqScanCheck(),))
        assert result["checks_applied"] == ["SeqScanCheck"]

# ---------------------------------------------------------------------------
# list_indexes formatting
# ---------------------------------------------------------------------------

class TestFormatIndexes:
    def test_empty_list(self):
        assert _format_indexes([]) == "No indexes found."

    def test_regular_index(self):
        rows = [{
            "schema_name": "public",
            "table_name": "orders",
            "index_name": "idx_orders_status",
            "index_def": "CREATE INDEX idx_orders_status ON orders USING btree (status)",
            "is_unique": False,
            "is_primary": False,
            "is_functional": False,
            "leading_attnum": 1,
            "plain_columns": ["status"],
        }]
        result = _format_indexes(rows)
        assert "idx_orders_status" in result
        assert "[INDEX]" in result
        assert "status" in result

    def test_unique_index(self):
        rows = [{
            "schema_name": "public",
            "table_name": "users",
            "index_name": "idx_users_email",
            "index_def": (
                "CREATE UNIQUE INDEX idx_users_email",
                " ON users USING btree (email)"
            ),
            "is_unique": True,
            "is_primary": False,
            "is_functional": False,
            "leading_attnum": 2,
            "plain_columns": ["email"],
        }]
        assert "[UNIQUE]" in _format_indexes(rows)

    def test_primary_key(self):
        rows = [{
            "schema_name": "public",
            "table_name": "users",
            "index_name": "users_pkey",
            "index_def": (
                "CREATE UNIQUE INDEX users_pkey ON"
                " users USING btree (id)"
            ),
            "is_unique": True,
            "is_primary": True,
            "is_functional": False,
            "leading_attnum": 1,
            "plain_columns": ["id"],
        }]
        assert "[PRIMARY KEY]" in _format_indexes(rows)

    def test_functional_index(self):
        rows = [{
            "schema_name": "public",
            "table_name": "users",
            "index_name": "idx_lower_email",
            "index_def": (
                 "CREATE INDEX idx_lower_email ON "
                 "public.users USING btree (lower(email))"
            ),
            "is_unique": False,
            "is_primary": False,
            "is_functional": True,
            "leading_attnum": 0,
            "plain_columns": [],
        }]
        result = _format_indexes(rows)
        assert "[FUNCTIONAL]" in result
        assert "lower(email)" in result

    def test_composite_index(self):
        rows = [{
            "schema_name": "public",
            "table_name": "orders",
            "index_name": "idx_orders_status_created",
            "index_def": (
                "CREATE INDEX idx_orders_status_created ON orders "
                "USING btree (status, created_at)"
            ),
            "is_unique": False,
            "is_primary": False,
            "is_functional": False,
            "leading_attnum": 1,
            "plain_columns": ["status", "created_at"],
        }]
        assert "status, created_at" in _format_indexes(rows)

class TestParsePlan:
    def test_empty(self):
        assert parse_plan([]) == []

    def test_single_node(self):
        plan = [{
            "Plan": {
                "Node Type": "Limit",
                "Plan Rows": 100,
                "Actual Rows": 100,
            }
        }]
        nodes = parse_plan(plan)
        assert len(nodes) == 1
        n = nodes[0]
        assert n["id"] == 0
        assert n["parent_id"] is None
        assert n["depth"] == 0
        assert n["children_ids"] == []
        assert n["Node Type"] == "Limit"
        assert n["Plan Rows"] == 100
        assert n["Actual Rows"] == 100

    def test_two_levels(self):
        plan = [{
            "Plan": {
                "Node Type": "Limit",
                "Plans": [
                    {"Node Type": "Index Scan", "Relation Name": "t"},
                ],
            }
        }]
        nodes = parse_plan(plan)
        assert len(nodes) == 2
        assert nodes[0]["children_ids"] == [1]
        assert nodes[1]["parent_id"] == 0
        assert nodes[1]["depth"] == 1

    def test_preorder_traversal(self):
        plan = [{
            "Plan": {
                "Node Type": "A",
                "Plans": [
                    {"Node Type": "B", "Plans": [{"Node Type": "D"}]},
                    {"Node Type": "C"},
                ],
            }
        }]
        nodes = parse_plan(plan)
        assert [n["Node Type"] for n in nodes] == ["A", "B", "D", "C"]
        assert nodes[0]["children_ids"] == [1, 3]
        assert nodes[1]["children_ids"] == [2]

    def test_plans_field_not_preserved(self):
        plan = [{"Plan": {"Node Type": "X", "Plans": [{"Node Type": "Y"}]}}]
        nodes = parse_plan(plan)
        assert "Plans" not in nodes[0]
        assert "Plans" not in nodes[1]

    def test_all_fields_preserved(self):
        plan = [{
            "Plan": {
                "Node Type": "Index Scan",
                "Relation Name": "t",
                "Index Name": "idx_t",
                "Startup Cost": 0.42,
                "Total Cost": 100.5,
                "Actual Loops": 1,
                "Shared Read Blocks": 42,
                "Custom Field": "custom",
            }
        }]
        n = parse_plan(plan)[0]
        assert n["Relation Name"] == "t"
        assert n["Index Name"] == "idx_t"
        assert n["Startup Cost"] == 0.42
        assert n["Total Cost"] == 100.5
        assert n["Shared Read Blocks"] == 42
        assert n["Custom Field"] == "custom"

    def test_root_path(self):
        plan = [{"Plan": {"Node Type": "Limit"}}]
        assert parse_plan(plan)[0]["path"] == "0:0"

    def test_paths_in_siblings(self):
        plan = [{
            "Plan": {
                "Node Type": "Hash Join",
                "Plans": [
                    {"Node Type": "Seq Scan"},
                    {"Node Type": "Hash", "Plans": [{"Node Type": "Seq Scan"}]},
                ],
            }
        }]
        paths = [n["path"] for n in parse_plan(plan)]
        assert paths == [
            "0:0",
            "0:0/1:0",
            "0:0/1:1",
            "0:0/1:1/2:0",
        ]

class TestFormatPlanTree:
    def test_empty(self):
        assert format_plan_tree([]) == "(empty plan)"

    def test_single_node(self):
        nodes = parse_plan([{
            "Plan": {"Node Type": "Limit", "Plan Rows": 100}
        }])
        text = format_plan_tree(nodes, marker_tabs=1)
        lines = text.split("\n")
        assert lines[0] == "Limit\t[0:0]"
        assert "Plan Rows: 100" in text

    def test_child_is_indented(self):
        nodes = parse_plan([{
            "Plan": {
                "Node Type": "Limit",
                "Plan Rows": 100,
                "Plans": [{"Node Type": "Index Scan", "Plan Rows": 500}],
            }
        }])
        text = format_plan_tree(nodes, marker_tabs=1)
        lines = text.split("\n")
        assert lines[0] == "Limit\t[0:0]"
        child_line = next(line for line in lines if "Index Scan" in line)
        assert child_line == "  Index Scan\t[1:0]"

    def test_structural_fields_not_printed(self):
        nodes = parse_plan([{
            "Plan": {"Node Type": "Limit", "Plan Rows": 100}
        }])
        text = format_plan_tree(nodes)
        assert "id:" not in text
        assert "parent_id:" not in text
        assert "depth:" not in text
        assert "children_ids:" not in text

    def test_siblings_are_distinguished(self):
        nodes = parse_plan([{
            "Plan": {
                "Node Type": "Hash Join",
                "Plans": [
                    {"Node Type": "Seq Scan"},
                    {"Node Type": "Seq Scan"},
                ],
            }
        }])
        text = format_plan_tree(nodes, marker_tabs=1)
        lines = text.split("\n")
        seq_lines = [line for line in lines if "Seq Scan" in line]
        assert seq_lines[0] == "  Seq Scan\t[1:0]"
        assert seq_lines[1] == "  Seq Scan\t[1:1]"

    def test_parse_plan_keeps_zeros(self):
        """parse_plan is honest — zero fields stay."""
        plan = [{"Plan": {
            "Node Type": "Limit",
            "Actual Rows": 10,
            "Shared Read Blocks": 0,
            "Temp Read Blocks": 0,
        }}]
        node = parse_plan(plan)[0]
        assert node["Shared Read Blocks"] == 0
        assert node["Temp Read Blocks"] == 0

    def test_depth_increases_with_nesting(self):
        nodes = parse_plan([{
            "Plan": {
                "Node Type": "Limit",
                "Plans": [{
                    "Node Type": "Sort",
                    "Plans": [{"Node Type": "Seq Scan"}],
                }],
            }
        }])
        text = format_plan_tree(nodes, marker_tabs=1)
        lines = text.split("\n")

        # Root — depth 0, path 0:0
        assert lines[0] == "Limit\t[0:0]"

        # Sort — child of root, first (and only) sibling
        sort_line = next(line for line in lines if "Sort" in line)
        assert sort_line == "  Sort\t[1:0]"

        # Seq Scan — grandchild, first (and only) sibling
        scan_line = next(line for line in lines if "Seq Scan" in line)
        assert scan_line == "    Seq Scan\t[2:0]"

class TestFilteredParsePlan:
    def test_drops_zero_numeric(self):
        plan = [{"Plan": {
            "Node Type": "Limit",
            "Actual Rows": 10,
            "Shared Read Blocks": 0,
            "Shared Hit Blocks": 5,
        }}]
        node = filtered_parse_plan(plan)[0]
        assert "Shared Read Blocks" not in node
        assert node["Shared Hit Blocks"] == 5

    def test_keeps_booleans(self):
        plan = [{"Plan": {
            "Node Type": "Limit",
            "Parallel Aware": False,
            "Async Capable": False,
        }}]
        node = filtered_parse_plan(plan)[0]
        assert node["Parallel Aware"] is False
        assert node["Async Capable"] is False

    def test_keeps_empty_children_ids(self):
        plan = [{"Plan": {"Node Type": "Index Scan"}}]
        node = filtered_parse_plan(plan)[0]
        assert node["children_ids"] == []

    def test_keeps_parent_id_null(self):
        plan = [{"Plan": {"Node Type": "Limit"}}]
        node = filtered_parse_plan(plan)[0]
        assert node["parent_id"] is None

    def test_does_not_drop_false_like_zero(self):
        """False is not a numeric zero — must survive."""
        plan = [{"Plan": {"Node Type": "Limit", "Disabled": False}}]
        node = filtered_parse_plan(plan)[0]
        assert node["Disabled"] is False

    def test_structural_fields_always_present(self):
        plan = [{"Plan": {
            "Node Type": "Index Scan",
            "Actual Rows": 0,   # dropped as zero
        }}]
        node = filtered_parse_plan(plan)[0]
        # structural fields are added after filtering — always there
        assert node["id"] == 0
        assert node["depth"] == 0
        assert "children_ids" in node
        assert "parent_id" in node

    def test_policy_mode_zero_hides_field(self):
        plan = [{"Plan": {
            "Node Type": "Limit",
            "Parallel Aware": True,   # even True is hidden
            "Actual Rows": 10,
        }}]
        node = filtered_parse_plan(plan, field_policy={"Parallel Aware": 0})[0]
        assert "Parallel Aware" not in node
        assert node["Actual Rows"] == 10

    def test_policy_mode_one_keeps_zero(self):
        plan = [{"Plan": {
            "Node Type": "Index Only Scan",
            "Heap Fetches": 0,
            "Shared Read Blocks": 0,
        }}]
        policy = {"Heap Fetches": 1, "Shared Read Blocks": 1}
        node = filtered_parse_plan(plan, field_policy=policy)[0]
        assert node["Heap Fetches"] == 0
        assert node["Shared Read Blocks"] == 0

    def test_policy_mode_999_drops_zero(self):
        plan = [{"Plan": {
            "Node Type": "Limit",
            "Sort Space Used": 0,
            "Sort Space Used Other": 5,   # sanity: non-zero kept
        }}]
        node = filtered_parse_plan(plan, field_policy={"Sort Space Used": 999})[0]
        assert "Sort Space Used" not in node
        assert node["Sort Space Used Other"] == 5

    def test_unknown_field_defaults_to_999(self):
        """Fields not in the policy are treated as mode 999."""
        plan = [{"Plan": {"Node Type": "X", "Mystery": 0, "Known": 0}}]
        node = filtered_parse_plan(plan, field_policy={"Known": 1})[0]
        assert "Mystery" not in node      # 999 → dropped
        assert node["Known"] == 0          # 1 → kept

