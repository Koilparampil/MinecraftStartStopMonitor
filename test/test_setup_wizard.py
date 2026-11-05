"""Setup wizard: server/launch detection, RCON + EULA edits, settings file,
and the interactive flow driven by scripted answers."""

import os
import stat
import sys

import pytest
import yaml

from src.minecraft.server_files import read_eula, read_properties
from src.settings import YAML_MAPPING, Settings
from src.setup_wizard import (
    configure_rcon,
    default_config,
    detect_server_dir,
    detect_start_command,
    find_server_jar,
    generate_password,
    interactive_setup,
    java_command,
    looks_like_server_dir,
    no_terminal_help,
    rcon_status,
    restart_loop_scripts,
    suggest_max_memory,
    write_settings_file,
)

GIB = 1 << 30


def touch(directory, *names, mode=None):
    for name in names:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        if mode is not None:
            path.chmod(mode)


class Script:
    """Scripted answers for input()/getpass(); records prompts and output."""

    def __init__(self, answers, secrets=()):
        self.answers = list(answers)
        self.secrets = list(secrets)
        self.prompts = []
        self.output = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def ask_secret(self, prompt):
        self.prompts.append(prompt)
        if not self.secrets:
            raise EOFError
        return self.secrets.pop(0)

    def out(self, text=""):
        self.output.append(text)

    @property
    def text(self):
        return "\n".join(self.output)


def run_wizard(settings_path, detected, script):
    return interactive_setup(
        str(settings_path),
        detected,
        ask=script.ask,
        ask_secret=script.ask_secret,
        out=script.out,
    )


# ----------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------


class TestDetectStartCommand:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_forge_run_script(self, tmp_path):
        touch(tmp_path, "run.sh", "run.bat", "user_jvm_args.txt", mode=0o755)
        assert detect_start_command(str(tmp_path), "posix") == "./run.sh nogui"
        assert detect_start_command(str(tmp_path), "nt") == "run.bat nogui"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_non_executable_run_script_goes_through_sh(self, tmp_path):
        touch(tmp_path, "run.sh", mode=0o644)
        assert detect_start_command(str(tmp_path), "posix") == "sh ./run.sh nogui"

    def test_run_bat_only_is_ignored_on_posix(self, tmp_path):
        touch(tmp_path, "run.bat", "server.jar")
        assert detect_start_command(str(tmp_path), "posix", "6G") == (
            "java -Xms1G -Xmx6G -jar server.jar nogui"
        )

    @pytest.mark.parametrize(
        ("files", "kind", "jar"),
        [
            (
                ["fabric-server-launch.jar", "server.jar"],
                "fabric",
                "fabric-server-launch.jar",
            ),
            (
                ["fabric-server-mc.1.20.1-loader.0.15.11-launcher.1.0.1.jar"],
                "fabric",
                "fabric-server-mc.1.20.1-loader.0.15.11-launcher.1.0.1.jar",
            ),
            (
                ["quilt-server-launch.jar", "server.jar"],
                "quilt",
                "quilt-server-launch.jar",
            ),
            (
                [
                    "forge-1.16.5-36.2.39-installer.jar",
                    "forge-1.16.5-36.2.39.jar",
                    "minecraft_server.1.16.5.jar",
                ],
                "forge",
                "forge-1.16.5-36.2.39.jar",
            ),
            (["minecraft_server.1.12.2.jar"], "vanilla", "minecraft_server.1.12.2.jar"),
            (["server.jar"], "vanilla", "server.jar"),
            (["paper-1.20.1-196.jar"], "jar", "paper-1.20.1-196.jar"),
        ],
    )
    def test_server_jars(self, tmp_path, files, kind, jar):
        touch(tmp_path, *files)
        found = find_server_jar(str(tmp_path))
        assert (found.kind, found.name) == (kind, jar)
        assert detect_start_command(str(tmp_path), "posix", "8G") == (
            f"java -Xms1G -Xmx8G -jar {jar} nogui"
        )

    def test_nothing_launchable(self, tmp_path):
        assert detect_start_command(str(tmp_path), "posix") is None
        touch(tmp_path, "a.jar", "b.jar", "forge-1.20.1-installer.jar")
        assert detect_start_command(str(tmp_path), "posix") is None

    def test_jar_names_are_quoted_per_os(self):
        assert java_command("my server.jar", "4G", "posix") == (
            "java -Xms1G -Xmx4G -jar 'my server.jar' nogui"
        )
        assert java_command("my server.jar", "4G", "nt") == (
            'java -Xms1G -Xmx4G -jar "my server.jar" nogui'
        )


class TestServerDir:
    @pytest.mark.parametrize(
        "files",
        [["server.properties"], ["eula.txt"], ["run.sh"], ["fabric-server-launch.jar"]],
    )
    def test_looks_like_server_dir(self, tmp_path, files):
        touch(tmp_path, *files)
        assert looks_like_server_dir(str(tmp_path))

    def test_mods_and_libraries_count(self, tmp_path):
        (tmp_path / "mods").mkdir()
        assert not looks_like_server_dir(str(tmp_path))
        (tmp_path / "libraries").mkdir()
        assert looks_like_server_dir(str(tmp_path))

    def test_not_a_server(self, tmp_path):
        touch(tmp_path, "notes.txt")
        assert not looks_like_server_dir(str(tmp_path))
        assert not looks_like_server_dir(str(tmp_path / "missing"))
        assert not looks_like_server_dir("")

    def test_detect_server_dir(self, tmp_path):
        assert detect_server_dir(str(tmp_path)) is None
        touch(tmp_path, "server/server.properties")
        assert detect_server_dir(str(tmp_path)) == str(tmp_path / "server")
        touch(tmp_path, "eula.txt")
        assert detect_server_dir(str(tmp_path)) == str(tmp_path)


class TestHelpers:
    @pytest.mark.parametrize(
        ("total", "expected"),
        [
            (None, "4G"),
            (2 * GIB, "1G"),
            (8 * GIB, "4G"),
            (16 * GIB, "8G"),
            (128 * GIB, "16G"),
        ],
    )
    def test_suggest_max_memory(self, total, expected):
        assert suggest_max_memory(total) == expected

    def test_restart_loop_scripts(self, tmp_path):
        (tmp_path / "start.sh").write_text(
            "#!/bin/bash\nwhile true; do\n  java -jar server.jar\n  sleep 5\ndone\n"
        )
        (tmp_path / "ServerStart.bat").write_text(
            ":start\njava -jar forge.jar nogui\ngoto start\n"
        )
        (tmp_path / "run.sh").write_text(
            '#!/usr/bin/env sh\njava @user_jvm_args.txt "$@"\n'
        )
        (tmp_path / "once.bat").write_text("java -jar server.jar\ngoto :eof\n:eof\n")
        assert restart_loop_scripts(str(tmp_path)) == ["ServerStart.bat", "start.sh"]

    def test_generate_password(self):
        password = generate_password()
        assert len(password) == 24
        assert password.isalnum()
        assert generate_password() != password
        assert len(generate_password(16)) == 16


# ----------------------------------------------------------------------
# server.properties / eula.txt
# ----------------------------------------------------------------------


class TestConfigureRcon:
    def test_creates_rcon_settings_in_a_fresh_server(self, tmp_path):
        setup = configure_rcon(str(tmp_path))
        props = read_properties(str(tmp_path / "server.properties"))
        assert props == {
            "enable-rcon": "true",
            "rcon.port": "25575",
            "rcon.password": setup.password,
            "broadcast-rcon-to-ops": "false",
        }
        assert setup.password.isalnum() and len(setup.password) == 24
        assert (setup.port, setup.changed, setup.already_enabled) == (
            25575,
            True,
            False,
        )
        assert rcon_status(str(tmp_path)).enabled

    def test_keeps_other_properties_and_comments(self, tmp_path):
        path = tmp_path / "server.properties"
        path.write_text("#Minecraft server properties\nmotd=Hi\nserver-port=25570\n")
        configure_rcon(str(tmp_path))
        text = path.read_text()
        assert text.startswith(
            "#Minecraft server properties\nmotd=Hi\nserver-port=25570\n"
        )
        assert read_properties(str(path))["enable-rcon"] == "true"

    def test_rcon_port_never_equals_the_game_port(self, tmp_path):
        (tmp_path / "server.properties").write_text("server-port=25575\n")
        assert configure_rcon(str(tmp_path)).port != 25575

    def test_existing_rcon_password_is_kept(self, tmp_path):
        path = tmp_path / "server.properties"
        path.write_text("enable-rcon=true\nrcon.port=25600\nrcon.password=existing\n")
        setup = configure_rcon(str(tmp_path))
        assert (setup.port, setup.password, setup.already_enabled) == (
            25600,
            "existing",
            True,
        )
        props = read_properties(str(path))
        assert props["rcon.password"] == "existing"
        assert props["broadcast-rcon-to-ops"] == "false"

    def test_rcon_status(self, tmp_path):
        status = rcon_status(str(tmp_path))
        assert (status.enabled, status.port, status.broadcasts_to_ops) == (
            False,
            25575,
            True,
        )


# ----------------------------------------------------------------------
# settings.yaml
# ----------------------------------------------------------------------


class TestSettingsFile:
    def test_default_config_uses_the_yaml_layout(self, tmp_path):
        config = default_config("/srv/mc", "./run.sh nogui", "pw", webServerPort=8080)
        assert set(config) == set(YAML_MAPPING)
        assert all(set(config[s]) == set(m) for s, m in YAML_MAPPING.items())
        assert config["minecraft"]["serverDir"] == "/srv/mc"
        assert config["web"]["port"] == 8080
        assert (
            config["minecraft"]["rconPassword"] is None
        )  # read from server.properties

    def test_written_file_loads_back_and_validates(self, tmp_path):
        server = tmp_path / "srv"
        touch(server, "run.sh", "eula.txt")
        path = tmp_path / "conf" / "settings.yaml"
        write_settings_file(
            str(path), default_config(str(server), "./run.sh nogui", "pw1234")
        )
        assert path.read_text(encoding="utf-8").startswith(
            "# minecraft-monitor settings"
        )
        loaded = Settings()
        loaded.readSettings(str(path))
        loaded.resolve_server_properties()
        loaded.validate_settings()
        assert (loaded.serverDir, loaded.webPassword) == (str(server), "pw1234")
        assert sorted(p.name for p in path.parent.iterdir()) == [
            "session_secret.key",
            "settings.yaml",
        ]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_settings_file_is_private(self, tmp_path):
        path = tmp_path / "settings.yaml"
        write_settings_file(str(path), {"web": {"password": "pw"}})
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_no_terminal_help_points_to_the_example(self, tmp_path):
        lines = no_terminal_help(str(tmp_path / "settings.yaml"))
        assert "no terminal" in lines[0]
        assert "settings.yaml.example" in lines[1]
        assert str(tmp_path / "settings.yaml") in lines[1]


# ----------------------------------------------------------------------
# Interactive flow
# ----------------------------------------------------------------------


@pytest.fixture
def forge_server(tmp_path):
    server = tmp_path / "server"
    touch(server, "run.sh", "user_jvm_args.txt", mode=0o755)
    return server


class TestInteractiveSetup:
    def test_happy_path_accepting_everything(self, tmp_path, forge_server):
        settings_path = tmp_path / "settings.yaml"
        script = Script(["", "", "", "yes"], secrets=["web-pass", "web-pass"])
        config = run_wizard(settings_path, str(forge_server), script)

        saved = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
        assert saved == config
        assert saved["minecraft"]["serverDir"] == str(forge_server)
        assert saved["minecraft"]["startCommand"].endswith("run.sh nogui")
        assert saved["web"]["password"] == "web-pass"
        props = read_properties(str(forge_server / "server.properties"))
        assert props["enable-rcon"] == "true"
        assert props["broadcast-rcon-to-ops"] == "false"
        assert read_eula(str(forge_server)) is True
        assert "https://aka.ms/MinecraftEULA" in script.text
        assert "user_jvm_args.txt" in script.text

    @pytest.mark.parametrize("answer", ["", "y", "no", "YES please"])
    def test_eula_is_only_accepted_by_yes(self, tmp_path, forge_server, answer):
        script = Script(["", "", "n", answer], secrets=["pw", "pw"])
        run_wizard(tmp_path / "settings.yaml", str(forge_server), script)
        assert read_eula(str(forge_server)) is not True
        assert "will not start" in script.text

    def test_eula_already_accepted_is_not_asked(self, tmp_path, forge_server):
        (forge_server / "eula.txt").write_text("eula=true\n")
        script = Script(["", "", "n"], secrets=["pw", "pw"])
        run_wizard(tmp_path / "settings.yaml", str(forge_server), script)
        assert not any("EULA" in prompt for prompt in script.prompts)

    def test_declining_rcon_leaves_server_properties_alone(
        self, tmp_path, forge_server
    ):
        script = Script(["", "", "n", "yes"], secrets=["pw", "pw"])
        run_wizard(tmp_path / "settings.yaml", str(forge_server), script)
        assert not (forge_server / "server.properties").exists()

    def test_existing_rcon_only_offers_to_silence_broadcasts(
        self, tmp_path, forge_server
    ):
        (forge_server / "server.properties").write_text(
            "enable-rcon=true\nrcon.password=keep-me\n"
        )
        script = Script(["", "", "", "yes"], secrets=["pw", "pw"])
        run_wizard(tmp_path / "settings.yaml", str(forge_server), script)
        props = read_properties(str(forge_server / "server.properties"))
        assert props["rcon.password"] == "keep-me"
        assert props["broadcast-rcon-to-ops"] == "false"

    def test_empty_web_password_is_generated_and_shown(self, tmp_path, forge_server):
        script = Script(["", "", "n", "yes"], secrets=[""])
        config = run_wizard(tmp_path / "settings.yaml", str(forge_server), script)
        password = config["web"]["password"]
        assert len(password) == 16 and password.isalnum()
        assert password in script.text

    def test_mismatched_password_is_asked_again(self, tmp_path, forge_server):
        script = Script(["", "", "n", "yes"], secrets=["one", "two", "three", "three"])
        config = run_wizard(tmp_path / "settings.yaml", str(forge_server), script)
        assert config["web"]["password"] == "three"
        assert "did not match" in script.text

    def test_asks_for_folder_and_command_when_nothing_is_detected(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        script = Script(
            [
                str(tmp_path / "missing"),  # not a folder -> asked again
                str(plain),  # not a server
                "n",  # -> don't use it, asked again
                str(plain),
                "y",  # use it anyway
                "java -jar 'broken",  # unbalanced quotes -> asked again
                "java -jar custom.jar nogui",
                "n",  # no RCON
                "no",  # EULA declined
            ],
            secrets=["pw", "pw"],
        )
        config = run_wizard(tmp_path / "settings.yaml", None, script)
        assert config["minecraft"]["serverDir"] == str(plain)
        assert config["minecraft"]["startCommand"] == "java -jar custom.jar nogui"
        assert "is not a folder" in script.text
        assert "startCommand is invalid" in script.text

    def test_typed_command_overrides_the_detected_one(self, tmp_path, forge_server):
        script = Script(
            ["", "java -Xmx2G -jar x.jar nogui", "n", "yes"], secrets=["p", "p"]
        )
        config = run_wizard(tmp_path / "settings.yaml", str(forge_server), script)
        assert config["minecraft"]["startCommand"] == "java -Xmx2G -jar x.jar nogui"

    def test_restart_loop_scripts_are_warned_about(self, tmp_path, forge_server):
        (forge_server / "start.sh").write_text("while true; do ./run.sh; done\n")
        script = Script(["", "", "n", "yes"], secrets=["pw", "pw"])
        run_wizard(tmp_path / "settings.yaml", str(forge_server), script)
        assert "start.sh looks like it restarts the server in a loop" in script.text

    def test_cancel_at_the_last_prompt_changes_no_server_file(
        self, tmp_path, forge_server
    ):
        settings_path = tmp_path / "settings.yaml"
        script = Script(["", "", "", "yes"], secrets=[])  # EOF at the password
        with pytest.raises(EOFError):
            run_wizard(settings_path, str(forge_server), script)
        assert not (forge_server / "server.properties").exists()  # RCON not applied
        assert not (forge_server / "eula.txt").exists()  # EULA not applied
        assert not settings_path.exists()

    def test_eof_aborts_without_writing(self, tmp_path, forge_server):
        settings_path = tmp_path / "settings.yaml"
        with pytest.raises(EOFError):
            run_wizard(settings_path, str(forge_server), Script([]))
        assert not settings_path.exists()

    def test_uses_builtin_input_by_default(self, tmp_path, forge_server, monkeypatch):
        answers = iter(["", "", "n", "yes"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        monkeypatch.setattr("getpass.getpass", lambda prompt: "pw")
        interactive_setup(
            str(tmp_path / "settings.yaml"), str(forge_server), out=lambda *_: None
        )
        assert (tmp_path / "settings.yaml").exists()
