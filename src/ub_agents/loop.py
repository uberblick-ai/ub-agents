"""One serial observe → claim → execute → observe → release loop."""

import json
import os
import socket
import threading
from time import monotonic
from dataclasses import replace

from .approvals import ApprovalCheck, check_issue, check_pr, resolve_policy, trusted_input
from .config import LEASE_SECONDS, instruction_text, load_config, resolve_config_path
from .coordination import Coordinator, Plan
from .dependencies import Dependencies
from .discovery import Discovery
from .eligibility import AgentMatches, check_start, open_blockers
from .errors import (AgentError, CleanupError, GitHubError, LostOwnership, RecordError,
                     RetryableExecutionError, TransitionPaused, ValidationError)
from .execution import ScratchDirectory, Workspace, command_for, repository_checks, supervise
from .report_command import launcher_report_command
from .github import RATE_LIMIT_FALLBACK_SECONDS, RATE_LIMIT_MAX_SECONDS, closing_issues, links_issue
from .rate_limits import RateLimitReads
from .polling import idle_interval
from .hooks import run_hook
from .records import (attempts, backoff, declared_transition, iso, latest_leases, lease_by_id,
                      lease_summary, resolve_transition, seconds, timestamp, validate_report_action)
from .status import refusal_reason
from .refresh import refresh_checkout, refresh_instructions
from .renewal import LeaseRenewal
from .runtime_updates import RuntimeMaintenance
from .runtime_usage import RuntimeUsage
from .usage_output import UsageOutput
from .trust import LauncherTrust

POLL_RETRY_BASE_SECONDS = 5
POLL_RETRY_MAX_SECONDS = 60
POLL_FAILURE_LIMIT = 6
COMMENT_RECOVERY_SECONDS = 7 * 24 * 60 * 60


class _GracefulStop(Exception):
    pass


class _InvalidReload(AgentError):
    pass


class Loop:
    def __init__(self, config, github, actor, stop_event=None, output=print,
                 config_path=None, interrupt_event=None,
                 default_config=False, observer=None):
        self.observer = observer
        self.updates = None
        self._last_update = None
        self._update_texts = set()
        self._observation_warning = False
        self.config = config
        self.approvals = config.approvals or "on"
        self.github = RateLimitReads(github, self.wait_rate_limit)
        self.coordinator = Coordinator(self.github, actor, queue=config.queue, output=output,
                                       on_claim=self.claimed, launchers=config.launchers,
                                       on_record=lambda record: self._observe("record", record),
                                       on_author=lambda *args: self._observe("coordination_author", *args),
                                       on_action=lambda *args: self._observe("action_needed", *args))
        self._renewal = None
        self.stop_event = stop_event or threading.Event()
        self.interrupt_event = interrupt_event or self.stop_event
        self.config_path = config_path
        self.default_config = default_config
        self.output = output
        self.usage = RuntimeUsage(clock=lambda: self.coordinator.clock(), output=output)
        self.coordinator.runtime_paused = self.usage.paused
        self.discovery = Discovery(self.github)
        self._shown = {}
        self._released_blockers = {}
        self._poll_complete = False
        self._refreshing_checkout = False
        self._launch_number = None
        self._launch_agent = None
        self._launcher_reason = None
        self._maintaining = False
        self.maintenance = RuntimeMaintenance(output=output, stop_event=self.stop_event)
        self.coordinator.runtime_available = self.maintenance.available

    def _observe(self, method, *args):
        if self.observer is not None:
            try:
                getattr(self.observer, method)(*args)
            except _GracefulStop:
                raise
            except Exception as exc:
                if not self._observation_warning:
                    self._observation_warning = True
                    if hasattr(self.observer, "warning"):
                        self.observer.warning(str(exc))
                    else:
                        self.output(f"Cannot publish launcher observations: {exc}")

    def _wait(self, event, delay, reason):
        if self.observer is not None:
            self._observe("activity", "waiting", iso(self.coordinator.clock() + delay), reason)
        if self.updates is None:
            event.wait(delay)
        else:
            remaining = delay
            while remaining > 0:
                interval = min(1, remaining)
                if event.wait(interval):
                    break
                remaining -= interval
                self._poll_updates()
        self._observe("activity", "running assignment" if self.github.lease else "polling")

    def _poll_updates(self):
        banner = self.updates.banner if self.updates is not None else None
        state = getattr(self.observer, 'state', None)
        if banner != self._last_update or (isinstance(state, dict) and state.get('update') != banner):
            self._last_update = banner
            self._observe("update", banner)
        if banner and banner['text'] not in self._update_texts:
            from .updates import release_age
            age = release_age(banner.get('released_at'))
            self.output(banner['text'] + (f'  {age}' if age else ''))
            self._update_texts.add(banner['text'])

    def wait_rate_limit(self, error, lease=None):
        now = self.coordinator.clock()
        reset = error.reset_at if error.reset_at is not None else now + RATE_LIMIT_FALLBACK_SECONDS
        delay = min(RATE_LIMIT_MAX_SECONDS, max(0, reset - now))
        self.output(f"GitHub rate limit reached; waiting until {iso(now + delay)} ({delay / 60:g} min)")
        # SIGTERM wakes discovery, but an owned run keeps draining. Only Ctrl-C
        # and SIGHUP wake the in-run wait.
        event = self.interrupt_event if lease is not None else self.stop_event
        if lease is None:
            self._wait(event, delay, "rate-limit reset")
            if self.interrupt_event.is_set():
                raise KeyboardInterrupt
            if self.stop_event.is_set():
                raise _GracefulStop
            return
        until, remaining = now + delay, delay
        while remaining > 0:
            now = self.coordinator.clock()
            try:
                expiry = self.coordinator.deadline(lease)
            except LostOwnership as exc:
                raise LostOwnership(f"Lease expired or ownership lost while waiting for GitHub rate limit reset: {exc}") from exc
            remaining = min(remaining, until - now)
            if remaining <= 0:
                break
            # Renewal runs independently. Wake at least once a minute so an
            # extended expiry or known ownership loss changes this wait's bound.
            wait = min(60, remaining, expiry - now)
            self._wait(event, wait, "rate-limit reset")
            remaining -= wait
            if self.interrupt_event.is_set():
                raise KeyboardInterrupt
        if self.interrupt_event.is_set():
            raise KeyboardInterrupt
        try:
            self.coordinator.deadline(lease)
        except LostOwnership as exc:
            raise LostOwnership(f"Lease expired or ownership lost while waiting for GitHub rate limit reset: {exc}") from exc

    def stop_gracefully(self):
        self._observe("activity", "stopping")
        if self.interrupt_event.is_set():
            # SIGINT/SIGHUP already requested termination. Leave process-group
            # cleanup and durable release to finish without another exception.
            self.stop_event.set()
            return
        if self._maintaining:
            self._maintenance_graceful_stop = True
        self.stop_event.set()
        if not self._poll_complete and not self._refreshing_checkout and not self._maintaining:
            # Unwind even a slow discovery subprocess. Once a claim write starts,
            # it must finish election, execution/recovery and durable completion.
            raise _GracefulStop

    def _before_claim(self):
        self._poll_updates()
        if self.interrupt_event.is_set():
            raise KeyboardInterrupt
        if self.stop_event.is_set():
            raise _GracefulStop

    def input_check(self, item, github=None, matches=None):
        github = github or self.github
        if self.approvals == "off":
            return trusted_input(github, item)
        if matches is None or matches.configured != self.config.agents:
            matches = AgentMatches.for_item(item, self.config.agents)
        triggers = matches.trigger_labels
        check = (check_issue(github, item.number, triggers) if item.kind == "issue" else
                 check_pr(github, item.number, triggers, self.coordinator.actor,
                          launchers=self.config.launchers))
        if check.snapshot and (check.snapshot["title"], check.snapshot["body"],
                              check.snapshot.get("head")) != (item.title, item.body, item.head):
            return ApprovalCheck(False, "Assignment changed while reading approval input; retry")
        return check

    @staticmethod
    def _rank(plan, priority):
        existing = plan.item.kind == "pr" or plan.state in {"owned", "recover"}
        return (0 if existing else 1,
                0 if existing else plan.milestone_rank,
                priority.labels.index(plan.priority) if plan.priority is not None else len(priority.labels),
                seconds(plan.item.created_at), plan.item.number)

    def plans(self):
        # Status evaluates every row, with fresh inputs even on a reused Loop.
        return sorted(self.iter_plans(cached=False),
                      key=lambda plan: self._rank(plan, self.config.queue.priority))

    def iter_plans(self, cached=True):
        self.approvals, _ = resolve_policy(self.config.approvals, self.github.visibility)
        github = self.discovery if cached else Discovery(self.github)
        lookback = LEASE_SECONDS + COMMENT_RECOVERY_SECONDS
        items, comments = github.observe(lookback)
        self._observe("discovered", items, self.config.agents, True)
        coordinator = Coordinator(github, self.coordinator.actor, clock=self.coordinator.clock,
                                  queue=self.config.queue, output=self.output,
                                  runtime_available=self.maintenance.available,
                                  runtime_paused=self.usage.paused, launchers=self.config.launchers,
                                  role=github.current_role,
                                  on_author=lambda *args: self._observe("coordination_author", *args))
        history_index, invalid, histories = coordinator.repository_history(comments, by_item=True)
        now = coordinator.clock()
        latest = latest_leases(history_index)
        unfinished = {r["assignment"] for r in latest.values()
                      if r["state"] in {"claiming", "running"} or r.get("result") in {"retry", "blocked"}}
        for number in sorted(unfinished | invalid):
            if number not in items:
                item = github.item(number)
                items[item.number] = item
        active_milestone = (self.github.active_milestone()
                            if self.config.queue.milestones == "gate" else None)
        milestones = (self.github.milestone_order()
                      if self.config.queue.milestones == "order" else ())
        milestone_ranks = {number: rank for rank, number in enumerate(milestones)}
        priority = self.config.queue.priority
        candidates = []
        for item in items.values():
            matches = AgentMatches.for_item(item, self.config.agents)
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
            return
        # Priority or milestone inheritance requires the open local graph.
        # Otherwise read only a reached item's links for dependency waits.
        inherit = (self.config.queue.dependencies == "wait" and
                   (priority.labels or self.config.queue.milestones == "order"))
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
        for candidate in sorted(ranked, key=lambda plan: self._rank(plan, priority)):
            item = candidate.item
            github.scope = item.number
            if item.kind == "pr":
                item = github.item(item.number, "pr")
            self._observe("discovered", {item.number: item}, self.config.agents)
            matches = candidate.matches
            if (item.kind, item.state, item.labels) != (candidate.item.kind, candidate.item.state, candidate.item.labels):
                matches = AgentMatches.for_item(item, self.config.agents)
            blockers = ()
            if item.kind == "issue" and item.state == "open" and self.config.queue.dependencies == "wait":
                if dependencies:
                    blockers = dependencies.blockers.get(item.number, ())
                else:
                    blockers = self._open_blockers(item, github)
            plans = self._item_plans(item, now, github, coordinator, matches, active_milestone, blockers)
            for plan in plans:
                observed = replace(plan, priority=candidate.priority, priority_source=candidate.priority_source,
                              priority_from_issue=candidate.priority_from_issue, blockers=blockers,
                              milestone=candidate.milestone, milestone_source=candidate.milestone_source,
                              milestone_rank=candidate.milestone_rank)
                self._observe_plan(observed, github)
                yield observed

    def _open_blockers(self, item, github):
        if (item.kind != "issue" or item.state != "open" or
                self.config.queue.dependencies != "wait" or item.total_blocked_by == 0):
            return ()
        return open_blockers(github, item)

    @staticmethod
    def _gate_plan(plan, start, blockers):
        if (plan.state == "ready" or plan.approval_gate) and not start.allowed:
            plan = replace(plan, state="parked", runtime=None, reason=start.reason,
                           approval_gate=None)
        return replace(plan, blockers=blockers)

    def item_plans(self, number, agent_name=None):
        self.approvals, _ = resolve_policy(self.config.approvals, self.github.visibility)
        # Share this item's history and approval reads, without repository
        # discovery, priority inheritance or milestone ordering.
        github = Discovery(self.github)
        github.scope = number
        try:
            item = github.item(number)
        except GitHubError as exc:
            raise AgentError(f"Cannot read #{number}: {exc}") from exc
        self._observe("discovered", {item.number: item}, self.config.agents)
        agents = tuple(a for a in self.config.agents if agent_name is None or a.name == agent_name)
        coordinator = Coordinator(github, self.coordinator.actor, clock=self.coordinator.clock,
                                  queue=self.config.queue, output=self.output,
                                  runtime_available=self.maintenance.available,
                                  runtime_paused=self.usage.paused, launchers=self.config.launchers,
                                  role=github.current_role,
                                  on_author=lambda *args: self._observe("coordination_author", *args))
        active = (self.github.active_milestone() if item.kind == "issue" and item.state == "open"
                  and self.config.queue.milestones == "gate" else None)
        blockers = self._open_blockers(item, github)
        matches = AgentMatches.for_item(item, self.config.agents)
        plans = self._item_plans(item, coordinator.clock(), github, coordinator, matches, active, blockers, agents)

        def observed_plans():
            for plan in plans:
                self._observe_plan(plan, github)
                yield plan
        return item, observed_plans()

    def _observe_plan(self, plan, github):
        closing = sorted(closing_issues(plan.item, self.config.repository)) if plan.item.kind == "pr" else []
        filing = github.observed_item(closing[0]) if closing else None
        authors = {login: role in {"write", "maintain", "admin"} and
                   (self.config.launchers is None or login in {a.casefold() for a in self.config.launchers})
                   for login, role in github.pass_roles.items()}
        self._observe("plan", plan, filing, github.observed_comments(plan.item.number), authors)

    def _item_plans(self, item, now, github, coordinator, matches, active_milestone, blockers, agents=None):
        agents = self.config.agents if agents is None else agents
        starts = {a.name: check_start(item, a, matches, self.config.stop_labels,
                                     self.config.queue, active_milestone, blockers) for a in agents}
        for plan in self._ungated_item_plans(item, now, github, coordinator, matches, starts, agents):
            yield self._gate_plan(replace(plan, matches=matches), starts[plan.agent.name], blockers)

    def _ungated_item_plans(self, item, now, github, coordinator, matches, starts, agents):
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
            if isinstance(exc, GitHubError) and (exc.rate_limited or exc.retryable):
                raise
            # Unreadable item input cannot authorize a claim. A transient
            # coordination failure with readable approval input still fails the poll.
            approval = self.input_check(item, github, matches) if matched else None
            if approval is None or approval.allowed:
                raise
            yield from (Plan(item, a, None, "parked", approval.reason, 1, history_read=False) for a in matched)
            return
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
            if (agent in matched or pending or parked or (record and record["state"] in {"claiming", "running"}
                                                and seconds(record["expires"]) > now)):
                plan = coordinator.plan(item, agent, self.config.stop_labels, history,
                                        start=starts[agent.name], matches=matches)
                if plan.state in {"ready", "recover", "blocked", "backoff"} and coordinator.actor is not None:
                    if reason := coordinator.trust.reason(coordinator.actor):
                        yield replace(plan, state="blocked", runtime=None, reason=reason, history=tuple(history))
                        continue
                if plan.state in {"ready", "blocked", "backoff"} and agent in matched:
                    if approval is None:
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

    def tick(self):
        self._observe("begin_pass")
        self.maintain_runtimes()
        config = self.config
        present = set()
        for plan in self.iter_plans():
            present.add((plan.item.number, plan.agent.name))
            self._before_claim()
            if plan.state in {"ready", "recover"}:
                self._shown.pop((plan.item.number, plan.agent.name), None)
            if plan.state == "ready":
                if self.execute(plan):
                    return True
                if self.config != config:
                    # Remaining discovery plans belong to the old configuration.
                    return False
            elif plan.state == "recover":
                if self.recover(plan):
                    return True
            else:
                if plan.approval_gate:
                    self.coordinator.notices.advisory(f"approval parking on #{plan.item.number}",
                                                      lambda: self.park_approval(plan))
                key, value = (plan.item.number, plan.agent.name), (plan.state, plan.reason)
                released = self._released_blockers.pop(key, None)
                announced = plan.state == "blocked" and released is not None and released in plan.reason
                if not announced and (plan.state not in {"blocked", "parked", "waiting"} or self._shown.get(key) != value):
                    self.output(f"#{plan.item.number} {plan.agent.name}: {plan.state} — {plan.reason}")
                self._shown[key] = value
        self._shown = {key: value for key, value in self._shown.items() if key in present}
        self._released_blockers = {key: value for key, value in self._released_blockers.items() if key in present}
        # Even an empty or already-owned queue must explain why this account
        # cannot claim. Reuse the discovery pass's permission observation.
        if self.coordinator.actor is not None:
            trusted = LauncherTrust(self.discovery, self.config.launchers, self.discovery.current_role,
                                    lambda *args: self._observe("coordination_author", *args))
            reason = trusted.reason(self.coordinator.actor)
            if reason and reason != self._launcher_reason:
                self.output(f"{reason}; claiming no work")
            self._launcher_reason = reason
        self._observe("complete_pass")
        return False

    def tick_item(self, number, agent_name=None):
        self._observe("begin_pass")
        self.maintain_runtimes()
        item, plans = self.item_plans(number, agent_name)
        shown = False
        for plan in plans:
            self._before_claim()
            if plan.state == "ready" and self.execute(plan):
                return True
            if plan.state == "recover" and self.recover(plan):
                return True
            if plan.state in {"ready", "recover"}:
                # A refresh or claim race may have changed eligibility. Explain
                # the fresh row rather than printing the stale ready verdict.
                item, current = self.item_plans(number, plan.agent.name)
                plan = next(current, None)
                if plan is None:
                    continue
            if plan.approval_gate:
                self.coordinator.notices.advisory(f"approval parking on #{number}",
                                                  lambda: self.park_approval(plan))
            state, reason = refusal_reason(plan, self.coordinator.clock(), socket.gethostname())
            self.output(f"#{number} {plan.agent.name}: {state} — {reason}")
            shown = True
        if not shown:
            if item.state != "open":
                reason = f"{item.kind} is {item.state}"
            else:
                agents = [a for a in self.config.agents if agent_name is None or a.name == agent_name]
                applicable = [a for a in agents if a.kind in {"either", item.kind}]
                if applicable:
                    labels = "; ".join(f"{a.name}: {', '.join(a.triggers)}" for a in applicable)
                    reason = f"No trigger matches; add a trigger label ({labels})"
                else:
                    reason = f"No evaluated agent applies to this {item.kind}"
                if not agents:
                    reason = f"Agent {agent_name} is no longer configured"
            self.output(f"#{number}: {reason}")
        self._observe("complete_pass")
        return False

    def park_approval(self, plan):
        # Recheck authority before advisory writes; stale discovery cannot park
        # closed, stopped, already owned or newly approved work.
        current = self.github.item(plan.item.number, plan.item.kind)
        matches = AgentMatches.for_item(current, self.config.agents)
        start = check_start(current, plan.agent, matches, self.config.stop_labels, self.config.queue)
        # Preserve the durable history read even when a stop label appeared.
        if not start.allowed and start.reason != start.stop_reason:
            return
        if self.coordinator.plan(current, plan.agent, self.config.stop_labels,
                                 start=start, matches=matches).state != "ready":
            return
        active = (self.github.active_milestone() if current.kind == "issue"
                  and self.config.queue.milestones == "gate" else None)
        if not check_start(current, plan.agent, matches, self.config.stop_labels,
                           self.config.queue, active).allowed:
            return
        blockers = (open_blockers(self.github, current) if current.kind == "issue"
                    and self.config.queue.dependencies == "wait" else ())
        if not check_start(current, plan.agent, matches, self.config.stop_labels,
                           self.config.queue, active, blockers).allowed:
            return
        approval = self.input_check(current, matches=matches)
        if approval.gate_key != plan.approval_gate.gate_key:
            return
        if self.github.item(current.number, current.kind) != current:
            return
        self.coordinator.notices.approval(current.number, approval, self.config.stop_labels,
                                          sorted(matches.trigger_labels))

    def execute(self, plan):
        self._renewal = LeaseRenewal(self.coordinator, self.github.github)
        try:
            return self._execute(plan)
        finally:
            self._renewal.close()
            self._renewal = None
            self._observe("clear_assignment")
            self.github.lease = None

    def claimed(self, lease):
        self.github.claimed(lease)
        if self._renewal is not None:
            self._renewal.claimed(lease)

    def maintain_runtimes(self):
        self._before_claim()
        self._maintenance_graceful_stop = False
        self._maintaining = True
        try:
            # Test/embedding callers can replace the loop's stop event.
            self.maintenance.stop_event = self.stop_event
            self.maintenance.boundary(self.config)
        finally:
            self._maintaining = False
        if self._maintenance_graceful_stop and self.interrupt_event is self.stop_event:
            # Embedding callers can use one shared event. Remember the explicit
            # graceful request after letting the updater finish its cancellation.
            raise _GracefulStop
        self._before_claim()

    def _execute(self, plan):
        # Between supervised runs and cleanup hooks, before any assignment writes.
        # Refresh errors belong to the operator, not to an assignment attempt.
        self._refreshing_checkout = True
        try:
            options = {"on_fetch": self.updates.fetched} if self.updates is not None else {}
            if self.config_path is None:
                instructions = refresh_instructions(self.config, plan.agent, self.github, **options)
            else:
                refresh_checkout(self.config, self.github, **options)
        finally:
            # An asynchronous exception in subprocess.run kills its child. Let
            # the checkout refresh finish so a fast-forward is never torn down
            # by SIGTERM, then stop before doing any assignment writes.
            self._refreshing_checkout = False
        self._before_claim()
        if self.config_path is not None:
            try:
                path = (resolve_config_path(root=self.config_path.parent) if self.default_config
                        else self.config_path)
                config = load_config(path)
                texts = {a.name: instruction_text(config.root, a.instructions, f"{a.name} instructions")
                         for a in config.agents}
            except AgentError as exc:
                # Configuration errors stop with the same diagnostic as check,
                # without entering discovery retries or writing an assignment.
                raise _InvalidReload(str(exc)) from exc
            if config.repository != self.github.repository:
                for _, error in repository_checks(config):
                    if error is not None:
                        raise error
                self.github.repository = config.repository
            self.config = config
            self._observe("configure", config, self.coordinator.actor, path)
            self.coordinator.queue = config.queue
            self.coordinator.trust.launchers = (None if config.launchers is None else
                                               {login.casefold() for login in config.launchers})
            plans = (self.iter_plans() if self._launch_number is None else
                     self.item_plans(self._launch_number, self._launch_agent)[1])
            plan = next((p for p in plans if p.item.number == plan.item.number
                         and p.agent.name == plan.agent.name and p.state == "ready"), None)
            if plan is None:
                return False
            instructions = texts[plan.agent.name]
        self.maintain_runtimes()
        if plan.runtime is None:
            return self._claim_execute(plan, instructions)
        # The snapshot runtime may have become guarded/broken since discovery,
        # or a reload may have enabled maintenance. Reapply runtime eligibility.
        try:
            runtime = self.coordinator.choose_runtime(plan.item, plan.agent, list(plan.history))
        except GitHubError:
            raise
        except AgentError as exc:
            self.output(f"#{plan.item.number} {plan.agent.name}: waiting — {exc}")
            return False
        with self.maintenance.reserve(runtime.cli) as reservation:
            if reservation is None:
                self.output(f"#{plan.item.number} {plan.agent.name}: waiting — "
                            f"{runtime.cli} runtime became unavailable before the claim; retry next poll")
                return False
            self._before_claim()
            return self._claim_execute(replace(plan, runtime=runtime), instructions, reservation)

    def _claim_execute(self, plan, instructions, reservation=None):
        def authorize(current, matches):
            # Discovery may have reused an approval verdict's inputs. Recheck
            # them before the first write as well as after the claim election.
            approval = self.input_check(current, matches=matches)
            if not approval.allowed:
                # Fresh authority can reveal a permission change that does not
                # advance item timestamps. Let the next poll plan its gate.
                self.discovery.invalidate(current.number)
                self.output(f"#{current.number} {plan.agent.name}: parked — {approval.reason}")
            return approval.allowed

        self._observe("assignment", plan)
        lease = self.coordinator.claim(plan, self.config.stop_labels,
                                       before_write=self._end_poll, authorize=authorize)
        if lease is None:
            self.discovery.invalidate(plan.item.number)
            return False
        try:
            fresh = self.github.item(plan.item.number, plan.item.kind)
            approval = self.input_check(fresh)
        except LostOwnership:
            raise
        except AgentError:
            approval = ApprovalCheck(False, "Assignment approval history is unreadable; retry or ask a maintainer")
        if not approval.allowed:
            self.discovery.invalidate(plan.item.number)
            self.coordinator.assert_owned(lease)
            self.coordinator.update(lease, state="withdrawn", started=False, attempt_effect="unchanged",
                                    expires=iso(self.coordinator.clock()), summary=approval.reason)
            self.output(f"#{plan.item.number} {plan.agent.name}: parked — {approval.reason}")
            return True
        run_dir = self.config.root.resolve() / ".ub-agents" / "runs" / lease["run"]
        scratch = ScratchDirectory(run_dir)
        preserve_scratch = False
        workspace = Workspace(self.config, plan.agent, plan.item, lease, self.github)
        self.output(f"#{plan.item.number} {plan.agent.name}: claimed {lease['run']} ({lease['runtime']})")
        self.output(f"Logs: {run_dir}")

        def diagnostic(event, **details):
            try:
                with (run_dir / "events.jsonl").open("a") as stream:
                    stream.write(json.dumps({"time": iso(timestamp()), "event": event, **details}) + "\n")
            except OSError as exc:
                self.output(f"Cannot write diagnostic {event}: {exc}")

        def record_uncertainty(exc):
            diagnostic("cleanup-unconfirmed", error=str(exc))
            cause = exc
            while cause is not None:
                if isinstance(cause, LostOwnership):
                    return  # Even a transient ownership loss forbids further writes.
                cause = cause.__context__
            try:
                self.coordinator.assert_owned(lease)
                self.coordinator.update(lease, cleanup="unconfirmed", summary=str(exc))
            except (AgentError, KeyboardInterrupt) as failure:
                if isinstance(failure, KeyboardInterrupt):
                    self.interrupt_event.set()
                    self.stop_event.set()
                diagnostic("cleanup-verdict-unrecorded", error=str(failure))

        hook_attempted = False

        def cleanup_workspace(record=True):
            nonlocal preserve_scratch
            def before_remove():
                nonlocal hook_attempted
                if hook_attempted:
                    return False
                hook_attempted = True
                try:
                    reported = self.coordinator.outcome(lease)
                except (AgentError, KeyboardInterrupt) as exc:
                    if isinstance(exc, KeyboardInterrupt):
                        self.interrupt_event.set()
                        self.stop_event.set()
                    reported = outcome
                failure = run_hook(self.config, lease, workspace.private, reported,
                                   expires=(lambda: self.coordinator.deadline(lease)) if record else None)
                if failure:
                    if record:
                        try:
                            self.coordinator.assert_owned(lease)
                            self.coordinator.update(lease, cleanup_hook_error=failure)
                        except (AgentError, KeyboardInterrupt) as exc:
                            if isinstance(exc, KeyboardInterrupt):
                                self.interrupt_event.set()
                                self.stop_event.set()
                            diagnostic("cleanup-hook-verdict-unrecorded", error=str(exc))
                    return False
                return True
            try:
                workspace.cleanup(before_remove)
            except CleanupError as exc:
                preserve_scratch = True
                if record:
                    record_uncertainty(exc)
                else:
                    diagnostic("cleanup-unconfirmed", error=str(exc))
                raise

        def process_started(pid):
            if reservation is not None:
                reservation.started()
            self.coordinator.assert_owned(lease)
            self.coordinator.update(lease, process_group=pid)
            self._observe("process", "running", "Supervision recorded a live process")
            self.coordinator.assert_owned(lease)

        interrupted = False
        completing = False
        setup = True
        effect = "failure"
        result, summary = "retry", "Assignment ended without a validated outcome"
        outcome = None
        usage_output = None
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            self.coordinator.assert_owned(lease)
            self.coordinator.update(lease, state="running", started=True,
                                    host=socket.gethostname(), log_dir=str(run_dir))
            scratch.prepare()
            cwd = workspace.prepare()
            self.coordinator.update(lease, branch=lease.get("branch"))
            current = self.github.item(plan.item.number, plan.item.kind)
            if current.labels.intersection(self.config.stop_labels):
                raise TransitionPaused("Stop label added before execution")
            if current.state != "open" or not current.labels.intersection(plan.agent.triggers):
                raise AgentError("State or trigger changed before execution")
            if current.head != plan.item.head:
                raise AgentError("Candidate changed before execution")
            self.coordinator.assert_owned(lease)
            report_command = launcher_report_command()
            context = {"repository": self.config.repository, "assignment": plan.item.number,
                       "kind": plan.item.kind, "title": approval.snapshot["title"],
                       "body": approval.snapshot["body"], "comments": approval.snapshot["comments"],
                       "feedback": self.coordinator.feedback(plan.item, plan.agent.name),
                       "candidate_sha": plan.item.head, "run": lease["run"],
                       "scratch": str(scratch.path),
                       "report_command": report_command,
                       "agent": plan.agent.name, "branch": lease.get("branch"),
                       "earlier_branches": self.earlier_branches(plan.item, plan.agent, lease["run"])}
            if plan.item.kind == "pr":
                context |= {name: approval.snapshot[name] for name in ("reviews", "review_comments")}
            context_path = run_dir / "context.json"
            context_path.write_text(json.dumps(context, indent=2))
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("UB_AGENTS_")}
            env.update({"UB_AGENTS_REPOSITORY": self.config.repository,
                        "UB_AGENTS_ASSIGNMENT": str(plan.item.number),
                        "UB_AGENTS_RUN": lease["run"], "UB_AGENTS_LEASE_ID": str(lease["id"]),
                        "UB_AGENTS_CONTEXT": str(context_path),
                        "UB_AGENTS_REPORT": report_command,
                        "UB_AGENTS_SCRATCH": str(scratch.path), "TMPDIR": str(scratch.path),
                        "UB_AGENTS_CANDIDATE_SHA": context["candidate_sha"] or "",
                        "UB_AGENTS_BRANCH": lease.get("branch") or ""})
            diagnostic("started", cwd=str(cwd))
            setup = False
            command = command_for(plan.agent, plan.runtime, scratch.path, report_command)
            if reservation is not None:
                command[0] = reservation.executable
            if plan.runtime:
                usage_output = UsageOutput(plan.runtime.cli, run_dir, self.usage, env)
            def observe_output(final=False):
                self._poll_updates()
                if usage_output:
                    usage_output.poll(final=final)
            code = supervise(command, cwd, env, run_dir,
                             plan.agent.timeout_seconds, self.interrupt_event,
                             self.prompt_for(plan, lease, context, instructions) if plan.runtime else None,
                             expires=lambda: self.coordinator.deadline(lease), process_started=process_started,
                             observe_output=observe_output if usage_output or self.updates else None,
                             **({"pass_fds": (reservation.descriptor,)} if reservation is not None else {}))
            if usage_output:
                usage_output.poll(final=True)
            self._observe("process", "exited", "Supervision confirmed execution has ended")
            diagnostic("execution-exited", code=code)
            # No acceptance or release until all attributable execution has ended.
            cleanup_workspace()
            completing = True
            self.coordinator.assert_owned(lease)
            outcome = self.coordinator.outcome(lease)
            if outcome is None:
                result = "retry"
                summary = f"Execution exited {code} without an explicit GitHub outcome; inspect process.log"
            elif outcome["status"] != "success":
                self.validate_failure_report(lease, outcome, lease)
                result, summary = outcome["status"], outcome["summary"]
                effect = "unchanged" if result == "blocked" else "failure"
            else:
                try:
                    self.finalize(lease, plan, outcome, "completion")
                except ValidationError:
                    result = "blocked"
                    raise
                result, summary = "success", outcome["summary"]
                effect = "reset"
        except CleanupError as exc:
            self._observe("process", "unknown", str(exc))
            preserve_scratch = True
            record_uncertainty(exc)
            raise
        except LostOwnership as exc:
            diagnostic("ownership-lost", error=str(exc))
            cleanup_workspace(record=False)
            # Do not report, release, or accept after losing ownership.
            raise
        except KeyboardInterrupt:
            if completing:
                diagnostic("transition-interrupted", outcome=outcome["id"] if outcome else None)
                cleanup_workspace()
                # A request can be interrupted after GitHub applied its write,
                # before our in-memory outcome reflects it. Even a read before
                # transition start must preserve the report for expiry recovery.
                raise
            interrupted = True
            effect = "unchanged"
            summary = "Launcher interrupted; attributable execution terminated"
            cleanup_workspace()
        except TransitionPaused as exc:
            result, summary, effect = "blocked", str(exc), "unchanged"
            cleanup_workspace()
        except Exception as exc:
            result = ("retry" if isinstance(exc, RetryableExecutionError)
                      or (setup and isinstance(exc, (AgentError, OSError))
                          and not isinstance(exc, (RecordError, ValidationError))) else "blocked")
            summary = str(exc)
            cleanup_workspace()
        finally:
            if not preserve_scratch:
                try:
                    scratch.cleanup()
                except AgentError as exc:
                    # Scratch removal cannot invalidate confirmed process termination
                    # or prevent release of a completed run's lease.
                    diagnostic("scratch-removal-failed", path=str(scratch.path), error=str(exc))
                    self.output(f"Scratch removal failed: {exc}")
        if usage_output and usage_output.reached and effect != "reset" and not interrupted:
            result, effect = "retry", "unchanged"
            summary = usage_output.summary
        delay = backoff(plan.agent, lease["attempt"]) if result == "retry" and effect == "failure" else 0
        if result != "success":
            # Persist the supervised verdict before a report/release can crash.
            # It supersedes early agent reports without relinquishing live ownership.
            self.coordinator.assert_owned(lease)
            self.coordinator.update(lease, result=result, summary=summary, attempt_effect=effect,
                                    retry_after=iso(self.coordinator.clock() + delay) if delay else None)
        # Framework failures are themselves explicit durable outcomes. If GitHub is
        # unreadable, this fails closed and the last lease expires without a lie.
        if outcome is None:
            # A report can precede a timeout/interruption. Keep that report
            # unaccepted and persist the supervisor's actual verdict on the lease.
            outcome = self.coordinator.outcome(lease)
        if outcome is None:
            self.coordinator.update(lease, unreported=True)
            self.coordinator.report(lease, result, summary, agent_report=False)
        self.coordinator.release(lease, result, summary, delay, attempt_effect=effect,
                                 max_attempts=plan.agent.max_attempts)
        diagnostic("released", result=result, summary=summary)
        self.output(f"#{plan.item.number} {plan.agent.name}: {result} — {summary}")
        if result == "blocked":
            self._released_blockers[(plan.item.number, plan.agent.name)] = summary
        if interrupted or self.interrupt_event.is_set():
            raise KeyboardInterrupt
        return True

    def earlier_branches(self, item, agent, run):
        """Branches recorded by this issue's other runs of the agent; an open PR there is the draft to continue."""
        if item.kind != "issue":
            return []
        return sorted({r["branch"] for r in self.coordinator.history(item.number) if r["kind"] == "lease"
                       and r["agent"] == agent.name and r.get("branch") and r["run"] != run})

    def prompt_for(self, plan, lease, context, instructions):
        earlier = context["earlier_branches"]
        report_command = context["report_command"]
        continuation = (f"Earlier runs of this issue recorded branches {json.dumps(earlier)}; check each with "
                        "gh pr list --state open --head BRANCH and continue an open draft PR there instead "
                        "of opening another. " if earlier else "")
        matches = plan.matches
        if matches is None or matches.configured != self.config.agents:
            matches = AgentMatches.for_item(plan.item, self.config.agents)
        workflow_labels = set(self.config.stop_labels).union(matches.workflow_triggers)
        for configured in self.config.agents:
            for changes in configured.outcomes.values():
                workflow_labels.update(changes["add"])
                workflow_labels.update(changes["remove"])
        return (f"You are the project-configured agent {plan.agent.name}.\n"
                "This run is a single, non-interactive session that is never resumed. "
                "Ending your turn ends the run. Run checks in the foreground or wait for every "
                "background job to finish before ending your turn. "
                f"End the run with {report_command} report.\n"
                f"Use {report_command} report wherever project instructions say `ub-agents report`. "
                "This command runs the launcher's own installation; write it literally in shell commands.\n"
                "Put temporary files in UB_AGENTS_SCRATCH, the run's private scratch directory, "
                "not directly under /tmp. TMPDIR points to the same directory. Its absolute "
                "path is the context's scratch value; use that path directly rather than "
                "expanding the variable in a shell command.\n"
                f"Assignment context:\n{json.dumps(context, indent=2)}\n\n"
                f"Project instructions:\n{instructions}\n\n"
                "The assignment context is the issue or PR input: use its title, body, comments, "
                "reviews, review comments and feedback. Feedback contains trusted accepted outcome "
                "summaries from other agents; address it when revising the work. "
                "Other comments on GitHub are not assignment input. "
                "This rule takes precedence over project instructions to read GitHub comments. "
                "Read shared repository guidance, current code/diff, and candidate-specific checks on GitHub. "
                "Use a fresh session; do not consume implementation reasoning transcripts. "
                "Apply only project-authorized handoffs and permissions. "
                f"Declared outcomes: {json.dumps(lease['outcomes'], sort_keys=True)}. "
                f"Report one with {report_command} report --outcome NAME --summary 'what happened' "
                "[--handoff PR_NUMBER] [--action 'one thing a person must do']. "
                "Stop reports (--status blocked or outcomes adding a configured stop label) require --action: "
                "one non-empty line of at most 300 characters. For a decision, name the choices, recommendation "
                "and who can answer. Do not change workflow labels "
                f"(trigger, transition or stop labels): {json.dumps(sorted(workflow_labels))}. "
                "Use --status retry|blocked for failures; those change no labels. "
                f"{continuation}"
                "Issue-to-PR handoffs must link the issue in the PR body. "
                "For candidate acceptance, results and checks must name the assigned SHA.\n")

    def finalize(self, lease, plan, outcome, what):
        """Validate a success report, apply its transition and accept it.

        A rejected report is recorded on the outcome and raised as ValidationError.
        A GitHub read or write failure becomes LostOwnership, so expiry recovery
        finishes the job instead of this run guessing."""
        try:
            if outcome.get("transition", {}).get("started"):
                # Start is durable proof that success validation passed. A later
                # head or link edit cannot strand already-applied changes.
                self.validate_report(outcome)
            else:
                self.validate_success(plan, outcome)
            self.apply_transition(lease, outcome)
        except ValidationError as exc:
            self.coordinator.update_outcome(lease, outcome, rejected=str(exc),
                                            attempt_effect="unchanged" if isinstance(exc, TransitionPaused) else "failure")
            raise
        except (AgentError, OSError) as exc:
            raise LostOwnership(f"Cannot observe {what}; leave expiry recovery: {exc}") from exc
        try:
            self.coordinator.accept(lease, outcome)
        except AgentError as exc:
            raise LostOwnership(f"Cannot finalize {what}; leave expiry recovery: {exc}") from exc

    def validate_success(self, plan, outcome):
        current = self.github.item(plan.item.number, plan.item.kind)
        self.validate_report(outcome)
        destination = (self.github.item(outcome["handoff"], "pr")
                       if outcome.get("handoff") else current)
        if outcome.get("handoff") and destination.draft:
            raise ValidationError(f"Handoff PR #{destination.number} is still a draft")
        if outcome["candidate_sha"] != destination.head:
            raise ValidationError("Outcome candidate SHA does not match the observed PR head")
        if plan.agent.different_from and current.head != plan.item.head:
            raise ValidationError("Independent result is stale: the assigned candidate moved")
        if outcome.get("handoff") and plan.item.kind == "issue":
            if not links_issue(destination, self.config.repository, plan.item.number):
                raise ValidationError("Implementation PR body does not link its original issue")
            # A cheap tripwire for the duplicate the instructions exist to prevent.
            for branch in self.earlier_branches(plan.item, plan.agent, outcome["run"]):
                others = [pr.number for pr in self.github.prs_for_branch(branch) if pr.number != destination.number]
                if others:
                    raise ValidationError(f"Another open PR #{others[0]} from an earlier run of this issue is on "
                                          f"{branch}; continue or close it before handing off")

    def validate_report(self, outcome):
        if outcome.get("rejected"):
            error = TransitionPaused if outcome.get("attempt_effect") == "unchanged" else ValidationError
            raise error(outcome["rejected"])
        history = self.coordinator.history(outcome["assignment"])
        source = lease_by_id(history, outcome["lease_id"])
        declarations = source.get("outcomes", {}) if source else {}
        transition = outcome.get("transition")
        name = outcome.get("outcome")
        if name not in declarations or transition is None:
            raise ValidationError("Success report must name a declared outcome")
        declaration = declared_transition(source, name)
        transition = resolve_transition(transition, declaration)
        if {k: v for k, v in transition.items() if k != "started"} != declaration:
            raise ValidationError("Reported transition does not match the running agent's declaration")
        try:
            validate_report_action(source, outcome)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return transition

    def validate_failure_report(self, lease, outcome, source):
        try:
            if outcome.get("rejected"):
                raise ValueError(outcome["rejected"])
            validate_report_action(source, outcome)
        except ValueError as exc:
            self.coordinator.update_outcome(lease, outcome, rejected=str(exc), attempt_effect="failure")
            raise ValidationError(str(exc)) from exc

    def apply_transition(self, lease, outcome):
        transition = self.validate_report(outcome)
        self.coordinator.assert_owned(lease)
        assignment = self.github.item(outcome["assignment"])
        target = outcome.get("handoff") or outcome["assignment"]
        destination = self.github.item(target)
        if not transition["started"]:
            stops = set(transition["stop_labels"]).union(self.config.stop_labels)
            if assignment.labels.union(destination.labels).intersection(stops):
                raise TransitionPaused("Transition paused: a stop label is on the assignment or handoff PR; "
                                      "set workflow labels manually or use ub-agents retry after unpausing")
            if not assignment.labels.intersection(transition["triggers"]):
                raise TransitionPaused("Transition blocked: assignment trigger disappeared before label changes")
            # Persist intent before the first mutation. Recovery must not mistake
            # our own trigger removal or human-gate addition for external pausing.
            self.coordinator.update_outcome(lease, outcome, transition=outcome["transition"] | {"started": True})
        # Publish pending provenance before the next role's trigger can appear.
        # Recovery refreshes this same comment before replaying the transition.
        self.coordinator.copy_handoff(lease, outcome)
        # Consume assignment labels before publishing the next role's trigger.
        for label in sorted(set(transition["remove"])):
            self.coordinator.assert_owned(lease)
            if label in self.github.item(outcome["assignment"]).labels:
                self.github.remove_label(outcome["assignment"], label)
        self.coordinator.assert_owned(lease)
        missing = sorted(set(transition["add"]).difference(self.github.item(target).labels))
        if missing:
            self.github.add_labels(target, missing)
        if not outcome.get("transition_complete"):
            self.coordinator.update_outcome(lease, outcome, transition_complete=True)

    def recover(self, plan):
        self._renewal = LeaseRenewal(self.coordinator, self.github.github)
        try:
            return self._recover(plan)
        finally:
            self._renewal.close()
            self._renewal = None
            self._observe("clear_assignment")
            self.github.lease = None

    def _recover(self, plan):
        history = self.coordinator.history(plan.item.number)
        outcome = self.coordinator.pending_completion(history, plan.agent.name, self.coordinator.clock())
        if outcome is None:
            return False
        self._observe("assignment", plan)
        recovery = self.coordinator.claim(plan, self.config.stop_labels, recovery=True,
                                          before_write=self._end_poll)
        if recovery is None:
            return False
        # The claim reread may have observed a supervisor's newer outcome flags.
        source = lease_by_id(self.coordinator.history(plan.item.number), recovery["recovered_lease_id"])
        outcome = self.coordinator.outcome(source) if source else None
        if outcome is None:
            raise LostOwnership("Recovered outcome disappeared after claiming")
        result, summary = outcome["status"], outcome["summary"]
        effect = "unchanged" if result == "blocked" else "failure"
        if result != "success":
            try:
                self.validate_failure_report(recovery, outcome, source)
            except ValidationError as exc:
                result, summary, effect = "blocked", f"Recorded outcome cannot be recovered: {exc}", "failure"
        if result == "success":
            # Validate against the originally assigned candidate, not today's head.
            original = replace(plan, item=replace(plan.item, head=outcome["assignment_sha"]))
            try:
                self.finalize(recovery, original, outcome, "recovered completion")
                effect = "reset"
            except ValidationError as exc:
                result, summary = "blocked", f"Recorded outcome cannot be recovered: {exc}"
                effect = "unchanged" if isinstance(exc, TransitionPaused) else "failure"
            except (LostOwnership, CleanupError):
                raise
            except Exception as exc:
                # An unclassified recovery failure must not be retried forever.
                result, summary, effect = "blocked", f"Unclassified recovery failure: {exc}", "failure"
                self.coordinator.assert_owned(recovery)
                self.coordinator.update(recovery, result=result, summary=summary, attempt_effect=effect)
        verdict = {"result": result, "attempt_effect": effect, "summary": f"Recovered {outcome['run']}: {summary}"}
        # Count the source using this verdict even when its lease is unexpired.
        # Persist the classification and backoff together before report/release,
        # just as the execution supervisor does, so a crash loses neither.
        history = [r | verdict if r["id"] == recovery["id"] else r
                   for r in self.coordinator.history(plan.item.number)]
        failures = len(attempts(history, plan.agent.name, self.coordinator.clock()))
        delay = backoff(plan.agent, max(1, failures)) if result == "retry" else 0
        self.coordinator.assert_owned(recovery)
        self.coordinator.update(recovery, **verdict,
                                retry_after=iso(self.coordinator.clock() + delay) if delay else None)
        self.coordinator.report(recovery, result, f"Recovered {outcome['run']}: {summary}", agent_report=False)
        self.coordinator.release(recovery, result, f"Recovered {outcome['run']}: {summary}", delay,
                                 attempt_effect=effect, parking_outcome=outcome)
        self.output(f"#{plan.item.number} {plan.agent.name}: recovered durable outcome; no execution started")
        return True

    def _end_poll(self):
        self._before_claim()
        # Even an unsuccessful lease write ends discovery. Never retry a tick that
        # may already have written a claim or withdrawn from a claim election.
        self._poll_complete = True

    def launch(self, once=False, number=None, agent_name=None):
        self._launch_number = number
        self._launch_agent = agent_name
        try:
            return self._launch(once or number is not None)
        finally:
            try:
                self._poll_updates()
                self._observe("close")
            finally:
                self._launch_number = None
                self._launch_agent = None

    def _launch(self, once):
        self.usage.reset()
        self.github.discovery = not once
        if self.coordinator.actor is None:
            self.coordinator.actor = self.github.actor()
            self.coordinator.notices.actor = self.coordinator.actor
        self._observe("configure", self.config, self.coordinator.actor, self.config_path)
        failures = 0
        idle_state = None
        while not self.stop_event.is_set():
            self.github.lease = None
            self._poll_complete = False
            started = monotonic()
            requests_before = self.github.quota_requests
            try:
                worked = (self.tick() if self._launch_number is None else
                          self.tick_item(self._launch_number, self._launch_agent))
            except _GracefulStop:
                return
            except (_InvalidReload, CleanupError, LostOwnership, RecordError):
                raise
            except AgentError as exc:
                if self.interrupt_event.is_set():
                    raise KeyboardInterrupt from None
                if once or self._poll_complete:
                    raise
                failures += 1
                delay = min(POLL_RETRY_MAX_SECONDS, POLL_RETRY_BASE_SECONDS * 2 ** (failures - 1))
                retryable = isinstance(exc, GitHubError) and exc.retryable
                detail = str(exc)
                # Keep each diagnostic on one line, even when gh prints several.
                detail = " ".join(detail.split())
                if not retryable or failures >= POLL_FAILURE_LIMIT:
                    reason = (f"retries exhausted after {failures} consecutive failed polls"
                              if retryable else "failure is not retryable; retries not exhausted")
                    raise AgentError(f"{detail}; {reason}. Fix the cause and restart ub-agents launch.") from exc
                self.output(f"Skipped GitHub poll: {detail}; retrying in {delay:g}s")
                delay = self.usage.bound_wait(delay)
                self._wait(self.stop_event, delay, "poll retry or runtime pause")
                continue
            failures = 0
            if self.interrupt_event.is_set():
                raise KeyboardInterrupt
            if self.stop_event.is_set():
                return
            if once:
                return (0 if worked else 1) if self._launch_number is not None else None
            elapsed = monotonic() - started
            interval = self.config.poll_seconds
            if worked:
                idle_state = None
            else:
                requests = self.github.quota_requests - requests_before
                interval, low = idle_interval(requests, interval,
                                              self.github.resource_quotas,
                                              self.coordinator.clock(), elapsed)
                interval = elapsed + self.usage.bound_wait(max(0, interval - elapsed))
                if idle_state != low:
                    self.output(f"No eligible work; next poll in {max(0, interval - elapsed) / 60:g} min "
                                f"({requests} requests last poll)")
                idle_state = low
            delay = self.usage.bound_wait(max(0, interval - elapsed))
            if delay:
                self._wait(self.stop_event, delay, "next poll or runtime pause")
        if self.interrupt_event.is_set():
            raise KeyboardInterrupt
