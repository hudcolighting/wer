"""Entry point for `python -m wer`.

The frozen exe uses the same path via `main()`.
"""

import sys

from wer.app import main

if __name__ == "__main__":
    sys.exit(main())
