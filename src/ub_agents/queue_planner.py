"""Plan reached queue items without advancing later agents or approval reads."""

from dataclasses import replace

from . import approvals as input_approvals
from .approvals import pr_author_exclusions, resolve_policy
from .coordination import Coordinator, Plan
from .discovery import ApprovalInput, Discovery
from .eligibility import AgentMatches, check_start, open_blockers
from .errors import AgentError, GitHubError, RecordError
from .records import attempts, latest_leases, lease_summary, seconds


class QueuePlanner:
    def __init__(self, config, github, coordinator, approvals, *, health, usage, maintenance,
                 output, observe, before_claim, reconcile_blocked_notices, targeted, claiming,
                 notice_agent):
        self._config = config
        self.github = github
        self.coordinator = coordinator
        self.approvals = approvals
        # The observation worker replaces its runtime helpers with the launcher's
        # shared objects after construction. Keep those and reloadable config live.
        self.health = health
        self.usage = usage
        self.maintenance = maintenance
        self.output = output
        self.observe = observe
        self.before_claim = before_claim
        self.reconcile_blocked_notices = reconcile_blocked_notices
        # Pass scope and claim-write state can change after construction or a yield.
        self.targeted = targeted
        self.claiming = claiming
        self.notice_agent = notice_agent

    @property
    def config(self):
        return self._config()

    def input_check(self, item, github=None, matches=None):
        github = github or self.github
        if matches is None or matches.configured != self.config.agents:
            matches = AgentMatches.for_item(item, self.config.agents)
        triggers = matches.trigger_labels
        def check():
            return input_approvals.filter_input(github, item, self.approvals, triggers,
                                               actor=self.coordinator.actor, launchers=self.config.launchers,
                                               sub_issue_labels=self.config.sub_issue_labels,
                                               trusted_bots=self.config.trusted_bots,
                                               trusted_pr_authors=self.config.trusted_pr_authors,
                                               raise_read_errors=self.targeted() or self.claiming())
        approval = check()
        if (isinstance(github, Discovery) and item.number in github.bulk_inputs
                and approval.gate == "input" and isinstance(approval.snapshot.get("title"), dict)):
            # Title gates embed the complete REST rename, including its ID and
            # actor. Reread both inputs so persisted keys and fresh parking match.
            github.fallback_approval_input(item.number)
            approval = check()
        return approval

    def open_blockers(self, item, github):
        if (item.kind != "issue" or item.state != "open" or
                self.config.queue.dependencies != "wait" or item.total_blocked_by == 0):
            return ()
        return open_blockers(github, item)

    def resolve_approvals(self):
        self.approvals, _ = resolve_policy(self.config.approvals, self.github.visibility)
        self.coordinator.trust.trusted_pr_authors = set(pr_author_exclusions(
            self.config.trusted_pr_authors, self.approvals, self.github))

    def planning_coordinator(self, github, observe=None):
        observe = observe or self.observe
        return Coordinator(github, self.coordinator.actor, clock=self.coordinator.clock,
                           queue=self.config.queue, output=self.output,
                           runtime_available=self.maintenance().available,
                           runtime_paused=self.usage().paused, launchers=self.config.launchers,
                           role=github.current_role, trusted_bots=self.config.trusted_bots,
                           trusted_pr_authors=self.coordinator.trust.trusted_pr_authors,
                           on_author=lambda *args: observe("coordination_author", *args))

    @staticmethod
    def _gate_plan(plan, start, blockers):
        if (plan.state == "ready" or plan.approval_gate) and not start.allowed:
            plan = replace(plan, state="parked", runtime=None, reason=start.reason,
                           approval_gate=None)
        return replace(plan, blockers=blockers)

    def item_plans(self, item, now, github, coordinator, matches, blockers, agents=None,  # noqa: C901
                   *, reconcile_notices=False, checked=None, health_notices=False, approval_errors=None,
                   defer_input=False):
        agents = self.config.agents if agents is None else agents
        starts = {a.name: check_start(item, a, matches, self.config.stop_labels,
                                     self.config.queue, blockers) for a in agents}
        matched = tuple(a for a in matches.matched if a in agents)
        approval = None
        # The same comments supply coordination history and approval input.
        # Claims and parking bypass this discovery reader for fresh authority.
        try:
            history = coordinator.history(item.number)
        except RecordError as exc:
            yield from (Plan(item, a, None, "blocked", str(exc), 1, history_read=False)
                        for a in matched or agents)
            return
        except AgentError as exc:
            # A shared failed comment read must still skip a transiently failed
            # poll, rather than turn its unreadable approval into a parked row.
            if isinstance(exc, GitHubError) and (self.targeted() or exc.rate_limited or exc.retryable):
                raise
            if approval_errors is not None and matched:
                # The eager fallback distinguishes unreadable approval input
                # from a coordination-only failure. Defer that verdict too;
                # these provisional rows are never observed or claimed as ready.
                approval_errors[item.number] = exc
                yield from (Plan(item, a, None, "ready" if starts[a.name].allowed else "parked",
                                 "" if starts[a.name].allowed else starts[a.name].reason,
                                 1, history_read=False, matches=matches) for a in matched)
                return
            # Unreadable item input cannot authorize a claim. A transient
            # coordination failure with readable approval input still fails the poll.
            if matched and defer_input:
                yield ApprovalInput(item)
            approval = self.input_check(item, github, matches) if matched else None
            if approval is None or approval.allowed:
                raise
            yield from (Plan(item, a, None, "parked", approval.reason, 1, history_read=False) for a in matched)
            return
        reconciled = set()
        for plan in self._ungated_item_plans(item, now, github, coordinator, matches, starts, agents,
                                              history, matched, approval, check_input=approval_errors is None,
                                              defer_input=defer_input):
            if isinstance(plan, ApprovalInput):
                yield plan
                continue
            plan = self._gate_plan(replace(plan, matches=matches), starts[plan.agent.name], blockers)
            plan = self.health_plan(plan, checked, announce=health_notices)
            if reconcile_notices:
                self.before_claim()
                reconciled.add(plan.agent.name)
                if not plan.health_wait:
                    self.reconcile_blocked_notices([r for r in history if r["agent"] == plan.agent.name], coordinator)
            yield plan
        if reconcile_notices:
            self.before_claim()
            # Closed items can have a missing advisory without an assignment
            # row. Keep that recovery, without evaluating later agents before
            # a reached ready agent's claim or posting for health-waiting rows.
            self.reconcile_blocked_notices([r for r in history if r["agent"] not in reconciled
                                           and (self.notice_agent() is None or r["agent"] == self.notice_agent())], coordinator)

    def health_plan(self, plan, checked=None, *, announce=False):
        if plan.state == "ready":
            reason = self.health().check(self.config.root, plan.agent, checked, announce=announce)
            if reason is not None:
                return replace(plan, state="waiting", runtime=None, reason=reason, health_wait=True)
        return plan

    def _ungated_item_plans(self, item, now, github, coordinator, matches, starts, agents,  # noqa: C901, PLR0912
                             history, matched, approval, *, check_input=True, defer_input=False):
        latest = latest_leases(history)
        for agent in agents:
            record = latest.get((item.number, agent.name))
            parked = (item.state == "open" and item.labels.intersection(self.config.stop_labels)
                      and any(r["kind"] == "outcome" and r["agent"] == agent.name
                              and r.get("accepted") and r.get("transition_complete")
                              and item.number == (r.get("handoff") or r["assignment"])
                              and item.labels.intersection(r.get("transition", {}).get("add", ()))
                                  .intersection(self.config.stop_labels) for r in history))
            try:
                pending = coordinator.pending_completion(history, agent.name, now)
            except RecordError as exc:
                yield Plan(item, agent, None, "blocked", str(exc),
                           len(attempts(history, agent.name, now)) + 1, history=tuple(history))
                continue
            if (item.state != "open" and record and record["state"] == "released"
                    and record.get("result") in {"retry", "blocked"}
                    and record.get("cleanup") != "unconfirmed" and not pending):
                continue
            if (agent in matched or pending or parked or (record and record["state"] in {"claiming", "running"}
                                                and seconds(record["expires"]) > now)):
                plan = coordinator.plan(item, agent, self.config.stop_labels, history,
                                        start=starts[agent.name], matches=matches)
                if plan.state in {"ready", "recover", "blocked", "backoff"} and coordinator.actor is not None:
                    if reason := coordinator.trust.reason(coordinator.actor):
                        yield replace(plan, state="blocked", runtime=None, reason=reason, history=tuple(history))
                        continue
                if check_input and plan.state in {"ready", "blocked", "backoff"} and agent in matched:
                    if approval is None:
                        if defer_input:
                            yield ApprovalInput(item)
                        approval = self.input_check(item, github, matches)
                    if not approval.allowed:
                        gate = approval if plan.state == "ready" and approval.gate else None
                        plan = replace(plan, state="parked", runtime=None, reason=approval.reason,
                                       approval_gate=gate)
                yield replace(plan, history=tuple(history))
            elif record and (record["state"] in {"claiming", "running"}
                             or record.get("result") in {"retry", "blocked"}):
                if record.get("cleanup") == "unconfirmed":
                    reason = f"Previous cleanup was unconfirmed: {lease_summary(history, record)}"
                elif record["state"] == "released":
                    reason = f"Last run {record['result']}: {lease_summary(history, record)}"
                else:
                    reason = "Expired run has no outcome or matching trigger"
                yield Plan(item, agent, None, "blocked",
                    f"{reason}; inspect GitHub and restore a trigger before retrying",
                    len(attempts(history, agent.name, now)) + 1, history=tuple(history))
