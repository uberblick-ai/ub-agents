"""No UI dependency: print/tail the same bounded local log projection."""
import argparse
from pathlib import Path
import time

from .logs import Tail


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--follow', action='store_true')
    args = parser.parse_args()
    tail = Tail(args.path)
    seen = 0
    try:
        while True:
            tail.poll()
            if tail.error:
                parser.exit(1, f'{tail.error}\n')
            count = tail.buffer.total - seen
            entries = list(tail.buffer.entries)
            if count > len(entries):
                print(f'[observer: skipped {count - len(entries)} records; full bytes in raw file]', flush=True)
            for entry in entries[-count:] if count else []:
                print(entry.display(), flush=True)
            seen = tail.buffer.total
            if not args.follow and tail.offset >= args.path.stat().st_size and not tail.buffer.ready():
                preview = tail.buffer.preview()
                if preview:
                    print(f'[unfinished record] {preview.text}')
                break
            time.sleep(.1)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
