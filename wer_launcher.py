"""PyInstaller entry point.

A dedicated launcher rather than pointing PyInstaller at `src/wer/__main__.py`:
using a package's `__main__` as the entry script makes the frozen module named
`__main__` while `wer.app` also imports `wer`, which can end up with the package
initialised twice under some PyInstaller versions. A three-line launcher sidesteps
the whole question.
"""

import sys

from wer.app import main

if __name__ == "__main__":
    sys.exit(main())
