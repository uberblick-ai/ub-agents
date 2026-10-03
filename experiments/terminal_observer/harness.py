"""Runnable single-launcher fixture harness. NO real GitHub or runtime CLI.

A recording transport carries ordinary Loop discovery, claiming, outcome writes,
acceptance and release. A real owned Python process writes runtime replay bytes.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from unittest.mock import patch
from urllib.parse import urlsplit

from ub_agents.config import instruction_text
from ub_agents.github import GitHub
from tests.support import DiscoveryCostRunner, agent, config
from ub_agents.records import iso, timestamp

from .local import LatestFile, Projection, TapLoop
from .replay import write_fixtures


class SessionRunner(DiscoveryCostRunner):
    """Stateful extension of the existing recording fake, not live network IO."""
    def __init__(self):
        super().__init__()
        self.next_id = 1000
        self.execution_entered = threading.Event()
        self.process_started = threading.Event()
        self.reported = threading.Event()
        self.finished = threading.Event()
        self.next_detail_error = None
        self.timeline_labels = {row['number']: [l['name'] for l in row['labels']] for row in self.rows}

    def __call__(self, command, **kwargs):
        endpoint = command[command.index('--include') + 1]
        method = command[command.index('--method') + 1]
        path = urlsplit(endpoint).path
        data = json.loads(kwargs['input']) if kwargs.get('input') else {}
        match = re.fullmatch(r'repos/org/project/issues/(\d+)(?:/(comments|labels)(?:/([^/]+))?)?', path)
        edit = re.fullmatch(r'repos/org/project/issues/comments/(\d+)', path)
        if match:
            number, part, label = int(match[1]), match[2], match[3]
            if part is None:
                if self.next_detail_error:
                    self.calls.append(command)
                    response = self.next_detail_error
                    self.next_detail_error = None
                    return subprocess.CompletedProcess(command, 1, response, 'fixture error')
                response = self.rows[number - 1]
            elif part == 'comments':
                if method == 'GET':
                    response = self.comments[number]
                else:
                    self.next_id += 1
                    response = {'id': self.next_id, 'node_id': f'fixture-{self.next_id}',
                        'body': data['body'], 'user': {'login': 'operator'},
                        'created_at': iso(timestamp()), 'updated_at': iso(timestamp()),
                        'html_url': f'https://example.invalid/comments/{self.next_id}',
                        'issue_url': f'https://api.github.com/repos/org/project/issues/{number}'}
                    self.comments[number].append(response)
            elif part == 'labels':
                if method == 'DELETE':
                    self.rows[number - 1]['labels'] = [l for l in self.rows[number - 1]['labels'] if l['name'] != label]
                    response = None
                else:
                    self.rows[number - 1]['labels'] = [{'name': name} for name in sorted(
                        {l['name'] for l in self.rows[number - 1]['labels']} | set(data['labels']))]
                    response = self.rows[number - 1]['labels']
            self.calls.append(command)
            if method == 'DELETE':
                return subprocess.CompletedProcess(command, 0, '', '')
            return subprocess.CompletedProcess(command, 0, json.dumps(response), '')
        if edit:
            comment = next(c for comments in self.comments.values() for c in comments if c['id'] == int(edit[1]))
            comment.update(body=data['body'], updated_at=iso(timestamp()))
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 0, json.dumps(comment), '')
        if path.endswith('/timeline'):
            self.calls.append(command)
            number = int(path.split('/')[-2])
            response = [{'event': 'labeled', 'actor': {'login': 'maintainer'},
                         'label': {'name': label}, 'created_at': iso(50)} for label in self.timeline_labels[number]]
            return subprocess.CompletedProcess(command, 0, json.dumps(response), '')
        if path == 'graphql' and 'isMinimized' in data.get('query', ''):
            self.calls.append(command)
            aliases = re.findall(r'(n\d+):', data['query'])
            response = {'data': {alias: {'isMinimized': True} for alias in aliases}}
            return subprocess.CompletedProcess(command, 0, json.dumps(response), '')
        return super().__call__(command, **kwargs)


def make_loop(folder, *, projection=None, runtime='claude', seconds=4, human=False):
    runner = SessionRunner()
    paths = write_fixtures(folder / 'replay')
    source = paths[runtime]
    release = folder / 'finish-dummy'
    # Bounded chunks, partial UTF-8/records, normal human text mixed with JSON.
    script = ('import pathlib,sys,time\n'
              'source=pathlib.Path(sys.argv[1]).read_bytes()\n'
              'for i in range(0,len(source),997):\n'
              ' sys.stdout.buffer.write(source[i:i+997]);sys.stdout.buffer.flush();time.sleep(.002)\n'
              'print("ordinary diagnostic after replay",flush=True)\n'
              'finish=pathlib.Path(sys.argv[2]); deadline=time.monotonic()+float(sys.argv[3])\n'
              'while not finish.exists() and time.monotonic()<deadline: time.sleep(.02)\n'
              'print("dummy worker finished; model text is not acceptance",flush=True)\n')
    role = agent(folder, kind='issue', command=(sys.executable, '-c', script, str(source), str(release), str(seconds)),
        timeout_seconds=30, outcomes={'done': {'add': ('needs-human',) if human else (), 'remove': ('ready',)}})
    github = GitHub('org/project', runner=runner)
    loop_cls = TapLoop if projection else __import__('ub_agents.loop', fromlist=['Loop']).Loop
    options = {'projection': projection} if projection else {}
    stop, interrupt = threading.Event(), threading.Event()
    loop = loop_cls(config(folder, role), github, 'operator', output=lambda *_: None,
                    stop_event=stop, interrupt_event=interrupt, **options)
    return loop, runner, release


@contextmanager
def fixture_execution(loop, runner, *, pause_after_report=None):
    from ub_agents.execution import supervise
    def execute(command, cwd, env, run_dir, timeout, interrupt, prompt=None, **kwargs):
        runner.execution_entered.set()
        started = kwargs.get('process_started')
        def process_started(pid):
            if started:
                started(pid)
            runner.process_started.set()
        code = supervise(command, cwd, env, run_dir, timeout, interrupt, prompt,
                         **(kwargs | {'process_started': process_started}))
        # Simulates a report submitted by the dummy agent, through the real transport.
        lease = next(r for r in loop.coordinator.history(1) if r['kind'] == 'lease')
        if code == 0:
            loop.coordinator.report(lease, 'success', 'Owned dummy replay completed', outcome='done')
            runner.reported.set()
            if pause_after_report:
                pause_after_report.wait(10)
        return code
    # Checkout/network Git refresh is deliberately stubbed for the owned fixture root.
    with patch('ub_agents.loop.refresh_instructions', side_effect=lambda cfg, role, github:
               instruction_text(cfg.root, role.instructions, role.name)), \
         patch('ub_agents.loop.supervise', side_effect=execute):
        yield


def run_session(loop, runner, *, pause_after_report=None):
    try:
        with fixture_execution(loop, runner, pause_after_report=pause_after_report):
            return loop.tick()
    finally:
        runner.finished.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True, help='Fresh owned directory inside this checkout')
    parser.add_argument('--runtime', choices=('claude', 'codex'), default='claude')
    parser.add_argument('--seconds', type=float, default=20)
    args = parser.parse_args()
    folder = args.directory.resolve()
    if not folder.is_relative_to(Path.cwd()) or folder.exists():
        parser.error('--directory must be a new directory inside this checkout')
    folder.mkdir(parents=True)
    publisher = LatestFile(folder / 'snapshot.json')
    projection = Projection('org/project', publisher)
    loop, runner, _ = make_loop(folder, projection=projection, runtime=args.runtime, seconds=args.seconds)
    try:
        print(f'Fixture launcher only; view: {sys.executable} -m experiments.terminal_observer.demo --snapshot {publisher.path}', flush=True)
        run_session(loop, runner)
        print(f'Fixture finished; ordinary transport calls: {len(runner.calls)}; snapshot retained', flush=True)
    finally:
        loop.interrupt_event.set()
        publisher.close()


if __name__ == '__main__':
    main()
