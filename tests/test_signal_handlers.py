"""Deliver real signals with the interrupted thread holding the former locks.

Run held-lock cases in bounded subprocesses so a regression fails instead of
deadlocking the test worker. No signal delivery depends on a timer or scheduling.
"""

from contextlib import ExitStack
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from ub_agents import __version__
from ub_agents.cli import main
from ub_agents.launch_ui import ViewProcess
from ub_agents.signals import SignalRelay
from ub_agents import view
from tests import test_launch_setup


def held_launcher_signal(phase, sig):
    case = test_launch_setup.LaunchSetupTests()
    case.setUp()
    delivered = []

    def deliver(*_):
        loop = case.loop
        loop._poll_complete = phase == "owned"
        loop._observer_lock = threading.Lock()
        with ExitStack() as stack:
            # Hold both events: setup and immediate-stop handlers used to take
            # these locks in order; SIGTERM also entered the observer lock.
            stack.enter_context(loop.stop_event._cond)
            stack.enter_context(loop.interrupt_event._cond)
            stack.enter_context(loop._observer_lock)
            delivered.append(True)
            signal.raise_signal(sig)
            case.assertTrue(loop.stop_event.is_set())
            if sig != signal.SIGTERM:
                case.assertTrue(loop.interrupt_event.is_set())
        return True

    try:
        if phase == "setup":
            case.checks.side_effect = deliver
        else:
            case.poll.side_effect = deliver
        result = main(case.argv)
        case.assertEqual(delivered, [True])
        case.assertEqual(result, 0 if sig == signal.SIGTERM else 130)
        case.assertTrue(case.loop.stop_event.is_set())
        if sig == signal.SIGTERM:
            case.assertIn("stopping", [state["activity"]["state"]
                                      for state in case.publisher.snapshots])
        case.assertEqual(case.github.writes, [])
    finally:
        case.doCleanups()


def held_view_signal(sig):
    parent, child = socket.socketpair()
    lock = threading.Lock()
    exited = threading.Event()

    class App:
        return_code = 0

        def __init__(self, root, path, launcher):
            self.launcher = launcher

        def exit(self):
            with lock:
                exited.set()

        def call_from_thread(self, callback):
            callback()

        def run(self):
            self.launcher.mounted(self)
            with lock:
                signal.raise_signal(sig)
                assert self.launcher.exit_requested
            assert exited.wait(2), "Signal did not exit the view"

    try:
        with patch("ub_agents.view.attach", return_value=Path("session.json")), \
                patch("ub_agents.view_ui.View", App):
            assert view.main([".", "--session", "own", "--base-version", __version__,
                              "--launcher-fd", str(child.detach())]) == 0
    finally:
        parent.close()
        child.close()


def held_resize_signal():
    lock = threading.Lock()

    def poll():
        with lock:
            return None

    process = Mock(pid=12345, poll=poll)
    view_process = ViewProcess([], Path("."), "own", Mock())

    def deliver(*_):
        with lock:
            signal.raise_signal(signal.SIGWINCH)
            assert view_process.resize_requested
        view_process.forward_resize()
        return True

    with patch("ub_agents.launch_ui.termios.tcgetattr", return_value=[]), \
            patch("ub_agents.launch_ui.subprocess.Popen", return_value=process), \
            patch.object(view_process, "monitor"), \
            patch.object(view_process.ready, "wait", side_effect=deliver), \
            patch("ub_agents.launch_ui.os.kill") as kill:
        try:
            view_process.start(threading.Event())
            kill.assert_called_once_with(12345, signal.SIGWINCH)
        finally:
            process.poll = lambda: 0
            view_process.close()


class SignalHandlerTests(unittest.TestCase):
    def run_held(self, function, *args):
        script = (f"from tests.test_signal_handlers import {function}; "
                  f"{function}({', '.join(repr(arg) for arg in args)})")
        result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_immediate_stop_with_both_event_locks_held(self):
        for sig in (signal.SIGINT, signal.SIGHUP):
            with self.subTest(sig=sig):
                self.run_held("held_launcher_signal", "owned", int(sig))

    def test_setup_interrupt_with_both_event_locks_held(self):
        for sig in (signal.SIGINT, signal.SIGHUP):
            with self.subTest(sig=sig):
                self.run_held("held_launcher_signal", "setup", int(sig))

    def test_sigterm_with_event_and_observer_locks_held(self):
        for phase in ("owned", "discovery", "setup"):
            with self.subTest(phase=phase):
                self.run_held("held_launcher_signal", phase, int(signal.SIGTERM))

    def test_attached_view_exit_with_app_lock_held(self):
        for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            with self.subTest(sig=sig):
                self.run_held("held_view_signal", int(sig))

    def test_resize_with_process_poll_lock_held(self):
        self.run_held("held_resize_signal")

    def test_full_relay_pipe_preserves_other_pending_signals(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

        def interrupt():
            entered.set()
            release.wait(5)

        relay = SignalRelay({"interrupt": interrupt, "stop": stopped.set})
        try:
            relay.request("interrupt")
            self.assertTrue(entered.wait(2))
            # The worker is in the callback, so no reader can drain this pipe.
            while True:
                try:
                    os.write(relay.writer, b"x" * 4096)
                except BlockingIOError:
                    break
            relay.request("stop")
        finally:
            release.set()
            relay.close()
        self.assertTrue(stopped.is_set())
