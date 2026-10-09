"""Read-only, in-memory discovery inputs; never used for claims or writes."""

from copy import deepcopy
from dataclasses import replace

from .errors import AgentError
from .records import seconds


class Discovery:
    ITEM_READS = {"item", "comments", "timeline", "issue_content", "pr_content",
                  "reviews", "review_comments", "blocked_by"}

    def __init__(self, github):
        self.github = github
        self.repository = github.repository
        self.items = {}
        self.closed_items = set()
        self.comments_index = {}
        self.comment_store = {}
        self.reconciled_comments = set()
        self.comment_window_start = None
        self.repository_index = {}
        self.cache = {}
        self.scope = None
        self.graph_loaded = False
        self.pass_roles = {}
        self.pass_errors = {}
        self.pass_comments = set()

    def observe(self, lookback_seconds):
        self.pass_roles = {}
        self.pass_errors = {}
        self.pass_comments = set()
        items = {item.number: item for item in self.github.observe(details=False)}
        comments = self.github.repository_comments(lookback_seconds=lookback_seconds)
        if self.repository != self.github.repository:
            self.cache.clear()
            self.graph_loaded = False
            self.closed_items.clear()
            self.items = {}
            self.comments_index = {}
            self.comment_store = {}
            self.reconciled_comments.clear()
            self.repository_index = {}
            self.comment_window_start = None
            self.repository = self.github.repository
        if self.comment_window_start is None:
            self.comment_window_start = getattr(self.github, "comment_window_start", None)
        groups = {}
        for comment in comments:
            # Malformed coordination comments are diagnosed by repository_history.
            try:
                number = int(comment["issue_url"].rsplit("/", 1)[1])
            except (KeyError, ValueError, AttributeError, TypeError):
                continue
            groups.setdefault(number, {})[comment["id"]] = comment.get("updated_at")
            # The bounded cache can still contain a deleted row after a per-item
            # repair. Only a new ID/update from the scan may change stored rows.
            previous = self.repository_index.get(number, {})
            if (number not in self.comment_store or comment["id"] not in previous
                    or previous[comment["id"]] != comment.get("updated_at")):
                self.comment_store.setdefault(number, {})[comment["id"]] = deepcopy(comment)
        retained = items.keys() | groups.keys()
        self.comment_store = {n: rows for n, rows in self.comment_store.items() if n in retained}
        self.reconciled_comments.intersection_update(retained)
        index = {n: self._comment_index(rows.values()) for n, rows in self.comment_store.items() if rows}
        list_changed = {n for n in self.items.keys() | items.keys()
                        if self.items.get(n) != items.get(n)}
        pr_changed = any((self.items.get(n) or items[n]).kind == "pr" for n in list_changed)
        changed = set(list_changed)
        changed.update(n for n in self.comments_index.keys() | index.keys()
                       if self.comments_index.get(n) != index.get(n))
        self.cache = {key: value for key, value in self.cache.items()
                      if not (key[0] in self.ITEM_READS and key[1][0] in changed)
                      and not (key[0] == "role" and key[2] in changed)
                      and not (key[0] == "prs_for_branch" and pr_changed)}
        self.closed_items.update(self.items.keys() - items.keys())
        self.closed_items.difference_update(items)
        self.items = items
        self.comments_index = index
        self.repository_index = groups
        return dict(items), comments

    @staticmethod
    def _comment_index(comments):
        return sorted((comment["id"], comment.get("updated_at")) for comment in comments)

    def comments(self, number):
        key = ("comments", (number,), None)
        if key in self.pass_errors:
            raise self.pass_errors[key]
        item = self.items.get(number)
        rows = self.comment_store.get(number, {})
        complete = (item is not None and item.state == "open"
                    and type(item.comments_count) is int and item.comments_count >= 0
                    and len(rows) == item.comments_count
                    and (number in self.reconciled_comments
                         or (self.comment_window_start is not None
                             and seconds(item.created_at) >= self.comment_window_start)))
        if complete:
            if key not in self.cache:
                self.cache[key] = deepcopy(sorted(rows.values(), key=lambda c: c["id"]))
        elif number not in self.pass_comments:
            try:
                comments = self.github.comments(number)
            except AgentError as exc:
                self.pass_errors[key] = exc
                raise
            index = self._comment_index(comments)
            if self.comments_index.get(number, []) != index:
                self.invalidate(number)
            self.comment_store[number] = {c["id"]: deepcopy(c) for c in comments}
            self.reconciled_comments.add(number)
            if comments:
                self.comments_index[number] = index
            else:
                self.comments_index.pop(number, None)
            self.cache[key] = deepcopy(comments)
            self.pass_comments.add(number)
        return deepcopy(self.cache[key])

    def invalidate(self, number):
        self.pass_comments.discard(number)
        self.cache = {key: value for key, value in self.cache.items()
                      if not (key[0] in self.ITEM_READS and key[1][0] == number)
                      and not (key[0] == "role" and key[2] == number)}

    def observed_item(self, number):
        """An already-read item for display only; never fetch missing data."""
        return next((value for (name, args, _), value in self.cache.items()
                     if name == "item" and args[0] == number), self.items.get(number))

    def observed_comments(self, number):
        """Only comments already read for this item; never fetch for presentation."""
        return self.cache.get(("comments", (number,), None), ())

    def prepare_dependencies(self, items):
        missing = [i.number for i in items if i.kind == "issue" and i.state == "open"
                   and i.total_blocked_by != 0 and ("blocked_by", (i.number,), None) not in self.cache]
        if missing and not self.graph_loaded:
            graph = self.github.dependency_graph()
            for number in missing:
                if number in graph:
                    self.cache[("blocked_by", (number,), None)] = deepcopy(graph[number])
            self.graph_loaded = True

    def role(self, login):
        # Unchanged items retain their own observations between polls (#79).
        # Only fresh reads share this pass's memo; an older item's cached role
        # must not supply authority to an item whose inputs changed.
        if not isinstance(login, str) or not login:
            return None
        account = login.casefold()
        key = ("role", (account,), self.scope)
        if account in self.pass_roles:
            value = self.pass_roles[account]
            if value is not None:
                self.cache[key] = value
            return value
        if key not in self.cache:
            if account not in self.pass_roles:
                self.pass_roles[account] = self.github.role(login)
            value = self.pass_roles[account]
            if value is None:
                return None  # Share unreadable roles this pass, retry next pass.
            self.cache[key] = value
        return self.cache[key]

    def current_role(self, login):
        # Coordination authors must be rechecked even if no item changed.
        if not isinstance(login, str) or not login:
            return None
        account = login.casefold()
        key = ("role", (account,))
        if key in self.pass_errors:
            raise self.pass_errors[key]
        if account not in self.pass_roles:
            try:
                self.pass_roles[account] = self.github.role(login)
            except AgentError as exc:
                self.pass_errors[key] = exc
                raise
        return self.pass_roles[account]

    def __getattr__(self, name):
        if name not in self.ITEM_READS | {"prs_for_branch"}:
            raise AttributeError(name)

        def read(*args):
            key = (name, args, None)
            if key in self.pass_errors:
                raise self.pass_errors[key]
            if key not in self.cache:
                # History and approval share failures too, but the next pass
                # retries them even when list/comment inputs are unchanged.
                try:
                    value = getattr(self.github, name)(*args)
                except AgentError as exc:
                    self.pass_errors[key] = exc
                    raise
                self.cache[key] = deepcopy(value)
            result = deepcopy(self.cache[key])
            if name == "blocked_by":
                # A local blocker's state is already in the fresh repository list.
                # Closing it need not reread every dependent's unchanged links.
                result = [replace(b, state=self.items[b.number].state if b.number in self.items else "closed")
                          if b.repository.casefold() == self.repository.casefold()
                          and (b.number in self.items or b.number in self.closed_items) else b for b in result]
            return result
        return read
