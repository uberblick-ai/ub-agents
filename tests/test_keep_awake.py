import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from tests.support import RecordingAwakeProcess
from ub_agents.keep_awake import KeepAwake
from ub_agents.launch_ui import ViewProcess
from ub_agents.view import LauncherConnection


class KeepAwakeTests(unittest.TestCase):
    def setUp(self):
        platform = patch('ub_agents.keep_awake.sys.platform', 'darwin')
        platform.start()
        self.addCleanup(platform.stop)
        self.states = []
        self.awake = KeepAwake(self.states.append)
        self.addCleanup(self.awake.close)

    def test_off_at_each_launch_and_toggle_publishes_only_after_helper_started(self):
        helper = RecordingAwakeProcess()
        with patch('ub_agents.keep_awake.subprocess.Popen', return_value=helper) as start:
            self.awake.state()
            self.assertEqual(self.states, [{'supported': True, 'enabled': False, 'notice': ''}])
            start.assert_not_called()
            self.awake.toggle()
            start.assert_called_once_with(
                ['caffeinate', '-i', '-w', str(os.getpid())], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self.assertEqual(helper.calls, [('wait', 0.1)])
            self.assertEqual(self.states[-1], {'supported': True, 'enabled': True, 'notice': ''})
            self.awake.toggle()
            self.assertEqual(helper.calls[-2:], [('terminate',), ('wait', 1)])
            self.assertFalse(self.states[-1]['enabled'])
            other = KeepAwake(self.states.append)
            other.state()
            self.assertFalse(self.states[-1]['enabled'])

    def test_missing_denied_and_early_exit_stay_off_and_can_retry(self):
        for failure, reason in ((FileNotFoundError(), 'caffeinate not found'),
                                (PermissionError('permission denied'), 'permission denied'),
                                (RecordingAwakeProcess(1), 'caffeinate exited 1')):
            with self.subTest(reason=reason):
                helper = RecordingAwakeProcess()
                with patch('ub_agents.keep_awake.subprocess.Popen', side_effect=[failure, helper]):
                    self.awake.toggle()
                    self.assertIsNone(self.awake.process)
                    self.assertEqual(self.states[-1], {'supported': True, 'enabled': False,
                                                      'notice': 'keep awake unavailable: ' + reason})
                    self.awake.toggle()
                    self.assertTrue(self.states[-1]['enabled'])
                    self.awake.toggle()

    def test_unexpected_exit_is_reaped_and_published_without_polling_github(self):
        helper = RecordingAwakeProcess()
        with patch('ub_agents.keep_awake.subprocess.Popen', return_value=helper):
            self.awake.toggle()
            helper.returncode = 2
            self.awake.check()
            self.assertIsNone(self.awake.process)
            self.assertEqual(helper.calls[-1], ('wait', None))
            self.assertEqual(self.states[-1], {'supported': True, 'enabled': False,
                                              'notice': 'keep awake stopped: caffeinate exited 2'})

    def test_other_platform_never_starts_a_helper(self):
        with patch('ub_agents.keep_awake.sys.platform', 'linux'), \
                patch('ub_agents.keep_awake.subprocess.Popen') as start:
            awake = KeepAwake(self.states.append)
            awake.toggle()
            awake.close()
        start.assert_not_called()
        self.assertEqual(self.states, [{'supported': False, 'enabled': False,
                                       'notice': 'keep awake unavailable on this platform'}])

    def test_close_kills_a_stalled_helper_and_prevents_late_toggle(self):
        helper = RecordingAwakeProcess(ignore_terminate=True)
        with patch('ub_agents.keep_awake.subprocess.Popen', return_value=helper) as start:
            self.awake.toggle()
            self.awake.close()
            self.awake.close()
            self.awake.toggle()
        self.assertEqual(start.call_count, 1)
        self.assertEqual(helper.calls, [('wait', 0.1), ('terminate',), ('wait', 1),
                                        ('kill',), ('wait', None)])
        self.assertFalse(self.states[-1]['enabled'])

    def test_separate_launchers_end_only_their_own_helper(self):
        first, second = RecordingAwakeProcess(), RecordingAwakeProcess()
        other_states = []
        other = KeepAwake(other_states.append)
        self.addCleanup(other.close)
        with patch('ub_agents.keep_awake.subprocess.Popen', side_effect=[first, second]):
            self.awake.toggle()
            other.toggle()
            self.awake.toggle()
            self.assertIsNone(second.returncode)
            self.assertTrue(other_states[-1]['enabled'])
            self.assertNotIn(('terminate',), second.calls)
            other.close()
            self.assertEqual(second.returncode, -15)

    def test_view_channel_toggle_and_close_release_helper_and_keep_launcher_running(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                parent, child = socket.socketpair()
                self.addCleanup(parent.close)
                connection = LauncherConnection(child.detach())
                self.addCleanup(connection.channel.close)
                poll = Mock()
                view = ViewProcess([], Path('.'), 'own', Mock(), poll=poll)
                view.channel = parent
                view.process = Mock()
                view.process.wait.return_value = 0
                helper = RecordingAwakeProcess()
                connection.send('ready')
                connection.awake()
                connection.poll()
                connection.channel.shutdown(socket.SHUT_WR)
                with patch('ub_agents.keep_awake.subprocess.Popen',
                           side_effect=FileNotFoundError() if failure else lambda *a, **kw: helper), \
                        patch('ub_agents.launch_ui.os.kill') as kill:
                    view.monitor()
                kill.assert_not_called()
                poll.assert_called_once_with()
                self.assertTrue(view.ready.is_set())
                self.assertTrue(view.awake.closed)
                self.assertEqual(helper.returncode, None if failure else -15)
                data = connection.channel.recv(4096)
                states = [json.loads(line[6:]) for line in data.splitlines()]
                self.assertFalse(states[0]['enabled'])
                self.assertEqual(states[1]['enabled'], not failure)
                self.assertFalse(states[-1]['enabled'])
                if failure:
                    self.assertIn('caffeinate not found', states[-1]['notice'])

    def test_launcher_close_releases_helper_even_without_supervisor_thread(self):
        view = ViewProcess([], Path('.'), 'own', Mock())
        helper = RecordingAwakeProcess()
        with patch('ub_agents.keep_awake.subprocess.Popen', return_value=helper):
            view.awake.toggle()
            view.close()
        self.assertEqual(helper.returncode, -15)
        self.assertTrue(view.awake.closed)

    def test_published_state_crosses_channel_immediately_and_preserves_stop_and_eof(self):
        for ending in ('stop', 'eof'):
            with self.subTest(ending=ending):
                parent, child = socket.socketpair()
                self.addCleanup(parent.close)
                connection = LauncherConnection(child.detach())
                self.addCleanup(connection.channel.close)
                received = threading.Event()
                app = Mock()
                app.call_from_thread.side_effect = lambda callback, *args: callback(*args)
                app.keep_awake_state.side_effect = lambda state: received.set()
                connection.mounted(app)
                self.assertEqual(parent.recv(1024), b'ready\n')
                state = {'enabled': True, 'supported': True, 'notice': ''}
                parent.sendall(b'awake {"enabled":')
                self.assertFalse(received.wait(0.02))
                parent.sendall(b'true, "supported": true, "notice": ""}\n')
                self.assertTrue(received.wait(1))
                app.keep_awake_state.assert_called_once_with(state)
                app.exit.assert_not_called()
                if ending == 'stop':
                    parent.sendall(b'stop\n')
                else:
                    parent.shutdown(socket.SHUT_WR)
                connection.thread.join(1)
                self.assertFalse(connection.thread.is_alive())
                app.exit.assert_called_once_with()
                connection.close()
