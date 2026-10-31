"""Cooperative leases and durable attempts, deliberately not an atomic lock service."""

from dataclasses import dataclass, field, replace
import re
import shutil
import threading
import uuid

from .approvals import ApprovalCheck
from .config import Agent, Queue, Runtime, LEASE_SECONDS, LEASE_RENEW_SECONDS, LEASE_RENEW_MARGIN_SECONDS
from .eligibility import AgentMatches, check_start, open_blockers
from .errors import AgentError, ExecutionStopped, GitHubError, LostOwnership, RecordError, RuntimePaused
from .finalization import FinalizationReads
from .github import Item, REQUEST_TIMEOUT_SECONDS
from .notices import Notices
from .records import (MARKER, attempt_effect, attempts, backoff, body, display_provenance, exhausted_rounds,
                      iso, issue_branch, latest_leases, lease_by_id, lease_summary, live_leases,
                      outcome_declarations, outcome_rounds, payload, records, recovers, report_asks,
                      same_handoff, same_run, seconds, stop_report, timestamp)
from .trust import LauncherTrust
from .run_usage import usage_fields


@dataclass(frozen=True)
class Plan:
    item: Item
    agent: Agent
    runtime: Runtime | None
    state: str
    reason: str
    attempt: int
    priority: str | None = None
    priority_source: int | None = None
    priority_from_issue: int | None = None
    milestone: int | None = None
    milestone_source: int | None = None
    milestone_rank: int = 0
    blockers: tuple[str, ...] = ()
    approval_gate: ApprovalCheck | None = None
    history: tuple[dict, ...] = field(default=(), compare=False, repr=False)
    history_read: bool = field(default=True, compare=False, repr=False)
    owner: dict | None = field(default=None, compare=False, repr=False)
    matches: AgentMatches | None = field(default=None, compare=False, repr=False)
    health_wait: bool = False
    paused_clis: tuple[str, ...] = ()
    # Display-only GitHub comment, never coordination input.
    attention_notice: dict | None = field(default=None, compare=False, repr=False)


class Coordinator:
    def __init__(self, github, actor, clock=timestamp, queue=Queue(), output=print, on_claim=None,
                 runtime_available=None, runtime_paused=None, launchers=None, role=None, on_record=None,
                 on_author=None, on_action=None, trusted_bots=(), on_decline=None,
                 lease_seconds=LEASE_SECONDS, lease_renew_seconds=LEASE_RENEW_SECONDS, trusted_pr_authors=(),
                 on_release=None):
        self.github = github
        self.actor = actor
        self.trust = LauncherTrust(github, launchers, role, on_author, trusted_bots=trusted_bots,
                                  trusted_pr_authors=trusted_pr_authors)
        self.output = output
        self.clock = clock
        self.queue = queue
        self.lease_seconds = lease_seconds
        self.lease_renew_seconds = lease_renew_seconds
        self.on_claim = on_claim
        self.on_release = on_release
        self.cleanup_control = None
        self.on_decline = on_decline
        self.on_record = on_record
        self.runtime_available = runtime_available
        self.runtime_paused = runtime_paused or (lambda cli: None)
        self.notices = Notices(github, actor, output, trusted=self.trust, on_action=on_action)
        # Renewal and the supervising launcher's state edits share one writer.
        # Never hold this lock over a rate-limit wait.
        self._lease_lock = threading.RLock()
        self._lease_revision = 0
        self._lost = {}
        self._renewal_failures = {}
        self._stop_labels = {}
        self._execution_stops = {}
        # A failed POST may have created this record without returning its ID.
        # Keep only this launcher's uncertain claim; restarts use normal expiry.
        self.lost_claim = None
        # In-process reports share claim configuration; agent commands use run.json.
        self._report_context = {}
        # Display callbacks join provenance from reads already needed for coordination.
        self._observed_leases = {}

    def observed(self, record):
        if self.on_record is not None:
            if record["kind"] == "lease":
                self._observed_leases[record["id"]] = record
            self.on_record(display_provenance(record, self._observed_leases.values()))

    def history(self, number):
        if isinstance(self.github, FinalizationReads):
            return self.github.read_history(number, lambda: self._history(number))
        return self._history(number)

    def _history(self, number):
        comments = self.github.comments(number)
        try:
            history = records(comments, trusted=self.trust.observation())
            if self.on_record is not None:
                self._observed_leases.update((r["id"], r) for r in history if r["kind"] == "lease")
            return history
        except (RecordError, GitHubError):
            raise
        except AgentError as exc:
            raise GitHubError("GET", f"repos/{self.github.repository}/issues/{number}/comments", str(exc)) from exc

    def feedback(self, item, agent):
        """Other agents' accepted outcomes since this agent last handled each item."""
        history = self.history(item.number)
        histories = [history]
        if item.kind == "issue":
            handoffs = sorted({r["handoff"] for r in history if r["kind"] == "outcome"
                               and r["accepted"] and not r.get("rejected") and r.get("handoff")})
            histories.extend(self.history(number) for number in handoffs if number != item.number)
        pending = []
        for history in histories:
            accepted = [r for r in history if r["kind"] == "outcome"
                        and r["accepted"] and not r.get("rejected")]
            cutoff = max((r["id"] for r in accepted if r["agent"] == agent), default=0)
            pending.extend(r for r in accepted if r["id"] > cutoff and r["agent"] != agent)
        seen, feedback = set(), []
        for record in sorted(pending, key=lambda r: r["id"]):
            # A handoff copy has a different comment ID but belongs to the same run.
            key = (record["assignment"], record["agent"], record["lease_id"])
            if key in seen:
                continue
            seen.add(key)
            feedback.append({"agent": record["agent"],
                             "outcome": record.get("outcome", record["status"]),
                             "summary": record["summary"], "candidate_sha": record["candidate_sha"],
                             "created": record["created"]})
        return feedback

    def repository_history(self, comments=None, by_item=False):
        groups = {}
        trusted = self.trust.observation()
        for comment in self.github.repository_comments() if comments is None else comments:
            if not isinstance(comment, dict):
                raise GitHubError("GET", f"repos/{self.github.repository}/issues/comments",
                                  "Unreadable repository comment")
            if not isinstance(comment.get("body"), str) or not comment["body"].startswith(MARKER):
                continue
            if not trusted(comment.get("user")):
                continue
            try:
                number = int(comment["issue_url"].rsplit("/", 1)[1])
            except (KeyError, ValueError, AttributeError, IndexError) as exc:
                raise GitHubError("GET", f"repos/{self.github.repository}/issues/comments",
                                  "Coordination comment has no GitHub assignment URL") from exc
            groups.setdefault(number, []).append(comment)
        history, invalid, histories = [], set(), {}
        for number, comments in groups.items():
            try:
                histories[number] = records(comments)
                history.extend(histories[number])
            except RecordError:
                invalid.add(number)
        result = sorted(history, key=lambda record: record["id"]), invalid
        return (*result, histories) if by_item else result

    def plan(self, item, agent, stop_labels, history=None, start=None, matches=None):  # noqa: C901, PLR0912, PLR0915
        history = self.history(item.number) if history is None else history
        matches = matches or AgentMatches.for_item(item, (agent,))
        start = start or check_start(item, agent, matches, stop_labels, self.queue)
        now = self.clock()
        previous = attempts(history, agent.name, now)
        latest = [r for r in latest_leases(history).values() if r["agent"] == agent.name]
        finished = [r for r in latest if r["state"] == "released"
                    or (r.get("result") and r.get("attempt_effect") in {"failure", "unchanged"})]
        attempt = len(previous) + 1
        state, reason = "ready", "Trigger matched"
        runtime = None
        paused_clis = ()
        owner = None
        owners = live_leases(history, now)
        if owners:
            owner = owners[0]
            state, reason = "owned", "An unexpired assignment owns this work item"
        elif self.pending_completion(history, agent.name, now):
            state, reason = "recover", "An expired run has an explicit outcome to validate without reexecution"
        elif latest and latest[-1].get("cleanup") == "unconfirmed":
            state, reason = "blocked", "Previous cleanup was unconfirmed; establish termination before an operator reset"
        elif (owner := self.shared_branch_owner(item, agent, history)) is not None:
            if owner.get("cleanup") == "unconfirmed":
                state, reason = "blocked", (f"Cleanup of #{owner['assignment']}, which shares this item's branch, "
                                            "was unconfirmed; establish termination before an operator reset")
            else:
                state, reason = "owned", f"A live run on #{owner['assignment']} owns this item's branch"
        elif start.stop_reason:
            outcomes = [r for r in history if r["kind"] == "outcome" and r["agent"] == agent.name]
            summary = lease_summary(history, latest[-1]) if latest else (outcomes[-1]["summary"] if outcomes else "")
            state, reason = "parked", start.stop_reason + (f": {summary}" if summary else "")
        elif (finished and finished[-1].get("result") == "blocked"
              and not self.head_moved_since_block(item, history, finished[-1])):
            state, reason = "blocked", (f"Last run blocked: {lease_summary(history, finished[-1])}; "
                                        f"inspect outcome and use ub-agents retry {item.number} "
                                        f"--agent {agent.name} --reason TEXT")
        elif attempt > agent.max_attempts:
            state, reason = "blocked", ("Attempt limit exhausted; inspect failures and use "
                                        f"ub-agents retry {item.number} --agent {agent.name} --reason TEXT")
        elif finished and seconds(finished[-1].get("retry_after", finished[-1]["expires"])) > now:
            state, reason = "backoff", "Durable retry backoff has not elapsed"
        elif (latest and latest[-1].get("attempt_effect") == "pending" and previous
              and latest[-1]["id"] == previous[-1]["id"]
              and seconds(latest[-1]["expires"]) + backoff(agent, len(previous)) > now):
            state, reason = "backoff", "Expired run has no outcome; durable retry backoff has not elapsed"
        else:
            try:
                runtime = self.choose_runtime(item, agent, history)
            except GitHubError:
                raise  # A failed provenance read invalidates the whole discovery poll.
            except RuntimePaused as exc:
                state, reason = "waiting", str(exc)
                paused_clis = exc.clis
            except AgentError as exc:
                state, reason = "blocked", str(exc)
        if state in {"ready", "recover"} and self.actor is not None:
            if untrusted := self.trust.reason(self.actor):
                state, reason, runtime = "blocked", untrusted, None
        return Plan(item, agent, runtime, state, reason, attempt, owner=owner, matches=matches,
                    paused_clis=paused_clis)

    @staticmethod
    def head_moved_since_block(item, history, lease):
        if item.kind != "pr":
            return False
        # Recovery settles the source run's outcome without copying it to its lease.
        source = lease_by_id(history, lease.get("recovered_lease_id", lease["id"])) or lease
        blocked_sha = next((r["candidate_sha"] for r in history
                            if r["kind"] == "outcome" and r["lease_id"] == source["id"]
                            and same_run(r, source)), lease["assignment_sha"])
        return item.head != blocked_sha

    def choose_runtime(self, item, agent, history):
        if agent.command:
            if not shutil.which(agent.command[0]):
                raise AgentError(f"Command is not installed: {agent.command[0]}")
            return None
        eligible = list(agent.runtimes)
        if agent.different_from:
            sources = [runtime for name in agent.different_from
                       if (runtime := self.source_runtime(item, name, history)) is not None]
            if not sources:
                eligible = eligible[:1]
            else:
                eligible = [r for r in eligible if all(r.different_from(source) for source in sources)]
                if not eligible:
                    raise AgentError("No runtime has a different CLI and model from the candidate author")
        paused = {}
        for runtime in eligible:
            if (self.runtime_available or shutil.which)(runtime.cli):
                pause = self.runtime_paused(runtime.cli)
                if pause:
                    paused[runtime.cli] = pause
                    continue
                return runtime
        if paused:
            raise RuntimePaused("Waiting for " + "; ".join(
                f"{cli}: {pause['reason']}; pause ends {pause['ends_at']}" for cli, pause in paused.items()),
                clis=tuple(paused))
        reason = ("No eligible runtime executable is installed, usable and available"
                  if self.runtime_available else "No eligible runtime executable is installed")
        raise AgentError(reason)

    def source_runtime(self, item, name, history):
        reports = [r for r in history if r["kind"] == "outcome" and r["status"] == "success"
                   and r["agent"] == name
                   and (r.get("candidate_sha") == item.head if item.kind == "pr"
                        else r["assignment"] == item.number)]
        accepted = [r for r in reports if r.get("accepted") is True]
        source = accepted[-1] if accepted else None
        for report in reports:
            # A newer issue revision can start routing while the previous accepted
            # outcome still exists. Wait for the revision to settle before using it.
            if source and (item.kind == "pr" or report["id"] <= source["id"]):
                continue
            if (not report.get("rejected") and report.get("transition", {}).get("started")
                    and self.handoff_pending(report)):
                raise AgentError(f"Candidate handoff from {name} is still pending")
        if source is None:
            return None
        origin = self.history(source["assignment"])
        lease = lease_by_id(origin, source["lease_id"])
        if lease and not same_handoff(source, lease):
            raise AgentError("Candidate provenance does not match its source lease")
        if not self.released_success(origin, source):
            raise AgentError("Candidate provenance has no successfully released source lease")
        try:
            cli, model, effort = lease["runtime"].split(":")
        except ValueError as exc:
            raise AgentError("Candidate runtime provenance is incomplete") from exc
        return Runtime(cli, model, effort)

    def handoff_pending(self, report):
        # A copied marker may lag acceptance or rejection on the source item.
        # Expiry alone does not abandon it: recovery must finish the handoff.
        origin = self.history(report["assignment"])
        lease = lease_by_id(origin, report["lease_id"])
        if lease is None or not same_handoff(report, lease):
            return False
        if any(r["kind"] == "reset" and r["agent"] == report["agent"] and r["id"] > lease["id"]
               for r in origin):
            return False
        outcome = next((r for r in origin if r["kind"] == "outcome" and r["lease_id"] == lease["id"]
                        and same_run(r, lease)), None)
        return bool(outcome and outcome["status"] == "success" and not outcome.get("rejected")
                    and outcome.get("transition", {}).get("started")
                    and attempt_effect(origin, lease, self.clock()) in {"pending", "reset"})

    @staticmethod
    def released_success(history, outcome):
        source = lease_by_id(history, outcome["lease_id"])
        if source is None or source.get("cleanup") == "unconfirmed" or not same_handoff(outcome, source):
            return False
        return ((source["state"] == "released" and source.get("result") == "success")
                or any(r["kind"] == "lease" and r["state"] == "released" and r.get("result") == "success"
                       and recovers(r, source) and r.get("cleanup") != "unconfirmed" for r in history))

    def pending_completion(self, history, agent, now, allow_live=False):
        latest = [r for r in latest_leases(history).values() if r["agent"] == agent]
        if not latest:
            return None
        lease = latest[-1]
        if lease.get("result") and lease.get("attempt_effect") in {"failure", "unchanged"}:
            return None  # A supervised verdict already superseded any early report.
        if lease["state"] not in {"running", "claiming"} or (not allow_live and seconds(lease["expires"]) > now):
            return None
        source_id = lease.get("recovered_lease_id", lease["id"])
        source = lease_by_id(history, source_id)
        if source is None or source["state"] not in {"running", "claiming"} or source.get("cleanup") == "unconfirmed":
            return None
        if source.get("result") and source.get("attempt_effect") in {"failure", "unchanged"}:
            return None
        matches = [r for r in history if r["kind"] == "outcome" and r["lease_id"] == source_id
                   and same_run(r, source)
                   and seconds(source["created"]) <= seconds(r["created"]) <= seconds(source["expires"])]
        if len(matches) > 1:
            raise RecordError("Expired run reported conflicting outcomes; inspect GitHub before resetting")
        return matches[0] if matches else None

    def shared_branch_owner(self, item, agent, history):
        """A live lease, or an unconfirmed cleanup, on another item that shares this item's branch.

        An issue's earlier run branches may carry an open draft PR that a PR-kind run can
        own, and a PR's branch may belong to an issue whose run is continuing it. One agent
        at a time touches a branch, so either side waits for the other."""
        if item.kind == "issue":
            branches = sorted({issue_branch(r) for r in history if r["kind"] == "lease"
                               and r["agent"] == agent.name and r["started"] and r.get("mode") != "recovery"})
            related = sorted({pr.number for branch in branches for pr in self.github.prs_for_branch(branch)})
        else:
            match = re.fullmatch(r"ub-agents/[a-z][a-z0-9_-]*/([1-9][0-9]*)/[A-Za-z0-9_-]+", item.branch or "")
            related = [int(match[1])] if match else []
        now = self.clock()
        for number in related:
            other = self.history(number)
            live = live_leases(other, now)
            if live:
                return live[0]
            unconfirmed = [r for r in latest_leases(other).values() if r.get("cleanup") == "unconfirmed"]
            if unconfirmed:
                return unconfirmed[0]
        return None

    def decline(self, plan, reason, *, fresh=None):
        if self.on_decline is not None:
            self.on_decline(fresh or plan, reason)
        else:
            self.output(f"#{plan.item.number} {plan.agent.name}: declined — {reason}")
        return None

    def claim(self, plan, stop_labels=(), recovery=False, before_write=None, authorize=None,  # noqa: C901, PLR0912, PLR0915
              confirm_stopped=None, on_refusal=None):
        reason = self.trust.reason(self.actor)
        if reason:
            if on_refusal is not None:
                on_refusal(plan, reason, "blocked")
            else:
                self.output(f"{reason}; claiming no work")
            return None
        # Reobserve state immediately before claiming. This also handles a label/head
        # changing after queue enumeration, without charging an attempt.
        current = self.github.item(plan.item.number, plan.item.kind)
        if current.head != plan.item.head:
            return self.decline(plan, "Head moved before claim")
        matches = AgentMatches.for_item(current, plan.matches.configured if plan.matches else (plan.agent,))
        if not recovery:
            start = check_start(current, plan.agent, matches, stop_labels, self.queue)
            # Stop labels still reach durable planning: its history reads and
            # precedence for ownership and pending outcomes must stay intact.
            if not start.allowed and start.reason != start.stop_reason:
                return self.decline(plan, f"Start gate: {start.reason}",
                                    fresh=replace(plan, item=current, state="parked", reason=start.reason,
                                                  matches=matches, history=(), owner=None))
        history = self.history(current.number)
        if recovery:
            outcome = self.pending_completion(history, plan.agent.name, self.clock())
            if outcome is None or any(r["id"] != outcome["lease_id"]
                                      for r in live_leases(history, self.clock())):
                return self.decline(plan, "Recovery outcome no longer matches")
        else:
            fresh = self.plan(current, plan.agent, stop_labels, history, start=start, matches=matches)
            if fresh.state != "ready":
                return self.decline(plan, f"Fresh plan is no longer ready: {fresh.state} — {fresh.reason}",
                                    fresh=replace(fresh, history=tuple(history)))
            if fresh.runtime != plan.runtime:
                return self.decline(plan, "Runtime changed before claim")
            blockers = (open_blockers(self.github, current) if current.kind == "issue"
                        and self.queue.dependencies == "wait" else ())
            if not check_start(current, plan.agent, matches, stop_labels, self.queue, blockers).allowed:
                return self.decline(plan, f"Open blockers: {', '.join(blockers)}")
        if authorize is not None and not authorize(current, matches):
            return None
        if confirm_stopped is not None:
            confirm_stopped(history)
        now = self.clock()
        record = {"kind": "lease", "run": uuid.uuid4().hex,
                  "agent": plan.agent.name, "assignment": current.number,
                  "assignment_sha": current.head,
                  "runtime": plan.runtime.name if plan.runtime else "direct",
                  "triggers": sorted(current.labels.intersection(plan.agent.triggers)),
                  "actor": self.actor, "created": iso(now),
                  "expires": iso(now + plan.agent.lease_seconds), "state": "claiming",
                  "attempt": len(attempts(history, plan.agent.name, now)) + 1, "started": False,
                  "attempt_effect": "pending"}
        if not recovery:
            rounds = {name: {"used": outcome_rounds(history, current.number, plan.agent.name, name),
                             "limit": changes["max-rounds"]}
                      for name, changes in plan.agent.outcomes.items() if "max-rounds" in changes}
            if rounds:
                record["outcome_rounds"] = rounds
        if recovery:
            record |= {"mode": "recovery", "recovered_lease_id": outcome["lease_id"],
                       "recovered_run": lease_by_id(history, outcome["lease_id"])["run"]}
        if self.cleanup_control is not None:
            self.cleanup_control.protect(record, branch=current.branch if current.kind == "pr" else issue_branch(record))
        if before_write is not None:
            before_write()
        try:
            comment = self.github.create_comment(current.number, body(record))
        except GitHubError as exc:
            if exc.retryable:
                self.lost_claim = record
            raise
        created = records([comment], self.actor)[0]
        self._report_context[created["id"]] = ({} if recovery else outcome_declarations(plan.agent), tuple(stop_labels), tuple(plan.agent.triggers))
        if not recovery:
            self._stop_labels[created["id"]] = tuple(stop_labels)
        self.observed(created)
        if self.on_claim is not None:
            self.on_claim(created)
        contenders = live_leases(self.history(current.number), self.clock())
        # Earliest GitHub comment id wins. Each contender has its own record; no
        # read-modify-write race on a shared lease comment is passed off as CAS.
        if not contenders or contenders[0]["id"] != created["id"]:
            self.update(created, state="withdrawn", summary="Lost the cooperative claim election.")
            self.notices.election_lost(created)
            self.notices.resumed(current.number)
            return self.decline(plan, "Claim election lost",
                                fresh=replace(plan, state="owned" if contenders else "declined", reason="Claim election lost",
                                              history=tuple(contenders), owner=contenders[0] if contenders else None))
        if not recovery:
            # Across an issue and a PR on its branch, the lowest live comment id wins too.
            owner = self.shared_branch_owner(current, plan.agent, self.history(current.number))
            if owner is not None and (owner.get("cleanup") == "unconfirmed" or owner["id"] < created["id"]):
                self.update(created, state="withdrawn", summary="Lost the shared-branch election.")
                self.notices.election_lost(created)
                self.notices.resumed(current.number)
                return self.decline(plan, "Claim election lost on shared branch",
                                    fresh=replace(plan, state="blocked" if owner.get("cleanup") == "unconfirmed" else "owned",
                                                  reason="Claim election lost on shared branch",
                                                  history=(), owner=owner))
        if self.clock() >= seconds(created["expires"]):
            raise LostOwnership("Lease expired during claiming")
        self.notices.resumed(current.number)
        return created

    def withdraw_lost_claim(self):
        """Resolve an uncertain claim POST before any new assignment planning."""
        if self.lost_claim is None:
            return None
        record = self.lost_claim
        history = self.history(record["assignment"])
        lease = next((r for r in history if r["kind"] == "lease" and same_run(r, record)), None)
        if lease is not None and lease["state"] != "withdrawn":
            if lease["state"] != "claiming" or lease.get("started"):
                raise LostOwnership("Lost-response claim changed before withdrawal")
            self.update(lease, state="withdrawn", attempt_effect="unchanged",
                        summary="Claim response was lost; withdrawn")
        # Retain the record if either the read or withdrawal failed. A lost PATCH
        # response is resolved by reading the same comment on the next pass.
        self.lost_claim = None
        return record["assignment"]

    def update(self, lease, **changes):
        with self._lease_lock:
            if changes.get("state") != "withdrawn" and lease["state"] in {"claiming", "running"}:
                self.deadline(lease)
            updated = payload(lease) | changes
            if changes.get("state") in {"released", "withdrawn"}:
                updated["expires"] = iso(self.clock())
            elif updated["state"] in {"claiming", "running"}:
                updated["expires"] = iso(max(seconds(lease["expires"]), seconds(updated["expires"])))
            # Even a failed write may have reached GitHub. A renewal read that
            # overlapped it cannot supply a safe payload for a later PATCH.
            self._lease_revision += 1
            result = records([self.github.update_comment(lease["id"], body(updated))])[0]
            lease.clear()
            lease.update(result)
        self.observed(lease)
        return lease

    def deadline(self, lease):
        """The last confirmed expiry, including sleeping-machine wall-clock loss."""
        with self._lease_lock:
            expiry = seconds(lease["expires"])
            if lease["id"] in self._lost:
                reason = self._lost[lease["id"]]
                cause = self._renewal_failures.get(lease["id"]) if reason == "Local lease deadline expired" else None
                raise LostOwnership(reason) from cause
            if lease["state"] not in {"claiming", "running"}:
                raise LostOwnership("Lease was released or withdrawn")
            if self.clock() >= expiry:
                self._lost[lease["id"]] = "Local lease deadline expired"
                raise LostOwnership(self._lost[lease["id"]]) from self._renewal_failures.get(lease["id"])
            return expiry

    def _owned(self, lease, history):
        # The local deadline must pass before an observation can extend it: a
        # late response must not resurrect an expired or withdrawn local claim.
        self.deadline(lease)
        contenders = live_leases(history, self.clock())
        if not contenders or contenders[0]["id"] != lease["id"] or not same_run(contenders[0], lease):
            self._renewal_failures.pop(lease["id"], None)
            self._lost[lease["id"]] = "Assignment ownership was lost or expired"
            raise LostOwnership(self._lost[lease["id"]])
        current = contenders[0]
        if seconds(current["expires"]) > seconds(lease["expires"]):
            lease["expires"] = current["expires"]
        return current

    def observe_execution_item(self, lease, item):
        with self._lease_lock:
            if lease["id"] not in self._stop_labels:
                return
            stops = set(self._stop_labels[lease["id"]]).intersection(item.labels)
            if item.state != "open":
                kind = "Pull request" if item.kind == "pr" else "Issue"
                state = "merged" if item.kind == "pr" and item.merged else "closed"
                reason = f"{kind} {state}"
            elif stops:
                reason = f"Stop label {sorted(stops)[0]} added"
            else:
                return
            self._execution_stops.setdefault(lease["id"], (reason, item.state != "open"))

    def execution_deadline(self, lease):
        """Check cancellation while keeping ownership available for cleanup and release."""
        with self._lease_lock:
            expiry = self.deadline(lease)
            stop = self._execution_stops.get(lease["id"])
            if stop is not None:
                reason, item_closed = stop
                raise ExecutionStopped(f"{reason}; attributable execution terminated", item_closed=item_closed)
            return expiry

    def execution_finished(self, lease):
        """Ignore item reads that finish after the supervised process exits."""
        with self._lease_lock:
            self._stop_labels.pop(lease["id"], None)

    def assert_owned(self, lease):
        self.deadline(lease)
        try:
            history = self.history(lease["assignment"])
        except AgentError as exc:
            raise LostOwnership(f"Cannot establish ownership: {exc}") from exc
        with self._lease_lock:
            if isinstance(self.github, FinalizationReads):
                history = self.github.ownership_history(lease, history)
            return self._owned(lease, history)

    def renew(self, lease, github):
        """One best-effort renewal, using reads that never wait for rate limits."""
        with self._lease_lock:
            self.deadline(lease)
            # A PATCH is bounded to 20s. Reserve an extra second for scheduling
            # and timestamp precision, then check again after the ownership read.
            if self.clock() + REQUEST_TIMEOUT_SECONDS + LEASE_RENEW_MARGIN_SECONDS >= seconds(lease["expires"]):
                return False
            revision = self._lease_revision
            assignment = lease["assignment"]
            watch_stops = lease["id"] in self._stop_labels
        try:
            # Pagination/role reads may outlast the lease. Keep deadlines and
            # local state edits available while those bounded requests finish.
            if watch_stops:
                self.observe_execution_item(lease, github.item(assignment))
            trusted = LauncherTrust(github, self.trust.launchers, trusted_bots=self.trust.trusted_bots,
                                    trusted_pr_authors=self.trust.trusted_pr_authors).observation()
            history = records(github.comments(assignment), trusted=trusted)
            with self._lease_lock:
                self.deadline(lease)
                if revision != self._lease_revision:
                    return False
                current = self._owned(lease, history)
                now = self.clock()
                if now + REQUEST_TIMEOUT_SECONDS + LEASE_RENEW_MARGIN_SECONDS >= seconds(lease["expires"]):
                    return False
                updated = payload(current) | {"expires": iso(max(seconds(current["expires"]), now + self.lease_seconds))}
                result = records([github.update_comment(lease["id"], body(updated))])[0]
                # Only expiry belongs to renewal. Workspace preparation may have
                # a branch staged locally while a Git command is in progress.
                lease["expires"] = result["expires"]
                self._renewal_failures.pop(lease["id"], None)
                if isinstance(self.github, FinalizationReads):
                    self.github.renewed(lease)
                return True
        except LostOwnership:
            raise
        except AgentError as exc:
            with self._lease_lock:
                # Preserve request classification if failed renewals exhaust
                # the last confirmed lease, including in the supervising thread.
                if isinstance(exc, GitHubError):
                    self._renewal_failures[lease["id"]] = exc
                else:
                    self._renewal_failures.pop(lease["id"], None)
            self.output(f"Lease renewal failed; retry at the next interval: {exc}")
            return False

    def release(self, lease, result, summary, backoff=0, attempt_effect=None, parking_outcome=None,
                max_attempts=None, *, unreported=False, stop_labels=None, triggers=None, usage=None):
        self.assert_owned(lease)
        reported = self.outcome(lease)
        now = self.clock()
        if attempt_effect is None:
            attempt_effect = ("reset" if result == "success" and reported and reported["accepted"]
                              else "unchanged" if result == "blocked" and reported
                              and reported["status"] == "blocked" else "failure")
        # A released lease expires now; retry_after is only needed for a backoff.
        self.update(lease, state="released", result=result, summary=summary, expires=iso(now),
                    retry_after=iso(now + backoff) if backoff else None, attempt_effect=attempt_effect,
                    **usage_fields({"usage": usage}),
                    **({"unreported": True} if unreported else {}))
        with self._lease_lock:
            self.execution_finished(lease)
            self._execution_stops.pop(lease["id"], None)
        if self.on_release is not None:
            self.on_release(lease, parking_outcome or reported)
        _, claimed_stops, claimed_triggers = self._report_context.pop(lease["id"], ({}, (), ()))
        self.notices.advisory("released run comments", lambda:
                              self.notices.released(lease, reported, summary, parking_outcome, max_attempts,
                                  stop_labels=claimed_stops if stop_labels is None else stop_labels,
                                  triggers=claimed_triggers if triggers is None else triggers))

    def outcome(self, lease):
        matches = [r for r in self.history(lease["assignment"])
                   if r["kind"] == "outcome" and r["lease_id"] == lease["id"] and same_run(r, lease)]
        if len(matches) > 1:
            raise RecordError("Run reported conflicting outcomes")
        if matches:
            self.observed(matches[0])
        return matches[0] if matches else None

    def report(self, lease, status, summary, handoff=None, outcome=None, action=None, *, option=None, agent_report=True,  # noqa: C901, PLR0912
               declarations=None, stop_labels=None):
        if declarations is None:
            declarations, claimed_stops = self._report_context.get(lease["id"], ({}, (), ()))[:2]
            if stop_labels is None:
                stop_labels = claimed_stops
        stop_labels = stop_labels or ()
        transition = None
        if outcome is not None:
            if outcome not in declarations or status != "success":
                raise AgentError("Outcome is not declared by the running agent")
        elif status == "success" and declarations:
            raise AgentError("Success must name a declared outcome: report --outcome NAME")
        if outcome is not None:
            transition = declarations[outcome] | {"started": False}
            if exhausted_rounds(lease, outcome):
                transition = transition | {"add": [stop_labels[0]]}
                if action is None and option is None:
                    action = "Maintainer: review the open findings and decide whether to allow more rounds."
        try:
            actions, options = report_asks(action, option, required=agent_report and stop_report(status, transition, stop_labels))
        except ValueError as exc:
            raise AgentError(str(exc)) from exc
        self.assert_owned(lease)
        if self.outcome(lease):
            raise AgentError("This run already has an outcome")
        destination = self.github.item(handoff, "pr") if handoff else self.github.item(lease["assignment"])
        record = {k: lease[k] for k in ("agent", "assignment", "actor")}
        record |= {"kind": "outcome", "lease_id": lease["id"],
                   "created": iso(self.clock()), "status": status, "summary": summary,
                   "handoff": handoff, "candidate_sha": destination.head, "accepted": False}
        if actions:
            record["actions"] = actions
        if options:
            record["options"] = options
        if outcome is not None:
            record |= {"outcome": outcome, "transition": transition}
            declaration = transition
            if "sub-issues" in declaration:
                assignment = self.github.item(lease["assignment"])
                if assignment.kind != "issue":
                    raise AgentError("A sub-issues outcome requires an issue assignment")
                children, skipped = [], []
                for child in self.github.sub_issues(lease["assignment"]):
                    if (child.repository.casefold() == self.github.repository.casefold()
                            and child.state == "open" and child.author is not None
                            and child.author.casefold() == lease["actor"].casefold()
                            and seconds(child.created_at) > seconds(lease["created"])):
                        children.append(child.number)
                    else:
                        skipped.append(child.reference(self.github.repository))
                record["sub_issues"] = sorted(set(children))
                if skipped:
                    record["summary"] += "\n\nSub-issues left unchanged (only open, same-repository sub-issues created by " \
                                         "this run's launcher account after its claim qualify): " + ", ".join(sorted(set(skipped))) + "."
        self.assert_owned(lease)
        reported = records([self.github.create_comment(lease["assignment"], body(record))], self.actor)[0]
        self.observed(reported)
        return reported

    def update_outcome(self, lease, outcome, **changes):
        self.assert_owned(lease)
        updated = records([self.github.update_comment(outcome["id"], body(payload(outcome) | changes))])[0]
        outcome.clear()
        outcome.update(updated)
        self.observed(outcome)

    def accept(self, lease, outcome):
        self.assert_owned(lease)
        accepted = payload(outcome) | {"accepted": True}
        updated = records([self.github.update_comment(outcome["id"], body(accepted))])[0]
        outcome.clear()
        outcome.update(updated)
        self.observed(outcome)
        self.copy_handoff(lease, outcome)

    def copy_handoff(self, lease, outcome):
        """Publish or refresh the same provenance record before routing and accepting."""
        if outcome.get("handoff") and outcome["handoff"] != lease["assignment"]:
            copies = [r for r in self.history(outcome["handoff"]) if r["kind"] == "outcome"
                      and r["lease_id"] == outcome["lease_id"] and same_handoff(r, outcome)]
            self.assert_owned(lease)
            if copies:
                self.github.update_comment(copies[-1]["id"], body(payload(outcome)))
            else:
                self.github.create_comment(outcome["handoff"], body(payload(outcome)))
