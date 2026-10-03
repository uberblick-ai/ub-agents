"""Renew one launcher's claim throughout setup, execution and completion."""

import threading

from .config import LEASE_RENEW_SECONDS
from .errors import LostOwnership


class LeaseRenewal:
    def __init__(self, coordinator, github):
        self.coordinator = coordinator
        self.github = github
        self.lease = None
        self.next_renewal = None
        self.stop = threading.Event()
        self.thread = None

    def claimed(self, lease):
        self.lease = lease
        self.next_renewal = self.coordinator.clock() + LEASE_RENEW_SECONDS
        self.thread = threading.Thread(target=self._run, name="lease-renewal")
        self.thread.start()

    def tick(self):
        """Also usable with a controlled clock without starting a worker."""
        self.coordinator.deadline(self.lease)
        now = self.coordinator.clock()
        if now >= self.next_renewal:
            # Failures wait for the next interval too; no write replay.
            self.next_renewal = now + LEASE_RENEW_SECONDS
            self.coordinator.renew(self.lease, self.github)

    def _run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except LostOwnership:
                return
            # Check wall clock on wake, including after a machine sleeps.
            self.stop.wait(min(1, max(0, self.next_renewal - self.coordinator.clock())))

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
