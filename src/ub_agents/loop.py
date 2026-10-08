"""One serial observe → claim → execute → observe → release loop."""

import json
import os
import socket
import threading
from contextlib import contextmanager, nullcontext
from time import monotonic
from dataclasses import replace

from . import approvals as input_approvals
from .agent_health import AgentHealth
from .approvals import ApprovalCheck, resolve_policy
from .config import LEASE_SECONDS, instruction_text, load_config, resolve_config_path
from .checkout_setup import discard_unused_baseline, preserve_baseline, run_setup
from .coordination import Coordinator, Plan
from .dependencies import Dependencies
from .denials import collect_denials
from .discovery import Discovery
from .eligibility import AgentMatches, check_start, open_blockers
from .errors import (AgentError, CleanupError, GitHubError, LostOwnership, RecordError,
                     RetryableExecutionError, TransitionPaused, ValidationError)
from .execution import ScratchDirectory, Workspace, command_for, repository_checks, supervise
from .report_command import launcher_report_command
from .launcher_code import descriptors as code_descriptors
from .github import RATE_LIMIT_FALLBACK_SECONDS, RATE_LIMIT_MAX_SECONDS, closing_issues, links_issue
from .rate_limits import RateLimitReads
from .polling import idle_interval, poll_delay
from .poll_now import PollNow
from .prompts import CONTINUATION_PROMPT, RETROSPECTIVE_PROMPT, RUN_PROMPT
from .hooks import run_hook
from .notices import ACTION_MARKER
from .records import (attempts, backoff, declared_transition, iso, latest_leases, lease_by_id,
                      lease_summary, resolve_transition, seconds, timestamp, validate_report_action)
from .status import refusal_reason
from .refresh import refresh_checkout, refresh_instructions
from .renewal import LeaseRenewal
from .runtime_updates import RuntimeMaintenance
from .runtime_usage import RuntimeUsage
from .usage_output import UsageOutput
from .trust import LauncherTrust
from .run_planning import ObservationReads, RunPlanning
from .run_config import run_config, run_directory

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
        self._observer_lock = threading.RLock()
        self._continuous = False
        self._run_planning = None
        self._planning_rate_until = None
        self._planning_workers = []
        self.poll_now = None
        self.updates = None
        self._last_update = None
        self._update_texts = set()
        self._observation_warning = False
        self.config = config
        self.approvals = config.approvals or "on"
        self.github = RateLimitReads(github, self.wait_rate_limit)
        self.coordinator = Coordinator(self.github, actor, queue=config.queue, output=output,
                                       on_claim=self.claimed, launchers=config.launchers, trusted_bots=config.trusted_bots,
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
        self.health = AgentHealth(output=self._health_notice)
        self.coordinator.runtime_paused = self.usage.paused
        self.discovery = Discovery(self.github)
        self._shown = {}
        self._released_blockers = {}
        self._poll_complete = False
        self._finalizing = False
        self._refreshing_checkout = False
        self._launch_number = None
        self._launch_agent = None
        self._launcher_reason = None
        self._has_trigger = None
        self._active_milestone = None
        self._maintaining = False
        self._github_waiting = False
        self._github_reservation = None
        self.maintenance = RuntimeMaintenance(output=output, stop_event=self.stop_event)
        self.coordinator.runtime_available = self.maintenance.available

    def _observe(self, method, *args):
        with self._observer_lock:
            self._publish_observation(method, *args)

    def _health_notice(self, line):
        self.output(line)
        self._observe("health_notice", line)

    def _publish_observation(self, method, *args):
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

    def enable_poll_now(self):
        self.poll_now = PollNow(
            lambda cooldown, limited, waiting: self._observe("poll_now", cooldown, limited, waiting),
            lambda: self.coordinator.clock(), clock=lambda: monotonic())

    def request_poll(self):
        if self.poll_now is not None:
            self.poll_now.request()

    def _wait(self, event, delay, reason):
        if self.observer is not None:
            self._observe("activity", "waiting", iso(self.coordinator.clock() + delay), reason)
        if self.poll_now is not None and reason == "next poll or runtime pause":
            self.poll_now.wait(event, delay, self._poll_updates if self.updates is not None else None)
        elif self.updates is None:
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
        until, remaining = now + delay, delay
        # Publish the actual reset once, and reject forced planning polls even
        # between the ownership checks that split an owned run's wait.
        with self.poll_now.rate_limit(until) if self.poll_now is not None else nullcontext():
            if lease is None:
                self._wait(event, delay, "rate-limit reset")
                if self.interrupt_event.is_set():
                    raise KeyboardInterrupt
                if self.stop_event.is_set():
                    raise _GracefulStop
                return
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
        if matches is None or matches.configured != self.config.agents:
            matches = AgentMatches.for_item(item, self.config.agents)
        triggers = matches.trigger_labels
        return input_approvals.filter_input(github, item, self.approvals, triggers,
                                           actor=self.coordinator.actor, launchers=self.config.launchers,
                                           trusted_bots=self.config.trusted_bots)

    @staticmethod
    def _rank(plan, priority):
        existing = plan.item.kind == "pr" or plan.state in {"owned", "recover"}
        return (priority.labels.index(plan.priority) if plan.priority is not None else len(priority.labels),
                0 if existing else 1,
                0 if existing else plan.milestone_rank,
                seconds(plan.item.created_at), plan.item.number)

    def plans(self):
        # Status evaluates every row, with fresh inputs even on a reused Loop.
        return sorted(self.iter_plans(cached=False),
                      key=lambda plan: self._rank(plan, self.config.queue.priority))

    def iter_plans(self, cached=True, *, reconcile_notices=False, health_notices=False):
        checked = {}
        self.approvals, _ = resolve_policy(self.config.approvals, self.github.visibility)
        github = self.discovery if cached else Discovery(self.github)
        lookback = LEASE_SECONDS + COMMENT_RECOVERY_SECONDS
        items, comments = github.observe(lookback)
        triggers = {label for agent in self.config.agents for label in agent.triggers}
        self._has_trigger = any(item.state == "open" and item.labels.intersection(triggers)
                                for item in items.values())
        self._observe("discovered", items, self.config.agents, True)
        coordinator = self._planning_coordinator(github)
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
        self._active_milestone = active_milestone
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
            plans = self._item_plans(item, now, github, coordinator, matches, active_milestone, blockers,
                                     reconcile_notices=reconcile_notices, checked=checked,
                                     health_notices=health_notices or reconcile_notices)
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

    def item_plans(self, number, agent_name=None, *, reconcile_notices=False, health_notices=False):
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
        coordinator = self._planning_coordinator(github)
        active = (self.github.active_milestone() if item.kind == "issue" and item.state == "open"
                  and self.config.queue.milestones == "gate" else None)
        self._active_milestone = active
        blockers = self._open_blockers(item, github)
        matches = AgentMatches.for_item(item, self.config.agents)
        plans = self._item_plans(item, coordinator.clock(), github, coordinator, matches, active, blockers, agents,
                                 reconcile_notices=reconcile_notices, checked={},
                                 health_notices=health_notices or reconcile_notices)

        def observed_plans():
            for plan in plans:
                self._observe_plan(plan, github)
                yield plan
        return item, observed_plans()

    def _planning_coordinator(self, github, observe=None):
        observe = observe or self._observe
        return Coordinator(github, self.coordinator.actor, clock=self.coordinator.clock,
                           queue=self.config.queue, output=self.output,
                           runtime_available=self.maintenance.available,
                           runtime_paused=self.usage.paused, launchers=self.config.launchers,
                           role=github.current_role, trusted_bots=self.config.trusted_bots,
                           on_author=lambda *args: observe("coordination_author", *args))

    def _observe_plan(self, plan, github, observe=None):
        closing = sorted(closing_issues(plan.item, self.config.repository)) if plan.item.kind == "pr" else []
        filing = github.observed_item(closing[0]) if closing else None
        authors = {login: role in {"write", "maintain", "admin"} and
                   (self.config.launchers is None or login in {a.casefold() for a in self.config.launchers})
                   for login, role in github.pass_roles.items()}
        (observe or self._observe)("plan", plan, filing, github.observed_comments(plan.item.number), authors)

    def _replan_finished_item(self, plan):
        # A cancelled worker cannot publish an older pass over this item's update.
        self._stop_planning()
        if self.observer is None:
            return
        events = []
        def observe(method, *args):
            events.append((method, args))
        # Bypass in-run rate-limit retries and forbid writes. Failure here must
        # neither change the settled verdict nor defer the next claiming pass.
        github = Discovery(ObservationReads(self.github.github, self.interrupt_event))
        github.scope = plan.item.number
        try:
            item = github.item(plan.item.number, plan.item.kind)
            matches = AgentMatches.for_item(item, self.config.agents)
            coordinator = self._planning_coordinator(github, observe)
            blockers = self._open_blockers(item, github)
            priority = self.config.queue.priority.effective(item.labels)
            if plan.priority is not None and (plan.priority_source or plan.priority_from_issue):
                labels = self.config.queue.priority.labels
                if priority is None or labels.index(plan.priority) < labels.index(priority):
                    priority = plan.priority  # Retain already-observed inheritance without a graph read.
            for refreshed in self._item_plans(item, coordinator.clock(), github, coordinator,
                                              matches, self._active_milestone, blockers, checked={}):
                refreshed = replace(refreshed, priority=priority)
                self._observe_plan(refreshed, github, observe)
        except Exception:
            return  # Discard incomplete observations; the normal pass retries.
        self._observe("replanned_item", item.number, events)

    def _item_plans(self, item, now, github, coordinator, matches, active_milestone, blockers, agents=None,
                    *, reconcile_notices=False, checked=None, health_notices=False):
        agents = self.config.agents if agents is None else agents
        starts = {a.name: check_start(item, a, matches, self.config.stop_labels,
                                     self.config.queue, active_milestone, blockers) for a in agents}
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
        reconciled = set()
        for plan in self._ungated_item_plans(item, now, github, coordinator, matches, starts, agents,
                                             history, matched, approval):
            plan = self._gate_plan(replace(plan, matches=matches), starts[plan.agent.name], blockers)
            plan = self._health_plan(plan, checked, announce=health_notices)
            if reconcile_notices:
                self._before_claim()
                reconciled.add(plan.agent.name)
                if not plan.health_wait:
                    self.reconcile_blocked_notices([r for r in history if r["agent"] == plan.agent.name], coordinator)
            yield plan
        if reconcile_notices:
            self._before_claim()
            # Closed items can have a missing advisory without an assignment
            # row. Keep that recovery, without evaluating later agents before
            # a reached ready agent's claim or posting for health-waiting rows.
            self.reconcile_blocked_notices([r for r in history if r["agent"] not in reconciled], coordinator)

    def _health_plan(self, plan, checked=None, *, announce=False):
        if plan.state == "ready":
            reason = self.health.check(self.config.root, plan.agent, checked, announce=announce)
            if reason is not None:
                return replace(plan, state="waiting", runtime=None, reason=reason, health_wait=True)
        return plan

    def _ungated_item_plans(self, item, now, github, coordinator, matches, starts, agents,
                            history, matched, approval):
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
        with self.github_pass() as ready:
            if not ready:
                self._observe("complete_pass")
                return False
            self._withdraw_lost_claim()
            return self._tick()

    def _withdraw_lost_claim(self):
        number = self.coordinator.withdraw_lost_claim()
        if number is not None:
            self.discovery.invalidate(number)

    def _tick(self):
        config = self.config
        present = set()
        for plan in self.iter_plans(reconcile_notices=True):
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
                if not plan.health_wait and not announced and self._shown.get(key) != value:
                    self.output(f"#{plan.item.number} {plan.agent.name}: {plan.state} — {plan.reason}")
                self._shown[key] = value
        self._shown = {key: value for key, value in self._shown.items() if key in present}
        self._released_blockers = {key: value for key, value in self._released_blockers.items() if key in present}
        # Even an empty or already-owned queue must explain why this account
        # cannot claim. Reuse the discovery pass's permission observation.
        if self.coordinator.actor is not None:
            trusted = LauncherTrust(self.discovery, self.config.launchers, self.discovery.current_role,
                                    lambda *args: self._observe("coordination_author", *args),
                                    trusted_bots=self.config.trusted_bots)
            reason = trusted.reason(self.coordinator.actor)
            if reason and reason != self._launcher_reason:
                self.output(f"{reason}; claiming no work")
            self._launcher_reason = reason
        self._observe("complete_pass")
        return False

    def tick_item(self, number, agent_name=None):
        self._observe("begin_pass")
        self.maintain_runtimes()
        with self.github_pass() as ready:
            if not ready:
                self._observe("complete_pass")
                return False
            self._withdraw_lost_claim()
            return self._tick_item(number, agent_name)

    def _tick_item(self, number, agent_name=None):
        item, plans = self.item_plans(number, agent_name, reconcile_notices=True)
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
            if plan.health_wait:
                shown = True
                continue
            state, reason = refusal_reason(plan, self.coordinator.clock(), socket.gethostname())
            self.output(f"#{number} {plan.agent.name}: {state} — {reason}")
            shown = True
        if not shown:
            self.output(f"#{number}: {self.item_explanation(item, agent_name)}")
        self._observe("complete_pass")
        return False

    def item_explanation(self, item, agent_name=None):
        """Explain scoped evaluation when it produces no assignment rows."""
        if item.state != "open":
            return f"{item.kind} is {item.state}"
        agents = [a for a in self.config.agents if agent_name is None or a.name == agent_name]
        if not agents:
            return f"Agent {agent_name} is no longer configured"
        applicable = [a for a in agents if a.kind in {"either", item.kind}]
        if applicable:
            labels = "; ".join(f"{a.name}: {', '.join(a.triggers)}" for a in applicable)
            return f"No trigger matches; add a trigger label ({labels})"
        return f"No evaluated agent applies to this {item.kind}"

    def reconcile_blocked_notices(self, history, coordinator):
        """Retry a blocked run's advisory notice from existing coordination records."""
        for lease in latest_leases(history).values():
            if lease.get("result") != "blocked":
                continue
            if lease["state"] != "released" and seconds(lease["expires"]) > self.coordinator.clock():
                continue
            source_id = lease.get("recovered_lease_id", lease["id"])
            outcome = next((r for r in history if r["kind"] == "outcome"
                            and r["lease_id"] == source_id), None)

            def post_missing():
                marker = f"{ACTION_MARKER}{lease['run']} -->"
                trusted = coordinator.trust.observation()
                if any((comment.get("body") or "").startswith(marker) and trusted(comment.get("user"))
                       for comment in coordinator.github.observed_comments(lease['assignment'])):
                    return
                # Discovery can skip an existing notice; writes still reread
                # fresh comments to deduplicate and check for a later resume.
                self.coordinator.notices.post_action(lease['assignment'], lease, outcome,
                                                      lease_summary(history, lease), ())

            self.coordinator.notices.advisory(f"Action needed post on #{lease['assignment']}", post_missing)

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
        self._finalizing = False
        self._renewal = LeaseRenewal(self.coordinator, self.github.github)
        try:
            return self._execute(plan)
        finally:
            self._stop_planning()
            self._renewal.close()
            self._renewal = None
            self._observe("clear_assignment")
            self.github.lease = None

    def claimed(self, lease):
        self.github.claimed(lease)
        if self._renewal is not None:
            self._renewal.claimed(lease)
        if self._continuous and self.observer is not None and self._run_planning is None:
            self._planning_workers = [worker for worker in self._planning_workers if worker.thread.is_alive()]
            worker = None
            try:
                worker = RunPlanning(self, self._pass_started, clock=monotonic)
                worker.start()
            except Exception as exc:
                if worker is not None:
                    worker.cancel()
                    if worker.thread.is_alive():
                        self._planning_workers.append(worker)
                self.output(f"Cannot start queue observations: {exc}")
            else:
                self._run_planning = worker
                self._planning_workers.append(worker)

    def _stop_planning(self):
        if self._run_planning is not None:
            self._planning_rate_until = self._run_planning.cancel()
            self._run_planning = None

    def github_ready(self):
        """A broken/guarded gh cannot perform discovery; keep recovery polling."""
        return self._github_status(self.maintenance.available("gh"))

    def _github_status(self, ready):
        if not ready:
            self._has_trigger = None
            if not self._github_waiting:
                self.output("GitHub CLI gh is unavailable or under maintenance; waiting for the next runtime boundary")
        self._github_waiting = not ready
        return ready

    @contextmanager
    def github_pass(self):
        """Protect every discovery/recovery read and write, including the claim."""
        if not self.github_ready():
            yield False
            return
        with self.maintenance.reserve("gh") as reservation:
            if reservation is None:
                self._github_status(False)
                yield False
                return
            self._github_reservation = reservation
            try:
                if self.coordinator.actor is None:
                    self.coordinator.actor = self.github.actor()
                    self.coordinator.notices.actor = self.coordinator.actor
                    self._observe("configure", self.config, self.coordinator.actor, self.config_path)
                yield True
            finally:
                self._github_reservation = None

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
        plan = self._health_plan(plan, announce=True)
        if plan.health_wait:
            self._observe_plan(plan, self.discovery)
            return False
        # Between supervised runs and cleanup hooks, before any assignment writes.
        # Refresh errors belong to the operator, not to an assignment attempt.
        self._refreshing_checkout = True
        previous = None
        try:
            options = {"on_fetch": self.updates.fetched} if self.updates is not None else {}
            if self.config_path is not None or self.config.checkout_setup is not None:
                def fetched(default, before, head):
                    nonlocal previous
                    previous = before
                    # The refreshed configuration may introduce setup alongside
                    # a lockfile change. Persist before merge, stop or reload.
                    preserve_baseline(self.config.root, before, head)
                    if self.updates is not None:
                        self.updates.fetched(default, before, head)
                options["on_fetch"] = fetched
            if self.config_path is None:
                instructions = refresh_instructions(self.config, plan.agent, self.github, **options)
                shared = instruction_text(self.config.root, self.config.shared_instructions, "shared-instructions")
            else:
                previous = refresh_checkout(self.config, self.github, **options)
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
                shared = instruction_text(config.root, config.shared_instructions, "shared-instructions")
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
            self.coordinator.trust.trusted_bots = {login.casefold() for login in config.trusted_bots}
            self.coordinator.trust.launchers = (None if config.launchers is None else
                                               {login.casefold() for login in config.launchers})
        self._refreshing_checkout = True
        try:
            if self.config_path is not None and self.config.checkout_setup is None:
                discard_unused_baseline(self.config.root)
            run_setup(self.config, self.interrupt_event, self.output,
                      lambda state: self._observe("activity", state), previous)
        finally:
            self._refreshing_checkout = False
        self._before_claim()
        if self.config_path is not None:
            plans = (self.iter_plans(health_notices=True) if self._launch_number is None else
                     self.item_plans(self._launch_number, self._launch_agent, health_notices=True)[1])
            plan = next((p for p in plans if p.item.number == plan.item.number
                         and p.agent.name == plan.agent.name and p.state == "ready"), None)
            if plan is None:
                return False
            instructions = texts[plan.agent.name]
        self.maintain_runtimes()
        if not self.github_ready():
            return False
        # A slow refresh or maintenance can outlive the cached pass. Recheck
        # before reserving a runtime and before the first assignment write.
        plan = self._health_plan(plan, announce=True)
        if plan.health_wait:
            self._observe_plan(plan, self.discovery)
            return False
        # The snapshot runtime may have become guarded/broken since discovery,
        # or a reload may have enabled maintenance. Reapply runtime eligibility.
        runtime = None
        try:
            if plan.runtime is not None:
                runtime = self.coordinator.choose_runtime(plan.item, plan.agent, list(plan.history))
        except GitHubError:
            raise
        except AgentError as exc:
            self.output(f"#{plan.item.number} {plan.agent.name}: waiting — {exc}")
            return False
        with self.maintenance.reserve_run(runtime.cli if runtime is not None else None,
                                          github=self._github_reservation) as reservation:
            if reservation is None:
                self.output(f"#{plan.item.number} {plan.agent.name}: waiting — "
                            f"{runtime.cli + ' runtime or gh' if runtime else 'gh'} became unavailable "
                            "before the claim; retry next poll")
                return False
            self._before_claim()
            return self._claim_execute(replace(plan, runtime=runtime), instructions, reservation,
                                       shared_instructions=shared)

    def _claim_execute(self, plan, instructions, reservation=None, *, shared_instructions=""):
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
        run_dir = run_directory(self.config.root.resolve(), lease["run"])
        scratch = ScratchDirectory(self.config.root, self.config.repository, lease["run"])
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
                                   expires=(lambda: self.coordinator.deadline(lease)) if record else None,
                                   pass_fds=reservation.descriptors if reservation is not None else ())
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
        denials = {}
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
                       "withheld_counts": approval.snapshot["withheld_counts"],
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
            run_config_path = run_dir / "run.json"
            run_config_path.write_text(json.dumps(run_config(self.config, plan.agent, lease)))
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("UB_AGENTS_")}
            env.update({"UB_AGENTS_REPOSITORY": self.config.repository,
                        "UB_AGENTS_ASSIGNMENT": str(plan.item.number),
                        "UB_AGENTS_RUN": lease["run"], "UB_AGENTS_LEASE_ID": str(lease["id"]),
                        "UB_AGENTS_CONTEXT": str(context_path),
                        "UB_AGENTS_RUN_CONFIG": str(run_config_path),
                        "UB_AGENTS_REPORT": report_command,
                        "UB_AGENTS_SCRATCH": str(scratch.path), "TMPDIR": str(scratch.path),
                        "UB_AGENTS_CANDIDATE_SHA": context["candidate_sha"] or "",
                        "UB_AGENTS_BRANCH": lease.get("branch") or ""})
            diagnostic("started", cwd=str(cwd))
            setup = False
            command = command_for(plan.agent, plan.runtime, scratch.path, report_command)
            if reservation is not None and reservation.executable is not None:
                command[0] = reservation.executable
            if plan.runtime:
                usage_output = UsageOutput(plan.runtime.cli, run_dir, self.usage, env)
            def observe_output(final=False):
                self._poll_updates()
                if usage_output:
                    usage_output.poll(final=final)
            try:
                code = supervise(command, cwd, env, run_dir,
                                 plan.agent.timeout_seconds, self.interrupt_event,
                                 self.prompt_for(plan, lease, context, instructions,
                                                 shared_instructions=shared_instructions) if plan.runtime else None,
                                 expires=lambda: self.coordinator.deadline(lease), process_started=process_started,
                                 observe_output=observe_output if usage_output or self.updates else None,
                                 pass_fds=code_descriptors() + (reservation.descriptors if reservation is not None else ()))
            finally:
                denials = collect_denials(plan.runtime.cli if plan.runtime else None, run_dir / "process.log")
            if usage_output:
                usage_output.poll(final=True)
            self._observe("process", "exited", "Supervision confirmed execution has ended")
            diagnostic("execution-exited", code=code)
            # No acceptance or release until all attributable execution has ended.
            cleanup_workspace()
            completing = True
            self._finalizing = True
            self.coordinator.assert_owned(lease)
            outcome = self.coordinator.outcome(lease)
            if outcome is not None and denials:
                self.coordinator.update_outcome(lease, outcome, **denials)
            if outcome is None:
                result = "retry"
                summary = f"Execution exited {code} without an explicit GitHub outcome; inspect process.log"
            else:
                result, summary, effect = self.reported_verdict(lease, plan, outcome, lease, "completion")
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
            if completing and not isinstance(exc, ValidationError):
                raise  # Keep completed reports recoverable; never invent a failure verdict.
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
            completing = False  # This supervision verdict supersedes an early report.
        if outcome is None:
            # A report can precede a timeout/interruption. Keep that report
            # unaccepted and persist the supervisor's actual verdict on the lease.
            outcome = self.coordinator.outcome(lease)
        self.settle(plan, lease, lease["attempt"], result, summary, effect, outcome, denials,
                    completed=completing)
        self._replan_finished_item(plan)
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

    def prompt_for(self, plan, lease, context, instructions, *, shared_instructions=""):
        earlier = context["earlier_branches"]
        report_command = context["report_command"]
        retrospective = (RETROSPECTIVE_PROMPT.format(report_command=report_command)
                         if plan.agent.retrospectives is not None else "")
        continuation = (CONTINUATION_PROMPT.format(earlier_branches=json.dumps(earlier)) if earlier else "")
        matches = plan.matches
        if matches is None or matches.configured != self.config.agents:
            matches = AgentMatches.for_item(plan.item, self.config.agents)
        workflow_labels = set(self.config.stop_labels).union(matches.workflow_triggers)
        for configured in self.config.agents:
            for changes in configured.outcomes.values():
                workflow_labels.update(changes["add"])
                workflow_labels.update(changes["remove"])
        shared = (f"Shared project policy:\n{shared_instructions}\n\n"
                  if self.config.shared_instructions is not None else "")
        return RUN_PROMPT.format(agent=plan.agent.name, report_command=report_command,
                                 outcomes=json.dumps(lease["outcomes"], sort_keys=True),
                                 labels=json.dumps(sorted(workflow_labels)), continuation=continuation,
                                 retrospective=retrospective, context=json.dumps(context, indent=2),
                                 shared_instructions=shared, instructions=instructions)

    def reported_verdict(self, lease, plan, outcome, source, what):
        """A recorded report's (result, summary, attempt effect); raises ValidationError if rejected."""
        if outcome["status"] != "success":
            self.validate_failure_report(lease, outcome, source)
            return outcome["status"], outcome["summary"], "unchanged" if outcome["status"] == "blocked" else "failure"
        self.finalize(lease, plan, outcome, what)
        return "success", outcome["summary"], "reset"

    def settle(self, plan, lease, attempt, result, summary, effect, outcome=None, denials=None,
               parking_outcome=None, *, completed=False):
        """Release with the verdict and backoff after persisting the report.

        Supervision failures persist their verdict first to supersede early reports.
        Completed reports remain recoverable until release succeeds.
        Without an outcome the launcher writes the run's report. The run is unreported
        unless a recovery parks the source's agent report."""
        delay = backoff(plan.agent, attempt) if result == "retry" and effect == "failure" else 0
        unreported = outcome is None and parking_outcome is None
        if result != "success" and not completed:
            # The verdict supersedes early agent reports without relinquishing live ownership.
            # If GitHub is unreadable, this fails closed and the lease expires without a lie.
            changes = {"unreported": True} if unreported else {}
            self.coordinator.assert_owned(lease)
            self.coordinator.update(lease, result=result, summary=summary, attempt_effect=effect,
                                    retry_after=iso(self.coordinator.clock() + delay) if delay else None,
                                    **changes)
        if outcome is None:
            outcome = self.coordinator.report(lease, result, summary, agent_report=False)
        if denials and any(outcome.get(key) != value for key, value in denials.items()):
            self.coordinator.update_outcome(lease, outcome, **denials)
        self.coordinator.release(lease, result, summary, delay, attempt_effect=effect,
                                 parking_outcome=parking_outcome, max_attempts=plan.agent.max_attempts,
                                 unreported=unreported)

    def finalize(self, lease, plan, outcome, what):
        """Validate a success report, apply its transition and accept it.

        A rejected report is recorded on the outcome and raised as ValidationError.
        GitHub errors retain their retry classification and leave expiry recovery
        to finish the job."""
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
        self.coordinator.accept(lease, outcome)

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
            self._stop_planning()
            self._renewal.close()
            self._renewal = None
            self._observe("clear_assignment")
            self.github.lease = None

    def _recover(self, plan):
        self._finalizing = False
        history = self.coordinator.history(plan.item.number)
        outcome = self.coordinator.pending_completion(history, plan.agent.name, self.coordinator.clock())
        if outcome is None:
            return False
        self._observe("assignment", plan)
        self._finalizing = True
        recovery = self.coordinator.claim(plan, self.config.stop_labels, recovery=True,
                                          before_write=self._end_poll)
        if recovery is None:
            self._finalizing = False
            return False
        # The claim reread may have observed a supervisor's newer outcome flags.
        source = lease_by_id(self.coordinator.history(plan.item.number), recovery["recovered_lease_id"])
        outcome = self.coordinator.outcome(source) if source else None
        if outcome is None:
            raise LostOwnership("Recovered outcome disappeared after claiming")
        # Validate against the originally assigned candidate, not today's head.
        original = replace(plan, item=replace(plan.item, head=outcome["assignment_sha"]))
        try:
            result, summary, effect = self.reported_verdict(recovery, original, outcome, source,
                                                            "recovered completion")
        except ValidationError as exc:
            result, summary = "blocked", f"Recorded outcome cannot be recovered: {exc}"
            effect = "unchanged" if isinstance(exc, TransitionPaused) else "failure"
        # The source's attempt counts this failure, as an execution lease's does.
        self.settle(plan, recovery, source["attempt"], result, f"Recovered {outcome['run']}:\n\n{summary}",
                    effect, parking_outcome=outcome, completed=True)
        self.output(f"#{plan.item.number} {plan.agent.name}: recovered durable outcome; no execution started")
        return True

    def _end_poll(self):
        self._before_claim()
        # Starting a claim write ends discovery. Only a retryable POST failure
        # may skip this pass; its uncertain record is withdrawn before planning.
        self._poll_complete = True

    def launch(self, once=False, number=None, agent_name=None):
        self._launch_number = number
        self._launch_agent = agent_name
        try:
            return self._launch(once or number is not None)
        finally:
            try:
                self._stop_planning()
                for worker in self._planning_workers:
                    worker.close()
                self._planning_workers.clear()
                self._continuous = False
                self._poll_updates()
                self._observe("close")
            finally:
                self._launch_number = None
                self._launch_agent = None

    def _launch(self, once):
        self.usage.reset()
        self._planning_rate_until = None
        self._continuous = not once
        self.github.discovery = not once
        self._observe("configure", self.config, self.coordinator.actor, self.config_path)
        failures = 0
        idle_state = None
        while not self.stop_event.is_set():
            self.github.lease = None
            self._poll_complete = False
            self._finalizing = False
            self._pass_started = monotonic()
            requests_before = self._requests_before = self.github.quota_requests
            try:
                worked = (self.tick() if self._launch_number is None else
                          self.tick_item(self._launch_number, self._launch_agent))
            except _GracefulStop:
                return
            except (_InvalidReload, CleanupError, RecordError):
                raise
            except AgentError as exc:
                if isinstance(exc, LostOwnership):
                    if not self._finalizing or not isinstance(exc.__cause__, GitHubError):
                        raise
                    # Ownership reads fail closed, but a completed agent needs no
                    # termination. Preserve the underlying request's poll policy.
                    exc = exc.__cause__
                if self.interrupt_event.is_set():
                    raise KeyboardInterrupt from None
                if once or (self._poll_complete and not self._finalizing
                            and self.coordinator.lost_claim is None):
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
                if not worked and self._launch_number is None and self._has_trigger is False:
                    self.output(self.idle_message())
                return (0 if worked else 1) if self._launch_number is not None else None
            if worked:
                idle_state = None
                # Start fresh claiming discovery as soon as work settles. An
                # observation's quota pacing must not delay the next claim, but
                # its actual rate-limit wait still applies after cancellation.
                until = self._planning_rate_until
                self._planning_rate_until = None
                delay = max(0, until - self.coordinator.clock()) if until is not None else 0
                if delay:
                    self.output(f"GitHub rate limit reached; waiting until {iso(until)} ({delay / 60:g} min)")
                    with self.poll_now.rate_limit(until) if self.poll_now is not None else nullcontext():
                        self._wait(self.stop_event, delay, "rate-limit reset")
                continue
            elapsed = monotonic() - self._pass_started
            requests = self.github.quota_requests - requests_before
            interval, low = idle_interval(requests, self.config.poll_seconds,
                                          self.github.resource_quotas,
                                          self.coordinator.clock(), elapsed)
            interval = elapsed + self.usage.bound_wait(max(0, interval - elapsed))
            message = self.idle_message()
            if idle_state != (low, message):
                self.output(f"{message}; next poll in {poll_delay(max(0, interval - elapsed))} "
                            f"({requests} requests last poll)")
            idle_state = (low, message)
            delay = self.usage.bound_wait(max(0, interval - elapsed))
            if delay:
                self._wait(self.stop_event, delay, "next poll or runtime pause")
        if self.interrupt_event.is_set():
            raise KeyboardInterrupt

    def idle_message(self):
        if self._has_trigger is False:
            labels = dict.fromkeys(label for agent in self.config.agents for label in agent.triggers)
            return (f"No open issue or PR has a trigger label ({', '.join(labels)}); "
                    "add one to start")
        return "No eligible work"
