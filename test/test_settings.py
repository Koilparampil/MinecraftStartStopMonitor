"""Settings: YAML loading/coercion, server.properties resolution, validation."""

import logging
from pathlib import Path

import pytest
import yaml

from src.settings import (
    DEFAULTS,
    SENSITIVE_SETTINGS,
    YAML_MAPPING,
    Settings,
    mask_setting,
    start_command_argv,
    start_command_errors,
    start_command_warnings,
    yaml_name,
)


def write_yaml(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


def props(server_dir, text):
    (server_dir / "server.properties").write_text(text, encoding="utf-8")


@pytest.fixture
def valid(tmp_path):
    """A Settings instance that passes validation."""
    server = tmp_path / "srv"
    server.mkdir()
    (server / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    s = Settings()
    s.serverDir = str(server)
    s.startCommand = "./run.sh nogui"
    s.webPassword = "secret-pass"
    s.rconPassword = "rcon-pass"
    s.resolve_server_properties()
    return s


class TestLoading:
    def test_defaults(self):
        s = Settings()
        assert (s.serverPort, s.rconPort, s.rconPassword, s.listenerBindHost) == (
            None,
            None,
            None,
            None,
        )
        assert (s.webServerPort, s.processName, s.autoStopDelay, s.dataDir) == (
            25580,
            "java",
            600,
            "data",
        )
        assert s["serverHost"] == "127.0.0.1"
        assert s.get("missing", 7) == 7

    def test_every_yaml_key_maps_to_a_default(self):
        flat = {attr for mapping in YAML_MAPPING.values() for attr in mapping.values()}
        assert flat <= set(DEFAULTS)
        assert SENSITIVE_SETTINGS <= set(DEFAULTS)

    def test_nested_yaml_is_flattened(self, tmp_path):
        path = write_yaml(
            tmp_path / "settings.yaml",
            {
                "minecraft": {
                    "serverDir": "/srv/mc",
                    "startCommand": ["java", "-jar", "server.jar"],
                    "port": 25570,
                },
                "web": {"port": 8080, "password": "pw", "enabled": False},
                "autoStart": {"motd": "zzz", "bindHost": ""},
                "autoStop": {"stopDelay": 300},
                "monitor": {"dataDir": "state"},
                "security": {"sessionSecretKey": "k" * 64},
                "unknownSection": {"x": 1},
            },
        )
        s = Settings()
        s.readSettings(path)
        assert s.serverDir == "/srv/mc"
        assert s.startCommand == ["java", "-jar", "server.jar"]
        assert (s.serverPort, s.webServerPort, s.useWebServer) == (25570, 8080, False)
        assert (s.sleepingMotd, s.listenerBindHost, s.autoStopDelay) == ("zzz", "", 300)
        assert s.dataDir == str(tmp_path / "state")  # relative to the settings file
        assert not hasattr(s, "x")

    def test_quoted_values_are_coerced(self, tmp_path):
        path = write_yaml(
            tmp_path / "s.yaml",
            {
                "minecraft": {"port": "25570", "pollingRate": "2.5"},
                "features": {"autoStop": "false", "autoStart": "yes"},
                "web": {"port": "not-a-port"},
            },
        )
        s = Settings()
        s.readSettings(path)
        assert (s.serverPort, s.pollingRate) == (25570, 2.5)
        assert (s.autoStop, s.autoStart) == (False, True)
        assert s.webServerPort == "not-a-port"  # left for validation to report

    @pytest.mark.parametrize("content", ["key: [unclosed", "- just\n- a list\n"])
    def test_bad_yaml_keeps_defaults(self, tmp_path, caplog, content):
        path = tmp_path / "s.yaml"
        path.write_text(content, encoding="utf-8")
        s = Settings()
        with caplog.at_level(logging.WARNING):
            s.readSettings(str(path))
        assert s.webServerPort == 25580
        assert "Invalid YAML" in caplog.text

    def test_session_secret_generated_next_to_the_settings_file(self, tmp_path):
        path = write_yaml(tmp_path / "s.yaml", {"web": {"password": "pw"}})
        s = Settings()
        s.readSettings(path)
        key_file = tmp_path / "session_secret.key"
        assert key_file.read_text() == s.sessionSecretKey
        assert len(s.sessionSecretKey) == 64
        again = Settings()
        again.readSettings(path)
        assert again.sessionSecretKey == s.sessionSecretKey

    def test_relative_paths_follow_the_settings_file_not_the_cwd(
        self, tmp_path, monkeypatch
    ):
        conf = tmp_path / "conf"
        conf.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        path = write_yaml(
            conf / "settings.yaml",
            {"minecraft": {"serverDir": "../server"}, "monitor": {"dataDir": "state"}},
        )
        s = Settings()
        s.readSettings(path)
        assert s.serverDir == str(tmp_path / "server")
        assert s.dataDir == str(conf / "state")
        s.readSettings(write_yaml(conf / "other.yaml", {"web": {"password": "x"}}))
        assert s.dataDir == str(conf / "state")  # absolute now: kept

    def test_default_data_dir_sits_next_to_the_settings_file(self, tmp_path):
        s = Settings()
        s.readSettings(
            write_yaml(tmp_path / "settings.yaml", {"web": {"password": "x"}})
        )
        assert s.dataDir == str(tmp_path / "data")

    def test_home_relative_paths_are_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        s = Settings()
        s.readSettings(
            write_yaml(tmp_path / "s.yaml", {"monitor": {"dataDir": "~/mm"}})
        )
        assert s.dataDir == str(tmp_path / "mm")

    def test_data_path_creates_the_directory(self, tmp_path):
        s = Settings()
        s.dataDir = str(tmp_path / "state")
        assert s.data_path("players.json") == str(tmp_path / "state" / "players.json")
        assert (tmp_path / "state").is_dir()


class TestResolveServerProperties:
    def test_missing_file_uses_defaults(self, tmp_path):
        s = Settings()
        s.serverDir = str(tmp_path)
        assert s.resolve_server_properties() == {}
        assert (s.serverPort, s.rconPort, s.rconPassword, s.listenerBindHost) == (
            25565,
            25575,
            None,
            "",
        )

    def test_values_come_from_server_properties(self, tmp_path):
        props(
            tmp_path,
            "server-port=25570\nrcon.port=25571\nenable-rcon=true\n"
            "rcon.password=from-file\nserver-ip=0.0.0.0\n",
        )
        s = Settings()
        s.serverDir = str(tmp_path)
        s.resolve_server_properties()
        assert (s.serverPort, s.rconPort, s.rconPassword, s.listenerBindHost) == (
            25570,
            25571,
            "from-file",
            "0.0.0.0",
        )

    def test_explicit_settings_win(self, tmp_path):
        props(
            tmp_path,
            "server-port=25570\nenable-rcon=true\nrcon.password=file\nserver-ip=10.0.0.1\n",
        )
        s = Settings()
        s.serverDir = str(tmp_path)
        s.serverPort, s.rconPassword, s.listenerBindHost = "25599", "mine", ""
        s.resolve_server_properties()
        assert (s.serverPort, s.rconPassword, s.listenerBindHost) == (25599, "mine", "")

    def test_password_needs_enable_rcon(self, tmp_path):
        props(tmp_path, "enable-rcon=false\nrcon.password=file\n")
        s = Settings()
        s.serverDir = str(tmp_path)
        s.resolve_server_properties()
        assert s.rconPassword is None

    def test_blank_password_counts_as_unset(self, tmp_path):
        props(tmp_path, "enable-rcon=true\nrcon.password=file\n")
        s = Settings()
        s.serverDir = str(tmp_path)
        s.rconPassword = "  "
        s.resolve_server_properties()
        assert s.rconPassword == "file"

    def test_invalid_port_in_file_falls_back(self, tmp_path):
        props(tmp_path, "server-port=abc\n")
        s = Settings()
        s.serverDir = str(tmp_path)
        s.resolve_server_properties()
        assert s.serverPort == 25565

    def test_server_dir_becomes_absolute(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "srv").mkdir()
        s = Settings()
        s.serverDir = "srv"
        s.resolve_server_properties()
        assert s.serverDir == str(tmp_path / "srv")

    def test_warns_about_unreachable_server_ip_and_disabled_rcon(
        self, tmp_path, caplog
    ):
        props(tmp_path, "server-ip=192.168.1.20\nenable-rcon=false\n")
        s = Settings()
        s.serverDir = str(tmp_path)
        s.rconPassword = "explicit"
        with caplog.at_level(logging.WARNING):
            s.resolve_server_properties()
        assert "Set minecraft.host to 192.168.1.20" in caplog.text
        assert "enable-rcon=true" in caplog.text


class TestValidation:
    def test_valid_settings_pass(self, valid):
        assert isinstance(valid.validate_settings(), list)

    def test_all_problems_are_reported_together(self):
        s = Settings()
        with pytest.raises(ValueError) as error:
            s.validate_settings()
        message = str(error.value)
        assert "minecraft.serverDir is required" in message
        assert "minecraft.startCommand is required" in message
        assert "web.password is required" in message

    def test_server_dir_must_exist(self, valid, tmp_path):
        valid.serverDir = str(tmp_path / "nope")
        with pytest.raises(ValueError, match="does not exist"):
            valid.validate_settings()

    def test_web_password_optional_without_web(self, valid):
        valid.webPassword = None
        with pytest.raises(ValueError, match="web.password"):
            valid.validate_settings()
        valid.useWebServer = False
        valid.validate_settings()

    @pytest.mark.parametrize(
        "command",
        ["", "   ", [], ["java", ""], ["java", 5], 42, "java -jar 'server.jar"],
    )
    def test_bad_start_commands(self, valid, command):
        valid.startCommand = command
        with pytest.raises(ValueError, match="startCommand"):
            valid.validate_settings()

    @pytest.mark.parametrize(
        ("key", "value", "match"),
        [
            ("serverPort", 0, "minecraft.port must be between"),
            ("rconPort", 70000, "minecraft.rconPort must be between"),
            ("webServerPort", "abc", "web.port is not a valid integer"),
            ("webServerPort", True, "web.port is not a valid integer"),
            ("pollingRate", 0, "minecraft.pollingRate must be positive"),
            ("autoStopDelay", "soon", "autoStop.stopDelay is not a number"),
            ("stopTimeout", -1, "minecraft.stopTimeout must be positive"),
            ("autoStop", "maybe", "features.autoStop must be true or false"),
            ("processName", "", "minecraft.processName"),
            ("serverHost", " ", "minecraft.host"),
            ("rconPassword", 1234, "minecraft.rconPassword must be a string"),
        ],
    )
    def test_bad_values(self, valid, key, value, match):
        valid[key] = value
        with pytest.raises(ValueError, match=match):
            valid.validate_settings()

    def test_port_conflicts(self, valid):
        valid.serverPort = valid.webServerPort = 25565
        with pytest.raises(ValueError, match="web.port must differ from the game port"):
            valid.validate_settings()
        valid.webServerPort = 25580
        valid.rconPort = 25565
        with pytest.raises(ValueError, match="rconPort must differ from the game port"):
            valid.validate_settings()

    def test_warns_when_rcon_is_not_configured(self, valid, caplog):
        valid.rconPassword = None
        with caplog.at_level(logging.WARNING):
            warnings = valid.validate_settings()
        assert any("RCON is not configured" in w for w in warnings)
        assert "kick/ban/unban and console commands are disabled" in caplog.text

    def test_no_rcon_warning_with_password(self, valid):
        assert not any("RCON is not configured" in w for w in valid.validate_settings())

    def test_warns_about_eula_and_missing_properties(self, valid):
        warnings = " ".join(valid.validate_settings())
        assert "EULA is not accepted" in warnings
        assert "No server.properties" in warnings
        server = Path(valid.serverDir)
        (server / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        (server / "server.properties").write_text(
            "server-port=25565\n", encoding="utf-8"
        )
        warnings = " ".join(valid.validate_settings())
        assert "EULA" not in warnings
        assert "No server.properties" not in warnings


class TestStartCommandHelpers:
    def test_argv_forms(self):
        assert start_command_argv(
            "java -Xmx4G -jar 'my server.jar' nogui", "posix"
        ) == [
            "java",
            "-Xmx4G",
            "-jar",
            "my server.jar",
            "nogui",
        ]
        assert start_command_argv("run.bat nogui", "nt") == ["run.bat nogui"]
        assert start_command_argv(["java", "-jar", "x.jar"]) == [
            "java",
            "-jar",
            "x.jar",
        ]

    def test_errors(self):
        assert start_command_errors(None) == ["minecraft.startCommand is required"]
        assert start_command_errors("java 'oops", "posix")
        assert start_command_errors("java 'oops", "nt") == []

    def test_warnings_for_missing_executables(self, tmp_path):
        assert start_command_warnings("./run.sh nogui", str(tmp_path), "posix")
        (tmp_path / "run.sh").write_text("", encoding="utf-8")
        assert start_command_warnings("./run.sh nogui", str(tmp_path), "posix") == []
        assert start_command_warnings(
            "surely-not-a-real-binary-xyz -jar a.jar", str(tmp_path), "posix"
        )
        assert start_command_warnings("whatever.exe", str(tmp_path), "nt") == []


class TestHelpers:
    def test_mask_setting(self):
        assert mask_setting("webPassword", "hunter2") == "***"
        assert mask_setting("rconPassword", None) == "None"
        assert mask_setting("webServerPort", 8080) == "8080"

    def test_yaml_name(self):
        assert yaml_name("autoStopDelay") == "autoStop.stopDelay"
        assert yaml_name("listenerBindHost") == "autoStart.bindHost"
        assert yaml_name("notASetting") == "notASetting"
