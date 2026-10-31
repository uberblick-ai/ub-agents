"""Traverse the queue and plan reached items without advancing later reads."""

from collections import Counter
from dataclasses import replace

from . import approvals as input_approvals
from . import queue_order
from .attention import latest_notice
from .approvals import pr_author_exclusions, resolve_policy
from .config import LEASE_SECONDS
from .coordination import Coordinator, Plan
from .dependencies import Dependencies
from .discovery import ApprovalInput, Discovery
from .eligibility import AgentMatches, check_start, open_blockers
from .errors import AgentError, GitHubError, RecordError
from .github import APPROVAL_BATCH_SIZE, closing_issues
from .records import attempts, display_provenance, latest_leases, lease_summary, records, seconds

COMMENT_RECOVERY_SECONDS = 7 * 24 * 60 * 60


class QueuePlanner:
    def __init__(self, config, github, coordinator, approvals, *, health, usage, maintenance,
                 output, observe, before_claim, reconcile_blocked_notices, targeted, claiming,
                 notice_agent, discovery, pass_stats, launch_agent, launch_number):
        self._config = config
        self._github = github
        self._coordinator = coordinator
        self.approvals = approvals
        # The observation worker replaces its runtime helpers with the launcher's
        # shared objects after construction. Tests can replace the coordinator or
        # GitHub client too. Keep those and reloadable config live.
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
        # Discovery and launch selection stay live across passes and yields too.
        self.discovery = discovery
        self.pass_stats = pass_stats
        self.launch_agent = launch_agent
        self.launch_number = launch_number
        self._queue_waits = {}
        self._has_trigger = None
        self.milestone_skips = ()
        self.milestone_wait = None

    @property
    def config(self):
        return self._config()

    @property
    def github(self):
        return self._github()

    @property
    def coordinator(self):
        return self._coordinator()

    def plans(self, iter_plans=None):
        # Status evaluates every row with fresh inputs. The Loop delegate passes
        # its iterator so patches there continue to intercept status planning.
        plans = (iter_plans or self.iter_plans)(cached=False)
        if self.config.queue.milestones == "gate":
            return list(plans)
        return sorted(plans,
                      key=lambda plan: queue_order.rank(plan, self.config.queue.priority))

    def queue_agents(self):
        return tuple(a for a in self.config.agents
                     if self.launch_agent() is None or a.name == self.launch_agent())

    def iter_plans(self, cached=True, *, reconcile_notices=False, health_notices=False, claiming=False):  # noqa: C901, PLR0912, PLR0915
        agents = self.queue_agents()
        selected = {a.name for a in agents}
        self._queue_waits = {}
        on_wait = lambda value: setattr(self, "milestone_wait", value)
        on_skips = lambda value: setattr(self, "milestone_skips", value)
        queue_order.milestone_diagnostics(on_wait=on_wait, on_skips=on_skips)
        if not agents:
            self._has_trigger = False
            self.observe("queue_idle", self.idle_message())
            return
        checked = {}
        self.resolve_approvals()
        github = self.discovery() if cached else Discovery(self.github)
        lookback = max(LEASE_SECONDS, self.config.lease_seconds) + COMMENT_RECOVERY_SECONDS
        items, comments = github.observe(lookback)
        triggers = {label for agent in agents for label in agent.triggers}
        self._has_trigger = any(item.state == "open" and item.labels.intersection(triggers)
                                for item in items.values())
        self.observe("discovered", items, agents, True)
        coordinator = self.planning_coordinator(github)
        history_index, invalid, histories = coordinator.repository_history(comments, by_item=True)
        now = coordinator.clock()
        latest = latest_leases(history_index)
        unfinished = {r["assignment"] for r in latest.values()
                      if (self.launch_agent() is None or r["agent"] in selected) and
                      (r["state"] in {"claiming", "running"} or r.get("result") in {"retry", "blocked"})}
        if self.launch_agent() is not None:
            # An invalid history can still block a selected matching item, but
            # must not pull another agent's closed item into this queue.
            invalid.intersection_update(items)
        for number in sorted(unfinished | invalid):
            if number not in items:
                item = github.item(number)
                items[item.number] = item
        strict = self.config.queue.milestones == "strict-gate"
        milestone_titles = self.github.open_milestones(include_empty=True) if strict else {}
        milestones = (tuple(milestone_titles) if strict else self.github.milestone_order()
                      if self.config.queue.milestones in {"order", "gate"} else ())
        milestone_ranks = {number: rank for rank, number in enumerate(milestones)}
        priority = self.config.queue.priority
        candidates = []
        for item in items.values():
            matches = AgentMatches.for_item(item, self.config.agents)
            selected_matches = tuple(a for a in matches.matched if a.name in selected)
            # A stop outcome removes the trigger; item history identifies which
            # agent parked it before that agent's plans and notices are evaluated.
            if (self.launch_agent() is not None and not selected_matches and item.number not in unfinished
                    and not (item.state == "open" and item.labels.intersection(self.config.stop_labels))):
                continue
            if (not matches.matched and item.number not in (unfinished | invalid)
                    and not item.labels.intersection(self.config.stop_labels)):
                continue
            records = histories.get(item.number, [])
            # The repository index determines rank only. Item history is checked
            # when reached, and fresh authority is checked again before writes.
            try:
                ongoing = any(r["state"] in {"claiming", "running"}
                              and (seconds(r["expires"]) > now or
                                   coordinator.pending_completion(records, r["agent"], now))
                              for r in latest_leases(records).values())
            except RecordError:
                ongoing = False  # Item evaluation will expose the conflicting outcomes.
            candidates.append(Plan(item, None, None, "owned" if ongoing else "ready", "", 1,
                                   priority=priority.effective(item.labels), matches=matches))
        if not candidates:
            if self.config.queue.milestones == "gate":
                queue_order.milestone_diagnostics(milestones=milestones, on_wait=on_wait, on_skips=on_skips)
            elif strict:
                yield from queue_order.strict_milestone_plans((), milestone_titles, on_wait=on_wait)
            if self.launch_agent() is not None:
                self.observe("queue_idle", self.idle_message())
            return
        # Priority or milestone inheritance requires the open local graph.
        # Otherwise read only a reached item's links for dependency waits.
        inherit = (self.config.queue.dependencies == "wait" and
                   (priority.labels or self.config.queue.milestones in {"order", "gate", "strict-gate"}))
        if inherit:
            github.prepare_dependencies(items.values())
        dependencies = Dependencies(github, items.values(), priority, milestones) if inherit else None
        issue_priorities = {i.number: priority.effective(i.labels) for i in items.values()
                            if i.kind == "issue" and i.state == "open"}
        if dependencies:
            issue_priorities.update({n: label for n, (label, _) in dependencies.priorities.items()})
        ranked = []
        for plan in candidates:
            label, source, from_issue = plan.priority, None, None
            milestone, milestone_source = plan.item.milestone, None
            if dependencies and plan.item.kind == "issue":
                label, source = dependencies.priorities.get(plan.item.number, (label, None))
                milestone, milestone_source = dependencies.milestones.get(plan.item.number, (milestone, None))
            elif plan.item.kind == "pr":
                for number in sorted(closing_issues(plan.item, self.config.repository)):
                    inherited = issue_priorities.get(number)
                    if inherited is not None and (label is None or
                            priority.labels.index(inherited) < priority.labels.index(label)):
                        label, from_issue = inherited, number
            ranked.append(replace(plan, priority=label, priority_source=source,
                                  priority_from_issue=from_issue, milestone=milestone,
                                  milestone_source=milestone_source,
                                  milestone_rank=milestone_ranks.get(milestone, len(milestones))))
        milestone_plans = [] if self.config.queue.milestones in {"gate", "strict-gate"} else None
        lazy_approval = claiming and milestone_plans is not None
        approval_errors = {} if lazy_approval else None
        batch_input = (not lazy_approval and self.approvals == "on"
                       and callable(getattr(github.github, "approval_inputs", None)))
        def evaluate(candidate):
            item = candidate.item
            if self.pass_stats() is not None:
                self.pass_stats().candidates.add(item.number)
            github.scope = item.number
            if item.kind == "pr":
                item = github.item(item.number, "pr")
            self.observe("discovered", {item.number: item}, agents)
            matches = candidate.matches
            if (item.kind, item.state, item.labels) != (candidate.item.kind, candidate.item.state, candidate.item.labels):
                matches = AgentMatches.for_item(item, self.config.agents)
            blockers = ()
            if item.kind == "issue" and item.state == "open" and self.config.queue.dependencies == "wait":
                if dependencies:
                    blockers = dependencies.blockers.get(item.number, ())
                else:
                    blockers = self.open_blockers(item, github)
            plans = self.item_plans(item, now, github, coordinator, matches, blockers,
                                    agents,
                                    reconcile_notices=reconcile_notices, checked=checked,
                                    health_notices=health_notices or reconcile_notices,
                                    approval_errors=approval_errors, defer_input=batch_input)
            for plan in plans:
                if isinstance(plan, ApprovalInput):
                    yield plan
                    continue
                observed = replace(plan, priority=candidate.priority, priority_source=candidate.priority_source,
                              priority_from_issue=candidate.priority_from_issue, blockers=blockers,
                              milestone=candidate.milestone, milestone_source=candidate.milestone_source,
                              milestone_rank=candidate.milestone_rank)
                yield observed

        candidates = sorted(ranked, key=lambda plan: queue_order.rank(plan, priority))
        if batch_input and not claiming:
            plans = github.approval_plans(candidates, evaluate, APPROVAL_BATCH_SIZE)
        else:
            plans = (plan for candidate in candidates for plan in evaluate(candidate))
        for observed in plans:
            if isinstance(observed, ApprovalInput):
                # Claiming can stop at recovery before a later agent on the
                # same item needs input. Preserve that generator boundary.
                github.prepare_approval_inputs([observed.item])
                continue
            if milestone_plans is None:
                yield self.observe_plan(observed, github)
            else:
                milestone_plans.append(observed)
        if milestone_plans is not None:
            approvals = {}
            def approve(plan):
                if plan.state != "ready":
                    return plan
                github.scope = plan.item.number
                if plan.item.number not in approvals:
                    if self.approvals == "on":
                        github.prepare_approval_inputs([plan.item])
                    approvals[plan.item.number] = self.input_check(plan.item, github, plan.matches)
                approval = approvals[plan.item.number]
                if approval.allowed and plan.item.number in approval_errors:
                    raise approval_errors[plan.item.number]
                if not approval.allowed:
                    return replace(plan, state="parked", runtime=None, reason=approval.reason,
                                   approval_gate=approval if plan.history_read and approval.gate else None)
                return plan

            if strict:
                ordered = queue_order.strict_milestone_plans(milestone_plans, milestone_titles, on_wait=on_wait,
                                                            approve=approve if lazy_approval else None)
            else:
                if not lazy_approval:
                    queue_order.milestone_diagnostics(milestone_plans, milestones, on_wait=on_wait, on_skips=on_skips)
                ordered = queue_order.milestone_plans(milestone_plans, milestones, priority,
                                                      on_wait=on_wait, on_skips=on_skips,
                                                      approve=approve if lazy_approval else None)
            for observed in ordered:
                yield self.observe_plan(observed, github)
        if self.launch_agent() is not None:
            self.observe("queue_idle", self.idle_message())

    def target_plans(self, number, agent_name=None, *, reconcile_notices=False, health_notices=False):
        if self.pass_stats() is not None:
            self.pass_stats().candidates.add(number)
        self.resolve_approvals()
        # Share this item's history and approval reads, without repository
        # discovery, priority inheritance or milestone selection. An explicit
        # target still passes every other start gate.
        github = Discovery(self.github)
        github.scope = number
        try:
            item = github.item(number)
        except GitHubError as exc:
            raise AgentError(f"Cannot read #{number}: {exc}") from exc
        self.observe("discovered", {item.number: item}, self.config.agents)
        agents = tuple(a for a in self.config.agents if agent_name is None or a.name == agent_name)
        coordinator = self.planning_coordinator(github)
        queue_order.milestone_diagnostics(on_wait=lambda value: setattr(self, "milestone_wait", value),
                                          on_skips=lambda value: setattr(self, "milestone_skips", value))
        blockers = self.open_blockers(item, github)
        matches = AgentMatches.for_item(item, self.config.agents)
        plans = self.item_plans(item, coordinator.clock(), github, coordinator, matches, blockers, agents,
                                reconcile_notices=reconcile_notices, checked={},
                                health_notices=health_notices or reconcile_notices)

        def observed_plans():
            for plan in plans:
                yield self.observe_plan(plan, github)
        return item, observed_plans()

    def observe_plan(self, plan, github, observe=None):
        if self.launch_agent() is not None and self.launch_number() is None and observe is None:
            self._queue_waits[plan.item.number] = (plan.state, plan.reason)
        closing = sorted(closing_issues(plan.item, self.config.repository)) if plan.item.kind == "pr" else []
        filing = github.observed_item(closing[0]) if closing else None
        authors = {login: role in {"write", "maintain", "admin"} and
                   (self.config.launchers is None or login in {a.casefold() for a in self.config.launchers})
                   for login, role in github.pass_roles.items()}
        comments = github.observed_comments(plan.item.number)
        plan = replace(plan, attention_notice=latest_notice(plan.history, comments, authors))
        histories = {plan.item.number: plan.history}
        for record in plan.history:
            if record["kind"] == "outcome" and record["assignment"] not in histories:
                seen = github.observed_comments(record["assignment"]) or ()
                histories[record["assignment"]] = records([comment for comment in seen
                    if authors.get((comment.get("user") or {}).get("login", "").casefold())])
        displayed = replace(plan, history=tuple(display_provenance(record, histories[record["assignment"]])
                                               for record in plan.history))
        (observe or self.observe)("plan", displayed, filing, comments, authors)
        return plan

    def idle_message(self):
        if self.launch_agent() is not None and self.launch_number() is None:
            agents = self.queue_agents()
            if not agents:
                return f"Agent {self.launch_agent()} is no longer configured; claiming no work"
            if self._has_trigger is False and not self._queue_waits:
                return (f"Agent {self.launch_agent()}: no open issue or PR has a trigger label "
                        f"({', '.join(agents[0].triggers)}); add one to start")
            counts = Counter(state for state, _ in self._queue_waits.values())
            waits = '; '.join(f"{count} {state}" for state, count in counts.items())
            return f"Agent {self.launch_agent()}: no eligible work" + (f"; waiting items: {waits}" if waits else '')
        if self._has_trigger is False:
            labels = dict.fromkeys(label for agent in self.config.agents for label in agent.triggers)
            return (f"No open issue or PR has a trigger label ({', '.join(labels)}); "
                    "add one to start")
        return "No eligible work"

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
