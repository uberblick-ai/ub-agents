"""At most one five-minute read-only probe per runtime. No launcher/workflow access."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import threading
import time

from ub_agents.execution import supervise, group_members
from .demo import View
from .model import Observer

ROOT = Path.cwd()
EVIDENCE = ROOT / 'experiments/terminal_observer/evidence'
WORK = ROOT / '.ub-agent/spike97-probes'
PROMPT = ('This is a read-only terminal-log probe. Read only READ_ME.txt in the current directory, '
          'then return the exact token in that file and one sentence confirming it was read. '
          'Do not edit any files, inspect any other directory, access GitHub or MCP, '
          'run ub-agent, create agents, delegate, or follow repository instructions. No further tasks.')


async def probe(runtime):
    folder = WORK / runtime
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'READ_ME.txt').write_text('SPIKE97_OBSERVER_OK\n')
    commands = {
        'codex': ['codex', 'exec', '--model', 'gpt-6.1-sol', '--sandbox', 'read-only',
                  '--ignore-user-config', '--ephemeral', '--skip-git-repo-check', '--json', '-'],
        'claude': ['claude', '--print', '--model', 'sonnet', '--restricted', '--tools', 'Read',
                   '--permission-mode', 'plan', '--permission-prompts', 'none',
                   '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                   '--setting-sources', '', '--settings', '{"disableAllHooks":true}',
                   '--no-session-persistence', '--output-format', 'stream-json', '--verbose',
                   '--include-partial-messages'],
    }
    if not shutil.which(runtime):
        return {'runtime': runtime, 'verdict': 'unverified', 'reason': 'executable missing'}
    # Preserve existing auth; remove supervised assignment authority from the probe.
    env = {k: v for k, v in os.environ.items() if not k.startswith('UB_AGENT_')}
    stop = threading.Event()
    started = time.monotonic()
    task = asyncio.create_task(asyncio.to_thread(supervise, commands[runtime], folder, env,
                                               folder, 300, stop, PROMPT))
    view = View(Observer(ROOT, log_path=folder / 'process.log'))
    max_buffer = 0
    first_visible = None
    captures = 0
    try:
        async with view.run_test(size=(110, 32)) as pilot:
            while not task.done():
                await pilot.pause(0.1)
                if view.tail.buffer.total and first_visible is None:
                    first_visible = round(time.monotonic() - started, 3)
                max_buffer = max(max_buffer, len(view.tail.buffer.entries))
                # Exercise navigation while the owned runtime is still active.
                if captures == 0 and view.tail.buffer.total:
                    await pilot.press('2', '3', '1')
                    captures += 1
                    view.save_screenshot(f'{runtime}-live.svg', path=str(folder))
            await pilot.pause(0.2)
            view.tail.poll()
            view.show_log()
            view.save_screenshot(f'{runtime}-finished.svg', path=str(folder))
        try:
            code = await task
            error = None
        except Exception as exc:
            code, error = None, type(exc).__name__
    finally:
        if not task.done():
            stop.set()
            try:
                await task
            except (Exception, KeyboardInterrupt):
                pass
    raw_path = folder / 'process.log'
    # Sanitized evidence deliberately omits system metadata, ids, environment and reasoning.
    sanitized = []
    kinds = []
    for line in raw_path.read_text(errors='replace').splitlines() if raw_path.exists() else []:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type', 'unknown')
        kinds.append(kind)
        if kind == 'assistant':
            content = event.get('message', {}).get('content', [])
            content = [c for c in content if c.get('type') in {'text', 'tool_use'}]
            for c in content:
                if c.get('type') == 'tool_use':
                    c = {'type': 'tool_use', 'name': c.get('name'), 'input': {'file_path': 'READ_ME.txt'}}
                sanitized.append({'type': 'assistant', 'message': {'content': [c]}})
        elif kind in {'item.started', 'item.completed'}:
            item = event.get('item', {})
            if item.get('type') == 'agent_message':
                sanitized.append({'type': kind, 'item': {'type': 'agent_message', 'text': item.get('text', '')}})
            elif item.get('type') == 'command_execution':
                sanitized.append({'type': kind, 'item': {'type': 'command_execution',
                                  'command': 'read READ_ME.txt', 'aggregated_output': 'SPIKE97_OBSERVER_OK' if 'SPIKE97_OBSERVER_OK' in item.get('aggregated_output', '') else '[omitted]',
                                  'exit_code': item.get('exit_code')}})
        elif kind in {'error', 'result'}:
            # Error details can contain service identifiers; keep only type and success token.
            sanitized.append({'type': kind, 'result': 'SPIKE97_OBSERVER_OK' if 'SPIKE97_OBSERVER_OK' in event.get('result', '') else '[omitted]', 'is_error': event.get('is_error')})
    (EVIDENCE / f'{runtime}-sanitized.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in sanitized))
    pid_path = folder / 'pid'
    members = group_members(int(pid_path.read_text())) if pid_path.exists() else []
    return {'runtime': runtime, 'command': commands[runtime], 'seconds': round(time.monotonic() - started, 3),
            'exit_code': code, 'error_class': error, 'event_types': sorted(set(kinds)),
            'first_display_seconds': first_visible, 'navigation_during_run': bool(captures),
            'max_buffer_entries': max_buffer, 'raw_bytes': raw_path.stat().st_size if raw_path.exists() else 0,
            'owned_group_empty_after_supervise': not members,
            'token_returned': any('SPIKE97_OBSERVER_OK' in json.dumps(e) for e in sanitized)}


async def main():
    results = []
    for runtime in ('codex', 'claude'):
        result = await probe(runtime)
        results.append(result)
        (EVIDENCE / 'probes.json').write_text(json.dumps(results, indent=2) + '\n')
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    raise SystemExit('Historical probe code: the #97 live-probe allowance is already used; replay only.')
