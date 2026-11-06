"""What the browser monitor shows, read with the launcher's own record rules.

Every launcher records its runs as coordination comments on the item it works
on, so GitHub sees all of them. This module never writes to GitHub.
"""

import socket

from ..errors import RecordError
from ..notices import ACTION_MARKER
from ..records import MARKER, iso, lease_summary, live_leases, record_version, records, seconds
from .eligible import EligiblePlanning

WINDOW_SECONDS = 24 * 60 * 60
CLAIMED = {"claiming", "running"}


def later_on_item(history, record):
    """A later lease or reset on the same item means the loop moved on."""
    return any(other["id"] > record["id"] and other["assignment"] == record["assignment"]
               and other["kind"] in {"lease", "reset"} for other in history)


def ask(outcome, lease=None):
    """What a stopped run asks of a person: its outcome's actions and options."""
    source = outcome or lease
    if source is None:
        return None
    return {"agent": source["agent"],
            "outcome": (outcome or {}).get("outcome") or (lease or {}).get("result") or "",
            "summary": (outcome or {}).get("summary") or (lease or {}).get("summary") or "",
            "actions": (outcome or {}).get("actions", []), "options": (outcome or {}).get("options", []),
            "created": source["created"], "url": source["url"]}


class MonitorSource:
    def __init__(self, config, github, coordinator, clock):
        self.config = config
        self.github = github
        self.trust = coordinator.trust
        self.coordinator = coordinator
        self.clock = clock
        self.titles = {}
        self.planning = EligiblePlanning(config, github, coordinator, clock)

    def snapshot(self):
        now = self.clock()
        comments = self.github.repository_comments(lookback_seconds=WINDOW_SECONDS)
        history, invalid = self.coordinator.repository_history(comments)
        renewed = {comment["id"]: comment.get("updated_at") for comment in comments}
        notices = [f"Unreadable coordination records on #{number}" for number in sorted(invalid)]
        newest = max((record_version(comment.get("body")) or 0 for comment in comments), default=0)
        if newest > record_version(MARKER):
            notices.append(f"Some records use format v{newest}, newer than this ub-agents reads: upgrade it.")
        open_items = {item.number: item for item in self.github.observe(details=False)}
        self.titles.update((number, item.title) for number, item in open_items.items())
        leases = [r for r in history if r["kind"] == "lease"]
        labelled = self.labelled(open_items, notices)
        stopped = {entry["item"]["number"] for entry in labelled}
        blocked = [lease for lease in leases if lease.get("result") == "blocked"
                   and lease["assignment"] in open_items and lease["assignment"] not in stopped
                   and not later_on_item(history, lease)]
        eligible = self.planning.snapshot()
        return {
            "repository": self.config.repository,
            "folder": str(self.config.root),
            "local_host": socket.gethostname(),
            "stop_labels": list(self.config.stop_labels),
            "stop_outcomes": self.stop_outcomes(),
            "generated_at": iso(now),
            "notices": notices,
            "needs_human": labelled + [{"item": self.item(lease), "labels": ["blocked"], "labelled": False,
                                        "ask": ask(self.outcome(history, lease), lease), "notice": None}
                                       for lease in blocked],
            "running": [self.run(lease, renewed) for lease in live_leases(history, now)],
            "eligible": eligible,
            "eligible_error": self.planning.error,
            "expired": [self.run(lease, renewed) for lease in leases if lease["state"] in CLAIMED
                        and seconds(lease["expires"]) <= now and not later_on_item(history, lease)],
            "recent": sorted((self.ended(history, lease, renewed) for lease in leases
                              if lease["state"] == "released" and seconds(lease["expires"]) >= now - WINDOW_SECONDS),
                             key=lambda run: run["ended"], reverse=True),
            "rates": self.rates(),
        }

    def stop_outcomes(self):
        """The configured outcomes that hand an item to a person through a stop label."""
        stops = set(self.config.stop_labels)
        return sorted({name for agent in self.config.agents for name, changes in agent.outcomes.items()
                       if stops.intersection(changes.get("add", ()))})

    def labelled(self, open_items, notices):
        """Open items with a stop label, each with the ask of its latest outcome."""
        entries, trusted = [], self.trust.observation()
        for number, item in sorted(open_items.items()):
            labels = sorted(item.labels.intersection(self.config.stop_labels))
            if not labels:
                continue
            comments = self.github.comments(number)
            try:
                history = records(comments, trusted=trusted)
            except RecordError:
                notices.append(f"Unreadable coordination records on #{number}")
                history = []
            outcomes = [r for r in history if r["kind"] == "outcome"]
            released = [r for r in history if r["kind"] == "lease" and r["state"] == "released"]
            notice = next((c for c in reversed(comments) if (c.get("body") or "").startswith(ACTION_MARKER)
                           and trusted(c.get("user"))), None)
            entries.append({"item": self.reference(number, item.kind), "labels": labels, "labelled": True,
                            "ask": ask(outcomes[-1] if outcomes else None, released[-1] if released else None),
                            "notice": notice.get("html_url") if notice else None})
        return entries

    @staticmethod
    def outcome(history, lease):
        return next((r for r in reversed(history) if r["kind"] == "outcome"
                     and r["lease_id"] == lease["id"] and r["agent"] == lease["agent"]), None)

    def reference(self, number, kind):
        path = "pull" if kind == "pr" else "issues"
        return {"number": number, "kind": kind, "title": self.title(number, kind),
                "url": f"https://github.com/{self.config.repository}/{path}/{number}"}

    def title(self, number, kind):
        # A closed item's title is read once per monitor; it rarely changes.
        if number not in self.titles:
            self.titles[number] = self.github.item(number, kind if kind == "pr" else None).title
        return self.titles[number]

    def item(self, lease):
        # The launcher pins a PR's head in its lease; an issue has none.
        return self.reference(lease["assignment"], "pr" if lease.get("assignment_sha") else "issue")

    def run(self, lease, renewed):
        return {"id": lease["id"], "agent": lease["agent"], "item": self.item(lease), "host": lease.get("host") or "",
                "actor": lease["actor"], "runtime": lease["runtime"], "attempt": lease["attempt"],
                "state": lease["state"], "started": lease["created"], "renewed": renewed.get(lease["id"]),
                "expires": lease["expires"], "url": lease["url"]}

    def ended(self, history, lease, renewed):
        outcome = self.outcome(history, lease)
        usage = lease.get("usage") or {}
        return self.run(lease, renewed) | {
            "ended": lease["expires"], "status": lease.get("result") or "released",
            "result": (outcome or {}).get("outcome") or lease.get("result") or "released",
            "summary": lease_summary(history, lease), "tokens": usage.get("tokens"),
            "cost": usage.get("total_cost_usd")}

    def rates(self):
        # GitHub counts REST and GraphQL calls in separate hourly buckets, and
        # every tool using the same account draws from them.
        result = {}
        for resource, headers in getattr(self.github, "resource_quotas", {}).items():
            try:
                result[resource] = {key: int(headers[f"x-ratelimit-{key}"]) for key in ("remaining", "limit", "reset")}
            except (KeyError, TypeError, ValueError):
                continue
        return result | {"counted": getattr(self.github, "quota_requests", 0),
                         "graphql_calls": getattr(self.github, "graphql_calls", 0),
                         "not_modified": getattr(self.github, "not_modified_responses", 0)}
