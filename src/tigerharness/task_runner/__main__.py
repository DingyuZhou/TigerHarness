"""Allow `python -m tigerharness.task_runner <subcommand>`."""

import sys

from .cli import main

sys.exit(main())
