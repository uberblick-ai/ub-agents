"""Local claims and cancellation for a launcher's background cleanup."""

from contextlib import contextmanager
import threading

from .errors import AgentError


class CleanupStopped(Exception):
    pass


class CleanupLogError(Exception):
    pass


class CleanupControl:
    def __init__(self, stop):
        self.stop = stop
        self.stopping = False
        self.condition = threading.Condition()
        self.active = False
        self.locks = {}
        self.claims = set()

    def keys(self, artifact):
        keys = (('run', artifact.run),)
        return keys + ((('branch', artifact.name),) if artifact.kind == 'branch' else ())

    def lock(self, key):
        with self.condition:
            return self.locks.setdefault(key, threading.RLock())

    def protect(self, lease):
        # Register before publishing the claim, hence before creating a tree.
        # Only a claim for the very artifact being removed can wait here.
        keys = [('run', lease['run'])]
        if lease.get('recovered_run'):
            keys.append(('run', lease['recovered_run']))
        if lease.get('branch'):
            keys.append(('branch', lease['branch']))
        for key in keys:
            with self.lock(key):
                self.claims.add(key)

    def checkpoint(self):
        if self.stopping or self.stop.is_set():
            raise CleanupStopped()

    def check(self, artifact):
        self.checkpoint()
        if any(key in self.claims for key in self.keys(artifact)):
            raise AgentError('Artifact belongs to a run claimed by this launcher')

    @contextmanager
    def mutation(self, artifact):
        keys = self.keys(artifact)
        locks = [self.lock(key) for key in keys]
        for lock in locks:
            lock.acquire()
        try:
            with self.condition:
                self.check(artifact)
                self.active = True
            try:
                yield
            finally:
                with self.condition:
                    self.active = False
                    self.condition.notify_all()
        finally:
            for lock in reversed(locks):
                lock.release()

    def close(self):
        # Do not join inventory, GitHub reads or fetches. The daemon may finish
        # those reads, but cannot start another hook, removal or prune.
        with self.condition:
            self.stopping = True
            while self.active:
                self.condition.wait()
