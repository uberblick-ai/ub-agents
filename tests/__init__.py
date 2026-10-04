import os
import sys

# An interactive launch opens the terminal view. The suite must behave the same
# from a developer's terminal as from a pipe, so it never reads the terminal;
# view tests supply their own tty fakes or real ptys.
sys.stdin = open(os.devnull)
