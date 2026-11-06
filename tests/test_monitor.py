"""The browser monitor shows every launcher's trusted runs and serves only this machine."""

from contextlib import chdir, redirect_stderr, redirect_stdout
from dataclasses import replace
from io import BytesIO, StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote

from ub_agents.config import Priority, Queue, Runtime
from ub_agents.cli import main
from ub_agents.coordination import Coordinator, Plan
from ub_agents.errors import AgentError
from ub_agents.loop import Loop
from ub_agents.monitor import monitor_background, monitor_sources
from ub_agents.monitor.eligible import EligiblePlanning, PLAN_SECONDS
from ub_agents.monitor.local import runtime_pauses
from ub_agents.monitor.server import Repositories, Snapshots, allowed, create, page
from ub_agents.monitor.snapshot import MonitorSource
from ub_agents.records import body, iso, timestamp
from tests.support import FakeGitHub, PollGitHub, agent, config, issue, pr


def lease(number, run, **changes):
    now = timestamp()
    return {"kind": "lease", "run": run, "agent": "worker", "assignment": number, "assignment_sha": None,
            "runtime": "direct", "triggers": [], "actor": "operator", "created": iso(now - 60),
            "expires": iso(now + 600), "state": "running", "attempt": 1, "started": True,
            "attempt_effect": "pending", "host": "server"} | changes


def snapshot_request(port):
    request = Mock()
    request.makefile.return_value = BytesIO(
        f"GET /snapshot.json HTTP/1.0\r\nHost: localhost:{port}\r\n\r\n".encode())
    return request


def checkout(root, name):
    folder = root / name
    folder.mkdir()
    (folder / 'ub-agents.yaml').write_text(
        f'repository: org/{name}\nagents:\n  worker:\n    command: [echo]\n'
        '    trigger: ready\n    outcomes: {done: {}}\n')
    return folder


class MonitorTests(unittest.TestCase):
    def test_monitor_file_works_without_a_project_and_preserves_checkout_order_and_overrides(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            first, second = checkout(root, 'backend'), checkout(root, 'studio')
            (second / 'ub-agents.local.yaml').write_text('stop-labels: [pause]\n')
            (root / 'ub-agents-monitor.yaml').write_text('checkouts: [studio/ub-agents.yaml, backend]\n')
            with chdir(root), patch('ub_agents.monitor.monitor_sources') as sources, \
                    patch('ub_agents.monitor.serve', return_value=0) as serve:
                self.assertEqual(main(['monitor', '--no-open']), 0)
            configs = sources.call_args.args[0]
            self.assertEqual([cfg.repository for cfg in configs], ['org/studio', 'org/backend'])
            self.assertEqual([cfg.root for cfg in configs], [second, first])
            self.assertEqual(configs[0].stop_labels, ('pause',))
            serve.assert_called_once_with(sources.return_value, 4377, open_page=False)

    def test_monitor_file_yields_to_explicit_checkouts_and_config_but_wins_over_current_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            listed, explicit = checkout(root, 'listed'), checkout(root, 'explicit')
            (root / 'ub-agents.yaml').write_text((explicit / 'ub-agents.yaml').read_text())
            monitor = root / 'ub-agents-monitor.yaml'
            monitor.write_text('checkouts: [listed]\n')
            with chdir(root), patch('ub_agents.monitor.monitor_sources') as sources, \
                    patch('ub_agents.monitor.serve', return_value=0):
                self.assertEqual(main(['monitor']), 0)
                self.assertEqual(sources.call_args.args[0][0].root, listed)
                monitor.write_text('invalid: file\n')
                for arguments in (['monitor', 'explicit'], ['monitor', '--config', 'explicit/ub-agents.yaml'],
                                  ['--config', 'explicit/ub-agents.yaml', 'monitor'],
                                  ['monitor', '--config', 'ub-agents.yaml']):
                    with self.subTest(arguments=arguments):
                        self.assertEqual(main(arguments), 0)
                        self.assertEqual(sources.call_args.args[0][0].repository, 'org/explicit')

    def test_monitor_ignores_parent_monitor_file_and_keeps_missing_project_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            child = checkout(root, 'child')
            (root / 'ub-agents-monitor.yaml').write_text('invalid: file\n')
            with chdir(child), patch('ub_agents.monitor.monitor_sources') as sources, \
                    patch('ub_agents.monitor.serve', return_value=0):
                self.assertEqual(main(['monitor']), 0)
                self.assertEqual(sources.call_args.args[0][0].root, child)
                (child / 'ub-agents.yaml').unlink()
                with redirect_stderr(StringIO()) as error:
                    self.assertEqual(main(['monitor']), 1)
                self.assertIn('No ub-agents.yaml here', error.getvalue())

    def test_monitor_file_rejects_invalid_schema_before_starting(self):
        cases = [('extra: true\ncheckouts: [backend]', 'extra'), ('{}', 'checkouts'),
                 ('checkouts: []', 'checkouts'), ('checkouts: backend', 'checkouts'),
                 ('checkouts: null', 'checkouts'), ('[]', 'mapping'), ('checkouts: [', 'parsing'),
                 ('checkouts: [backend]\ncheckouts: [studio]', 'unique'),
                 ('checkouts: [null]', 'entry 1 (None)'), ('checkouts: [42]', 'entry 1 (42)'),
                 ('checkouts: [true]', 'entry 1 (True)'), ('checkouts: [{}]', 'entry 1 ({})'),
                 ('checkouts: [""]', "entry 1 ('')"), ('checkouts: ["\\0"]', 'entry 1')]
        with tempfile.TemporaryDirectory() as folder, chdir(folder):
            monitor = Path(folder).resolve() / 'ub-agents-monitor.yaml'
            for content, message in cases:
                monitor.write_text(content)
                with self.subTest(content=content), patch('ub_agents.monitor.monitor_sources') as sources, \
                        patch('ub_agents.monitor.monitor_background') as background, \
                        redirect_stderr(StringIO()) as error:
                    self.assertEqual(main(['monitor', '--background']), 1)
                    self.assertIn(str(monitor), error.getvalue())
                    self.assertIn(message, error.getvalue())
                    sources.assert_not_called()
                    background.assert_not_called()

    def test_monitor_file_rejects_missing_invalid_and_duplicate_checkouts_before_starting(self):
        with tempfile.TemporaryDirectory() as folder, chdir(folder):
            root = Path(folder).resolve()
            checkout(root, 'good')
            bad = checkout(root, 'bad')
            (bad / 'ub-agents.yaml').write_text('repository: [invalid]\n')
            monitor = root / 'ub-agents-monitor.yaml'
            for entries, messages in [('good, missing', ('missing',)), ('good, bad', ('bad', 'repository')),
                                      ('good, good/ub-agents.yaml', ('good', 'good/ub-agents.yaml', 'same repository')),
                                      ('good, ~ub-agents-no-such-user', ('~ub-agents-no-such-user',))]:
                monitor.write_text(f'checkouts: [{entries}]\n')
                with self.subTest(entries=entries), patch('ub_agents.monitor.monitor_sources') as sources, \
                        patch('ub_agents.monitor.monitor_background') as background, \
                        redirect_stderr(StringIO()) as error:
                    self.assertEqual(main(['monitor', '--background']), 1)
                    self.assertIn(str(monitor), error.getvalue())
                    for message in messages:
                        self.assertIn(message, error.getvalue())
                    sources.assert_not_called()
                    background.assert_not_called()

    def test_background_monitor_file_expands_home_passes_resolved_checkouts_and_logs_next_to_file(self):
        with tempfile.TemporaryDirectory() as folder, chdir(folder):
            root = Path(folder).resolve()
            first, second = checkout(root, 'backend'), checkout(root, 'studio')
            (root / 'ub-agents-monitor.yaml').write_text('checkouts: [~/backend, studio/ub-agents.yaml]\n')
            with patch('os.path.expanduser', side_effect=lambda path: path.replace('~', str(root), 1)), \
                    patch('ub_agents.monitor.already_serving', side_effect=[False, True]), \
                    patch('ub_agents.monitor.subprocess.Popen') as popen, redirect_stdout(StringIO()):
                self.assertEqual(main(['monitor', '--background', '--no-open', '--port', '4400']), 0)
            self.assertEqual(popen.call_args.args[0][-6:],
                             ['monitor', str(first / 'ub-agents.yaml'), str(second / 'ub-agents.yaml'),
                              '--port', '4400', '--no-open'])
            self.assertEqual(popen.call_args.kwargs['stdout'].name, str(root / '.ub-agents' / 'monitor.log'))

    def test_cli_loads_each_checkout_and_its_local_overrides_before_serving(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            first, second = root / 'backend', root / 'studio'
            first.mkdir()
            second.mkdir()
            content = ('repository: org/backend\nagents:\n  worker:\n    command: [echo]\n'
                       '    trigger: ready\n    outcomes: {done: {}}\n')
            (first / 'ub-agents.yaml').write_text(content)
            (second / 'ub-agents.yaml').write_text(content.replace('org/backend', 'org/studio'))
            (second / 'ub-agents.local.yaml').write_text('stop-labels: [pause]\n')
            with patch('ub_agents.monitor.monitor_sources') as sources, \
                    patch('ub_agents.monitor.serve') as serve:
                serve.return_value = 0
                self.assertEqual(main(['monitor', str(first), str(second / 'ub-agents.yaml'), '--no-open']), 0)
            configs = sources.call_args.args[0]
            self.assertEqual([cfg.repository for cfg in configs], ['org/backend', 'org/studio'])
            self.assertEqual([cfg.root for cfg in configs], [first, second])
            self.assertEqual(configs[1].stop_labels, ('pause',))
            serve.assert_called_once_with(sources.return_value, 4377, open_page=False)

    def test_cli_without_checkouts_uses_the_current_config_and_background_passes_all_paths(self):
        with patch('ub_agents.cli.monitor.load_config', return_value=config(Path('/checkout'))), \
                patch('ub_agents.monitor.monitor_sources') as sources, \
                patch('ub_agents.monitor.serve', return_value=0), \
                patch('ub_agents.monitor.monitor_background', return_value=0) as background:
            self.assertEqual(main(['monitor']), 0)
            self.assertEqual(sources.call_args.args[0][0].repository, 'org/project')
            self.assertEqual(main(['monitor', '--config', 'ub-agents.yaml', '--background', '--no-open']), 0)
            background.assert_called_once_with([Path('ub-agents.yaml').resolve()], Path('/checkout'), 4377,
                                               open_page=False)
        with patch('ub_agents.cli.monitor.load_config', side_effect=[config(Path('/a')),
                replace(config(Path('/b')), repository='org/second')]), \
                patch('ub_agents.monitor.monitor_background', return_value=0) as background:
            self.assertEqual(main(['monitor', 'a.yaml', 'b.yaml', '--background', '--no-open']), 0)
            self.assertEqual(background.call_args.args[0], [Path('a.yaml').resolve(), Path('b.yaml').resolve()])

    def test_cli_rejects_mixed_config_duplicates_and_bad_checkouts_before_starting(self):
        cases = [(['A', '--config', 'custom.yaml'], None, ('--config', 'A')),
                 (['A', 'B'], [config(Path('/a')), config(Path('/b'))], ('A', 'B', 'org/project')),
                 (['A', 'B'], [config(Path('/a')), AgentError('invalid YAML')], ('B', 'invalid YAML'))]
        for arguments, loaded, messages in cases:
            with self.subTest(arguments=arguments), \
                    patch('ub_agents.cli.monitor.load_config', side_effect=loaded), \
                    patch('ub_agents.monitor.monitor_sources') as sources, \
                    patch('ub_agents.monitor.monitor_background') as background, \
                    redirect_stderr(StringIO()) as error:
                self.assertEqual(main(['monitor', *arguments]), 1)
                for message in messages:
                    self.assertIn(message, error.getvalue())
                sources.assert_not_called()
                background.assert_not_called()

    def test_repositories_read_in_parallel_reuse_independently_and_isolate_failures(self):
        gate = threading.Barrier(2)
        githubs = [FakeGitHub(issue(1)), FakeGitHub(issue(2))]
        sources = [MonitorSource(replace(config(Path(f'/checkout/{n}')), repository=f'org/repo{n}'),
                                 github, Coordinator(github, None), timestamp)
                   for n, github in enumerate(githubs)]
        snapshots = Repositories(sources)
        def together(source):
            gate.wait(timeout=5)
            return MonitorSource.snapshot(source)
        with patch.object(sources[0], 'snapshot', side_effect=lambda: together(sources[0])) as first, \
                patch.object(sources[1], 'snapshot', side_effect=lambda: together(sources[1])) as second:
            result = snapshots.get()
            self.assertEqual([entry['repository'] for entry in result['repositories']], ['org/repo0', 'org/repo1'])
            snapshots.get()
            first.assert_called_once_with()
            second.assert_called_once_with()
        githubs[1].create_comment(2, body(lease(2, 'new')))
        with patch.object(sources[0], 'snapshot', side_effect=AgentError('repo0 unavailable')):
            failed = snapshots.get(fresh=True)
        old, current = failed['repositories']
        self.assertEqual(old, result['repositories'][0] | {'error': 'repo0 unavailable'})
        self.assertIsNone(current['error'])
        self.assertEqual([run['item']['number'] for run in current['running']], [2])
        self.assertIsNone(snapshots.get(fresh=True)['repositories'][0]['error'])
        # Reusing the first repository does not prevent the second from reading again.
        snapshots.snapshots[1].read_at = None
        with patch.object(sources[0], 'snapshot') as first, patch.object(sources[1], 'snapshot', wraps=sources[1].snapshot) as second:
            snapshots.get()
            first.assert_not_called()
            second.assert_called_once_with()

    def test_initial_failure_has_only_identity_and_error_and_still_serves_json(self):
        github = FakeGitHub()
        source = MonitorSource(config(Path('/checkout')), github, Coordinator(github, None), timestamp)
        with patch.object(source, 'snapshot', side_effect=RuntimeError('read failed')), \
                self.assertLogs('ub_agents.monitor.server', level='ERROR') as logged:
            with create(Repositories([source]), port=0) as server:
                request = snapshot_request(server.server_address[1])
                server.process_request_thread(request, ('127.0.0.1', 12345))
        response = b''.join(call.args[0] for call in request.sendall.call_args_list)
        self.assertIn(b'200 OK', response)
        data = json.loads(response.split(b'\r\n\r\n', 1)[1])
        self.assertEqual(set(data), {'generated_at', 'local_host', 'rates', 'repositories'})
        self.assertEqual(data['repositories'], [{'repository': 'org/project', 'folder': '/checkout', 'error': 'read failed'}])
        self.assertIn("Couldn't refresh monitor repository org/project", logged.output[0])
        self.assertIn('Traceback (most recent call last):', logged.output[0])
        self.assertIn('RuntimeError: read failed', logged.output[0])

    def test_expected_repository_read_errors_are_quiet_and_keep_the_last_result(self):
        github = FakeGitHub()
        source = MonitorSource(config(Path('/checkout')), github, Coordinator(github, None), timestamp)
        snapshots = Repositories([source])
        previous = snapshots.get()['repositories'][0]
        for error in (AgentError('login required'), OSError('connection lost')):
            with self.subTest(error=error), patch.object(source, 'snapshot', side_effect=error), \
                    self.assertNoLogs('ub_agents.monitor.server'):
                self.assertEqual(snapshots.get(fresh=True)['repositories'][0], previous | {'error': str(error)})

    def test_source_github_setup_is_deferred_and_retries_after_an_initial_failure(self):
        github = FakeGitHub(issue(1))
        github.actor = Mock(side_effect=[AgentError('account unavailable'), 'operator'])
        with patch('ub_agents.monitor.GitHub', return_value=github):
            sources = monitor_sources([config(Path('/checkout'))])
        github.actor.assert_not_called()
        snapshots = Repositories(sources)
        self.assertEqual(snapshots.get()['repositories'][0]['error'], 'account unavailable')
        recovered = snapshots.get(fresh=True)['repositories'][0]
        self.assertIsNone(recovered['error'])
        self.assertEqual(recovered['eligible']['count'], 1)

    def test_rates_use_the_latest_headers_per_resource_and_sum_client_counters(self):
        sources = []
        for index in (0, 1):
            github = FakeGitHub()
            github.resource_quotas = {'core': {'x-ratelimit-remaining': str(4000 + index),
                'x-ratelimit-limit': '5000', 'x-ratelimit-reset': '8000'},
                'graphql': {'x-ratelimit-remaining': str(500 + index),
                'x-ratelimit-limit': '1000', 'x-ratelimit-reset': '9000'}}
            github.resource_quota_times = {'core': 2 - index, 'graphql': index}
            github.quota_requests, github.graphql_calls, github.not_modified_responses = 3 + index, 1, 4
            sources.append(MonitorSource(config(Path('/checkout')), github, Coordinator(github, None), timestamp))
        rates = Repositories(sources).rates()
        self.assertEqual(rates, {'core': {'remaining': 4000, 'limit': 5000, 'reset': 8000},
            'graphql': {'remaining': 501, 'limit': 1000, 'reset': 9000},
            'counted': 7, 'graphql_calls': 2, 'not_modified': 8})

    def test_page_read_errors_repository_filtering_notifications_and_recovery(self):
        try:
            subprocess.run(['node', '--version'], check=True, capture_output=True, timeout=5)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            self.skipTest('Node.js must be runnable to run the browser script')
        result = subprocess.run(['node', str(Path(__file__).with_name('monitor_page.cjs'))],
                                input=page().decode(), text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_page_combines_panels_and_names_repositories_without_changing_storage_keys(self):
        html = page().decode()
        for expected in ('snapshot.repositories.map', 'source.repository : short(source.repository)',
                         'needs_human: rows("needs_human")',
                         'time(a.started) - time(b.started)', 'time(a.expires) - time(b.expires)',
                         'time(b.ended) - time(a.ended)',
                         'data.multiple ? eligibleGroups(data.repositories) : eligiblePanel(data.eligible)',
                         '!source.eligible || source.eligible.count',
                         'source.eligible?.rows.length', 'source.eligible ? eligibleRows(source.eligible)',
                         'item.repository_name', 'params.set("folder", item.folder)',
                         'Showing its last result; other repositories are current.',
                         '`${source.name}: `', 'source.notices ?? []', 'source.eligible_error',
                         'ub-agents-monitor.notify-events-v2', 'ub-agents-monitor.machine',
                         'ub-agents-monitor.repository', '"aria-label": "Repository"'):
            self.assertIn(expected, html)

    def test_notification_keys_titles_and_outcomes_preserve_repository_identity(self):
        html = page().decode()
        for expected in ('`${entry.item.repository}:${entry.item.number}',
                         '`${run.item.repository}:${run.id}`',
                         '${title} · ${item.repository_name} ${ref(item)} ${item.title}',
                         'outcomes.set(name, { name, agents: new Set(), repositories: [] })',
                         'outcome.repositories.length === repositories.length ? null',
                         '${repositories.join(", ")} only',
                         'entry.stop_outcomes.some'):
            self.assertIn(expected, html)

    def test_page_has_the_robot_favicon_as_an_inline_svg(self):
        html = page().decode()
        self.assertIn('<link rel="icon" href="data:image/svg+xml,', html)
        svg = unquote(html.split('href="data:image/svg+xml,', 1)[1].split('"', 1)[0])
        self.assertEqual(svg, (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            '<style>.tile{fill:#1d1b24}@media (prefers-color-scheme:dark){.tile{fill:#2c2937}}</style>'
            '<rect class="tile" width="64" height="64" rx="14"/>'
            '<path d="M32 8V17" stroke="#fff" stroke-width="5"/>'
            '<circle cx="32" cy="8" r="4.5" fill="#bc7509"/>'
            '<path d="M8 27V37M56 27V37" stroke="#fff" stroke-width="5" stroke-linecap="round"/>'
            '<rect x="12" y="16" width="40" height="30" rx="8" fill="#fff"/>'
            '<rect class="tile" x="17" y="23" width="30" height="16" rx="6"/>'
            '<circle cx="25" cy="31" r="4" fill="#bc7509"/>'
            '<circle cx="39" cy="31" r="4" fill="#bc7509"/>'
            '<rect x="9" y="52" width="46" height="5" rx="1" fill="#bc7509"/>'
            '</svg>'
        ))

    def test_page_titles_snapshots_with_the_repository_short_name(self):
        html = page().decode()
        self.assertIn('<title>ub-agents</title>', html)
        self.assertIn('document.title = titleOf(data)', html)
        self.assertIn('`(${rows.length}) needs human · ${repositories}`', html)
        self.assertIn('`${rows.length} running · ${repositories}` : repositories', html)
        self.assertIn('`${names[0]} +${names.length - 1}` : names.join(", ")', html)
        self.assertIn('row.item.repository === source.repository', html)

    def test_snapshot_identifies_the_serving_host_and_preserves_record_hosts(self):
        github = FakeGitHub(issue(1), issue(2), issue(3))
        github.create_comment(1, body(lease(1, 'local', host='laptop')))
        github.create_comment(2, body(lease(2, 'remote', host='loop-server')))
        github.create_comment(3, body(lease(3, 'ended', host='laptop', state='released',
                                           result='success', attempt_effect='unchanged')))
        writes = list(github.writes)
        source = MonitorSource(config(Path('/checkout')), github, Coordinator(github, None), timestamp)

        with patch('ub_agents.monitor.snapshot.socket.gethostname', return_value='laptop'):
            snapshot = source.snapshot()

        self.assertEqual(snapshot['local_host'], 'laptop')
        self.assertEqual([run['host'] for run in snapshot['running']], ['laptop', 'loop-server'])
        self.assertEqual([run['host'] for run in snapshot['recent']], ['laptop'])
        self.assertEqual(github.writes, writes)

    def test_page_has_a_remembered_machine_dropdown_scoped_to_running(self):
        html = page().decode()
        self.assertIn('class: "machine-picker muted"', html)
        self.assertIn('"aria-label": "Running machine"', html)
        self.assertIn('All Machines (${hosts.length})', html)
        self.assertIn('stored(MACHINE_KEY, null)', html)
        self.assertIn('store(MACHINE_KEY, state.machine)', html)
        self.assertIn('[...data.running, ...data.recent]', html)
        self.assertIn('if (state.machine) others.add(state.machine)', html)
        self.assertIn('[data.local_host, ...[...others].sort()]', html)
        self.assertIn('host === state.data?.local_host ? "this machine"', html)
        self.assertIn('runs.filter((run) => run.host === state.machine)', html)
        self.assertIn('panel("Running", shown.length', html)
        self.assertIn('state.machine === null ? "any machine" : machine(state.machine)', html)
        self.assertIn('needsPanel(data.needs_human, data.stop_labels,', html)
        self.assertIn('recentPanel(data.recent)', html)
        self.assertIn('started: new Set(data.running.map(runKey))', html)

    def test_planner_uses_the_local_runtime_pause_and_recovers_after_it_ends(self):
        root, now = Path('/checkout'), [timestamp()]
        worker = agent(root, command=(), runtimes=(Runtime('claude', 'model', 'high'),))
        self.enterContext(patch('ub_agents.updates.runtime_updates.RuntimeMaintenance.available', return_value=True))
        github = FakeGitHub(issue(1))
        source = MonitorSource(config(root, worker), github, Coordinator(github, None), lambda: now[0])
        with patch('ub_agents.monitor.eligible.runtime_pauses', return_value={'claude': now[0] + 100}):
            row = source.snapshot()['eligible']['rows'][0]
        self.assertEqual((row['glyph'], row['state'], list(row['pauses'])), ('◷', 'waiting', ['claude']))
        now[0] += PLAN_SECONDS
        row = source.snapshot()['eligible']['rows'][0]
        self.assertEqual((row['glyph'], row['state'], row['pauses']), ('●', 'next', {}))

    def test_inheriting_a_default_without_a_priority_label_shows_no_word(self):
        priority = Priority(('priority:high', 'priority:low'), 'priority:high')
        github = FakeGitHub(issue(1), issue(2, labels=('ready', 'priority:low')))
        github.dependencies = {1: [2]}
        source = MonitorSource(config(Path('/checkout'), queue=Queue(priority=priority)),
                               github, Coordinator(github, None), timestamp)
        [row] = source.snapshot()['eligible']['rows']
        self.assertEqual((row['item']['number'], row['priority']), (2, None))

    def test_pr_priority_uses_effective_inheritance_and_hides_the_default(self):
        for labels, default, expected in ((('priority:urgent',), 'priority:medium', 'urgent'),
                                          (('priority:urgent',), 'priority:urgent', None),
                                          (('priority:urgent',), None, 'urgent'),
                                          ((), 'priority:urgent', None)):
            with self.subTest(labels=labels, default=default):
                priority = Priority(('priority:urgent', 'priority:medium', 'priority:low'), default)
                github = FakeGitHub(issue(1, labels=('ready', 'priority:low')),
                                    issue(10, labels=('ready', *labels)),
                                    pr(2, labels=('ready', 'priority:low')))
                github.dependencies = {10: [1]}
                source = MonitorSource(config(Path('/checkout'), queue=Queue(priority=priority)),
                                       github, Coordinator(github, None), timestamp)

                rows = source.snapshot()['eligible']['rows']

                self.assertEqual([row['item']['number'] for row in rows], [2, 1])
                self.assertEqual([row['priority'] for row in rows], [expected, expected])
                self.assertEqual(rows[0]['state'], 'next')
                self.assertEqual(github.writes, [])

    def test_eligible_uses_claim_order_inheritance_and_excludes_unavailable_items(self):
        priority = Priority(('priority:urgent', 'priority:high', 'priority:medium', 'priority:low'), 'priority:medium')
        cfg = config(Path('/checkout'), queue=Queue(milestones='strict-gate', priority=priority))
        github = FakeGitHub(*(issue(n, milestone=1) for n in range(1, 26)))
        github.milestones = [{'number': n, 'title': str(n), 'state': 'open'} for n in (1, 2)]
        github.change(2, labels=frozenset({'ready', 'needs-human'}))
        github.change(3, labels=frozenset({'ready', 'priority:high'}))
        github.dependencies = {3: [4]}
        github.change(24, labels=frozenset({'ready', 'priority:urgent'}))
        github.change(25, milestone=2)
        github.create_comment(1, body(lease(1, 'owned')))
        writes = list(github.writes)
        source = MonitorSource(cfg, github, Coordinator(github, 'operator'), timestamp)

        eligible = source.snapshot()['eligible']

        self.assertEqual(eligible['count'], 21)
        self.assertEqual([row['item']['number'] for row in eligible['rows']], [24, 4, *range(5, 23)])
        self.assertEqual([row['priority'] for row in eligible['rows'][:3]], ['urgent', 'high', None])
        self.assertEqual([row['state'] for row in eligible['rows'][:2]], ['next', 'ready'])
        self.assertEqual(github.writes, writes)
        with self.assertRaises(AttributeError):
            source.planning.github.create_comment(1, 'forbidden')

    def test_eligible_counts_all_items_beyond_the_terminal_snapshot_limit(self):
        github = FakeGitHub(*(issue(n) for n in range(1, 131)))
        source = MonitorSource(config(Path('/checkout')), github, Coordinator(github, None), timestamp)
        eligible = source.snapshot()['eligible']
        self.assertEqual(eligible['count'], 130)
        self.assertEqual([row['item']['number'] for row in eligible['rows']], list(range(1, 21)))

    def test_eligible_groups_agents_and_puts_ready_and_recovery_before_waits(self):
        root, now = Path('/checkout'), timestamp()
        first, second = agent(root), agent(root, name='reviewer')
        cfg = config(root, first, second, queue=Queue(priority=Priority(('priority:high',), 'priority:high')))
        github = FakeGitHub()
        planning = EligiblePlanning(cfg, github, Coordinator(github, None), lambda: now)
        planning.loop = Loop(cfg, planning.github, None, output=lambda *_: None)
        plans = [Plan(issue(1), first, None, 'backoff', '', 2),
                 Plan(pr(2), first, None, 'recover', '', 1, priority='priority:high'),
                 Plan(pr(2), second, None, 'ready', '', 1, priority='priority:high'),
                 Plan(issue(3, labels=('ready', 'priority:high')), first, None, 'ready', '', 1,
                      priority='priority:high'),
                 Plan(issue(4), first, None, 'waiting', '', 1, paused_clis=('claude',)),
                 Plan(issue(5), first, None, 'waiting', '', 1)]
        with patch.object(planning.loop, 'iter_plans', return_value=plans), \
                patch('ub_agents.monitor.eligible.runtime_pauses', return_value={'claude': now + 100800}):
            rows = planning.snapshot()['rows']
        self.assertEqual([row['item']['number'] for row in rows], [2, 3, 1, 4, 5])
        self.assertEqual([(row['glyph'], row['state']) for row in rows],
                         [('●', 'next'), ('●', 'ready'), ('◷', 'backoff'), ('◷', 'waiting'), ('◷', 'waiting')])
        self.assertEqual([a['name'] for a in rows[0]['agents']], ['worker', 'reviewer'])
        self.assertEqual(rows[0]['item']['url'], 'https://github.com/org/project/pull/2')
        self.assertEqual(rows[2]['agents'][0]['failures'], 1)
        self.assertEqual(rows[3]['pauses'], {'claude': iso(now + 100800)})

    def test_priority_is_shown_only_when_effective_priority_differs_from_default(self):
        root = Path('/checkout')
        labels = ('priority:high', 'priority:medium', 'priority:low')
        for priority, expected in ((Priority(), {1: None, 2: None, 3: None, 4: None}),
                                   (Priority(labels, 'priority:medium'), {1: None, 2: None, 3: 'high', 4: 'low'}),
                                   (Priority(labels), {1: None, 2: 'medium', 3: 'high', 4: 'low'})):
            with self.subTest(priority=priority):
                github = FakeGitHub(issue(1), issue(2, labels=('ready', 'priority:medium')),
                                    issue(3, labels=('ready', 'priority:high')),
                                    issue(4, labels=('ready', 'priority:low')))
                source = MonitorSource(config(root, queue=Queue(priority=priority)),
                                       github, Coordinator(github, None), timestamp)
                rows = source.snapshot()['eligible']['rows']
                self.assertEqual({row['item']['number']: row['priority'] for row in rows}, expected)

    def test_planning_interval_reuses_discovery_and_refreshes_after_changes(self):
        now = [timestamp()]
        github = PollGitHub(issue(1))
        source = MonitorSource(config(Path('/checkout')), github, Coordinator(github, None), lambda: now[0])
        first = source.snapshot()['eligible']
        discovery = source.planning.loop.discovery
        github.reads.clear()
        now[0] += 30
        self.assertIs(source.snapshot()['eligible'], first)
        self.assertFalse(any(name == 'timeline' for name, _ in github.reads))
        now[0] += PLAN_SECONDS
        source.snapshot()
        self.assertIs(source.planning.loop.discovery, discovery)
        self.assertFalse(any(name == 'timeline' for name, _ in github.reads))
        github.change(1, labels=frozenset())
        now[0] += PLAN_SECONDS
        self.assertEqual(source.snapshot()['eligible']['count'], 0)
        self.assertEqual(github.writes, [])

    def test_failed_planning_preserves_the_last_snapshot_until_retry_succeeds(self):
        now = [timestamp()]
        github = FakeGitHub(issue(1), issue(2, labels=()))
        source = MonitorSource(config(Path('/checkout')), github, Coordinator(github, None), lambda: now[0])
        snapshots = Snapshots(source, clock=lambda: now[0])
        first = snapshots.get()
        now[0] += PLAN_SECONDS
        github.create_comment(1, body(lease(1, 'running')))
        github.change(2, labels=frozenset({'needs-human'}))
        with patch.object(source.planning.loop, 'iter_plans', side_effect=AgentError('Cannot plan queue')):
            for _ in range(2):
                failed = snapshots.get()
                self.assertIs(failed['eligible'], first['eligible'])
                self.assertEqual(failed['eligible_error'], 'Cannot plan queue')
                self.assertEqual(failed['generated_at'], iso(now[0]))
                self.assertEqual([run['item']['number'] for run in failed['running']], [1])
                self.assertEqual([entry['item']['number'] for entry in failed['needs_human']], [2])
                now[0] += 30
        recovered = snapshots.get()
        self.assertIsNone(recovered['eligible_error'])
        self.assertIsNot(recovered['eligible'], first['eligible'])
        self.assertEqual(recovered['eligible']['count'], 0)

    def test_an_initial_planning_failure_still_shows_other_panels(self):
        github = FakeGitHub(issue(1))
        github.create_comment(1, body(lease(1, 'running')))
        source = MonitorSource(config(Path('/checkout')), github, Coordinator(github, None), timestamp)
        with patch.object(Loop, 'iter_plans', side_effect=AgentError('Cannot plan queue')):
            failed = source.snapshot()
        self.assertIsNone(failed['eligible'])
        self.assertEqual(failed['eligible_error'], 'Cannot plan queue')
        self.assertEqual([run['item']['number'] for run in failed['running']], [1])
        self.assertIsNone(source.snapshot()['eligible_error'])

    def test_local_pauses_come_only_from_the_live_launchers_fresh_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            cfg, now = config(Path(root)), timestamp()
            directory = Path(root) / '.ub-agents' / 'sessions'
            directory.mkdir(parents=True)
            data = {'version': 1, 'repository': cfg.repository, 'pid': 123, 'host': 'test',
                    'published_at': iso(now), 'runtime_pauses': {'claude': iso(now + 100800)}}
            for changes, expected in (({}, {'claude': now + 100800}), ({'ended': True}, {}),
                                       ({'pid': 456}, {}), ({'published_at': iso(now - 31)}, {})):
                (directory / 'session.json').write_text(json.dumps(data | changes))
                with patch('ub_agents.monitor.local.running_launcher', return_value={'pid': 123, 'host': 'test'}):
                    self.assertEqual(runtime_pauses(cfg, now), expected)
            with patch('ub_agents.monitor.local.running_launcher', return_value=None):
                self.assertEqual(runtime_pauses(cfg, now), {})

    def test_page_has_bounded_eligible_scrolling_empty_state_and_failure_banner(self):
        html = page().decode()
        self.assertLess(html.index('runningPanel(data.running, data)'), html.index('eligiblePanel(data.eligible)'))
        self.assertLess(html.index('eligiblePanel(data.eligible)'), html.index('expiredPanel(data.expired)'))
        self.assertIn('max-height: 280px; overflow-y: auto', html)
        self.assertIn('height: 56px;', html)
        self.assertIn('Nothing eligible', html)
        self.assertIn('Planning unavailable', html)
        self.assertIn('Showing the last Eligible result.', html)
        self.assertIn('Other panels are current.', html)
        self.assertIn(' · showing ${eligible.rows.length}', html)
        self.assertIn('Couldn\'t refresh:', html)
        self.assertIn('--priority-urgent: #e0524a', html)
        self.assertIn('--priority-high: #c98a86', html)
        self.assertIn('--priority-low: #86a891', html)
        self.assertIn('.priority-medium { color: var(--muted); }', html)

    def test_snapshot_shows_trusted_runs_and_stopped_items_with_their_ask(self):
        github = FakeGitHub(issue(1), issue(2, labels=("needs-human",)))
        github.create_comment(1, body(lease(1, "a")))
        github.create_comment(1, body(lease(1, "b")), login="outsider")
        released = github.create_comment(2, body(lease(2, "c", state="released", result="blocked",
                                                      attempt_effect="unchanged")))
        github.create_comment(2, body({"kind": "outcome", "agent": "worker", "actor": "operator", "assignment": 2,
                                       "created": iso(timestamp()), "status": "blocked", "summary": "Unclear scope",
                                       "lease_id": released["id"], "accepted": True,
                                       "actions": ["Choose a scope"], "options": ["Small", "Large"]}))
        source = MonitorSource(config(Path("/checkout")), github, Coordinator(github, None), timestamp)

        snapshot = source.snapshot()

        self.assertEqual([run["item"]["number"] for run in snapshot["running"]], [1])
        self.assertEqual(snapshot["running"][0]["host"], "server")
        [stopped] = snapshot["needs_human"]
        self.assertEqual((stopped["item"]["number"], stopped["labels"]), (2, ["needs-human"]))
        self.assertEqual((stopped["ask"]["actions"], stopped["ask"]["options"]), (["Choose a scope"], ["Small", "Large"]))
        self.assertEqual([run["result"] for run in snapshot["recent"]], ["blocked"])

    def test_a_blocked_run_needs_a_person_only_while_its_item_is_open(self):
        github = FakeGitHub(issue(3, labels=()))
        github.create_comment(3, body(lease(3, "d", state="released", result="blocked", attempt_effect="unchanged")))
        source = MonitorSource(config(Path("/checkout")), github, Coordinator(github, None), timestamp)
        waiting = []
        for state in ("open", "closed", "open"):
            github.change(3, state=state)
            waiting.append([entry["item"]["number"] for entry in source.snapshot()["needs_human"]])
        self.assertEqual(waiting, [[3], [], [3]])

    def test_snapshot_offers_each_configured_outcome_that_stops_for_a_person(self):
        root = Path("/checkout")
        integrator = agent(root, name="integrator", outcomes={
            "merged": {}, "maintainer-merge": {"add": ("needs-human",)}, "changes-requested": {"add": ("needs-changes",)}})
        preparer = agent(root, name="issue-preparer", outcomes={"needs-human": {"add": ("needs-human",)}})
        reviewer = agent(root, name="reviewer", outcomes={
            "maintainer-merge": {"add": ("needs-human",)}, "needs-human": {"add": ("needs-changes",)}})
        github = FakeGitHub()
        source = MonitorSource(config(root, reviewer, preparer, integrator), github, Coordinator(github, None), timestamp)

        self.assertEqual(source.snapshot()["stop_outcomes"], [
            {"name": "maintainer-merge", "agents": ["integrator", "reviewer"]},
            {"name": "needs-human", "agents": ["issue-preparer"]}])

    def test_page_groups_stop_choices_and_closes_the_picker_outside_its_controls(self):
        html = page().decode()
        self.assertIn('"aria-label": "a person is needed"', html)
        self.assertIn('.stop-events { padding-left: 3ch; }', html)
        self.assertIn('is added another way (by hand, or a round or attempt limit)', html)
        self.assertEqual(html.count('" · stays until dismissed"'), 1)
        self.assertIn('parent.indeterminate = !parent.checked && stops.some', html)
        self.assertIn('stored(EVENTS_KEY, stopEvents().map((e) => e.id))', html)
        self.assertIn('event.target.closest("#picker, #notify-when")', html)
        self.assertIn('event.key === "Escape" && state.choosing', html)

    def test_server_answers_only_this_machine_and_its_own_page(self):
        self.assertTrue(allowed("127.0.0.1:4377", 4377, "/snapshot.json", "same-origin", "cors"))
        self.assertTrue(allowed("localhost:4377", 4377, "/", "cross-site", "navigate"))
        self.assertFalse(allowed("rebound.example:4377", 4377, "/snapshot.json", None, None))
        self.assertFalse(allowed("127.0.0.1:4377", 4377, "/snapshot.json", "cross-site", "cors"))
        self.assertFalse(allowed("127.0.0.1:4377", 4377, "/snapshot.json", "cross-site", "navigate"))

    def test_server_silently_finishes_replies_to_disconnected_viewers(self):
        for error in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            for written in (0, 1):
                with self.subTest(error=error, written=written):
                    source = Mock()
                    source.snapshot.return_value = {}
                    stdout, stderr = StringIO(), StringIO()
                    with create(source, port=0) as server:
                        request = snapshot_request(server.server_address[1])
                        request.sendall.side_effect = [None] * written + [error("viewer left")]
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            server.process_request_thread(request, ("127.0.0.1", 12345))
                    source.snapshot.assert_called_once_with()
                    self.assertEqual(request.sendall.call_count, written + 1)
                    request.close.assert_called_once_with()
                    self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))

    def test_server_reports_other_request_errors_normally(self):
        for error, stage in ((OSError("unexpected header failure"), "headers"),
                             (RuntimeError("unexpected reply failure"), "body"),
                             (ConnectionResetError("upstream disconnected"), "snapshot")):
            with self.subTest(error=error, stage=stage):
                source = Mock()
                source.snapshot.return_value = {}
                if stage == "snapshot":
                    source.snapshot.side_effect = error
                stdout, stderr = StringIO(), StringIO()
                with create(source, port=0) as server:
                    request = snapshot_request(server.server_address[1])
                    request.sendall.side_effect = [None] * (stage == "body") + [error]
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        server.process_request_thread(request, ("127.0.0.1", 12345))
                request.close.assert_called_once_with()
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("Exception occurred during processing of request", stderr.getvalue())
                self.assertIn("Traceback (most recent call last):", stderr.getvalue())
                self.assertIn(f"{type(error).__name__}: {error}", stderr.getvalue())

    def test_background_monitor_runs_in_its_own_session_until_it_answers(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch("ub_agents.monitor.already_serving", side_effect=[False, False, True]), \
                patch("ub_agents.monitor.subprocess.Popen") as popen, patch("ub_agents.monitor.sleep"):
            root = Path(folder).resolve()
            popen.return_value.poll.return_value = None
            monitor_background([Path(root) / "ub-agents.yaml", Path(root) / "other.yaml"],
                               Path(root), 4400, open_page=False)
        command, options = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(command[-6:], ["monitor", f"{root}/ub-agents.yaml", f"{root}/other.yaml", "--port", "4400", "--no-open"])
        self.assertEqual(options['stdout'].name, f'{root}/.ub-agents/monitor.log')
        self.assertTrue(options["start_new_session"])


if __name__ == "__main__":
    unittest.main()
