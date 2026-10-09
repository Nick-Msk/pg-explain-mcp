"""Tests for tune_settings CRUD API."""

import pytest
from pg_tune.config import (
    get_setting,
    init_db,
    is_allowed,
    list_settings,
    reset_setting,
    set_setting,
)

ALLOW_NAMES = {
    "backup",
    "restore",
    "set_session_guc",
    "analyze",
    "create_index",
    "create_index_concurrent",
    "drop_own_objects",
}

SETTINGS_NAMES = {
    "default_cold_run",
    "default_max_iters",
    "default_dry_run",
}


# ---------------------------------------------------------------------------
# list_settings
# ---------------------------------------------------------------------------


class TestListSettings:
    def test_all_rows_present(self, tune_db):
        rows = list_settings(db_path=tune_db)
        names = {(r["category"], r["name"]) for r in rows}
        assert names == (
            {("ALLOWS", n) for n in ALLOW_NAMES}
            | {("SETTINGS", n) for n in SETTINGS_NAMES}
        )

    def test_filter_allows(self, tune_db):
        rows = list_settings("ALLOWS", tune_db)
        assert {r["name"] for r in rows} == ALLOW_NAMES
        assert all(r["category"] == "ALLOWS" for r in rows)

    def test_filter_setting(self, tune_db):
        rows = list_settings("SETTINGS", tune_db)
        assert {r["name"] for r in rows} == SETTINGS_NAMES
        assert all(r["category"] == "SETTINGS" for r in rows)

    def test_row_shape(self, tune_db):
        for r in list_settings(db_path=tune_db):
            assert set(r.keys()) == {
                "category", "name", "value",
                "default_value", "desc",
            }

    def test_sorted_by_category_then_name(self, tune_db):
        rows = list_settings(db_path=tune_db)
        keys = [(r["category"], r["name"]) for r in rows]
        assert keys == sorted(keys)

    def test_unknown_category_returns_empty(self, tune_db):
        # list_settings does not validate the category — an unknown
        # one simply matches no rows.
        assert list_settings("NOPE", tune_db) == []

    def test_backup_enabled_by_default(self, tune_db):
        rows = list_settings("ALLOWS", tune_db)
        backup = next(r for r in rows if r["name"] == "backup")
        assert backup["value"] == "1"
        assert backup["default_value"] == "1"

    def test_everything_else_disabled_by_default(self, tune_db):
        rows = list_settings("ALLOWS", tune_db)
        for r in rows:
            if r["name"] == "backup":
                continue
            assert r["value"] == "0", r["name"]
            assert r["default_value"] == "0", r["name"]


# ---------------------------------------------------------------------------
# get_setting
# ---------------------------------------------------------------------------


class TestGetSetting:
    def test_existing(self, tune_db):
        assert get_setting("ALLOWS", "backup", tune_db) == "1"
        assert get_setting("ALLOWS", "restore", tune_db) == "0"
        assert get_setting(
            "SETTINGS", "default_max_iters", tune_db
        ) == "30"

    def test_missing_name_returns_none(self, tune_db):
        assert get_setting("ALLOWS", "no_such", tune_db) is None


# ---------------------------------------------------------------------------
# set_setting
# ---------------------------------------------------------------------------


class TestSetSetting:
    def test_enable_returns_old_and_new(self, tune_db):
        r = set_setting("ALLOWS", "restore", "1", tune_db)
        assert r["category"] == "ALLOWS"
        assert r["name"] == "restore"
        assert r["old"] == "0"
        assert r["new"] == "1"

    def test_disable_returns_old_and_new(self, tune_db):
        r = set_setting("ALLOWS", "backup", "0", tune_db)
        assert r["old"] == "1"
        assert r["new"] == "0"

    def test_idempotent(self, tune_db):
        set_setting("ALLOWS", "restore", "1", tune_db)
        r = set_setting("ALLOWS", "restore", "1", tune_db)
        assert r["old"] == "1"
        assert r["new"] == "1"

    def test_unknown_name_raises(self, tune_db):
        with pytest.raises(KeyError, match="no_such"):
            set_setting("ALLOWS", "no_such", "1", tune_db)

    def test_unknown_category_returns_none(self, tune_db):
        assert get_setting("NOPE", "backup", tune_db) is None

    def test_persists(self, tune_db):
        set_setting("ALLOWS", "restore", "1", tune_db)
        assert get_setting("ALLOWS", "restore", tune_db) == "1"

    def test_setting_category_raw_value(self, tune_db):
        r = set_setting("SETTINGS", "default_cold_run", "5", tune_db)
        assert r["old"] == "3"
        assert r["new"] == "5"
        assert get_setting("SETTINGS", "default_cold_run", tune_db) == "5"

    def test_is_allowed_reflects_change(self, tune_db):
        assert not is_allowed("restore", tune_db)
        set_setting("ALLOWS", "restore", "1", tune_db)
        assert is_allowed("restore", tune_db)


# ---------------------------------------------------------------------------
# reset_setting
# ---------------------------------------------------------------------------


class TestResetSetting:
    def test_reset_single(self, tune_db):
        set_setting("ALLOWS", "restore", "1", tune_db)
        changes = reset_setting("ALLOWS", "restore", tune_db)
        assert len(changes) == 1
        c = changes[0]
        assert c["category"] == "ALLOWS"
        assert c["name"] == "restore"
        assert c["old"] == "1"
        assert c["new"] == "0"

    def test_reset_single_at_default_is_noop(self, tune_db):
        assert reset_setting("ALLOWS", "restore", tune_db) == []

    def test_reset_category(self, tune_db):
        set_setting("ALLOWS", "restore", "1", tune_db)
        set_setting("ALLOWS", "analyze", "1", tune_db)
        set_setting("ALLOWS", "backup", "0", tune_db)

        changes = reset_setting("ALLOWS", db_path=tune_db)
        names = {c["name"] for c in changes}
        assert names == {"restore", "analyze", "backup"}

    def test_reset_category_clean_is_noop(self, tune_db):
        assert reset_setting("ALLOWS", db_path=tune_db) == []

    def test_reset_everything(self, tune_db):
        set_setting("ALLOWS", "restore", "1", tune_db)
        set_setting("SETTINGS", "default_cold_run", "5", tune_db)

        changes = reset_setting(db_path=tune_db)
        cats = {(c["category"], c["name"]) for c in changes}
        assert cats == {
            ("ALLOWS", "restore"),
            ("SETTINGS", "default_cold_run"),
        }

    def test_reset_everything_clean_is_noop(self, tune_db):
        assert reset_setting(db_path=tune_db) == []

    def test_reset_unknown_name_raises(self, tune_db):
        with pytest.raises(KeyError, match="no_such"):
            reset_setting("ALLOWS", "no_such", tune_db)

    def test_reset_unknown_category_raises(self, tune_db):
        with pytest.raises(KeyError):
            reset_setting("NOPE", db_path=tune_db)


# ---------------------------------------------------------------------------
# init_db semantics
# ---------------------------------------------------------------------------


class TestInitDb:
    def test_reinit_drops_changes(self, tune_db):
        set_setting("ALLOWS", "restore", "1", tune_db)
        init_db(tune_db)
        assert not is_allowed("restore", tune_db)

    def test_reinit_drops_backup_metadata(self, tune_db):
        import sqlite3
        with sqlite3.connect(tune_db) as conn:
            conn.execute(
                "insert into tune_backups "
                "(database, path, size_bytes, sha256, "
                " pg_version, pg_explain_version) "
                "values ('x', '/tmp/x.dump', 0, 'abc', '18.6', '0.5.2')"
            )
            conn.commit()

        init_db(tune_db)

        with sqlite3.connect(tune_db) as conn:
            n = conn.execute(
                "select count(*) from tune_backups"
            ).fetchone()[0]
        # Current behavior: --init wipes everything.
        assert n == 0


# ---------------------------------------------------------------------------
# Lazy db creation
# ---------------------------------------------------------------------------


class TestEnsureDb:
    def test_list_settings_creates_db(self, tmp_path):
        db = tmp_path / "sub" / "new.db"
        assert not db.exists()
        rows = list_settings(db_path=db)
        assert rows
        assert db.exists()

