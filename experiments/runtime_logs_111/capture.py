"""One guarded read-only probe/runtime. Replay is separate and needs no auth."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace
from datetime import datetime, timezone

from ub_agents.config import Runtime
from ub_agents.execution import command_for, group_members, supervise

ROOT = Path.cwd()
HERE = ROOT / 'experiments/runtime_logs_111'
PRIVATE = ROOT / '.ub-agent/spike111-private'
EVIDENCE = HERE / 'evidence'
PROMPT = '''This is an owned read-only terminal-log fixture, not a repository task.
Do not inspect any parent directory, credentials, configuration, network or MCP.
Do not delegate, implement work, write files or retry a failed command.
First say SPIKE111_MESSAGE_BEGIN and briefly describe the read-only inspection.
Read fixture_output.py, then run exactly: python3 fixture_output.py
Then run exactly: cat missing-owned.txt
The second command is an intentional missing-file failure. Do not fix it.
Finish with SPIKE111_MESSAGE_END and a short account of success, the long output,
and the intentional failed read. Do not reproduce all output in your final answer.
'''
GUIDANCE = 'Owned read-only log fixture. Follow the supplied prompt. Never inspect parents, use connectors, spawn agents, modify files/configuration or run git/ub-agent.\n'


def scrubber(cwd):
    replacements = [(str(cwd), '<fixture>'), (str(ROOT), '<worktree>'),
                    (str(Path.home()), '<home>')]
    ids = {}

    def identity(value):
        return ids.setdefault(value, f'<id-{len(ids) + 1}>')

    def scrub(value, key=''):
        if key in {'utilization', 'resetsAt', 'isUsingOverage', 'surpassedThreshold'}:
            return '<redacted>'
        if isinstance(value, dict):
            return {scrub(k): scrub(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v, key) for v in value]
        if not isinstance(value, str):
            return value
        if key in {'session_id', 'uuid', 'id', 'tool_use_id', 'parent_tool_use_id', 'request_id'}:
            return identity(value) if value else value
        if re.search(r'(?i)(api.?key|access.?token|refresh.?token|authorization|email|signature|socket_path)', key):
            return '<redacted>'
        for old, new in replacements:
            value = value.replace(old, new)
        value = re.sub(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', '<uuid>', value)
        value = re.sub(r'\b(?:toolu_|msg_|req_)[A-Za-z0-9_-]+', lambda match: identity(match[0]), value)
        value = re.sub(r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}', '<credential>', value)
        value = re.sub(r'(?i)Bearer\s+\S+', 'Bearer <credential>', value)
        value = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '<email>', value)
        return value

    return scrub


def capture(runtime):
    private = PRIVATE / runtime
    # Creation is the attempt guard, even if startup/auth fails. Never overwrite/retry.
    private.mkdir(parents=True, exist_ok=False)
    cwd = private / 'fixture'
    cwd.mkdir()
    shutil.copyfile(HERE / 'fixture_output.py', cwd / 'fixture_output.py')
    (cwd / 'AGENTS.md').write_text(GUIDANCE)
    (cwd / 'CLAUDE.md').write_text(GUIDANCE)
    if runtime == 'claude':
        spec = Runtime('claude', 'claude-opus-5-5', 'high')
        args = ('--restricted', '--tools', 'Read,Bash', '--permission-mode', 'plan',
                '--permission-prompts', 'none', '--allowedTools',
                'Bash(python3 fixture_output.py)', 'Bash(cat missing-owned.txt)',
                '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                '--setting-sources', '', '--settings', '{"disableAllHooks":true}',
                '--no-session-persistence')
    else:
        spec = Runtime('codex', 'gpt-6.1-sol', 'xhigh')
        args = ('--sandbox', 'read-only', '--ignore-user-config', '--ephemeral',
                '--skip-git-repo-check')
    argv = command_for(SimpleNamespace(command=(), runtime_args=args), spec)
    version = subprocess.run([runtime, '--version'], capture_output=True, text=True,
                             timeout=10, check=True).stdout.strip()
    run_dir = private / 'run'
    started = time.monotonic()
    utc = datetime.now(timezone.utc).isoformat()
    groups = []
    arrivals = []
    raw_count = 0
    partial = b''
    failure = None
    exit_code = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(supervise, argv, cwd, os.environ.copy(), run_dir,
                                 120, threading.Event(), PROMPT,
                                 process_started=groups.append)
        # Capture line-completion read times, not producer timestamps or wire chunks.
        while True:
            path = run_dir / 'process.log'
            if path.exists():
                with path.open('rb') as stream:
                    stream.seek(raw_count)
                    chunk = stream.read()
                raw_count += len(chunk)
                partial += chunk
                while b'\n' in partial:
                    line, partial = partial.split(b'\n', 1)
                    arrivals.append((round(time.monotonic() - started, 3), line))
            if future.done():
                # One more iteration drains bytes written since the prior read.
                if path.exists() and path.stat().st_size > raw_count:
                    continue
                break
            time.sleep(.02)
        try:
            exit_code = future.result()
        except Exception as exc:
            failure = type(exc).__name__ + ': ' + str(exc)
    if partial:
        arrivals.append((round(time.monotonic() - started, 3), partial))
    raw = (run_dir / 'process.log').read_bytes()
    scrub = scrubber(cwd)
    public_lines = []
    timeline = []
    end = 0
    for index, (stamp, line) in enumerate(arrivals):
        try:
            decoded = json.loads(line)
        except (ValueError, UnicodeError):
            clean_line = scrub(line.decode('utf-8', errors='replace'))
        else:
            clean_line = json.dumps(scrub(decoded), ensure_ascii=False)
        terminated = index < len(arrivals) - 1 or raw.endswith(b'\n')
        encoded = clean_line.encode() + (b'\n' if terminated else b'')
        public_lines.append(encoded)
        end += len(encoded)
        timeline.append({'read_seconds': stamp, 'end': end})
    sanitized = b''.join(public_lines)
    target = EVIDENCE / runtime
    target.mkdir(parents=True, exist_ok=False)
    (target / 'process.log').write_bytes(sanitized)
    (target / 'arrivals.json').write_text(json.dumps(timeline, indent=2) + '\n')
    final_result = None
    if runtime == 'claude' and public_lines:
        try:
            last = json.loads(public_lines[-1])
            final_result = {'type': last.get('type'), 'subtype': last.get('subtype'),
                            'is_error': last.get('is_error')}
        except ValueError:
            pass
    complete = exit_code == 0 and (final_result and final_result['type'] == 'result'
                                   if runtime == 'claude' else b'SPIKE111_MESSAGE_END' in sanitized)
    meta = {'runtime': runtime, 'version': version, 'runtime_spec': spec.name,
            'invocation': argv, 'cwd': '<owned fixture>', 'started_utc': utc,
            'seconds': round(time.monotonic() - started, 3), 'timeout_seconds': 120,
            'exit_code': exit_code, 'failure': scrub(failure) if failure else None,
            'complete': bool(complete), 'final_result': final_result,
            'owned_group_empty': all(not group_members(g) for g in groups),
            'raw_sha256': hashlib.sha256(raw).hexdigest(), 'raw_bytes': len(raw),
            'sanitized_sha256': hashlib.sha256(sanitized).hexdigest(),
            'sanitized_bytes': len(sanitized), 'lines': len(public_lines),
            'provenance': 'one new owned read-only probe; launcher command_for and supervise; merged stdout/stderr to file',
            'sanitization': 'entire stream; no records selected or added; paths, IDs, credential-like values/email replaced; JSON reserialized',
            'limitations': ['synthetic workload, not a complete issue implementation',
                            'runtime safety arguments narrower than operator worker arguments',
                            'arrivals are line-completion observer read times, not producer timestamps',
                            'raw private recording is ignored and is never published',
                            'no actual operator-terminal observation']}
    (target / 'capture.json').write_text(json.dumps(meta, indent=2) + '\n')
    print(json.dumps({k: meta[k] for k in ('runtime', 'version', 'seconds', 'exit_code',
                                        'complete', 'failure', 'sanitized_bytes', 'owned_group_empty')}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-once', required=True, choices=('claude', 'codex'))
    args = parser.parse_args()
    capture(args.run_once)


if __name__ == '__main__':
    main()
