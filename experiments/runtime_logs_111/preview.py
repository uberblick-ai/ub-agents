"""Exact offline preview of the owned recording; never starts a runtime."""
import argparse
import asyncio
from contextlib import suppress
from pathlib import Path
import tempfile

from experiments.terminal_observer.local import fixture_observer
from .view import View

SOURCE = Path('experiments/runtime_logs_111/evidence/claude/process.log')


async def stream(path, data, chunk_bytes, interval):
    # Deliberately controlled cadence, not captured producer timestamps.
    await asyncio.sleep(1)
    for offset in range(0, len(data), chunk_bytes):
        with path.open('ab') as writer:
            writer.write(data[offset:offset + chunk_bytes])
        await asyncio.sleep(interval)


async def preview(args):
    if not args.stream:
        await View(fixture_observer(Path.cwd(), args.log)).run_async()
        return
    private = Path('.ub-agent/spike111-preview')
    private.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=private) as directory:
        path = Path(directory) / 'process.log'
        path.touch()
        writer = asyncio.create_task(stream(path, args.log.read_bytes(), args.chunk_bytes, args.interval))
        try:
            await View(fixture_observer(Path.cwd(), path)).run_async()
        finally:
            writer.cancel()
            with suppress(asyncio.CancelledError):
                await writer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', type=Path, default=SOURCE)
    parser.add_argument('--stream', action='store_true', help='Controlled chunk replay, not original cadence')
    parser.add_argument('--chunk-bytes', type=int, default=256)
    parser.add_argument('--interval', type=float, default=.1)
    args = parser.parse_args()
    if not 1 <= args.chunk_bytes <= 32768 or not .001 <= args.interval <= 5:
        parser.error('chunk bytes must be 1–32768 and interval .001–5 seconds')
    if args.log.name != 'process.log':
        parser.error('--log must name process.log')
    asyncio.run(preview(args))


if __name__ == '__main__':
    main()
