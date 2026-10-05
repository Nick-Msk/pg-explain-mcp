"""Tests for the pg-tune-config CLI."""

from pg_tune.cli import config_main


class TestInit:
    def test_creates_db(self, cli_db):
        assert not cli_db.exists()
        assert config_main(["--init"]) == 0
        assert cli_db.exists()


class TestShow:
    def test_lists_seed_actions(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()

        assert config_main(["--show"]) == 0
        out = capsys.readouterr().out
        assert "backup" in out
        assert "restore" in out
        assert "enabled" in out
        assert "disabled" in out

    def test_header_present(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        config_main(["--show"])
        header = capsys.readouterr().out.splitlines()[0]
        assert "action" in header
        assert "state" in header
        assert "description" in header

    def test_backup_shows_enabled(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        config_main(["--show"])
        out = capsys.readouterr().out
        line = next(
            ln for ln in out.splitlines()
            if ln.strip().startswith("backup")
        )
        assert "enabled" in line

    def test_restore_shows_disabled(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        config_main(["--show"])
        out = capsys.readouterr().out
        line = next(
            ln for ln in out.splitlines()
            if ln.strip().startswith("restore")
        )
        assert "disabled" in line


class TestAllowDeny:
    def test_allow(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(["--allow", "restore"]) == 0
        assert "enabled" in capsys.readouterr().out

    def test_deny(self, cli_db, capsys):
        config_main(["--init"])
        config_main(["--allow", "restore"])
        capsys.readouterr()
        assert config_main(["--deny", "restore"]) == 0
        assert "disabled" in capsys.readouterr().out

    def test_allow_unknown_action_exits_nonzero(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        rc = config_main(["--allow", "no_such"])
        assert rc == 1
        assert "no_such" in capsys.readouterr().err

    def test_deny_unknown_action_exits_nonzero(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(["--deny", "no_such"]) == 1

    def test_round_trip(self, cli_db, capsys):
        config_main(["--init"])
        config_main(["--allow", "restore"])
        capsys.readouterr()

        config_main(["--show"])
        out = capsys.readouterr().out
        line = next(
            ln for ln in out.splitlines()
            if ln.strip().startswith("restore")
        )
        assert "enabled" in line


class TestReset:
    def test_reset_single(self, cli_db, capsys):
        config_main(["--init"])
        config_main(["--allow", "restore"])
        capsys.readouterr()

        assert config_main(["--reset", "restore"]) == 0
        out = capsys.readouterr().out
        assert "restore" in out
        assert "enabled" in out  # old
        assert "disabled" in out  # new

    def test_reset_all(self, cli_db, capsys):
        config_main(["--init"])
        config_main(["--allow", "restore"])
        config_main(["--allow", "analyze"])
        capsys.readouterr()

        assert config_main(["--reset"]) == 0
        out = capsys.readouterr().out
        assert "restore" in out
        assert "analyze" in out

    def test_reset_when_clean(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(["--reset"]) == 0
        assert "Already at defaults" in capsys.readouterr().out

    def test_reset_unknown_action(self, cli_db, capsys):
        config_main(["--init"])
        capsys.readouterr()
        assert config_main(["--reset", "no_such"]) == 1
        assert "no_such" in capsys.readouterr().err


class TestArgParsing:
    def test_no_args_exits(self, cli_db):
        import pytest
        with pytest.raises(SystemExit):
            config_main([])

    def test_two_actions_rejected(self, cli_db):
        import pytest
        with pytest.raises(SystemExit):
            config_main(["--init", "--show"])

