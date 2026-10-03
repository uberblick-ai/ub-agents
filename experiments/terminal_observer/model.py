"""Presentation of existing status/plans. No coordination or execution writes."""
from pathlib import Path
import socket
import time

from ub_agents.cli import status_rows
from ub_agents.config import Queue, load_config
from ub_agents.errors import GitHubError
from ub_agents.github import GitHub
from ub_agents.loop import Loop
from ub_agents.polling import idle_interval
from tests.support import FakeGitHub, agent, config, issue

GROUPS = ('Running', 'Needs attention', 'Eligible', 'Waiting', 'Recent activity')


def group(row):
    # Display buckets only: the original state and reason remain visible.
    if row['lease'] or row['state'] == 'owned':
        return 'Running'
    if row['outcome'] and row['outcome'].get('accepted') and row['result'] == 'success':
        return 'Recent activity'
    if row['state'] == 'blocked' or (row['state'] == 'parked' and not row['open_blockers']
                                    and not row['reason'].startswith('Waiting')):
        return 'Needs attention'
    if row['state'] in {'ready', 'recover'}:
        return 'Eligible'
    return 'Waiting'


def fixture_loop(root):
    worker = agent(root, kind='issue', outcomes={'done': {'add': ('needs-human',), 'remove': ('ready',)}})
    gh = FakeGitHub(*(issue(n) for n in range(1, 7)))
    gh.change(1, title='Local read-only probe')
    gh.change(2, title='Remote worker (no local log)')
    gh.change(3, title='Owner decision needed', body='Owner must choose attached view or separate observer before implementation.')
    gh.change(4, title='Eligible work')
    gh.change(5, title='Waiting for dependency')
    gh.change(6, title='Completed step')
    gh.dependencies[5] = [4]
    loop = Loop(config(root, worker, queue=Queue(dependencies='wait')), gh, 'operator', output=lambda *_: None)
    for number in (1, 2, 3, 6):
        plan = next(p for p in loop.plans() if p.item.number == number)
        lease = loop.coordinator.claim(plan)
        loop.coordinator.update(lease, state='running', started=True,
                                host=socket.gethostname() if number != 2 else 'remote.example')
        if number == 3:
            loop.coordinator.report(lease, 'blocked', 'Need owner choice: attached view or separate observer')
            loop.coordinator.release(lease, 'blocked', 'Need owner choice: attached view or separate observer')
            gh.change(3, labels=frozenset({'ready', 'needs-human'}))
        if number == 6:
            report = loop.coordinator.report(lease, 'success', 'Read-only verification completed', outcome='done')
            loop.finalize(lease, plan, report, 'fixture completion')
            loop.coordinator.release(lease, 'success', 'Read-only verification completed')
    return loop, gh


class Observer:
    def __init__(self, root, config_path=None):
        self.live = config_path is not None
        if self.live:
            cfg = load_config(config_path)
            self.github = GitHub(cfg.repository)
            # Identity is read once at first explicit refresh, counted there.
            self.loop = None
            self.config = cfg
        else:
            self.loop, self.github = fixture_loop(root)
        self.rows = []
        self.details = {}
        self.last_requests = 0
        self.last_rest = 0
        self.poll = 'not refreshed'
        self.refreshed = None
        self.wait_until = 0.0

    def refresh(self):
        if time.time() < self.wait_until:
            return False
        gh = self.github
        calls = []
        # Count every gh subprocess, including GraphQL and permission reads.
        original = gh.runner if self.live else None
        if self.live:
            import subprocess
            def count(command, **kwargs):
                calls.append(command)
                return (original or subprocess.run)(command, **kwargs)
            gh.runner = count
        rest_before = gh.rest_requests
        start = time.time()
        try:
            if self.loop is None:
                self.loop = Loop(self.config, gh, gh.actor(), output=lambda *_: None)
            rows = status_rows(self.loop)
            # Fixture issue details are already local; live details left unverified.
            details = {n: (i.title, i.body) for n, i in gh.items.items()} if not self.live else {}
            self.rows, self.details = rows, details
            self.refreshed = time.time()
            self.poll = 'manual refresh; idle'
        except GitHubError as error:
            self.wait_until = error.reset_at or (time.time() + 60)
            self.poll = f'quota wait until {self.wait_until:.0f}; stale snapshot' if error.rate_limited else 'read failed; stale snapshot'
        finally:
            self.last_requests = len(calls)
            self.last_rest = gh.rest_requests - rest_before
            if self.live:
                gh.runner = original
        if self.live and self.poll == 'manual refresh; idle':
            gap, low = idle_interval(self.last_rest, 30, gh.resource_quotas, start, time.time() - start)
            self.wait_until = start + gap
            self.poll = f'manual; refresh cooldown {gap:.0f}s' + ('; low quota' if low else '')
        return True

    def simulate_quota(self):
        self.wait_until = time.time() + 60
        self.poll = 'SIMULATED quota wait 60s; stale snapshot; local logs continue'

    def log_allowed(self, row):
        lease = row['lease']
        return bool(lease and lease.get('host') == socket.gethostname()
                    and lease.get('state') == 'running')
