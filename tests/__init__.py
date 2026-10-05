import os
import sys

# An interactive launch opens the terminal view. The suite must behave the same
# from a developer's terminal as from a pipe, so it never reads the terminal;
# view tests supply their own tty fakes or real ptys.
sys.stdin = open(os.devnull)

# A supervised run exports UB_AGENTS_* to everything its agent starts, including
# `mise run ci` and this suite. The launcher drops inherited ones before setting
# its own, and so does the suite: tests that need them set them explicitly.
for name in [name for name in os.environ if name.startswith('UB_AGENTS_')]:
    del os.environ[name]
