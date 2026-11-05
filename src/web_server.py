"""Flask web dashboard.

OWNER: web agent. STUB: replace the body, keep the API
(see docs/ARCHITECTURE.md, "WebServer").
"""


class WebServer:
    def __init__(self, controller, bus):
        raise NotImplementedError

    def run(self) -> None:
        """Serve on settings.webBindHost:settings.webServerPort. Blocks."""
        raise NotImplementedError

    def shutdown(self) -> None:
        """Make a blocking run() return (used by tests and main's signal path)."""
        raise NotImplementedError

    def close(self) -> None:
        """Unsubscribe from the bus. Idempotent."""
        raise NotImplementedError
