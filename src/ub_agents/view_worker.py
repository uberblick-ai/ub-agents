"""One expendable daemon reads local files; slow storage never blocks the UI."""

from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty, Full, Queue
import threading

from .view_data import context_text, local_description, load_session, mapping, rows, work_rows
from .view_logs import ViewReader


@dataclass(frozen=True)
class Request:
    key: str | None
    token: int
    older_end: int | None = None
    generation: int = 0


@dataclass(frozen=True)
class Result:
    session: object
    rows: tuple
    key: str | None
    token: int
    context: str
    log: object = None
    page: object = None
    history: object = None
    error: str | None = None
    runtime: str = 'unknown'
    description: object = None


class LocalWorker:
    def __init__(self, root, session_path):
        self.root, self.session_path = Path(root), Path(session_path)
        self.requests, self.results = Queue(maxsize=1), Queue(maxsize=1)
        self.stopping = threading.Event()
        self.readers = OrderedDict()
        self.contexts = OrderedDict()
        self.selected_row = None
        self.thread = threading.Thread(target=self.run, name='local-view-reader', daemon=True)

    def start(self):
        self.thread.start()

    def request(self, request):
        try:
            self.requests.put_nowait(request)
            return True
        except Full:
            return False

    def close(self):
        self.stopping.set()

    def read(self, request):
        session = load_session(self.session_path)
        work = work_rows(session, self.root)
        selected = next((r for r in work if r.key == request.key), None)
        if not request.key and work:
            selected = work[0]
        if selected is None and self.selected_row and self.selected_row.key == request.key:
            selected = self.selected_row
            foreign = any(row.get('state') == 'owned' and
                          (row.get('item'), row.get('agent')) == (selected.item, selected.agent)
                          for row in rows(mapping(session.data.get('latest_pass')).get('rows'), 100))
            if selected.key.startswith('plan:') and foreign:
                selected = replace(selected, hidden=True)
            if selected.state != 'earlier observation':
                selected = replace(selected, state='earlier observation',
                                   reason=f'Last observed state: {selected.state}. {selected.reason}')
            work.append(selected)
        self.selected_row = selected
        # Preserve the selected own run while a sparse snapshot or new pass arrives.
        # Its log and cached context remain a local snapshot of the prior selection.
        source = next((row for row in rows(mapping(session.data.get('latest_pass')).get('rows'), 100)
                       if selected and row.get('item') == selected.item), {})
        item_key = (session.data.get('repository'), selected.item) if selected else None
        context_key = (item_key, selected.key, selected.state, selected.reason, repr(selected.data), repr(source)) if selected else None
        context = self.contexts.get(context_key)
        if context is None:
            context = local_description(selected, session)
            self.contexts[context_key] = context
            while len(self.contexts) > 21:
                self.contexts.popitem(last=False)
        if not context.available:
            context = next((value for key, value in reversed(self.contexts.items())
                            if key and key[0] == item_key and value.available), context)
        log = page = history = None
        error = None
        runtime = 'unknown'
        if selected and selected.log:
            identity = str(selected.log)
            reader = self.readers.get(identity)
            if reader is None:
                reader = self.readers[identity] = ViewReader(selected.log, selected.runtime)
            self.readers.move_to_end(identity)
            runtime = reader.runtime
            while len(self.readers) > 21:
                self.readers.popitem(last=False)
            log, page = reader.update(), reader.page()
            if request.older_end is not None:
                try:
                    history = reader.older(request.older_end, request.generation)
                except (OSError, ValueError) as exc:
                    error = str(exc)
        return Result(session, tuple(work), selected.key if selected else request.key, request.token,
                      context_text(selected, context), log, page, history, error, runtime, context)

    def run(self):
        while not self.stopping.is_set():
            try:
                request = self.requests.get(timeout=0.1)
            except Empty:
                continue
            try:
                result = self.read(request)
            except Exception as exc:
                from .view_data import Session
                result = Result(Session(self.session_path, {}, str(exc)), (), request.key, request.token,
                                'Local read failed: ' + str(exc), error=str(exc))
            if self.stopping.is_set():
                return
            try:
                self.results.put_nowait(result)
            except Full:
                # Only the newest complete result matters; never a growing backlog.
                try:
                    self.results.get_nowait()
                except Empty:
                    pass
                self.results.put_nowait(result)
