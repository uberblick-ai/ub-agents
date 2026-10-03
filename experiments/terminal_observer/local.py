"""Experiment-only tap: no discovery reads, no workflow decisions, no worker IO.

TapLoop delegates every operation to Loop. The view consumes a bounded local
projection; it never receives the worker's GitHub facade or stop events.
"""
from collections import OrderedDict
from copy import deepcopy
from functools import wraps
import json
from pathlib import Path
from queue import Queue, Empty, Full
import socket
import threading
import time

from ub_agents.loop import Loop
from ub_agents.records import latest_leases, live_leases, same_run

from .logs import clean

MAX_ROWS = 64
MAX_RECENT = 20
MAX_DETAIL_REQUESTS = 20
MAX_DETAIL = 8192
MAX_SNAPSHOT = 2 * 1024 * 1024
LOCAL_GROUPS = ('Current work', 'Observed ownership', 'Needs attention', 'Eligible', 'Waiting', 'Local recent activity')


def bounded(value, limit=MAX_DETAIL):
    value = str(value)
    if len(value) > limit:
        value = value[:limit] + '\n[projection shortened; open details or original source]'
    return value


def record_view(record):
    if not record:
        return None
    # Drop declarations/context, never duplicate large prompts in the snapshot.
    keys = ('kind', 'id', 'run', 'agent', 'assignment', 'lease_id', 'state', 'actor',
            'host', 'log_dir', 'expires', 'created', 'runtime', 'result', 'summary',
            'status', 'accepted', 'transition_complete', 'outcome')
    result = {key: bounded(record[key], 2048) if isinstance(record[key], str) else record[key]
              for key in keys if key in record}
    transition = record.get('transition', {})
    if record.get('accepted') and record.get('transition_complete'):
        result['observed_stop_labels'] = [bounded(label, 256) for label in sorted(
            set(transition.get('add', ())).intersection(transition.get('stop_labels', ())))[:16]]
    return result


def row_for(plan):
    history = plan.history
    active = live_leases(history, time.time())
    latest = latest_leases(history).get((plan.item.number, plan.agent.name))
    source = next((r for r in active if r['agent'] == plan.agent.name), None) or latest
    outcomes = [r for r in history if source and r['kind'] == 'outcome'
                and r.get('lease_id') == source['id'] and same_run(r, source)]
    return {'number': plan.item.number, 'kind': plan.item.kind, 'agent': plan.agent.name,
            'state': plan.state, 'reason': bounded(plan.reason, 2048),
            'attempts': plan.attempt - 1, 'open_blockers': [bounded(b, 512) for b in plan.blockers[:16]],
            'runtime': plan.runtime.name if plan.runtime else None,
            'lease': record_view(active[0] if active else None),
            'outcome': record_view(outcomes[-1] if outcomes else None),
            'result': latest.get('result') if latest else None, 'observed_at': time.time()}


class LatestFile:
    """One pending snapshot, atomic replace, disk writes off the worker thread."""
    def __init__(self, path):
        self.path = path
        self.pending = Queue(maxsize=1)
        self.error = None
        self.coalesced = 0
        self.closed = False
        self.thread = threading.Thread(target=self._write, name='spike-projection', daemon=True)
        self.thread.start()

    def submit(self, snapshot):
        if self.closed:
            return
        try:
            self.pending.put_nowait(snapshot)
        except Full:
            try:
                self.pending.get_nowait()
                self.pending.task_done()
                self.coalesced += 1
            except Empty:
                pass
            self.pending.put_nowait(snapshot)

    def _write(self):
        while True:
            snapshot = self.pending.get()
            try:
                if snapshot is None:
                    return
                data = json.dumps(snapshot, ensure_ascii=False).encode()
                if len(data) > MAX_SNAPSHOT:
                    raise ValueError('bounded projection exceeded snapshot cap')
                temporary = self.path.with_suffix('.pending')
                temporary.write_bytes(data)
                temporary.replace(self.path)
                self.error = None
            except Exception as error:
                self.error = str(error)
            finally:
                self.pending.task_done()

    def close(self):
        # Only the harness teardown waits for disk IO, never an execution callback.
        self.closed = True
        self.pending.join()
        self.pending.put(None)
        self.thread.join()


class Projection:
    def __init__(self, repository, publisher=None):
        self.repository = repository
        self.publisher = publisher
        self.rows = OrderedDict()
        self.details = OrderedDict()
        self.recent = OrderedDict()
        self.current = None
        self.phase = 'not observed'
        self.pass_complete = False
        self.generation = 0
        self.evicted = 0
        self.error = None
        self.lock = threading.RLock()

    def snapshot(self):
        with self.lock:
            return deepcopy({'repository': self.repository, 'generation': self.generation,
                'published_at': time.time(), 'phase': self.phase, 'pass_complete': self.pass_complete,
                'current': self.current, 'rows': list(self.rows.values()),
                'details': dict(self.details), 'recent': list(self.recent.values()),
                'evicted': self.evicted, 'tap_error': self.error})

    def changed(self):
        self.generation += 1
        if self.publisher:
            self.publisher.submit(self.snapshot())

    def observe(self, plan):
        with self.lock:
            key = f'{plan.item.number}:{plan.agent.name}'
            self.rows[key] = row_for(plan)
            self.rows.move_to_end(key)
            self.details[str(plan.item.number)] = {'title': bounded(plan.item.title, 512),
                'body': bounded(plan.item.body), 'observed_at': time.time(),
                'complete': len(plan.item.body) <= MAX_DETAIL, 'source': 'launcher plan'}
            self.details.move_to_end(str(plan.item.number))
            while len(self.rows) > MAX_ROWS:
                self.rows.popitem(last=False)
                self.evicted += 1
            while len(self.details) > MAX_ROWS:
                self.details.popitem(last=False)
            self.changed()

    def begin(self, plan):
        with self.lock:
            self.current = row_for(plan) | {'local_current': True,
                'reason': 'Launcher preparing this assignment; execution not yet confirmed'}
            self.changed()

    def record(self, lease=None, outcome=None):
        with self.lock:
            if not self.current:
                return
            if lease and lease['assignment'] == self.current['number'] and lease['agent'] == self.current['agent']:
                self.current['lease'] = record_view(lease)
                self.current['result'] = lease.get('result')
                self.current['state'] = lease['state']
                self.current['reason'] = ('Local execution lease observed; output is untrusted'
                    if lease['state'] == 'running' else f'Local lease: {lease["state"]}')
            if outcome and outcome['assignment'] == self.current['number']:
                self.current['outcome'] = record_view(outcome)
            self.current['observed_at'] = time.time()
            self.changed()

    def finish(self):
        with self.lock:
            if self.current:
                row = self.current
                lease = row.get('lease')
                if lease and lease['state'] == 'released':
                    key = lease['run']
                    self.recent[key] = deepcopy(row) | {'local_current': False, 'local_recent': True}
                    self.recent.move_to_end(key)
                    outcome = row.get('outcome') or {}
                    observed_key = f'{row["number"]}:{row["agent"]}'
                    if outcome.get('accepted') and outcome.get('transition_complete'):
                        # This launcher observed the completed transition already.
                        # Keep its human handoff visible separately from history,
                        # without a read to discover the resulting labels again.
                        stops = outcome.get('observed_stop_labels', [])
                        if stops:
                            self.rows[observed_key] = deepcopy(row) | {'local_current': False,
                                'lease': None, 'state': 'parked',
                                'reason': 'Observed accepted transition added stop labels: ' + ', '.join(stops)}
                        else:
                            self.rows.pop(observed_key, None)
                    while len(self.recent) > MAX_RECENT:
                        self.recent.popitem(last=False)
                        self.evicted += 1
                # A failed authority read is not fabricated into a completed run.
                self.current = None
                self.changed()


class TapLoop(Loop):
    """Small experiment subclass; production modules and flags stay unchanged."""
    def __init__(self, *args, projection, **kwargs):
        super().__init__(*args, **kwargs)
        self.projection = projection
        self._tap_depth = 0
        # Observe return values/mutated arguments only AFTER existing methods succeed.
        for name in ('claim', 'update', 'outcome', 'report', 'update_outcome', 'accept', 'release'):
            original = getattr(self.coordinator, name)
            @wraps(original)
            def observed(*arguments, _name=name, _original=original, **options):
                result = _original(*arguments, **options)
                def capture():
                    if _name == 'claim':
                        self.projection.record(lease=result)
                    elif _name in {'outcome', 'report'}:
                        self.projection.record(outcome=result)
                    elif _name in {'accept', 'update_outcome'}:
                        self.projection.record(lease=arguments[0], outcome=arguments[1])
                    else:
                        self.projection.record(lease=arguments[0])
                self._tap(capture)
                return result
            setattr(self.coordinator, name, observed)

    def _tap(self, action):
        try:
            action()
        except Exception as error:
            # UI failure cannot veto an ownership write or terminate execution.
            self.projection.error = str(error)

    def iter_plans(self, cached=True):
        self._tap_depth += 1
        outer = self._tap_depth == 1
        if outer:
            self.projection.phase = 'ordinary discovery (partial)'
            self.projection.pass_complete = False
        complete = False
        try:
            for plan in super().iter_plans(cached=cached):
                self._tap(lambda: self.projection.observe(plan))
                yield plan
            complete = True
        finally:
            self._tap_depth -= 1
            if outer:
                self.projection.pass_complete = complete
                self.projection.phase = ('last ordinary discovery completed' if complete
                                         else 'ordinary discovery stopped early; partial list')
                self._tap(self.projection.changed)

    def execute(self, plan):
        self._tap(lambda: self.projection.begin(plan))
        try:
            return super().execute(plan)
        finally:
            self._tap(self.projection.finish)

    def recover(self, plan):
        self._tap(lambda: self.projection.begin(plan))
        try:
            return super().recover(plan)
        finally:
            self._tap(self.projection.finish)


class LocalObserver:
    """No automatic network operations. Missing details require an explicit open."""
    groups = LOCAL_GROUPS
    live = False

    def __init__(self, path=None, *, projection=None, detail_reader=None):
        self.path = path
        self.projection = projection
        self.detail_reader = detail_reader
        self.rows = []
        self.details = {}
        self.detail_meta = OrderedDict()
        self.refreshed = None
        self.poll = 'waiting for this launcher’s local projection'
        self.generation = -1
        self.file_signature = None
        self.loading = set()
        self.requested_details = {}
        self.detail_reads = 0
        self.detail_wait_until = 0
        self.phase = ''
        self.recent_count = 0
        self.evicted = 0

    @staticmethod
    def group(row):
        if row.get('local_current'):
            return 'Current work'
        if row.get('local_recent'):
            return 'Local recent activity'
        if row.get('lease') or row['state'] == 'owned':
            return 'Observed ownership'
        from .model import group
        name = group(row)
        return 'Local recent activity' if name == 'Recent activity' else name

    def refresh(self):
        try:
            if self.projection:
                if self.projection.generation == self.generation:
                    return False
                data = self.projection.snapshot()
            else:
                stat = self.path.stat()
                signature = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
                if signature == self.file_signature:
                    return False
                with self.path.open('rb') as stream:
                    raw = stream.read(MAX_SNAPSHOT + 1)
                if len(raw) > MAX_SNAPSHOT:
                    raise ValueError('local snapshot exceeds 2MiB cap')
                data = json.loads(raw)
                self.file_signature = signature
            if data['generation'] == self.generation:
                return False
            self.generation = data['generation']
            self.refreshed = data['published_at']
            current = data['current']
            rows = data['rows'][-MAX_ROWS:]
            if current:
                rows = [r for r in rows if (r['number'], r['agent']) != (current['number'], current['agent'])]
            self.rows = ([current] if current else []) + rows + data['recent'][-MAX_RECENT:]
            self.recent_count = len(data['recent'])
            self.evicted = data['evicted']
            for key, details in data['details'].items():
                number = int(key)
                previous = self.detail_meta.get(number) or self.requested_details.get(number)
                if not previous or (details['complete'] and details['observed_at'] > previous['observed_at']):
                    self.detail_meta[number] = details
                    self.details[number] = (details['title'], details['body'])
                    if number in self.requested_details:
                        self.requested_details[number] = details
            self._trim_details()
            self.phase = data['phase']
            self.poll = f'{self.phase}; list limited to observations; no background GitHub reads'
            if data['tap_error']:
                self.poll += f'; tap error: {clean(data["tap_error"])}'
            return True
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.poll = f'local projection unavailable/stale: {clean(error)}'
            return False

    def _trim_details(self):
        while len(self.detail_meta) > MAX_ROWS:
            number, _ = self.detail_meta.popitem(last=False)
            if number not in self.requested_details:
                self.details.pop(number, None)

    def details_needed(self, row):
        meta = self.requested_details.get(row['number']) or self.detail_meta.get(row['number'], {})
        return not meta.get('complete') and not meta.get('error')

    def open_details(self, row):
        # Called only by the explicit o key; one transport call, no pagination/retry.
        number = row['number']
        if not self.details_needed(row) or number in self.loading:
            return
        if time.time() < self.detail_wait_until or self.detail_reads >= MAX_DETAIL_REQUESTS or len(self.requested_details) >= MAX_DETAIL_REQUESTS:
            return
        self.loading.add(number)
        try:
            if not self.detail_reader:
                raise ValueError('no optional detail transport configured')
            self.detail_reads += 1
            result = self.detail_reader(row)
            title, body = bounded(result['title'], 512), bounded(result.get('body') or '', MAX_DETAIL)
            self.details[number] = (title, body)
            self.detail_meta[number] = {'title': title, 'body': body, 'complete': True,
                'shortened': len(result.get('body') or '') > MAX_DETAIL,
                'observed_at': time.time(), 'source': 'explicit one-item read; cached for session'}
        except Exception as error:
            from ub_agents.errors import GitHubError
            if isinstance(error, GitHubError) and error.rate_limited:
                self.detail_wait_until = error.reset_at or time.time() + 60
            previous = self.detail_meta.get(number, {})
            self.detail_meta[number] = previous | {'error': clean(error), 'observed_at': time.time(),
                'source': 'explicit detail read failed; no automatic retry'}
        finally:
            self.requested_details[number] = self.detail_meta[number]
            self.loading.discard(number)
            self._trim_details()

    def detail_state(self, row):
        number = row['number']
        if number in self.loading:
            return 'Loading explicitly requested details; worker continues'
        meta = self.requested_details.get(number) or self.detail_meta.get(number, {})
        if time.time() < self.detail_wait_until and not meta.get('complete'):
            return f'Detail reads rate-limited until {self.detail_wait_until:.0f}; no automatic retry'
        if (self.detail_reads >= MAX_DETAIL_REQUESTS or len(self.requested_details) >= MAX_DETAIL_REQUESTS) and number not in self.requested_details:
            return 'Session detail-read allowance exhausted (20); no request made'
        age = f'{max(0, time.time() - meta["observed_at"]):.0f}s old' if meta.get('observed_at') else 'age unavailable'
        if meta.get('error'):
            return f'Details error: {meta["error"]} ({age}); cached failure, no retry'
        if meta.get('complete'):
            return f'Details: {meta["source"]} ({age})' + ('; display shortened' if meta.get('shortened') else '')
        return f'Details unavailable/incomplete ({age}); press o to request this item once'

    def log_path(self, row):
        # Another launcher's same-host claim is not this launcher's current work.
        if not (row.get('local_current') or row.get('local_recent')):
            return None
        lease = row.get('lease')
        if not lease or lease.get('host') != socket.gethostname():
            return None
        directory = lease.get('log_dir')
        if not directory or not Path(directory).is_absolute():
            return None
        return Path(directory) / 'process.log'


def fixture_observer(root, path):
    """Reuse prior fixtures; this seeded projection is replay, not integration."""
    from .model import fixture_loop
    loop, _ = fixture_loop(root, path)
    projection = Projection(loop.config.repository)
    for plan in loop.iter_plans():
        projection.observe(plan)
    row = next(r for r in projection.rows.values() if r['number'] == 1)
    projection.current = row | {'local_current': True}
    projection.phase = 'seeded synthetic launcher observations'
    projection.changed()
    return LocalObserver(projection=projection)
