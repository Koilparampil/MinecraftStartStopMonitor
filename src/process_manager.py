"""OS process backends: launch, discover, gracefully stop the server process.

OWNER: lifecycle agent. STUB: replace the bodies, keep the API
(see docs/ARCHITECTURE.md, "Process manager").
"""

from typing import Callable, Optional

from src.lifecycle.messages import ProcessIdentity, ProcessSnapshot


class OSProcessManager:
    def __init__(
        self,
        graceful_stopper: Optional[Callable[[], bool]] = None,
        pid_file: Optional[str] = None,
    ):
        raise NotImplementedError

    @property
    def tracked_identity(self) -> Optional[ProcessIdentity]:
        raise NotImplementedError

    def set_known_identity(self, identity: ProcessIdentity) -> None:
        raise NotImplementedError

    def clear_identity(self, expected: Optional[ProcessIdentity]) -> bool:
        raise NotImplementedError

    def identity_is_running(self, identity: Optional[ProcessIdentity]) -> bool:
        raise NotImplementedError

    def find_process_identity(self) -> Optional[ProcessIdentity]:
        """A live (non-zombie) process whose cwd is settings.serverDir and whose
        name/cmdline contains settings.processName; prefers the tracked one;
        never this monitor process."""
        raise NotImplementedError

    def observe(self) -> ProcessSnapshot:
        raise NotImplementedError

    def launch_process(self) -> Optional[ProcessIdentity]:
        """Spawn settings.startCommand in settings.serverDir."""
        raise NotImplementedError

    def terminate_process(
        self, expected: Optional[ProcessIdentity] = None
    ) -> Optional[bool]:
        """Graceful stop chain. True=gone, False=still alive, None=pending."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def create_process_manager(
    graceful_stopper: Optional[Callable[[], bool]] = None,
) -> OSProcessManager:
    raise NotImplementedError
