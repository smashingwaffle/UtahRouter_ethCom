"""Launcher (also the PyInstaller entry point)."""
import sys

from utah_eth2rs422.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
