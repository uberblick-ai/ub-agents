"""Opt-in read-only refresh measurement; not part of offline validation."""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time

from ub_agents.cli import status_rows
from ub_agents.config import load_config
from ub_agents.github import GitHub, response_parts
from ub_agents.loop import Loop
from ub_agents.polling import idle_interval


def main():
    calls = []

    def record(command, **kwargs):
        endpoint = command[command.index('--include') + 1]
        method = command[command.index('--method') + 1]
        # GET REST and read-only GraphQL queries are the only permitted operations.
        if method != 'GET':
            assert endpoint == 'graphql' and method == 'POST'
            assert 'mutation' not in json.loads(kwargs['input'])['query']
        result = subprocess.run(command, **kwargs)
        status, headers, _ = response_parts(result.stdout)
        calls.append({'endpoint': endpoint, 'method': method, 'status': status,
                      'conditional': any(h.startswith('If-None-Match:') for h in command),
                      'quota': {k: v for k, v in headers.items() if k.startswith('x-ratelimit-')}})
        return result

    cfg = load_config(Path('ub-agent.yaml'))
    gh = GitHub(cfg.repository, runner=record)
    actor = gh.actor()
    result = {'repository': cfg.repository, 'observed_at': datetime.now(timezone.utc).isoformat(),
              'identity_calls': len(calls), 'source': 'real network; no runtime/log access', 'passes': []}
    loop = Loop(cfg, gh, actor, output=lambda *_: None)
    for mode in ['fresh status', 'fresh status with transport ETags', 'cached planner warm-up', 'cached planner unchanged']:
        before = len(calls), gh.rest_requests, gh.quota_requests
        started = time.time()
        rows = status_rows(loop) if mode.startswith('fresh') else list(loop.iter_plans())
        pass_calls = calls[before[0]:]
        gap, low = idle_interval(gh.quota_requests - before[2], cfg.poll_seconds,
                                 gh.resource_quotas, started, time.time() - started)
        result['passes'].append({'mode': mode, 'rows_or_plans': len(rows),
            'seconds': round(time.time() - started, 3), 'gh_calls': len(pass_calls),
            'rest_attempts': gh.rest_requests - before[1], 'rest_quota_requests': gh.quota_requests - before[2],
            'graphql_calls': sum(c['endpoint'] == 'graphql' for c in pass_calls),
            'http_statuses': dict(Counter(c['status'] for c in pass_calls)),
            'idle_start_gap_seconds': gap, 'low_resources': sorted(low), 'calls': pass_calls})
        path = Path('experiments/terminal_observer/evidence/network-refresh.json')
        path.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({k: v for k, v in result['passes'][-1].items() if k != 'calls'}), flush=True)


if __name__ == '__main__':
    main()
