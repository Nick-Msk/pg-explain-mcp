"""Tests for pg_tune.vector.extract_vector."""

from pg_tune.config import list_vec_params
from pg_tune.vector import extract_vector

# Mirror of the seed. Kept inline so these tests are pure functions
# with no DB dependency.
FULL_PARAMS = [
    {"name": "ela_time",             "scope": "root_meta", "raw_key": "Execution Time"},
    {"name": "shared_hit_blocks",    "scope": "root_plan", "raw_key": "Shared Hit Blocks"},
    {"name": "shared_read_blocks",   "scope": "root_plan", "raw_key": "Shared Read Blocks"},
    {"name": "temp_read_blocks",     "scope": "root_plan", "raw_key": "Temp Read Blocks"},
    {"name": "temp_written_blocks",  "scope": "root_plan", "raw_key": "Temp Written Blocks"},
    {"name": "shared_i_o_read_time", "scope": "root_plan", "raw_key": "Shared I/O Read Time"},
    {"name": "temp_i_o_write_time",  "scope": "root_plan", "raw_key": "Temp I/O Write Time"},
]


class TestExtractVector:
    def test_empty_plan(self):
        assert extract_vector([], FULL_PARAMS) == {}

    def test_execution_time_only(self):
        plan = [{"Plan": {"Node Type": "Result"}, "Execution Time": 12.5}]
        assert extract_vector(plan, FULL_PARAMS) == {"ela_time": 12.5}

    def test_all_metrics(self):
        plan = [{
            "Plan": {
                "Node Type": "Seq Scan",
                "Shared Hit Blocks": 100,
                "Shared Read Blocks": 50,
                "Temp Read Blocks": 10,
                "Temp Written Blocks": 20,
                "Shared I/O Read Time": 1.5,
                "Temp I/O Write Time": 0.7,
            },
            "Execution Time": 42.0,
        }]
        assert extract_vector(plan, FULL_PARAMS) == {
            "ela_time": 42.0,
            "shared_hit_blocks": 100.0,
            "shared_read_blocks": 50.0,
            "temp_read_blocks": 10.0,
            "temp_written_blocks": 20.0,
            "shared_i_o_read_time": 1.5,
            "temp_i_o_write_time": 0.7,
        }

    def test_missing_metrics_are_omitted(self):
        plan = [{
            "Plan": {"Shared Hit Blocks": 5},
            "Execution Time": 1.0,
        }]
        v = extract_vector(plan, FULL_PARAMS)
        assert v["shared_hit_blocks"] == 5.0
        assert v["ela_time"] == 1.0
        assert "shared_read_blocks" not in v
        assert "temp_read_blocks" not in v
        assert "temp_written_blocks" not in v

    def test_zero_is_preserved(self):
        plan = [{
            "Plan": {"Shared Read Blocks": 0},
            "Execution Time": 1.0,
        }]
        v = extract_vector(plan, FULL_PARAMS)
        assert "shared_read_blocks" in v
        assert v["shared_read_blocks"] == 0.0

    def test_children_are_not_read(self):
        plan = [{
            "Plan": {
                "Node Type": "Gather",
                "Plans": [{
                    "Node Type": "Seq Scan",
                    "Shared Read Blocks": 999,
                }],
            },
            "Execution Time": 1.0,
        }]
        v = extract_vector(plan, FULL_PARAMS)
        assert "shared_read_blocks" not in v

    def test_no_execution_time(self):
        plan = [{"Plan": {"Node Type": "Result"}}]
        assert extract_vector(plan, FULL_PARAMS) == {}

    def test_none_value_is_skipped(self):
        plan = [{
            "Plan": {"Shared Read Blocks": None},
            "Execution Time": 1.0,
        }]
        assert "shared_read_blocks" not in extract_vector(plan, FULL_PARAMS)

    def test_int_becomes_float(self):
        plan = [{
            "Plan": {"Shared Hit Blocks": 42},
            "Execution Time": 1,
        }]
        v = extract_vector(plan, FULL_PARAMS)
        assert isinstance(v["shared_hit_blocks"], float)
        assert isinstance(v["ela_time"], float)
        assert v["shared_hit_blocks"] == 42.0
        assert v["ela_time"] == 1.0

    def test_no_plan_key(self):
        plan = [{"Execution Time": 5.0}]
        assert extract_vector(plan, FULL_PARAMS) == {"ela_time": 5.0}

    def test_plan_none_is_ok(self):
        plan = [{"Plan": None, "Execution Time": 1.0}]
        assert extract_vector(plan, FULL_PARAMS) == {"ela_time": 1.0}

    def test_empty_params_returns_empty(self):
        plan = [{"Plan": {"Shared Read Blocks": 5}, "Execution Time": 1.0}]
        assert extract_vector(plan, []) == {}

    def test_custom_params(self):
        """A custom mapping drives extraction — no hardcoded keys."""
        params = [
            {"name": "my_cost", "scope": "root_plan", "raw_key": "Total Cost"},
            {"name": "my_rows", "scope": "root_plan", "raw_key": "Plan Rows"},
        ]
        plan = [{"Plan": {"Total Cost": 123.45, "Plan Rows": 1000}}]
        assert extract_vector(plan, params) == {
            "my_cost": 123.45,
            "my_rows": 1000.0,
        }

    def test_unknown_scope_is_skipped(self):
        params = [{"name": "x", "scope": "somewhere", "raw_key": "X"}]
        plan = [{"Plan": {"X": 1}, "Execution Time": 1.0}]
        assert extract_vector(plan, params) == {}


class TestSeedContract:
    """The seed and tune_vec_params schema must agree.

    If someone edits the seed (renames a metric, adds a scope typo),
    this catches it before the metric silently drops out of every
    vector.
    """

    def test_seed_has_expected_names(self, tune_db):
        names = {r["name"] for r in list_vec_params(tune_db)}
        assert names == {
            "ela_time",
            "shared_hit_blocks",
            "shared_read_blocks",
            "temp_read_blocks",
            "temp_written_blocks",
            "shared_i_o_read_time",
            "temp_i_o_write_time",
        }

    def test_every_row_has_scope_and_raw_key(self, tune_db):
        for r in list_vec_params(tune_db):
            assert r["scope"] in ("root_meta", "root_plan"), r["name"]
            assert r["raw_key"], r["name"]

    def test_ela_time_reads_from_root_meta(self, tune_db):
        ela = next(
            r for r in list_vec_params(tune_db)
            if r["name"] == "ela_time"
        )
        assert ela["scope"] == "root_meta"
        assert ela["raw_key"] == "Execution Time"

