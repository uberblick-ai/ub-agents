"""Offline provenance, sanitization and real-output coverage checks."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess

from experiments.terminal_observer.logs import Tail

ROOT = Path.cwd()
HERE = ROOT / 'experiments/runtime_logs_111'
EVIDENCE = HERE / 'evidence'
PIN = 'f9bdba7796e80c977e398dedc139d0717ba55db8'


def main():
    # Verify reused sources exactly; archival SVGs only normalize trailing whitespace.
    paths = subprocess.run(['git', 'ls-tree', '-r', '--name-only', PIN,
                            'experiments/terminal_observer'], capture_output=True,
                           text=True, check=True).stdout.splitlines()
    for path in paths:
        original = subprocess.run(['git', 'show', f'{PIN}:{path}'],
                                  capture_output=True, check=True).stdout
        actual = (ROOT / path).read_bytes()
        if path.endswith('.svg'):
            original = b'\n'.join(line.rstrip() for line in original.splitlines()) + b'\n'
        assert actual == original, path
    coverage = {'unchanged_source_commit': PIN, 'unchanged_files_checked': len(paths),
                'actual_operator_terminal': 'unverified', 'runtimes': {}}
    for runtime in ('claude', 'codex'):
        path = EVIDENCE / runtime / 'process.log'
        raw = path.read_bytes()
        text = raw.decode()
        metadata = json.loads((path.parent / 'capture.json').read_text())
        assert hashlib.sha256(raw).hexdigest() == metadata['sanitized_sha256']
        assert metadata['complete'] and metadata['exit_code'] == 0 and metadata['owned_group_empty']
        for pattern in (r'/(?:srv|mnt|home)/[^\s\"<>]+',
                        r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}',
                        r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}'):
            assert not re.search(pattern, text), 'publication scan failed; inspect privately'
        arrivals = json.loads((path.parent / 'arrivals.json').read_text())
        assert arrivals[-1]['end'] == len(raw)
        assert all(a['end'] < b['end'] and a['read_seconds'] <= b['read_seconds']
                   for a, b in zip(arrivals, arrivals[1:]))
        tail = Tail(path)
        entries = []
        seen = 0
        while tail.offset < len(raw) or tail.buffer.ready():
            tail.poll()
            count = tail.buffer.total - seen
            entries.extend(list(tail.buffer.entries)[-count:] if count else [])
            seen = tail.buffer.total
        common = {'complete_sanitized_stream': True, 'version': metadata['version'],
                  'invocation': metadata['invocation'], 'seconds': metadata['seconds'],
                  'decoded_records': len(entries), 'max_input_record_bytes': max(map(len, raw.splitlines())),
                  'adapter_shortening_visible': any('display shortened' in e.text for e in entries),
                  'messages': 'demonstrated: real start/end messages in owned probe',
                  'failures': 'demonstrated: intentional command exit 1; process/auth/transport failure unverified',
                  'partial': 'demonstrated in controlled replay of split real bytes; no partial-message flag',
                  'live_and_paused': 'headless controlled replay, not actual operator observation'}
        if runtime == 'claude':
            records = [json.loads(line) for line in raw.splitlines()]
            blocks = [block for record in records for block in record.get('message', {}).get('content', [])]
            tools = [b for b in blocks if b.get('type') == 'tool_use']
            results = [b for b in blocks if b.get('type') == 'tool_result']
            output = next(b['content'] for b in results if '\nSPIKE111_ROW_000:' in str(b['content']))
            failed = next(b for b in results if b.get('is_error'))
            assert 'Exit code 1' in failed['content']
            assert records[-1]['type'] == 'result' and records[-1]['is_error'] is False
            assert all('stream_event' != r['type'] for r in records)
            common.update(event_types=dict(Counter(r['type'] for r in records)),
                          tool_names=[b['name'] for b in tools],
                          real_tool_results=len(results),
                          command_output_bytes=len(output.encode()),
                          permission_denials=records[-1].get('permission_denials'),
                          final_result='success; not a workflow outcome')
        else:
            assert all(e.kind == 'text' for e in entries), 'Codex must remain human text'
            assert '--json' not in metadata['invocation']
            begin = text.index('SPIKE111_COMMAND_BEGIN\n')
            end = text.index('SPIKE111_COMMAND_END\n', begin)
            output = text[begin:end]
            assert 'exited 1' in text
            assert 'SPIKE111_MESSAGE_BEGIN\n' in text and 'SPIKE111_MESSAGE_END\n' in text
            common.update(event_types=f'none invented; {len(entries)} plain human-text lines',
                          commands='cat fixture_output.py; python3 fixture_output.py; cat missing-owned.txt',
                          command_output_bytes=len(output.encode()), final_result='exit 0 and final message; not a workflow outcome')
        rows = re.findall(r'^SPIKE111_ROW_\d{3}:', output, flags=re.MULTILINE)
        assert len(rows) == 240
        assert 'SPIKE111_LONG_BEGIN ' + 'abcdefghij' * 600 + ' SPIKE111_LONG_END' in output
        common['full_command_output_rows'] = len(rows)
        common['long_payload_chars'] = 6000
        common['fixture_output_complete'] = True
        coverage['runtimes'][runtime] = common
    (EVIDENCE / 'coverage.json').write_text(json.dumps(coverage, indent=2) + '\n')
    print('PASS: pinned sources/normalized SVGs verified; both complete owned probes verified; sanitized hashes, arrivals and real coverage checked')


if __name__ == '__main__':
    main()
