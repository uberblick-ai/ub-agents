"""Read the live local launcher's advisory usage pauses without changing it."""

from itertools import islice

from ..attention import stamp
from ..launcher_lock import running_launcher
from ..observation_data import mapping, read_json


def runtime_pauses(config, now):
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
    return {cli: end for cli, value in mapping(latest.get('runtime_pauses')).items()
            if cli in {'claude', 'codex'} and (end := stamp(value)) is not None and end > now}
