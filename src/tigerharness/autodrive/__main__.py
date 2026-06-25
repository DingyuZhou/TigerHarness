"""Allow ``python -m tigerharness.autodrive <subcommand>``.

This is how ``autodrive start`` re-launches the detached daemon
(``python -m tigerharness.autodrive _loop --state-file ...``), which works
regardless of whether the ``tigerharness`` console script is on PATH in
the spawned environment.
"""

import sys

from .cli import main

sys.exit(main())
