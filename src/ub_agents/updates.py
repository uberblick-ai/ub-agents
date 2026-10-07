"""Advisory launcher updates, checked off the execution and discovery paths."""

from datetime import datetime, timezone
from importlib.metadata import distribution, PackageNotFoundError
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlparse

from . import __version__

DAY = 24 * 60 * 60
CHECK_SECONDS = 10
REPOSITORY = 'uberblick-ai/ub-agents'


def installation(root, source=None, dist=None):
    """Only the control source checkout is eligible for commit notices."""
    source = Path(source or __file__).resolve()
    try:
        dist = dist or distribution('ub-agents')
        direct = json.loads(dist.read_text('direct_url.json') or '{}')
        if direct.get('dir_info', {}).get('editable') is True:
            url = urlparse(direct.get('url', ''))
            checkout = Path(unquote(url.path)).resolve() if url.scheme == 'file' else None
            if checkout == Path(root).resolve() and source.is_relative_to(checkout / 'src/ub_agents'):
                return 'checkout'
            return None
        # Also suppress release checks when running source without an editable
        # distribution, e.g. via PYTHONPATH alongside an installed release.
        if source.parent.parent.name == 'src' and (source.parents[2] / 'pyproject.toml').is_file():
            return None
        for parent in source.parents:
            if parent.name == 'ub-agents' and parent.parent.name == 'Cellar':
                return 'brew'
        return 'pip'
    except (PackageNotFoundError, OSError, ValueError, TypeError, AttributeError):
        return None


def version(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value) if isinstance(value, str) else None
    if match is None:
        raise ValueError('Not a release version')
    return tuple(map(int, match.groups()))


def release_banner(data, method, running=__version__):
    if not isinstance(data, dict) or data.get('draft') is True or data.get('prerelease') is True:
        raise ValueError('Not a stable release')
    latest = version(data.get('tag_name'))
    stamp = datetime.fromisoformat(data['published_at'].replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Release timestamp has no timezone')
    if latest <= version(running):
        return None
    command = ('brew update && brew upgrade ub-agents' if method == 'brew'
               else 'pip install -U ub-agents')
    return {'text': f'⬆ ub-agents {".".join(map(str, latest))} is available · you run {running} · '
                    f'{command}, then restart the launcher',
            'released_at': stamp.astimezone(timezone.utc).isoformat()}


def release_age(stamp, now=None):
    try:
        released = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if released.tzinfo is None:
            return ''
        days = max(0, int(((now or datetime.now(timezone.utc)) - released).total_seconds() // DAY))
        return 'released today' if not days else f'released {days} day{"s" if days != 1 else ""} ago'
    except (ValueError, TypeError, AttributeError, OverflowError):
        return ''


class Updates:
    """One background checker and a single immutable result for the launcher.

    No checker callbacks touch coordination, output or observation state. The
    launcher consumes the current result without waiting. A lifecycle pipe lets
    the request supervisor reap its command even if the launcher is killed.
    """
    def __init__(self, root, *, detect=installation, runner=None, clock=time.monotonic):
        self.root, self.detect, self.clock = root, detect, clock
        self.runner = runner or self.command
        self.banner = None
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.life = None
        self.started = self.first_previous = self.fetched_head = None
        self.generation = 0
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name='launcher-updates', daemon=True)
        try:
            self.thread.start()
        except RuntimeError:
            self.thread = None

    def fetched(self, branch, previous, incoming):
        if branch != 'main':
            return
        with self.lock:
            if self.first_previous is None:
                self.first_previous = previous
            self.fetched_head = incoming
            self.generation += 1
        self.wake.set()

    def command(self, args):
        read, life = os.pipe()
        process = None
        registered = False
        try:
            with self.lock:
                if self.stopped.is_set():
                    raise OSError('Checker stopped')
                self.life = life
                registered = True
            env = dict(os.environ, GH_PROMPT_DISABLED='1', GH_PAGER='cat')
            env.pop('GH_DEBUG', None)
            process = subprocess.Popen(
                [sys.executable, '-P', '-m', 'ub_agents.view_request', str(read), *args],
                pass_fds=(read,), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env)
            output, _ = process.communicate(timeout=CHECK_SECONDS)
            if process.returncode:
                raise ValueError('Update check failed')
            return output.decode('utf-8').strip()
        finally:
            os.close(read)
            with self.lock:
                if self.life == life:
                    os.close(life)
                    self.life = None
                elif not registered:
                    os.close(life)
            if process is not None:
                # Closing life cancels the command; its supervisor kills and
                # reaps the whole owned group, then exits within one tick.
                process.communicate()

    def git(self, *args):
        return self.runner(['git', '-C', str(self.root), *args])

    def run(self):
        try:
            method = self.detect(self.root)
            if method is None:
                return
            if method == 'checkout':
                try:
                    head = self.git('rev-parse', 'HEAD')
                except Exception:
                    head = None
                with self.lock:
                    # A first normal refresh can finish during the startup read.
                    # Its pre-merge HEAD still identifies the started source.
                    self.started = self.first_previous or head
            next_release, handled = float('-inf'), 0
            while not self.stopped.is_set():
                self.wake.clear()
                if method == 'checkout':
                    with self.lock:
                        incoming, generation = self.fetched_head, self.generation
                        self.started = self.started or self.first_previous
                    if generation != handled and self.started:
                        handled = generation
                        try:
                            count = int(self.git('rev-list', '--count', f'{self.started}..{incoming}'))
                            if count < 0:
                                raise ValueError('Invalid commit count')
                            banner = {'text': f'⬆ This launcher runs code {count} commits behind origin/main · '
                                              'restart the launcher'} if count else None
                            with self.lock:
                                if generation == self.generation:
                                    self.banner = banner
                        except Exception:
                            pass
                    delay = None
                else:
                    if self.clock() >= next_release:
                        next_release = self.clock() + DAY
                        try:
                            data = json.loads(self.runner([
                                'gh', 'api', '--hostname', 'github.com', '--method', 'GET',
                                f'repos/{REPOSITORY}/releases/latest', '--jq',
                                '{tag_name: .tag_name, published_at: .published_at, '
                                'draft: .draft, prerelease: .prerelease}']))
                            self.banner = release_banner(data, method)
                        except Exception:
                            pass
                    delay = max(0, next_release - self.clock())
                self.wake.wait(delay)
        except Exception:
            pass  # Advisory checks never change launcher eligibility or exit.

    def close(self):
        self.stopped.set()
        self.wake.set()
        with self.lock:
            if self.life is not None:
                os.close(self.life)
                self.life = None
        # A stalled metadata read must not delay even a --once launcher exit.
        # The daemon reaps its request; EOF also cancels it on process death.
