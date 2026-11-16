"""Read the live local launcher's advisory runtime state without changing it."""

from itertools import islice

from ..attention import stamp
from ..launcher_lock import running_launcher
from ..observation_data import mapping, read_json


def runtime_state(config, now):
    owner = running_launcher(config.root)
    if owner is None:
        return {}
    latest, published = {}, float('-inf')
    for path in islice((config.root / '.ub-agents' / 'sessions').glob('*.json'), 256):
        try:
            data = read_json(path)
        except (OSError, ValueError, RecursionError):
            continue  # An incomplete advisory file cannot pause a runtime.
        moment = stamp(data.get('published_at'))
        if (data.get('repository') == config.repository and data.get('pid') == owner['pid']
                and data.get('host') == owner['host'] and data.get('version') == 1 and not data.get('ended')
                and moment is not None and 0 <= now - moment <= 30 and moment > published):
            latest, published = data, moment
    return latest


def runtime_pauses(config, now):
    latest = runtime_state(config, now)
    return {cli: end for cli, value in mapping(latest.get('runtime_pauses')).items()
            if cli in {'claude', 'codex'} and (end := stamp(value)) is not None and end > now}


def runtime_blocks(config, now):
    return valid_blocks(runtime_state(config, now).get('runtime_blocks'))


def valid_blocks(values):
    if not isinstance(values, list):
        return []
    keys = ('agent', 'runtime', 'setting', 'remedy')
    return [{key: value[key] for key in keys} for value in values
            if isinstance(value, dict) and all(isinstance(value.get(key), str) and value[key] for key in keys)]


def held_block(blocks, agent, runtime):
    return next((block for block in blocks if block['agent'] == agent.name and block['runtime'] == runtime.name), None)
