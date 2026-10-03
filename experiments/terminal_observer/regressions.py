"""Prove the two review regressions reject their former behavior; no network."""
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from . import validate
from .logs import Buffer
from .model import group


def old_order(row):
    if row['lease'] or row['state'] == 'owned':
        return 'Running'
    if row['outcome'] and row['outcome'].get('accepted') and row['result'] == 'success':
        return 'Recent activity'
    return group(row)


def main():
    findings = {}
    for name, mutation in [
        ('test_current_human_blocker_and_completed_history', patch.object(validate, 'group', old_order)),
        ('test_truncation_clears_unfinished_record_and_time', patch.object(Buffer, 'reset_record', lambda self: None)),
    ]:
        with mutation:
            result = unittest.TextTestRunner(stream=io.StringIO()).run(validate.Evidence(name))
        assert len(result.failures) == 1 and not result.errors, (name, result.failures, result.errors)
        findings[name] = {'former_behavior_mutation_rejected': True, 'assertion_failures': 1}
    Path('experiments/terminal_observer/evidence/regressions.json').write_text(json.dumps(findings, indent=2) + '\n')
    print(json.dumps(findings))


if __name__ == '__main__':
    main()
