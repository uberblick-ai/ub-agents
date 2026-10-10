"""Share completion observations only until the launcher's next GitHub write."""

from copy import deepcopy

from .rate_limits import RateLimitReads, READS
from .records import same_run, seconds


class FinalizationReads(RateLimitReads):
    def __init__(self, github, wait):
        super().__init__(github, wait)
        self._finalization = None
        self._renewals = {}

    def begin_finalization(self):
        if self._finalization is None:
            self._finalization = {}

    def end_finalization(self):
        self._finalization = None
        self._renewals.clear()

    def _read(self, key, read):
        if self._finalization is None:
            return read()
        if key not in self._finalization:
            self._finalization[key] = read()
        # Consumers may edit an outcome after selecting it from history.
        return deepcopy(self._finalization[key])

    def read_history(self, number, read):
        return self._read(("history", number), read)

    def item(self, number, kind=None):
        read = super().__getattr__("item")
        return self._read(("item", number), lambda: read(number, kind))

    def renewed(self, lease):
        if self._finalization is not None:
            self._renewals[lease["id"], lease["run"]] = lease["expires"]

    def ownership_history(self, lease, history):
        expiry = self._renewals.get((lease["id"], lease["run"]))
        if self._finalization is None or expiry is None:
            return history
        # Renewal has its own fresh ownership read and changes only expiry.
        # Carry that confirmed extension into the shared observation, without
        # overwriting any observed release, withdrawal or competing owner.
        return [r | {"expires": expiry}
                if r["kind"] == "lease" and r["id"] == lease["id"] and same_run(r, lease)
                and r["state"] in {"claiming", "running"} and seconds(r["expires"]) < seconds(expiry)
                else r for r in history]

    def __getattr__(self, name):
        operation = super().__getattr__(name)
        if name in READS or not callable(operation):
            return operation

        def write(*args, **kwargs):
            # Even a failed or interrupted write may have reached GitHub. No
            # observation from before the attempt can authorize a later write.
            if self._finalization is not None:
                self._finalization.clear()
            try:
                return operation(*args, **kwargs)
            finally:
                if self._finalization is not None:
                    self._finalization.clear()
        return write
