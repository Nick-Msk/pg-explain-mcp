"""Tests for the pg-tune-config CLI."""

import pytest

from pg_tune.cli import config_main


class TestInit:
    def test_creates_db(self, cli_db):
        assert not cli_db.exists()
        assert config_main(["--init"]) == 0
        assert cli_db.exists()


class TestShow:
    def test_show_all_contains_both_categories(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(["--show"]) == 0
        out = capsys.readouterr().out
        assert "ALLOWS:" in out
        assert "SETTINGS:" in out

    def test_show_allows_only(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(["--show", "ALLOWS"]) == 0
        out = capsys.readouterr().out
        assert "ALLOWS:" in out
        assert "SETTINGS:" not in out
        assert "restore" in out
        assert "default_cold_run" not in out

    def test_show_setting_only(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(["--show", "SETTINGS"]) == 0
        out = capsys.readouterr().out
        assert "SETTINGS:" in out
        assert "ALLOWS:" not in out
        assert "default_cold_run" in out
        assert "restore" not in out

    def test_allows_header(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        config_main(["--show", "ALLOWS"])
        lines = capsys.readouterr().out.splitlines()
        # First line is the category banner.
        assert lines[0] == "ALLOWS:"
        # Second line is the header row.
        header = lines[1]
        assert "action" in header
        assert "state" in header
        assert "default" in header
        assert "description" in header

    def test_setting_header(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        config_main(["--show", "SETTINGS"])
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "SETTINGS:"
        header = lines[1]
        assert "name" in header
        assert "value" in header
        assert "default" in header
        assert "description" in header

    def test_backup_enabled_by_default(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        config_main(["--show", "ALLOWS"])
        out = capsys.readouterr().out
        line = next(
            ln for ln in out.splitlines()
            if ln.strip().startswith("backup")
        )
        assert "enabled" in line

    def test_restore_disabled_by_default(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        config_main(["--show", "ALLOWS"])
        out = capsys.readouterr().out
        line = next(
            ln for ln in out.splitlines()
            if ln.strip().startswith("restore")
        )
        assert "disabled" in line


class TestShowParams:
    def test_lists_all_metrics(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(["--show-params"]) == 0
        out = capsys.readouterr().out
        assert "ela_time" in out
        assert "shared_hit_blocks" in out
        assert "temp_written_blocks" in out

    def test_header_columns(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        config_main(["--show-params"])
        header = capsys.readouterr().out.splitlines()[0]
        for col in ("name", "scope", "raw_key", "measure", "description"):
            assert col in header, f"missing column: {col}"


class TestAllowDeny:
    def test_allow(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(["--allow", "restore"]) == 0
        out = capsys.readouterr().out
        assert "enabled" in out

    def test_deny(self, cli_db, capsys):
        config_main(["--init"])
        config_main(["--allow", "restore"])
        capsys.readouterr()
        assert config_main(["--deny", "restore"]) == 0
        out = capsys.readouterr().out
        assert "disabled" in out

    def test_allow_unknown_action(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(["--allow", "no_such"]) == 1
        assert "no_such" in capsys.readouterr().err

    def test_deny_unknown_action(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(["--deny", "no_such"]) == 1

    def test_round_trip(self, cli_db, capsys):
        config_main(["--init"])
        config_main(["--allow", "restore"])
        capsys.readouterr()

        config_main(["--show", "ALLOWS"])
        out = capsys.readouterr().out
        line = next(
            ln for ln in out.splitlines()
            if ln.strip().startswith("restore")
        )
        assert "enabled" in line


class TestSet:
    def test_set_setting(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(
            ["--set", "SETTINGS", "default_cold_run", "5"]
        ) == 0
        out = capsys.readouterr().out
        assert "default_cold_run" in out
        assert "3" in out  # old value
        assert "5" in out  # new value

    def test_set_allows(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(
            ["--set", "ALLOWS", "restore", "1"]
        ) == 0

    def test_set_unknown_category(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(
            ["--set", "NOPE", "restore", "1"]
        ) == 1
        assert "NOPE" in capsys.readouterr().err

    def test_set_unknown_name(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(
            ["--set", "ALLOWS", "no_such", "1"]
        ) == 1
        assert "no_such" in capsys.readouterr().err

    def test_set_missing_args(self, cli_db):
        config_main(["--init"])
        with pytest.raises(SystemExit):
            config_main(["--set", "SETTING", "default_cold_run"])


class TestReset:
    def test_reset_single(self, cli_db, capsys):
        config_main(["--init"])
        config_main(["--allow", "restore"])
        capsys.readouterr()

        assert config_main(
            ["--reset", "ALLOWS", "restore"]
        ) == 0
        out = capsys.readouterr().out
        assert "restore" in out
        assert "enabled" in out
        assert "disabled" in out

    def test_reset_category(self, cli_db, capsys):
        config_main(["--init"])
        config_main(["--allow", "restore"])
        config_main(["--allow", "analyze"])
        capsys.readouterr()

        assert config_main(["--reset", "ALLOWS"]) == 0
        out = capsys.readouterr().out
        assert "restore" in out
        assert "analyze" in out

    def test_reset_when_clean(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(["--reset", "ALLOWS"]) == 0
        assert "Already at defaults" in capsys.readouterr().out

    def test_reset_unknown_category(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(["--reset", "NOPE"]) == 1

    def test_reset_unknown_name(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(
            ["--reset", "ALLOWS", "no_such"]
        ) == 1
        assert "no_such" in capsys.readouterr().err

    def test_reset_no_category_is_error(self, cli_db):
        config_main(["--init"])
        with pytest.raises(SystemExit):
            config_main(["--reset"])


class TestResetAll:
    def test_reset_all(self, cli_db, capsys):
        config_main(["--init"])
        config_main(["--allow", "restore"])
        config_main(
            ["--set", "SETTINGS", "default_cold_run", "5"]
        )
        capsys.readouterr()

        assert config_main(["--reset-all"]) == 0
        out = capsys.readouterr().out
        assert "restore" in out
        assert "default_cold_run" in out

    def test_reset_all_when_clean(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(["--reset-all"]) == 0
        assert "Already at defaults" in capsys.readouterr().out


class TestArgParsing:
    def test_no_args_exits(self, cli_db):
        with pytest.raises(SystemExit):
            config_main([])

    def test_two_actions_rejected(self, cli_db):
        with pytest.raises(SystemExit):
            config_main(["--init", "--show"])

