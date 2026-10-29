"""
``DLNE-Donate`` (or ``python -m dlne_donate``).

    DLNE-Donate                 the tray app
    DLNE-Donate --settings      the tray app, with the Settings window open
    DLNE-Donate --donate        the tray app, donating from the start
    DLNE-Donate --window        no tray icon: the Settings window is the app (a desktop without a system tray)
    DLNE-Donate --version
    DLNE-Donate --hardware      print what this computer can give, as JSON, and exit
    DLNE-Donate --run-worker <engine folder> -- <worker args>
                                internal: how the app runs the verified engine's worker in a child process
"""

import json
import multiprocessing
import os
import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    multiprocessing.freeze_support()     # a frozen app's multiprocessing children start here, before anything else
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv[:1] == ["--run-worker"]:
        # Nothing but the worker: no window, no tray. The folder is one the controller verified.
        from .worker import run_worker_process
        if len(argv) < 3 or argv[2] != "--":
            print("usage: --run-worker <engine folder> -- <worker args>", file=sys.stderr)
            return 2
        return run_worker_process(argv[1], argv[3:])

    from . import APP_NAME, __version__

    if "--version" in argv:
        print(f"{APP_NAME} {__version__}")
        return 0
    if "--hardware" in argv:
        from . import hardware, tiers
        hw = hardware.detect()
        print(json.dumps({
            "gpu": hw.gpu_name, "vram_gb": hw.vram_gb, "gpu_count": hw.gpu_count, "ram_gb": hw.total_ram_gb,
            "physical_cores": hw.physical_cores, "threads": hw.logical_threads, "avx2": hw.avx2,
            "default_threads": tiers.default_threads(hw.physical_cores, hw.logical_threads),
        }, indent=2))
        return 0
    known = {"--settings", "--donate", "--window", "--autostart"}
    unknown = [a for a in argv if a not in known]
    if unknown:
        print(f"Unknown option {unknown[0]!r}. Options: --settings --donate --window --version --hardware", file=sys.stderr)
        return 2

    from .app import App
    code = App(show_settings="--settings" in argv, donate_now="--donate" in argv, window_only="--window" in argv,
               autostart="--autostart" in argv).run()
    # Everything was stopped on the way out (worker, tray, lock). The tray library's own thread can outlive the window
    # on a desktop with no tray, so the process ends here instead of waiting for it.
    flush_standard_streams()
    os._exit(code)


def flush_standard_streams() -> None:
    """
    Flush stdout and stderr, if there are any. A windowed Windows build (and a Linux app started from a launcher with its
    output closed) has ``sys.stdout`` and ``sys.stderr`` set to None, and Wine hides that by handing the process a console.
    Nothing here may raise: the process is about to end.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:  # noqa: BLE001 - a closed or broken stream must not stop the exit
            pass


if __name__ == "__main__":
    sys.exit(main())
