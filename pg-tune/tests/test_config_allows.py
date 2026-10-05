"""Tests for the tune_allows CRUD API."""

import pytest
from pg_tune.config import (
    init_db,
    is_allowed,
    list_allows,
    reset_allow,
    set_allow,
)


class TestListAllows:
    def test_all_seed_actions_present(self, tune_db):
        rows = list_allows(tune_db)
        names = {r["action"] for r in rows}
        assert names == {
            "backup",
            "restore",
            "set_session_guc",
            "analyze",
            "create_index",
            "create_index_concurrent",
            "drop_own_objects",
        }

    def test_sorted_alphabetically(self, tune_db):
        names = [r["action"] for r in list_allows(tune_db)]
        assert names == sorted(names)

    def test_row_shape(self, tune_db):
        for r in list_allows(tune_db):
            assert set(r.keys()) == {
                "action", "enabled", "default_enabled", "description"
            }

    def test_backup_enabled_by_default(self, tune_db):
        rows = list_allows(tune_db)
        backup = next(r for r in rows if r["action"] == "backup")
        assert backup["enabled"] == 1
        assert backup["default_enabled"] == 1

    def test_everything_else_disabled_by_default(self, tune_db):
        for r in list_allows(tune_db):
            if r["action"] == "backup":
                continue
            assert r["enabled"] == 0, r["action"]
            assert r["default_enabled"] == 0, r["action"]


class TestSetAllow:
    def test_enable_returns_old_and_new(self, tune_db):
        result = set_allow("restore", True, tune_db)
        assert result["action"] == "restore"
        assert result["old"] == 0
        assert result["new"] == 1

    def test_disable_returns_old_and_new(self, tune_db):
        result = set_allow("backup", False, tune_db)
        assert result["old"] == 1
        assert result["new"] == 0

    def test_idempotent(self, tune_db):
        set_allow("restore", True, tune_db)
        result = set_allow("restore", True, tune_db)
        assert result["old"] == 1
        assert result["new"] == 1

    def test_int_accepted(self, tune_db):
        assert set_allow("restore", 1, tune_db)["new"] == 1
        assert set_allow("restore", 0, tune_db)["new"] == 0

    def test_unknown_action_raises(self, tune_db):
        with pytest.raises(KeyError, match="no_such"):
            set_allow("no_such", True, tune_db)

    def test_persists_to_db(self, tune_db):
        set_allow("restore", True, tune_db)
        rows = list_allows(tune_db)
        restore = next(r for r in rows if r["action"] == "restore")
        assert restore["enabled"] == 1

    def test_is_allowed_reflects_change(self, tune_db):
        assert not is_allowed("restore", tune_db)
        set_allow("restore", True, tune_db)
        assert is_allowed("restore", tune_db)


class TestResetAllow:
    def test_reset_single_changed(self, tune_db):
        set_allow("restore", True, tune_db)
        changes = reset_allow("restore", tune_db)
        assert len(changes) == 1
        c = changes[0]
        assert c["action"] == "restore"
        assert c["old"] == 1
        assert c["new"] == 0

    def test_reset_single_at_default_is_noop(self, tune_db):
        assert reset_allow("restore", tune_db) == []

    def test_reset_all_changed(self, tune_db):
        set_allow("restore", True, tune_db)
        set_allow("analyze", True, tune_db)
        set_allow("backup", False, tune_db)

        changes = reset_allow("", tune_db)
        actions = {c["action"] for c in changes}
        assert actions == {"restore", "analyze", "backup"}

    def test_reset_all_clean_is_noop(self, tune_db):
        assert reset_allow("", tune_db) == []

    def test_reset_all_restores_backup(self, tune_db):
        set_allow("backup", False, tune_db)
        reset_allow("", tune_db)
        assert is_allowed("backup", tune_db)

    def test_reset_default_arg_means_all(self, tune_db):
        # Calling reset_allow with no action argument — but a custom
        # db_path — should reset everything, not one row.
        set_allow("restore", True, tune_db)
        changes = reset_allow(db_path=tune_db)
        assert len(changes) == 1

    def test_unknown_action_raises(self, tune_db):
        with pytest.raises(KeyError):
            reset_allow("no_such", tune_db)


class TestEnsureDb:
    """Every entry point lazily creates the db if it is missing."""

    def test_list_allows_creates_db(self, tmp_path):
        db = tmp_path / "sub" / "new.db"
        assert not db.exists()
        rows = list_allows(db)
        assert rows
        assert db.exists()


class TestInitDb:
    """Document current init_db semantics; change if the design does."""

    def test_reinit_drops_allow_changes(self, tune_db):
        set_allow("restore", True, tune_db)
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
        # Current behavior: --init wipes everything, including
        # backup metadata. If we later change init_db to preserve
        # tune_backups (like pg-explain does for config_audit),
        # flip this assertion.
        assert n == 0

