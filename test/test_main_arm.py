"""MonitorApp._arm_auto_start: never bind the game port while the scan is unsure."""

from src.main import MonitorApp
from src.process_manager import ProcessStateUnknown
from src.lifecycle.messages import ProcessIdentity


class _FakeListener:
    def __init__(self):
        self.listen_calls = 0

    def listen(self):
        self.listen_calls += 1


class _Scan:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def find_process_identity(self):
        if self.error is not None:
            raise self.error
        return self.result


def _app(scan):
    app = MonitorApp()
    app.process_manager = scan
    app.auto_start = _FakeListener()
    return app


def test_arms_when_no_server_is_found():
    app = _app(_Scan(result=None))
    app._arm_auto_start()
    assert app.auto_start.listen_calls == 1


def test_does_not_arm_when_a_server_is_found():
    app = _app(_Scan(result=ProcessIdentity(1234, 1.0)))
    app._arm_auto_start()
    assert app.auto_start.listen_calls == 0


def test_does_not_arm_or_crash_when_the_scan_is_unknown():
    app = _app(_Scan(error=ProcessStateUnknown("Too many open files")))
    app._arm_auto_start()  # must not raise
    assert app.auto_start.listen_calls == 0
