import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from ub_agents.view_clipboard import copy_with_pbcopy, local_pbcopy


class LocalClipboardTests(unittest.TestCase):
    def test_pbcopy_only_when_available_on_local_macos(self):
        cases = [
            ('darwin', {}, '/usr/bin/pbcopy', '/usr/bin/pbcopy'),
            ('darwin', {}, None, None),
            ('linux', {}, '/usr/bin/pbcopy', None),
            ('win32', {}, '/usr/bin/pbcopy', None),
            ('darwin', {'SSH_CONNECTION': 'remote'}, '/usr/bin/pbcopy', None),
            ('darwin', {'SSH_TTY': '/dev/pts/1'}, '/usr/bin/pbcopy', None),
            ('darwin', {'SSH_CONNECTION': ''}, '/usr/bin/pbcopy', None),
            ('darwin', {'SSH_TTY': ''}, '/usr/bin/pbcopy', None),
        ]
        for platform, environment, executable, expected in cases:
            with self.subTest(platform=platform, environment=environment, executable=executable):
                with (patch('ub_agents.view_clipboard.sys.platform', platform),
                      patch.dict('ub_agents.view_clipboard.os.environ', environment, clear=True),
                      patch('ub_agents.view_clipboard.shutil.which', return_value=executable) as which):
                    self.assertEqual(local_pbcopy(), expected)
                    if platform == 'darwin' and not environment:
                        which.assert_called_once_with('pbcopy')
                    else:
                        which.assert_not_called()


class PbcopyTests(unittest.IsolatedAsyncioTestCase):
    async def test_copies_exact_utf8_text_and_tolerates_nonzero_exit(self):
        value = '  café α\nsecond line  '
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                process = Mock(returncode=returncode, communicate=AsyncMock(), wait=AsyncMock())
                with patch('ub_agents.view_clipboard.asyncio.create_subprocess_exec',
                           new=AsyncMock(return_value=process)) as spawn:
                    await copy_with_pbcopy('/usr/bin/pbcopy', value)
                spawn.assert_awaited_once_with(
                    '/usr/bin/pbcopy', stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                process.communicate.assert_awaited_once_with(value.encode('utf-8'))
                process.kill.assert_not_called()
                process.wait.assert_awaited_once_with()

    async def test_missing_executable_and_broken_pipe_are_best_effort(self):
        with patch('ub_agents.view_clipboard.asyncio.create_subprocess_exec',
                   new=AsyncMock(side_effect=FileNotFoundError)):
            await copy_with_pbcopy('/usr/bin/pbcopy', 'text')
        process = Mock(returncode=None, communicate=AsyncMock(side_effect=BrokenPipeError),
                       wait=AsyncMock())
        with patch('ub_agents.view_clipboard.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=process)):
            await copy_with_pbcopy('/usr/bin/pbcopy', 'text')
        process.kill.assert_called_once_with()
        process.wait.assert_awaited_once_with()

    async def test_slow_pbcopy_is_killed_and_reaped_on_timeout_or_cancellation(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                started = asyncio.Event()

                async def communicate(value):
                    started.set()
                    await asyncio.Event().wait()

                process = Mock(returncode=None, communicate=AsyncMock(side_effect=communicate),
                               wait=AsyncMock())
                with patch('ub_agents.view_clipboard.asyncio.create_subprocess_exec',
                           new=AsyncMock(return_value=process)):
                    task = asyncio.create_task(copy_with_pbcopy('/usr/bin/pbcopy', 'text'))
                    await started.wait()
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        await asyncio.wait_for(task, timeout=2)
                process.kill.assert_called_once_with()
                process.wait.assert_awaited_once_with()
