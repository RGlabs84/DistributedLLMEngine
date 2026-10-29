"""
The logger the DLNE Donate app carries INSTEAD of the engine's ``engine/logger.py``, in its snapshot of engine files
(see engine_snapshot.py and dlne-donate.spec). It is not the engine's file and it is not shipped in any release.

``engine/hardware.py`` imports ``log_warning`` from ``engine.logger``, for one message the Settings window never
causes. The real module prints with Rich, wraps ``sys.stdout`` and, at import, opens a rotating log file under the
install root's ``logs/``; in a frozen app the install root is the folder of the executable, so importing it here
would drop a ``logs`` folder next to the downloaded program (a Windows user's Downloads folder; on Linux the
AppImage is read-only, so the file would land in the current folder). The tray app keeps its own log (donate.log).

These functions take what the real ones take and say nothing. The worker process never sees this file: it runs the
verified, downloaded engine with the engine's own logger.
"""


def _say_nothing(msg: object = "") -> None:
    return None


log_system = log_worker = log_coordinator = log_success = log_error = log_warning = _say_nothing


def __getattr__(name: str):
    """A ``log_*`` function the engine gains later is silent here too, instead of an ImportError."""
    if name.startswith("log_"):
        return _say_nothing
    raise AttributeError(name)
