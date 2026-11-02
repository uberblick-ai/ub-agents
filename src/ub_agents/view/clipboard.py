"""Best-effort local macOS clipboard support alongside the terminal's OSC 52."""

import asyncio
import os
import shutil
import sys


def local_pbcopy():
    if sys.platform != 'darwin' or 'SSH_CONNECTION' in os.environ or 'SSH_TTY' in os.environ:
        return None
    return shutil.which('pbcopy')


async def copy_with_pbcopy(command, value):
    try:
        process = await asyncio.create_subprocess_exec(
            command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return
    try:
        await asyncio.wait_for(process.communicate(value.encode('utf-8')), timeout=1)
    except (OSError, asyncio.TimeoutError):
        pass
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
