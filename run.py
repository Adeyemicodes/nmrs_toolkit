#!/usr/bin/env python3
"""PyInstaller entry point. `python -m nmrs_toolkit` is the dev invocation;
the frozen binary runs this, which dispatches the same way (headless flags vs
GUI). Kept tiny so PyInstaller's analysis starts from a plain top-level script.
"""
from nmrs_toolkit.__main__ import main

if __name__ == "__main__":
    main()
