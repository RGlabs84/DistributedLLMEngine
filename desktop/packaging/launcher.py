"""PyInstaller's entry point: the same as ``python -m dlne_donate``."""

import sys

from dlne_donate.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
