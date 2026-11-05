"""Readers/writers for files in the Minecraft server directory.

OWNER: protocol agent. STUB: replace the bodies, keep the signatures
(see docs/ARCHITECTURE.md, "server_files").
"""

from typing import Optional


def read_properties(path: str) -> dict[str, str]:
    """Parse a Java .properties file (server.properties). {} if missing."""
    raise NotImplementedError


def update_properties(path: str, updates: dict[str, str]) -> bool:
    """Set keys, preserving comments/order/unknown keys; create the file if
    missing; atomic write. Returns True if the file content changed."""
    raise NotImplementedError


def read_json_list(path: str) -> list[dict]:
    """banned-players.json / whitelist.json / usercache.json / ops.json /
    banned-ips.json. [] if missing or invalid."""
    raise NotImplementedError


def write_json_list(path: str, entries: list[dict]) -> None:
    """Atomic write, 2-space indent (vanilla style)."""
    raise NotImplementedError


def offline_uuid(name: str) -> str:
    """Offline-mode UUID (v3 of 'OfflinePlayer:<name>'), dashed lowercase."""
    raise NotImplementedError


def list_mods(server_dir: str) -> list[str]:
    """Sorted jar file names in <server_dir>/mods (ignores *.disabled)."""
    raise NotImplementedError


def read_server_icon(server_dir: str) -> Optional[str]:
    """'data:image/png;base64,...' for <server_dir>/server-icon.png, else None."""
    raise NotImplementedError


def read_eula(server_dir: str) -> Optional[bool]:
    """True/False from eula.txt, None if the file is missing."""
    raise NotImplementedError


def write_eula(server_dir: str, accepted: bool) -> None:
    raise NotImplementedError


def tail_file(path: str, lines: int = 200, max_bytes: int = 262144) -> list[str]:
    """Last `lines` lines of a text file (reads at most max_bytes from the end).
    [] if missing. Decodes UTF-8 with replacement."""
    raise NotImplementedError
