"""Reap an owned description request even if its UI is killed."""

import os
import select
import signal
import subprocess
import sys


def run(life, command):
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)
        while process.poll() is None:
            if select.select([life], [], [], 0.05)[0] and not os.read(life, 1):
                break
        code = process.poll()
        return code if code is not None and code >= 0 else 1
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        os.close(life)


if __name__ == '__main__':
    raise SystemExit(run(int(sys.argv[1]), sys.argv[2:]))
