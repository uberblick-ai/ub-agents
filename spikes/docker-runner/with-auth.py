"""Owner diagnostics within a running container, using its runtime-only token."""

import os
from pathlib import Path
import sys


if len(sys.argv) < 2:
    raise SystemExit("Supply a diagnostic command")
os.environ["GH_TOKEN"] = Path("/run/spike-auth/gh-token").read_text()
os.execvp(sys.argv[1], sys.argv[1:])
