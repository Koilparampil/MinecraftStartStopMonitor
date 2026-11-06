"""Startup validation refuses list-form .bat commands cmd.exe cannot pass."""

from src.settings import start_command_errors


def test_windows_list_bat_with_percent_is_reported_at_startup():
    errors = start_command_errors(["run.bat", "50%"], os_name="nt")
    assert len(errors) == 1
    assert "minecraft.startCommand" in errors[0]
    assert "one string" in errors[0]


def test_windows_list_bat_with_quote_is_reported_at_startup():
    errors = start_command_errors(["C:\\Pack\\run.bat", 'say "hi"'], os_name="nt")
    assert errors and "minecraft.startCommand" in errors[0]


def test_windows_list_bat_with_ampersand_in_path_is_accepted():
    # Every element is quoted for cmd.exe, so & | < > ^ are literal.
    assert (
        start_command_errors(
            ["C:\\Servers\\R&D Server\\run.bat", "nogui"], os_name="nt"
        )
        == []
    )


def test_windows_string_command_is_not_checked_as_a_list():
    assert start_command_errors('"C:\\100% Pack\\run.bat" nogui', os_name="nt") == []


def test_posix_list_is_never_checked_for_cmd_rules():
    assert start_command_errors(["./run.sh", "50%"], os_name="posix") == []
