"""Read-only, in-memory discovery inputs; never used for claims or writes."""

from copy import deepcopy
from dataclasses import replace


class Discovery:
    ITEM_READS = {"item", "comments", "timeline", "issue_content", "pr_content",
                  "reviews", "review_comments", "blocked_by"}

    def __init__(self, github):
        self.github = github
        self.repository = github.repository
        self.items = {}
        self.comments_index = {}
        self.cache = {}
        self.scope = None
        self.graph_loaded = False

    def observe(self):
        items = {item.number: item for item in self.github.observe(details=False)}
        comments = self.github.repository_comments()
        groups = {}
        for comment in comments:
            # Malformed coordination comments are diagnosed by repository_history.
            try:
                number = int(comment["issue_url"].rsplit("/", 1)[1])
            except (KeyError, ValueError, AttributeError, TypeError):
                continue
            groups.setdefault(number, []).append(comment)
        list_changed = {n for n in self.items.keys() | items.keys()
                        if self.items.get(n) != items.get(n)}
        pr_changed = any((self.items.get(n) or items[n]).kind == "pr" for n in list_changed)
        changed = set(list_changed)
        changed.update(n for n in self.comments_index.keys() | groups.keys()
                       if self.comments_index.get(n) != groups.get(n))
        if self.repository != self.github.repository:
            self.cache.clear()
            self.graph_loaded = False
            self.repository = self.github.repository
        self.cache = {key: value for key, value in self.cache.items()
                      if not (key[0] in self.ITEM_READS and key[1][0] in changed)
                      and not (key[0] == "role" and key[2] in changed)
                      and not (key[0] == "prs_for_branch" and pr_changed)}
        self.items = items
        self.comments_index = deepcopy(groups)
        return dict(items), comments

    def invalidate(self, number):
        self.cache = {key: value for key, value in self.cache.items()
                      if not (key[0] in self.ITEM_READS and key[1][0] == number)
                      and not (key[0] == "role" and key[2] == number)}

    def prepare_dependencies(self, items):
        missing = [i.number for i in items if i.kind == "issue" and i.state == "open"
                   and i.total_blocked_by != 0 and ("blocked_by", (i.number,), None) not in self.cache]
        if missing and not self.graph_loaded:
            graph = self.github.dependency_graph()
            for number in missing:
                if number in graph:
                    self.cache[("blocked_by", (number,), None)] = deepcopy(graph[number])
            self.graph_loaded = True

    def __getattr__(self, name):
        if name not in self.ITEM_READS | {"role", "prs_for_branch"}:
            raise AttributeError(name)

        def read(*args):
            key = (name, args, self.scope if name == "role" else None)
            if key not in self.cache:
                # Failed reads are never retained, so later polls can recover.
                value = getattr(self.github, name)(*args)
                if name == "role" and value is None:
                    return None  # An unreadable permission must be retried.
                self.cache[key] = deepcopy(value)
            result = deepcopy(self.cache[key])
            if name == "blocked_by":
                # A local blocker's state is already in the fresh repository list.
                # Closing it need not reread every dependent's unchanged links.
                result = [replace(b, state=self.items[b.number].state if b.number in self.items else "closed")
                          if b.repository.casefold() == self.repository.casefold() else b for b in result]
            return result
        return read
