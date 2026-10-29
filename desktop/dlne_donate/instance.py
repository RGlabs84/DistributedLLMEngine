"""
One DLNE Donate at a time. An exclusive lock on a file in the app-data folder: the operating system drops it when the
process dies, however it dies, so a crash never leaves the app locked out. A second launch asks the first to open Settings
by touching a request file (nothing listens on a socket for this).
"""

import os
import time
from typing import Optional

from . import paths

try:                  # POSIX
    import fcntl
except ImportError:   # Windows
    fcntl = None      # type: ignore[assignment]
try:                  # Windows
    import msvcrt
except ImportError:   # POSIX
    msvcrt = None     # type: ignore[assignment]


class InstanceLock:
    def __init__(self, home: str = ""):
        self.home = home
        self._handle: Optional[object] = None
        self.path = paths.lock_path(home)

    def acquire(self) -> bool:
        """True when this process now holds the lock; False when another DLNE Donate already runs."""
        paths.ensure_private_dir(os.path.dirname(self.path))
        handle = open(self.path, "a+")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return False
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        except OSError:
            pass
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)   # type: ignore[attr-defined]
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass


def ask_running_instance_to_show(home: str = "") -> None:
    """A second launch: leave a note for the first one."""
    path = paths.show_request_path(home)
    try:
        paths.ensure_private_dir(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
    except OSError:
        pass


def take_show_request(home: str = "") -> bool:
    """Whether a second launch asked this instance to open Settings (and forget the request)."""
    path = paths.show_request_path(home)
    if not os.path.exists(path):
        return False
    try:
        os.remove(path)
    except OSError:
        return False
    return True
