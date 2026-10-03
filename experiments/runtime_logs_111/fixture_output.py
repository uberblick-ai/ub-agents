"""Owned, deterministic command output; reads/writes no files, uses no network."""
import sys

print('SPIKE111_COMMAND_BEGIN', flush=True)
for row in range(240):
    print(f'SPIKE111_ROW_{row:03d}: owned fixture output, café, plain [text]', flush=True)
print('SPIKE111_LONG_BEGIN ' + 'abcdefghij' * 600 + ' SPIKE111_LONG_END', flush=True)
print('SPIKE111_STDERR: owned diagnostic, command still succeeds', file=sys.stderr, flush=True)
print('SPIKE111_COMMAND_END', flush=True)
