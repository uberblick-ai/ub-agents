"""Run the exact preview in owned real PTYs, without an operator terminal."""
import asyncio
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time


async def exercise(size):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', size[1], size[0], 0, 0))
    arguments = ['-m', 'experiments.runtime_logs_111.preview', '--stream',
                 '--chunk-bytes', '1024', '--interval', '.005']
    process = subprocess.Popen([sys.executable, *arguments], stdin=slave, stdout=slave,
                               stderr=slave, start_new_session=True,
                               env=os.environ | {'TERM': 'xterm-256color'})
    os.close(slave)
    data = bytearray()
    steps = []

    def drain():
        while select.select([master], [], [], 0)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            data.extend(chunk)

    async def step(label, keys=b'', seconds=.4):
        start = len(data)
        if keys:
            os.write(master, keys)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            drain()
            await asyncio.sleep(.02)
        drain()
        plain = re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]', b'', bytes(data[start:]))
        steps.append({'action': label, 'bytes_received': len(data) - start,
                      'follow_painted': b'FOLLOW' in plain,
                      'paused_painted': b'PAUSED' in plain,
                      'json_type_painted': b'"type"' in plain})
        return plain

    try:
        initial = await step('attach and replay captured bytes at controlled cadence', seconds=2)
        assert b'FOLLOW' in initial
        paused = await step('Page Up', b'\x1b[5~')
        assert b'PAUSED' in paused
        await step('Issue pane', b'2')
        await step('Log pane revisit', b'1')
        raw = await step('raw projection', b'u')
        # A paused position may show the middle of a wrapped JSON record. Check
        # the explicit mode, rather than require a particular key in the viewport.
        assert b'PAUSED RAW' in raw
        await step('formatted projection', b'u')
        followed = await step('resume follow', b'f', seconds=.8)
        assert b'FOLLOW' in followed
        await step('quit', b'q', seconds=.2)
        deadline = time.monotonic() + 5
        while process.poll() is None:
            drain()
            assert time.monotonic() < deadline, 'owned terminal did not close'
            await asyncio.sleep(.02)
        drain()
        code = process.wait(timeout=1)
        assert code == 0
        assert b'\x1b[?1049h' in data and b'\x1b[?1049l' in data
        assert b'Traceback' not in data
        return {'size': size, 'TERM': 'xterm-256color',
                'argv': ['.venv/bin/python', *arguments], 'steps': steps,
                'exit_code': code, 'alternate_screen_entered_and_restored': True,
                'owned_process_awaited': True, 'terminal_bytes': len(data),
                'observation': 'real owned PTY and ANSI output inspection; no owner presence or visual usability acceptance'}
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        os.close(master)


async def main():
    results = []
    for size in ((110, 32), (72, 24)):
        result = await exercise(size)
        results.append(result)
        print(f'PASS: owned real terminal {size}: pause, panes, raw, follow, clean exit', flush=True)
    path = Path('experiments/runtime_logs_111/evidence/continuation/terminal.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'owner_usability': 'unverified', 'sessions': results}, indent=2) + '\n')


if __name__ == '__main__':
    asyncio.run(main())
