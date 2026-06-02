"""Allow ``python -m tigerharness.journal <subcommand>``."""

import sys

from .cli import main

sys.exit(main())
