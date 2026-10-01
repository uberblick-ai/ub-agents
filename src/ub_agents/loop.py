"""One serial observe → claim → execute → observe → release loop."""

import json
import os
import threading
import time
from dataclasses import replace

from .coordination import Coordinator, Plan
from .dependencies import Dependencies
from .errors import AgentError, CleanupError, LostOwnership, RecordError, ValidationError
from .execution import Workspace, command_for, supervise
from .github import closing_issues, links_issue
from .records import attempts, iso, latest_leases, lease_summary, seconds, timestamp


class Loop:
    def __init__(self, config, github, actor, stop_event=None, output=print):
        self.config = config
        self.github = github
        self.coordinator = Coordinator(github, actor, trusted_actors=config.operators,
                                       queue=config.queue)
        self.stop_event = stop_event or threading.Event()
        self.output = output
        # The candidate cannot rewrite the operator's configured task policy.
        self.instructions = {a.name: a.instructions.read_text() if a.instructions else ""
                             for a in config.agents}

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
            if not matched and item.number not in (unfinished | invalid):
                continue
            # Discovery is cached, but authority always comes from a fresh item read.
            try:
                history = self.coordinator.history(item.number)
            except RecordError as exc:
                plans.extend(Plan(item, a, None, "blocked", str(exc), 1)
                             for a in matched or self.config.agents)
                continue
            latest = latest_leases(history)
            for agent in self.config.agents:
                record = latest.get((item.number, agent.name))
                try:
                    pending = self.coordinator.pending_completion(history, agent.name, now)
                except RecordError as exc:
                    plans.append(Plan(item, agent, None, "blocked", str(exc), 1))
                    continue
                if (agent in matched or pending or (record and record["state"] in {"claiming", "running"}
                                                    and seconds(record["expires"]) > now)):
                    plan = self.coordinator.plan(item, agent, self.config.stop_labels, history)
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
            if plan.state == "ready" and plan.item.kind == "issue" and plan.resume_pr is None:
                if active_milestone is not None and plan.item.milestone != active_milestone:
                    reasons.append(f"Waiting for active milestone #{active_milestone}")
                if blockers:
                    reasons.append(f"Waiting for blockers {', '.join(blockers)}")
            if reasons:
                plan = replace(plan, state="parked", runtime=None, reason="; ".join(reasons))
            ranked.append(replace(plan, priority=priority, priority_source=source,
                                  priority_from_issue=from_issue, blockers=blockers))
        # Rank after checkpoint/recovery discovery: these are PR work even when
        # their durable assignment is an issue. Stable sorting keeps YAML order
        # for agents on the same item within each work class.
        return sorted(ranked, key=lambda plan: (
            0 if (plan.item.kind == "pr" or plan.state in {"owned", "recover"}
                  or plan.resume_pr is not None) else 1,
            (priority_config.labels.index(plan.priority) if plan.priority is not None
             else len(priority_config.labels)),
            seconds(plan.item.created_at), plan.item.number))

    def tick(self):
        for plan in self.plans():
            if self.stop_event.is_set():
                raise KeyboardInterrupt
            if plan.state == "ready":
                if self.execute(plan):
                    return True
            elif plan.state == "recover":
                if self.recover(plan):
                    return True
            else:
                self.output(f"#{plan.item.number} {plan.agent.name}: {plan.state} — {plan.reason}")
        return False

    def execute(self, plan):
        lease = self.coordinator.claim(plan, self.config.stop_labels)
        if lease is None:
            return False
        run_dir = self.config.root / ".ub-agent" / "runs" / lease["run"]
        run_dir.mkdir(parents=True, exist_ok=True)
        workspace = Workspace(self.config, plan.agent, plan.item, lease, self.github)
        self.output(f"#{plan.item.number} {plan.agent.name}: claimed {lease['run']} ({lease['runtime']})")
        self.output(f"Logs: {run_dir}")

        def diagnostic(event, **details):
            with (run_dir / "events.jsonl").open("a") as stream:
                stream.write(json.dumps({"time": iso(timestamp()), "event": event, **details}) + "\n")

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
            except AgentError as failure:
                diagnostic("cleanup-verdict-unrecorded", error=str(failure))

        def cleanup_workspace(record=True):
            try:
                workspace.cleanup()
            except CleanupError as exc:
                if record:
                    record_uncertainty(exc)
                raise

        next_renewal = time.monotonic() + plan.agent.renewal_seconds

        def heartbeat():
            nonlocal next_renewal
            if timestamp() >= seconds(lease["expires"]):
                raise LostOwnership("Local lease deadline expired")
            if time.monotonic() >= next_renewal:
                self.coordinator.renew(lease, plan.agent.lease_seconds)
                next_renewal = time.monotonic() + plan.agent.renewal_seconds
                diagnostic("renewed", expires=lease["expires"])

        interrupted = False
        result, summary = "retry", "Assignment ended without a validated outcome"
        outcome = None
        try:
            self.coordinator.assert_owned(lease)
            self.coordinator.update(lease, state="running", started=True)
            cwd = workspace.prepare()
            self.coordinator.update(lease, branch=lease.get("branch"))
            fresh = self.github.item(plan.item.number, plan.item.kind)
            if (fresh.head != plan.item.head or fresh.state != "open"
                    or not fresh.labels.intersection(plan.agent.triggers)
                    or fresh.labels.intersection(self.config.stop_labels)):
                raise AgentError("Trigger or candidate changed before execution")
            self.coordinator.assert_owned(lease)
            if lease.get("resume_pr"):
                checkpoint = self.github.item(lease["resume_pr"], "pr")
                if (checkpoint.state != "open" or checkpoint.merged or not checkpoint.draft
                        or checkpoint.head != lease["resume_sha"] or checkpoint.branch != lease["branch"]
                        or checkpoint.head_repository != self.config.repository
                        or not links_issue(checkpoint, self.config.repository, plan.item.number)):
                    raise AgentError("Checkpoint changed before execution")
            context = {"repository": self.config.repository, "assignment": plan.item.number,
                       "kind": plan.item.kind, "title": plan.item.title, "body": plan.item.body,
                       "candidate_sha": plan.item.head, "run": lease["run"],
                       "agent": plan.agent.name, "branch": lease.get("branch")}
            if lease.get("resume_pr"):
                context |= {"resume_pr": lease["resume_pr"], "candidate_sha": lease["resume_sha"]}
            context_path = run_dir / "context.json"
            context_path.write_text(json.dumps(context, indent=2))
            env = os.environ.copy()
            env.update({"UB_AGENT_REPOSITORY": self.config.repository,
                        "UB_AGENT_ASSIGNMENT": str(plan.item.number),
                        "UB_AGENT_RUN": lease["run"], "UB_AGENT_LEASE_ID": str(lease["id"]),
                        "UB_AGENT_CONTEXT": str(context_path),
                        "UB_AGENT_OPERATORS": json.dumps(sorted(self.coordinator.trusted_actors)),
                        "UB_AGENT_CANDIDATE_SHA": context["candidate_sha"] or "",
                        "UB_AGENT_BRANCH": lease.get("branch") or ""})
            env["UB_AGENT_PR"] = str(lease.get("resume_pr") or (plan.item.number if plan.item.kind == "pr" else ""))
            instructions = self.instructions[plan.agent.name]
            if plan.agent.outcomes is not None:
                workflow_labels = set(self.config.stop_labels)
                for configured in self.config.agents:
                    workflow_labels.update(configured.triggers)
                    for changes in (configured.outcomes or {}).values():
                        workflow_labels.update(changes["add"])
                        workflow_labels.update(changes["remove"])
                reporting = (f"Declared outcomes: {json.dumps(lease['outcomes'], sort_keys=True)}. "
                             "Report one with ub-agent report --outcome NAME --summary 'what happened' "
                             "[--handoff PR_NUMBER]. Do not change workflow labels "
                             f"(trigger, transition or stop labels): {json.dumps(sorted(workflow_labels))}. "
                             "Use --status retry|blocked for failures; those change no labels. ")
            else:
                reporting = ("Remove the triggering labels before reporting success, or close the "
                             "assignment when policy permits. Record the explicit durable outcome with "
                             "ub-agent report --status success|retry|blocked --summary 'what happened' "
                             "[--handoff PR_NUMBER]. ")
            prompt = (f"You are the project-configured agent {plan.agent.name}.\n"
                      f"Assignment context:\n{json.dumps(context, indent=2)}\n\n"
                      f"Project instructions:\n{instructions}\n\n"
                      + (f"Resume existing draft PR #{lease['resume_pr']} on branch {lease['branch']}. "
                         "The checkout is detached: push HEAD explicitly to UB_AGENT_BRANCH. "
                         "Read the issue and this PR's feedback, continue that PR, and hand off its number; "
                         "do not create another PR.\n\n" if lease.get("resume_pr") else "") +
                      "Read shared repository guidance and the original issue requirements, acceptance "
                      "criteria, current code/diff, and candidate-specific checks on GitHub. "
                      "Use a fresh session; do not consume implementation reasoning transcripts. "
                      "Do not renew claims or start detached heartbeat helpers. The launcher owns renewal. "
                      "Apply only project-authorized handoffs and permissions. "
                      f"{reporting}"
                      "Issue-to-PR handoffs must link the issue in the PR body. "
                      "For candidate acceptance, results and checks must name the assigned SHA.\n")
            diagnostic("started", cwd=str(cwd))
            code = supervise(command_for(plan.agent, plan.runtime), cwd, env, run_dir,
                             plan.agent.timeout_seconds, heartbeat, self.stop_event,
                             prompt if plan.runtime else None)
            # No acceptance or release until all attributable execution has ended.
            workspace.cleanup()
            self.coordinator.assert_owned(lease)
            outcome = self.coordinator.outcome(lease)
            if outcome is None:
                result = "blocked" if code != 0 else "retry"
                summary = f"Execution exited {code} without an explicit GitHub outcome; inspect process.log"
            elif outcome["status"] != "success":
                result, summary = outcome["status"], outcome["summary"]
            elif code != 0:
                result, summary = "blocked", f"Success report conflicts with execution exit {code}"
            else:
                try:
                    self.validate_success(plan, outcome)
                    self.apply_transition(lease, outcome)
                except ValidationError as exc:
                    result = "blocked"
                    self.coordinator.update_outcome(lease, outcome, rejected=str(exc))
                    raise
                except (AgentError, OSError) as exc:
                    raise LostOwnership(f"Cannot observe completion; leave expiry recovery: {exc}") from exc
                try:
                    self.coordinator.accept(lease, outcome)
                except AgentError as exc:
                    raise LostOwnership(f"Cannot finalize durable outcome; leave expiry recovery: {exc}") from exc
                result, summary = "success", outcome["summary"]
        except CleanupError as exc:
            record_uncertainty(exc)
            raise
        except LostOwnership as exc:
            diagnostic("ownership-lost", error=str(exc))
            cleanup_workspace(record=False)
            # Do not renew, report, release, or accept after losing ownership.
            raise
        except KeyboardInterrupt:
            if outcome and outcome.get("transition", {}).get("started"):
                diagnostic("transition-interrupted", outcome=outcome["id"])
                cleanup_workspace()
                # Leave the durable transition pending for expiry recovery.
                raise
            interrupted = True
            summary = "Launcher interrupted; attributable execution terminated"
            cleanup_workspace()
        except (AgentError, OSError) as exc:
            summary = str(exc)
            cleanup_workspace()
        delay = min(plan.agent.max_backoff_seconds,
                    plan.agent.backoff_seconds * (2 ** min(lease["attempt"] - 1, 32))) if result == "retry" else 0
        # Framework failures are themselves explicit durable outcomes. If GitHub is
        # unreadable, this fails closed and the last lease expires without a lie.
        if outcome is None:
            # A report can precede a timeout/interruption. Keep that report
            # unaccepted and persist the supervisor's actual verdict on the lease.
            outcome = self.coordinator.outcome(lease)
        if outcome is None:
            self.coordinator.report(lease, result, summary)
        self.coordinator.release(lease, result, summary, delay)
        diagnostic("released", result=result, summary=summary)
        self.output(f"#{plan.item.number} {plan.agent.name}: {result} — {summary}")
        if interrupted:
            raise KeyboardInterrupt
        return True

    def validate_success(self, plan, outcome):
        current = self.github.item(plan.item.number, plan.item.kind)
        transition = self.validate_report(outcome)
        if (transition is None and current.state == "open" and current.labels.intersection(plan.agent.triggers)
                and not (plan.item.kind == "issue" and outcome.get("handoff"))):
            raise ValidationError("Success report left assignment trigger labels in place")
        destination = (self.github.item(outcome["handoff"], "pr")
                       if outcome.get("handoff") else current)
        resumed = plan.resume_pr.number if plan.resume_pr else outcome.get("resume_pr")
        if resumed and outcome.get("handoff") != resumed:
            raise ValidationError("Resumed issue must hand off its existing PR")
        if outcome.get("handoff") and destination.draft:
            raise ValidationError(f"Handoff PR #{destination.number} is still a draft")
        if outcome["candidate_sha"] != destination.head:
            raise ValidationError("Outcome candidate SHA does not match the observed PR head")
        if plan.agent.different_from and current.head != plan.item.head:
            raise ValidationError("Independent result is stale: the assigned candidate moved")
        if outcome.get("handoff") and plan.item.kind == "issue":
            if not links_issue(destination, self.config.repository, plan.item.number):
                raise ValidationError("Implementation PR body does not link its original issue")

    def validate_report(self, outcome):
        if outcome.get("rejected"):
            raise ValidationError(outcome["rejected"])
        history = self.coordinator.history(outcome["assignment"])
        source = next((r for r in history if r["kind"] == "lease" and r["id"] == outcome["lease_id"]), None)
        declarations = source.get("outcomes") if source else None
        transition = outcome.get("transition")
        name = outcome.get("outcome")
        if declarations is not None:
            if name not in declarations or transition is None:
                raise ValidationError("Success report must name a declared outcome")
            if {k: v for k, v in transition.items() if k != "started"} != declarations[name]:
                raise ValidationError("Reported transition does not match the running agent's declaration")
        elif name is not None or transition is not None:
            raise ValidationError("Outcome is not declared by the running agent")
        return transition

    def apply_transition(self, lease, outcome):
        transition = outcome.get("transition")
        if transition is None:
            return
        self.coordinator.assert_owned(lease)
        assignment = self.github.item(outcome["assignment"])
        target = outcome.get("handoff") or outcome["assignment"]
        destination = self.github.item(target)
        if not transition["started"]:
            stops = set(transition["stop_labels"]).union(self.config.stop_labels)
            if assignment.labels.union(destination.labels).intersection(stops):
                raise ValidationError("Transition paused: a stop label is on the assignment or handoff PR; "
                                      "set workflow labels manually or use ub-agent retry after unpausing")
            if not assignment.labels.intersection(transition["triggers"]):
                raise ValidationError("Transition blocked: assignment trigger disappeared before label changes")
            # Persist intent before the first mutation. Recovery must not mistake
            # our own trigger removal or human-gate addition for external pausing.
            self.coordinator.update_outcome(lease, outcome, transition=transition | {"started": True})
            transition = outcome["transition"]
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
        recovery = self.coordinator.claim(plan, self.config.stop_labels, recovery=True)
        if recovery is None:
            return False
        result, summary = outcome["status"], outcome["summary"]
        if result == "success":
            from dataclasses import replace
            original = replace(plan.item, head=outcome["assignment_sha"])
            try:
                if not outcome.get("transition") and plan.item.labels.intersection(self.config.stop_labels):
                    raise ValidationError("A configured stop label now parks the work")
                if outcome.get("transition", {}).get("started"):
                    # Start is durable proof that success validation passed. A
                    # later head/link edit cannot strand already-applied changes;
                    # finish the intent while preserving the original provenance.
                    self.validate_report(outcome)
                else:
                    self.validate_success(replace(plan, item=original), outcome)
                self.apply_transition(recovery, outcome)
            except ValidationError as exc:
                self.coordinator.update_outcome(recovery, outcome, rejected=str(exc))
                result, summary = "blocked", f"Recorded outcome cannot be recovered: {exc}"
            except AgentError as exc:
                raise LostOwnership(f"Cannot observe recovered completion; leave expiry recovery: {exc}") from exc
            else:
                try:
                    self.coordinator.accept(recovery, outcome)
                except AgentError as exc:
                    raise LostOwnership(f"Cannot finalize recovered outcome; leave expiry recovery: {exc}") from exc
        self.coordinator.update(recovery, recovered_run=outcome["run"],
                                recovered_lease_id=outcome["lease_id"])
        self.coordinator.report(recovery, result, f"Recovered {outcome['run']}: {summary}")
        # Expiry permits recovery; it is not positive proof of the old process's death.
        delay = min(plan.agent.max_backoff_seconds, plan.agent.backoff_seconds
                    * (2 ** min(max(0, plan.attempt - 2), 32))) if result == "retry" else 0
        self.coordinator.release(recovery, result, f"Recovered {outcome['run']}: {summary}", delay)
        self.output(f"#{plan.item.number} {plan.agent.name}: recovered durable outcome; no execution started")
        return True

    def launch(self, once=False):
        while not self.stop_event.is_set():
            worked = self.tick()
            if once:
                return
            if not worked:
                self.output("Waiting for eligible GitHub work")
                self.stop_event.wait(self.config.poll_seconds)
        raise KeyboardInterrupt
