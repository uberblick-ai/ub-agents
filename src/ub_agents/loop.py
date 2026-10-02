"""One serial observe → claim → execute → observe → release loop."""

import json
import os
import socket
import threading
from dataclasses import replace

from .approvals import ApprovalCheck, check_issue, check_pr
from .config import instruction_text, load_config
from .coordination import Coordinator, Plan
from .dependencies import Dependencies
from .errors import (AgentError, CleanupError, GitHubError, LostOwnership, RecordError,
                     RetryableExecutionError, TransitionPaused, ValidationError)
from .execution import Workspace, command_for, repository_checks, supervise
from .github import closing_issues, links_issue
from .hooks import run_hook
from .records import attempts, backoff, iso, latest_leases, lease_by_id, lease_summary, seconds, timestamp
from .refresh import refresh_checkout, refresh_instructions

POLL_RETRY_BASE_SECONDS = 5
POLL_RETRY_MAX_SECONDS = 60
POLL_FAILURE_LIMIT = 6


class _GracefulStop(Exception):
    pass


class _InvalidReload(AgentError):
    pass


class Loop:
    def __init__(self, config, github, actor, stop_event=None, output=print,
                 config_path=None, interrupt_event=None):
        self.config = config
        self.github = github
        self.coordinator = Coordinator(github, actor, queue=config.queue, output=output)
        self.stop_event = stop_event or threading.Event()
        self.interrupt_event = interrupt_event or self.stop_event
        self.config_path = config_path
        self.output = output
        self._shown = {}
        self._released_blockers = {}
        self._poll_complete = False
        self._refreshing_checkout = False

    def stop_gracefully(self):
        if self.interrupt_event.is_set():
            # SIGINT/SIGHUP already requested termination. Leave process-group
            # cleanup and durable release to finish without another exception.
            self.stop_event.set()
            return
        self.stop_event.set()
        if not self._poll_complete and not self._refreshing_checkout:
            # Unwind even a slow discovery subprocess. Once a claim write starts,
            # it must finish election, execution/recovery and durable completion.
            raise _GracefulStop

    def _before_claim(self):
        if self.interrupt_event.is_set():
            raise KeyboardInterrupt
        if self.stop_event.is_set():
            raise _GracefulStop

    def input_check(self, item):
        triggers = {label for a in self.config.agents if a.kind in {item.kind, "either"}
                    for label in a.triggers}
        check = (check_issue(self.github, item.number, triggers) if item.kind == "issue" else
                 check_pr(self.github, item.number, triggers, self.coordinator.actor))
        if check.allowed and (check.snapshot["title"], check.snapshot["body"],
                              check.snapshot.get("head")) != (item.title, item.body, item.head):
            return ApprovalCheck(False, "Assignment changed while reading approval input; retry")
        return check

    def plans(self):
        plans = []
        items = {item.number: item for item in self.github.observe()}
        active_milestone = (self.github.active_milestone()
                            if self.config.queue.milestones == "gate" else None)
        history_index, invalid = self.coordinator.repository_history()
        now = self.coordinator.clock()
        unfinished = {r["assignment"] for r in latest_leases(history_index).values()
                      if r["state"] in {"claiming", "running"} or r.get("result") in {"retry", "blocked"}}
        for number in sorted(unfinished | invalid):
            if number not in items:
                item = self.github.item(number)
                items[item.number] = item
        for item in items.values():
            matched = [a for a in self.config.agents if item.labels.intersection(a.triggers)
                       and a.kind in {"either", item.kind} and item.state == "open"]
            if (not matched and item.number not in (unfinished | invalid)
                    and not item.labels.intersection(self.config.stop_labels)):
                continue
            approval = None
            # Discovery is cached, but authority always comes from a fresh item read.
            try:
                history = self.coordinator.history(item.number)
            except RecordError as exc:
                plans.extend(Plan(item, a, None, "blocked", str(exc), 1)
                             for a in matched or self.config.agents)
                continue
            except AgentError:
                # Unreadable item input cannot authorize a claim. A transient
                # coordination failure with readable approval input still fails the poll.
                approval = self.input_check(item) if matched else None
                if approval is None or approval.allowed:
                    raise
                plans.extend(Plan(item, a, None, "parked", approval.reason, 1) for a in matched)
                continue
            latest = latest_leases(history)
            for agent in self.config.agents:
                record = latest.get((item.number, agent.name))
                parked = (item.state == "open" and item.labels.intersection(self.config.stop_labels)
                          and any(r["kind"] == "outcome" and r["agent"] == agent.name
                                  and r.get("accepted") and r.get("transition_complete")
                                  and item.number == (r.get("handoff") or r["assignment"])
                                  and item.labels.intersection(r.get("transition", {}).get("add", ()))
                                      .intersection(self.config.stop_labels) for r in history))
                try:
                    pending = self.coordinator.pending_completion(history, agent.name, now)
                except RecordError as exc:
                    plans.append(Plan(item, agent, None, "blocked", str(exc),
                                      len(attempts(history, agent.name, now)) + 1))
                    continue
                if (agent in matched or pending or parked or (record and record["state"] in {"claiming", "running"}
                                                    and seconds(record["expires"]) > now)):
                    plan = self.coordinator.plan(item, agent, self.config.stop_labels, history, history_index)
                    if plan.state in {"ready", "blocked", "backoff"} and agent in matched:
                        if approval is None:
                            approval = self.input_check(item)
                        if not approval.allowed:
                            plan = replace(plan, state="parked", runtime=None, reason=approval.reason)
                    plans.append(plan)
                elif record and (record["state"] in {"claiming", "running"}
                                 or record.get("result") in {"retry", "blocked"}):
                    if record.get("cleanup") == "unconfirmed":
                        reason = f"Previous cleanup was unconfirmed: {lease_summary(history, record)}"
                    elif record["state"] == "released":
                        reason = f"Last run {record['result']}: {lease_summary(history, record)}"
                    else:
                        reason = "Expired run has no outcome or matching trigger"
                    plans.append(Plan(item, agent, None, "blocked",
                        f"{reason}; inspect GitHub and restore a trigger before retrying",
                        len(attempts(history, agent.name, now)) + 1))
        if not plans:
            return []
        dependencies = (Dependencies(self.github, items.values(), self.config.queue.priority)
                        if self.config.queue.dependencies == "wait" else None)
        priority_config = self.config.queue.priority
        issue_priorities = {i.number: priority_config.effective(i.labels) for i in items.values()
                            if i.kind == "issue" and i.state == "open"}
        if dependencies:
            issue_priorities.update({n: label for n, (label, _) in dependencies.priorities.items()})
        ranked = []
        for plan in plans:
            priority = priority_config.effective(plan.item.labels)
            source, from_issue, blockers = None, None, ()
            if dependencies and plan.item.kind == "issue":
                priority, source = dependencies.priorities.get(plan.item.number, (priority, None))
                blockers = dependencies.blockers.get(plan.item.number, ())
            elif plan.item.kind == "pr":
                for number in sorted(closing_issues(plan.item, self.config.repository)):
                    label = issue_priorities.get(number)
                    if label is not None and (priority is None or
                            priority_config.labels.index(label) < priority_config.labels.index(priority)):
                        priority, from_issue = label, number
            reasons = []
            if plan.state == "ready" and plan.item.kind == "issue":
                if active_milestone is not None and plan.item.milestone != active_milestone:
                    reasons.append(f"Waiting for active milestone #{active_milestone}")
                if blockers:
                    reasons.append(f"Waiting for blockers {', '.join(blockers)}")
            if reasons:
                plan = replace(plan, state="parked", runtime=None, reason="; ".join(reasons))
            ranked.append(replace(plan, priority=priority, priority_source=source,
                                  priority_from_issue=from_issue, blockers=blockers))
        # PR work and recovery rank before new issue starts. Stable sorting keeps
        # YAML order for agents on the same item within each work class.
        return sorted(ranked, key=lambda plan: (
            0 if plan.item.kind == "pr" or plan.state in {"owned", "recover"} else 1,
            (priority_config.labels.index(plan.priority) if plan.priority is not None
             else len(priority_config.labels)),
            seconds(plan.item.created_at), plan.item.number))

    def tick(self):
        config = self.config
        plans = self.plans()
        present = {(p.item.number, p.agent.name) for p in plans}
        self._shown = {key: value for key, value in self._shown.items() if key in present}
        self._released_blockers = {key: value for key, value in self._released_blockers.items() if key in present}
        for plan in plans:
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
                key, value = (plan.item.number, plan.agent.name), (plan.state, plan.reason)
                released = self._released_blockers.pop(key, None)
                announced = plan.state == "blocked" and released is not None and released in plan.reason
                if not announced and (plan.state not in {"blocked", "parked"} or self._shown.get(key) != value):
                    self.output(f"#{plan.item.number} {plan.agent.name}: {plan.state} — {plan.reason}")
                self._shown[key] = value
        return False

    def execute(self, plan):
        # Between supervised runs and cleanup hooks, before any assignment writes.
        # Refresh errors belong to the operator, not to an assignment attempt.
        self._refreshing_checkout = True
        try:
            if self.config_path is None:
                instructions = refresh_instructions(self.config, plan.agent, self.github)
            else:
                refresh_checkout(self.config, self.github)
        finally:
            # An asynchronous exception in subprocess.run kills its child. Let
            # the checkout refresh finish so a fast-forward is never torn down
            # by SIGTERM, then stop before doing any assignment writes.
            self._refreshing_checkout = False
        self._before_claim()
        if self.config_path is not None:
            try:
                config = load_config(self.config_path)
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
            self.coordinator.queue = config.queue
            plan = next((p for p in self.plans() if p.item.number == plan.item.number
                         and p.agent.name == plan.agent.name and p.state == "ready"), None)
            if plan is None:
                return False
            instructions = texts[plan.agent.name]
        self._before_claim()
        lease = self.coordinator.claim(plan, self.config.stop_labels, before_write=self._end_poll)
        if lease is None:
            return False
        try:
            fresh = self.github.item(plan.item.number, plan.item.kind)
            approval = self.input_check(fresh)
        except AgentError:
            approval = ApprovalCheck(False, "Assignment approval history is unreadable; retry or ask a maintainer")
        if not approval.allowed:
            self.coordinator.assert_owned(lease)
            self.coordinator.update(lease, state="withdrawn", started=False, attempt_effect="unchanged",
                                    expires=iso(self.coordinator.clock()), summary=approval.reason)
            self.output(f"#{plan.item.number} {plan.agent.name}: parked — {approval.reason}")
            return True
        run_dir = self.config.root / ".ub-agent" / "runs" / lease["run"]
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
                                   expires=seconds(lease["expires"]) if record else None)
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
                if record:
                    record_uncertainty(exc)
                else:
                    diagnostic("cleanup-unconfirmed", error=str(exc))
                raise

        def process_started(pid):
            self.coordinator.assert_owned(lease)
            self.coordinator.update(lease, process_group=pid)
            self.coordinator.assert_owned(lease)

        interrupted = False
        completing = False
        setup = True
        effect = "failure"
        result, summary = "retry", "Assignment ended without a validated outcome"
        outcome = None
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            self.coordinator.assert_owned(lease)
            self.coordinator.update(lease, state="running", started=True,
                                    host=socket.gethostname(), log_dir=str(run_dir))
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
            context = {"repository": self.config.repository, "assignment": plan.item.number,
                       "kind": plan.item.kind, "title": approval.snapshot["title"],
                       "body": approval.snapshot["body"], "comments": approval.snapshot["comments"],
                       "feedback": self.coordinator.feedback(plan.item, plan.agent.name),
                       "candidate_sha": plan.item.head, "run": lease["run"],
                       "agent": plan.agent.name, "branch": lease.get("branch"),
                       "earlier_branches": self.earlier_branches(plan.item, plan.agent, lease["run"])}
            if plan.item.kind == "pr":
                context |= {name: approval.snapshot[name] for name in ("reviews", "review_comments")}
            context_path = run_dir / "context.json"
            context_path.write_text(json.dumps(context, indent=2))
            env = os.environ.copy()
            env.update({"UB_AGENT_REPOSITORY": self.config.repository,
                        "UB_AGENT_ASSIGNMENT": str(plan.item.number),
                        "UB_AGENT_RUN": lease["run"], "UB_AGENT_LEASE_ID": str(lease["id"]),
                        "UB_AGENT_CONTEXT": str(context_path),
                        "UB_AGENT_CANDIDATE_SHA": context["candidate_sha"] or "",
                        "UB_AGENT_BRANCH": lease.get("branch") or ""})
            diagnostic("started", cwd=str(cwd))
            setup = False
            code = supervise(command_for(plan.agent, plan.runtime), cwd, env, run_dir,
                             plan.agent.timeout_seconds, self.interrupt_event,
                             self.prompt_for(plan, lease, context, instructions) if plan.runtime else None,
                             expires=seconds(lease["expires"]), process_started=process_started)
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
            self.coordinator.report(lease, result, summary)
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
        continuation = (f"Earlier runs of this issue recorded branches {json.dumps(earlier)}; check each with "
                        "gh pr list --state open --head BRANCH and continue an open draft PR there instead "
                        "of opening another. " if earlier else "")
        workflow_labels = set(self.config.stop_labels)
        for configured in self.config.agents:
            workflow_labels.update(configured.triggers)
            for changes in configured.outcomes.values():
                workflow_labels.update(changes["add"])
                workflow_labels.update(changes["remove"])
        return (f"You are the project-configured agent {plan.agent.name}.\n"
                "This run is a single, non-interactive session that is never resumed. "
                "Ending your turn ends the run. Run checks in the foreground or wait for every "
                "background job to finish before ending your turn. End the run with ub-agent report.\n"
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
                "Report one with ub-agent report --outcome NAME --summary 'what happened' "
                "[--handoff PR_NUMBER]. Do not change workflow labels "
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
        if {k: v for k, v in transition.items() if k != "started"} != declarations[name]:
            raise ValidationError("Reported transition does not match the running agent's declaration")
        return transition

    def apply_transition(self, lease, outcome):
        transition = outcome["transition"]
        self.coordinator.assert_owned(lease)
        assignment = self.github.item(outcome["assignment"])
        target = outcome.get("handoff") or outcome["assignment"]
        destination = self.github.item(target)
        if not transition["started"]:
            stops = set(transition["stop_labels"]).union(self.config.stop_labels)
            if assignment.labels.union(destination.labels).intersection(stops):
                raise TransitionPaused("Transition paused: a stop label is on the assignment or handoff PR; "
                                      "set workflow labels manually or use ub-agent retry after unpausing")
            if not assignment.labels.intersection(transition["triggers"]):
                raise TransitionPaused("Transition blocked: assignment trigger disappeared before label changes")
            # Persist intent before the first mutation. Recovery must not mistake
            # our own trigger removal or human-gate addition for external pausing.
            self.coordinator.update_outcome(lease, outcome, transition=transition | {"started": True})
            transition = outcome["transition"]
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
        history = self.coordinator.history(plan.item.number)
        outcome = self.coordinator.pending_completion(history, plan.agent.name, self.coordinator.clock())
        if outcome is None:
            return False
        recovery = self.coordinator.claim(plan, self.config.stop_labels, recovery=True,
                                          before_write=self._end_poll)
        if recovery is None:
            return False
        result, summary = outcome["status"], outcome["summary"]
        effect = "unchanged" if result == "blocked" else "failure"
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
        self.coordinator.update(recovery, recovered_run=outcome["run"],
                                recovered_lease_id=outcome["lease_id"])
        self.coordinator.report(recovery, result, f"Recovered {outcome['run']}: {summary}")
        # Expiry permits recovery; it is not positive proof of the old process's death.
        failures = len(attempts(self.coordinator.history(plan.item.number), plan.agent.name, self.coordinator.clock()))
        delay = backoff(plan.agent, max(1, failures)) if result == "retry" else 0
        self.coordinator.release(recovery, result, f"Recovered {outcome['run']}: {summary}", delay,
                                 attempt_effect=effect, parking_outcome=outcome)
        self.output(f"#{plan.item.number} {plan.agent.name}: recovered durable outcome; no execution started")
        return True

    def _end_poll(self):
        self._before_claim()
        # Even an unsuccessful lease write ends discovery. Never retry a tick that
        # may already have written a claim or withdrawn from a claim election.
        self._poll_complete = True

    def launch(self, once=False):
        failures = 0
        while not self.stop_event.is_set():
            self._poll_complete = False
            try:
                worked = self.tick()
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
                if retryable and exc.reset_at is not None:
                    delay = max(0, exc.reset_at - timestamp())
                    retryable = delay <= POLL_RETRY_MAX_SECONDS
                detail = str(exc)
                # Keep each diagnostic on one line, even when gh prints several.
                detail = " ".join(detail.split())
                if not retryable or failures >= POLL_FAILURE_LIMIT:
                    reason = (f"retries exhausted after {failures} consecutive failed polls"
                              if retryable else "failure is not retryable; retries not exhausted")
                    raise AgentError(f"{detail}; {reason}. Fix the cause and restart ub-agent launch.") from exc
                self.output(f"Skipped GitHub poll: {detail}; retrying in {delay:g}s")
                self.stop_event.wait(delay)
                continue
            failures = 0
            if self.interrupt_event.is_set():
                raise KeyboardInterrupt
            if self.stop_event.is_set():
                return
            if once:
                return
            if not worked:
                self.output("Waiting for eligible GitHub work")
                self.stop_event.wait(self.config.poll_seconds)
        if self.interrupt_event.is_set():
            raise KeyboardInterrupt
