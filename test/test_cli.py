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
