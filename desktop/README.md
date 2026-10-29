# DLNE Donate

A small tray app for Windows and Linux that lends this computer's GPU or CPU to the DLNE swarm, without VS Code.
It does what the VS Code extension's worker manager does (`vscode-extension/src/worker/workerManager.ts`): ask the
coordinator which version it runs, fetch and verify the signed engine for that version, run the worker, show what the
swarm made of this machine.

Python 3.12, Tk for the window, `pystray` for the tray icon, PyInstaller for the executables.
The Windows build is cross-compiled from Linux under Wine.

## What a donor can usefully give

| Lend | Becomes | Needs |
| --- | --- | --- |
| A GPU | a **coding member**: runs a whole Qwen3.6 coding model and answers other people's coding requests | at least 8 GB of VRAM, plus about 16 to 24 GB of RAM beside it for the model's experts |
| A smaller GPU | a share of the **GPU pool** (one big model run together with other volunteers) | any GPU the swarm counts (a discrete one, Intel Arc 140V/140T, AMD Strix Halo) |
| A CPU with RAM | a **phone-app member**: runs the model behind the DLNE phone app | 4 physical cores, AVX2, 8 GB lent; the RAM lent picks the model (8 GB Qwen3 4B, 16 GB Qwen3.6, 24 GB full quality) |

RAM alone, disk space and bandwidth are not useful to the swarm and are not offered.

The Settings window follows exactly that:

* **GPU**: on/off, the VRAM to lend, the RAM that goes with it. A live line says what it powers (for a coding member:
  which model and an estimated speed, from `engine/coding_member_select.py` and the signed model list), or that it is
  below what the swarm can use.
* **CPU**: on/off, RAM that snaps to the swarm's model stops (read live from `/v1/cluster/status`, `app_pool.tiers`,
  each stop labelled with the model it runs).
* **Threads**: they mostly decide how much of the PC stays free. Default: physical cores minus 2, at least 4.
* Never more than half the installed RAM is offered by default; dragging past it (up to the installed RAM minus 4 GB)
  shows a warning.

Settings are saved as JSON (`settings.json`). Nothing runs until the owner presses Donate (or, if they chose it,
"Start donating when the app starts").

## Running it

From a checkout (Python 3.12 with Tk):

    pip install -r desktop/requirements.txt
    cd desktop && python -m dlne_donate            # tray app
    python -m dlne_donate --settings               # with the Settings window open
    python -m dlne_donate --window                 # no tray icon (a desktop without a system tray)
    python -m dlne_donate --hardware               # what this computer can give, as JSON

On a Linux desktop with no system tray (GNOME without its AppIndicator extension) the app notices that the icon was
not docked and shows its window instead, which then has a Quit button.

Files are kept per user, no administrator rights:

| | Windows | Linux |
| --- | --- | --- |
| App data | `%LOCALAPPDATA%\DLNE Donate` | `~/.local/share/dlne-donate` (`$XDG_DATA_HOME`) |
| Autostart ("Start with my computer") | `HKCU\...\Run` value `DLNE-Donate` | `~/.config/autostart/dlne-donate.desktop` |

Inside the app-data folder: `settings.json`, `donate.log` (this app's words and the worker's output; it stays on the
computer), `state.json` (what the app is doing now), and `engine/`, the verified engine with its `config/config.txt`,
`bin/` (llama.cpp programs) and `models/` (a coding member's model is downloaded once, about 13 to 29 GB, and kept).

## How the worker is run, and why it is safe

1. `GET <coordinator>/v1/release`: the signed manifest. Its Ed25519 signature must verify against the maintainer's key
   (`engine/release.py` `RELEASE_PUBKEY`, the same key the extension carries). Not signed, or signed by someone else:
   the app stops, says so, and runs nothing.
2. `/v1/download/source`: exactly the signed size and SHA-256, the signed version inside, and only the files a release
   may hold (`dlne_server.py`, `requirements.txt`, `engine/*.py`, `engine/static/*.png`). Anything else, or a name that
   would leave the engine folder, refuses the whole archive.
3. `/v1/download/rpc-bin/<platform>`: the llama.cpp programs for this platform, signed size and hash, and nothing in the
   zip but `bin/<servers and their libraries>`.
4. A release never goes backwards.
5. Every installed file's SHA-256 is recorded when it is verified and checked again before each start, so a changed file
   is put back from the signed archive instead of being run.
6. `config.txt` is written with the limits and the public network key; the worker runs as a child process of the app,
   from that folder (`DLNE-Donate --run-worker <folder> -- worker --coordinator ...`). A frozen app has no Python to hand
   the engine to, so it carries the libraries the engine imports (`requirements.txt`, checked against the engine by a
   test) and runs the engine's own source from the verified folder. If a later engine wants a library the app lacks, the
   app says to update itself instead of failing with an ImportError.
7. Exit 75 (the coordinator runs another version): fetch the new verified engine and restart, at most twice in ten
   minutes. Exit 78: the coordinator's refusal is shown. Slider changes rewrite `config.txt`; the running worker re-reads it.
8. The worker's `DLNE_ROLE`, `DLNE_ALSO`, `DLNE_REFUSAL` and `DLNE_CLOCK` lines are shown in the tray tooltip and the window,
   and so is what its log says about the model (downloading 40%, serving). If the app is killed, its worker notices within
   two seconds and ends itself.

Being polite: one instance (a lock file; a second launch opens the first one's Settings); Stop asks the worker to shut
down, then ends its whole process tree and any program running from the engine folder, and Quit does the same; no admin
rights; the app opens no socket of its own (the worker listens as it always does); no telemetry: the only network
traffic is the coordinator (`/v1/release`, `/v1/cluster/status`, the downloads it names) and, for a coding member, the
model files from Hugging Face that the worker verifies against the signed list.

## Building

Linux x86-64 (a one-folder app and a tar.gz), needs a Python 3.12 **with Tk** (python-build-standalone has it; check
its SHA-256 against the release's `SHA256SUMS`):

    DLNE_BUILD_PYTHON=/path/to/python/bin/python3 DLNE_BUILD_DIR=/path/to/scratch desktop/packaging/build_linux.sh

Linux AppImage (wraps the folder build; needs `appimagetool`, whose SHA-256 the script can check):

    DLNE_BUILD_DIR=/path/to/scratch DLNE_APPIMAGETOOL=/path/to/appimagetool-x86_64.AppImage \
    DLNE_APPIMAGETOOL_SHA256=<published hash> desktop/packaging/build_appimage.sh

Windows, cross-built under Wine with the real Windows Python and wheels (never use `~/.wine`):

    DLNE_WINE_PREFIX=/path/to/new/prefix desktop/packaging/setup_wine_python.sh     # once; verifies python.org's GPG signature
    DLNE_WINE_PREFIX=/path/to/new/prefix DLNE_BUILD_DIR=/path/to/scratch desktop/packaging/build_windows.sh

Heavy steps (pip installs, PyInstaller, Wine) belong behind `~/valheim-testbed/heavy.sh <cap> <name> <command>` on the
laptop. Tests (no network, no display):

    python -m pytest desktop/tests -q -p no:cacheprovider
    python -m pytest tests/test_desktop_not_shipped.py        # desktop/ is never in the engine release

`desktop/` is deliberately not in `engine/release.py`'s allow-list: the coordinator's source release never carries it.

## Files

    dlne_donate/        the app
      installer.py      fetch + verify + safe unpack of the engine and llama.cpp programs ("nothing unverified runs")
      worker.py         the controller: start, stop (whole tree), version change, refusal, live config
      tiers.py          the smart-slider logic and config.txt keys (pure)
      markers.py        DLNE_ROLE / DLNE_ALSO / DLNE_REFUSAL / DLNE_CLOCK
      ui.py trayicon.py app.py model.py     the window, the icon and menu, the wiring
      configfile.py settings.py autostart.py instance.py hardware.py netstatus.py bundled.py paths.py
    packaging/          PyInstaller spec, build scripts for Linux and Windows
      engine_snapshot.py  the engine files the Settings window is built with (hardware, release, ...) and
                          snapshot_engine/logger.py, a quiet stand-in for the engine's logger (see bundled.py)
    tests/              pytest
