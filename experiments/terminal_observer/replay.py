"""Synthetic production-format fixtures; this module starts no runtime."""
import json
from pathlib import Path


def write_fixtures(folder):
    long_text = 'BEGIN tool output\n' + ''.join(f'row {i}: inspected synthetic local data\n' for i in range(1200)) + 'END tool output'
    claude = [
        {'type': 'system', 'subtype': 'init', 'tools': ['Read', 'Bash'], 'session_id': 'synthetic'},
        {'type': 'assistant', 'message': {'content': [
            {'type': 'text', 'text': 'Inspecting a synthetic file.'},
            {'type': 'tool_use', 'id': 'synthetic-read', 'name': 'Read', 'input': {'file_path': 'READ_ME.txt'}}]}},
        {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'synthetic-read', 'content': 'SPIKE97_OBSERVER_OK'}]}},
        {'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'synthetic-long', 'name': 'Bash', 'input': {'command': 'cat synthetic.txt'}}]}},
        {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'synthetic-long', 'content': long_text}]}},
        {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'synthetic-failed', 'is_error': True,
             'content': [{'type': 'text', 'text': 'Synthetic command exited 1'}]}]}},
        {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed'}},
        {'type': 'future.event', 'payload': 'unfamiliar synthetic event'},
        {'type': 'error', 'message': 'Synthetic read error; no workflow result'},
        {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'SPIKE97_OBSERVER_OK: read completed.'}]}},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'SPIKE97_OBSERVER_OK'},
    ]
    codex = ('OpenAI Codex (synthetic production-format transcript)\n--------\n'
             'workdir: /synthetic/worktree\nmodel: synthetic\n--------\n'
             'user\nRead READ_ME.txt only.\n'
             'exec\n/bin/bash -lc "cat READ_ME.txt" in /synthetic/worktree succeeded in 4ms:\n'
             'SPIKE97_OBSERVER_OK\n' + long_text + '\n'
             'ERROR: synthetic unfamiliar runtime diagnostic\n'
             'codex\nSPIKE97_OBSERVER_OK: read completed.\ntokens used\n123\n')
    paths = {}
    for runtime, text in [('claude', ''.join(json.dumps(r) + '\n' for r in claude)), ('codex', codex)]:
        path = folder / f'{runtime}-production' / 'process.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        paths[runtime] = path
    return paths


if __name__ == '__main__':
    write_fixtures(Path('experiments/terminal_observer/evidence'))
