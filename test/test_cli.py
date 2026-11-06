"""CLI flags, overrides (masked in logs) and the settings-file location."""

import logging
import os

import pytest

from src import cli
from src.cli import (
    SETTINGS_ENV_VAR,
    apply_overrides,
    default_settings_path,
    determine_settings_path,
    parse_args,
)
from src.settings import Settings

ALL_FLAGS = [
    "--settings", "/etc/mm/settings.yaml",
    "--server-dir", "/srv/mc",
    "--start-command", "./run.sh nogui",
    "--host", "10.0.0.2",
    "--server-port", "25570",
    "--rcon-port", "25571",
    "--rcon-password", "rcon-secret",
    "--polling-rate", "2.5",
    "--stop-timeout", "180",
    "--no-web",
    "--web-port", "8080",
    "--web-username", "boss",
    "--web-password", "web-secret",
    "--no-auto-start",
    "--no-auto-stop",
    "--auto-stop-delay", "300",
]  # fmt: skip


class TestParsing:
    def test_all_contract_flags(self):
        args = parse_args(ALL_FLAGS)
        assert vars(args) == {
            "settings": "/etc/mm/settings.yaml",
            "serverDir": "/srv/mc",
            "startCommand": "./run.sh nogui",
            "serverHost": "10.0.0.2",
            "serverPort": 25570,
            "rconPort": 25571,
            "rconPassword": "rcon-secret",
            "pollingRate": 2.5,
            "stopTimeout": 180,
            "useWebServer": False,
            "webServerPort": 8080,
            "webUsername": "boss",
            "webPassword": "web-secret",
            "autoStart": False,
            "autoStop": False,
            "autoStopDelay": 300,
            "trustedProxies": None,
            "secureCookies": None,
        }

    def test_no_flags_means_no_overrides(self):
        assert all(value is None for value in vars(parse_args([])).values())

    @pytest.mark.parametrize(
        "argv",
        [
            ["--server-port", "0"],
            ["--web-port", "65536"],
            ["--rcon-port", "abc"],
            ["--polling-rate", "-1"],
            ["--auto-stop-delay", "soon"],
            ["--start-command", "  "],
        ],
    )
    def test_invalid_values_exit_with_usage(self, argv, capsys):
        with pytest.raises(SystemExit) as exit_info:
            parse_args(argv)
        assert exit_info.value.code == 2
        assert "usage:" in capsys.readouterr().err


class TestOverrides:
    def test_applies_only_given_flags(self):
        target = Settings()
        applied = apply_overrides(
            parse_args(["--web-port", "8080", "--no-auto-stop"]), target
        )
        assert applied == {"webServerPort": 8080, "autoStop": False}
        assert (target.webServerPort, target.autoStop, target.autoStart) == (
            8080,
            False,
            True,
        )

    def test_sensitive_values_are_masked_in_logs(self, caplog):
        target = Settings()
        args = parse_args(
            ["--rcon-password", "rcon-secret", "--web-password", "web-secret"]
        )
        with caplog.at_level(logging.INFO):
            apply_overrides(args, target)
        assert target.rconPassword == "rcon-secret"
        assert "rcon-secret" not in caplog.text
        assert "web-secret" not in caplog.text
        assert "CLI override: rconPassword=***" in caplog.text

    def test_defaults_to_the_singleton(self, mock_settings):
        apply_overrides(parse_args(["--auto-stop-delay", "42"]))
        assert mock_settings.autoStopDelay == 42


class TestSettingsPath:
    def test_flag_wins(self, tmp_path):
        args = parse_args(["--settings", str(tmp_path / "a.yaml")])
        env = {SETTINGS_ENV_VAR: str(tmp_path / "b.yaml")}
        assert determine_settings_path(args, environ=env) == str(tmp_path / "a.yaml")

    def test_environment_variable(self, tmp_path):
        env = {SETTINGS_ENV_VAR: str(tmp_path / "b.yaml")}
        assert determine_settings_path(parse_args([]), environ=env) == str(
            tmp_path / "b.yaml"
        )

    def test_relative_paths_are_made_absolute(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = parse_args(["--settings", "conf.yaml"])
        assert determine_settings_path(args, environ={}) == str(tmp_path / "conf.yaml")

    def test_default_is_inside_src(self):
        expected = os.path.join(
            os.path.dirname(os.path.abspath(cli.__file__)), "settings.yaml"
        )
        assert (
            determine_settings_path(parse_args([]), environ={}, frozen=False)
            == expected
        )

    def test_frozen_default_is_next_to_the_executable(self, tmp_path):
        exe = str(tmp_path / "dist" / "minecraft-monitor")
        assert default_settings_path(frozen=True, executable=exe) == str(
            tmp_path / "dist" / "settings.yaml"
        )


class TestQaFixes:
    @pytest.mark.parametrize(
        "argv",
        [
            ["--polling-rate", "nan"],
            ["--auto-stop-delay", "inf"],
            ["--trusted-proxies", "10.0.0.0/8,bogus"],
        ],
    )
    def test_more_invalid_values(self, argv, capsys):
        with pytest.raises(SystemExit):
            parse_args(argv)

    def test_proxy_flags(self):
        args = parse_args(
            ["--trusted-proxies", "127.0.0.1, 10.0.0.0/8", "--secure-cookies"]
        )
        assert args.trustedProxies == ["127.0.0.1", "10.0.0.0/8"]
        assert args.secureCookies is True

    def test_a_complete_command_line_needs_no_settings_file(self):
        from src.main import cli_settings_complete

        base = ["--server-dir", "/srv/mc", "--start-command", "./run.sh nogui"]
        assert cli_settings_complete(parse_args([*base, "--web-password", "pw"]))
        assert cli_settings_complete(parse_args([*base, "--no-web"]))
        assert not cli_settings_complete(parse_args(base))


class TestFirstRunWithoutTerminal:
    """main.run_first_time_setup: the no-terminal path and its error messages."""

    @pytest.fixture
    def no_tty(self, monkeypatch, mock_settings):
        monkeypatch.setattr("src.main._stdin_is_a_terminal", lambda: False)
        monkeypatch.setattr("src.setup_wizard.port_is_free", lambda port: True)

    def test_complete_flags_are_saved_like_the_wizard(self, tmp_path, no_tty, capsys):
        from src.main import run_first_time_setup

        server = tmp_path / "server"
        server.mkdir(exist_ok=True)
        settings_path = tmp_path / "conf" / "settings.yaml"
        args = parse_args(
            [
                "--server-dir",
                str(server),
                "--start-command",
                "./run.sh nogui",
                "--web-password",
                "secret-pw",
                "--auto-stop-delay",
                "300",
            ]
        )
        assert run_first_time_setup(str(settings_path), args)
        assert (
            f"saved the command-line settings to {settings_path}"
            in capsys.readouterr().out
        )
        if os.name != "nt":
            assert settings_path.stat().st_mode & 0o777 == 0o600
        loaded = Settings()
        loaded.readSettings(str(settings_path))
        assert loaded.serverDir == str(server)
        assert loaded.startCommand == "./run.sh nogui"
        assert loaded.webPassword == "secret-pw"
        assert loaded.autoStopDelay == 300

    def test_example_password_is_refused_before_saving(self, tmp_path, no_tty, capsys):
        from src.main import run_first_time_setup

        settings_path = tmp_path / "settings.yaml"
        args = parse_args(
            [
                "--server-dir",
                str(tmp_path),
                "--start-command",
                "./run.sh nogui",
                "--web-password",
                "changeme",
            ]
        )
        assert not run_first_time_setup(str(settings_path), args)
        assert "example password" in capsys.readouterr().out
        assert not settings_path.exists()

    def test_server_file_errors_are_not_blamed_on_the_settings_file(
        self, tmp_path, monkeypatch, capsys
    ):
        from src import main
        from src.setup_wizard import SettingsWriteError

        def unreadable(*_args, **_kwargs):
            raise PermissionError(13, "Permission denied", "/srv/server.properties")

        monkeypatch.setattr(main, "interactive_setup", unreadable)
        assert not main.run_first_time_setup(str(tmp_path / "s.yaml"), parse_args([]))
        out = capsys.readouterr().out
        assert (
            "/srv/server.properties" in out and "cannot write the settings" not in out
        )

        def unwritable(*_args, **_kwargs):
            raise SettingsWriteError(13, "Permission denied", str(tmp_path))

        monkeypatch.setattr(main, "interactive_setup", unwritable)
        assert not main.run_first_time_setup(str(tmp_path / "s.yaml"), parse_args([]))
        assert "cannot write the settings file" in capsys.readouterr().out
