"""Execute an agent's merge decision only while the exact candidate is safe."""

from .errors import AgentError, GitHubError, LostOwnership, TransitionPaused
from .records import exhausted_rounds, outcome_declarations, seconds

CHECK_POLL_SECONDS = 10
MERGEABILITY_READS = 4
MERGEABILITY_POLL_SECONDS = 2


class MergeNotRecovered(TransitionPaused):
    """A new agent must judge an unmerged candidate; recovery never merges."""


class LauncherMerge:
    def __init__(self, loop):
        self.github = loop.github
        self.coordinator = loop.coordinator
        self.config = loop.config
        self.interrupt = loop.interrupt_event
        self.output = loop.output

    def current(self, lease, plan, outcome):
        self.github.refresh_finalization()
        self.coordinator.assert_owned(lease)
        current = self.github.item(plan.item.number, "pr")
        if current.head != outcome["candidate_sha"]:
            refusal = MergeNotRecovered if lease.get("mode") == "recovery" else TransitionPaused
            raise refusal("Merge refused: PR head moved from the reported candidate SHA")
        return current

    def open_candidate(self, current, plan):
        if current.state != "open" or current.merged:
            raise TransitionPaused("Merge refused: PR is not open")
        stops = current.labels.intersection(self.config.stop_labels)
        if stops:
            raise TransitionPaused("Merge refused: stop label present: " + ", ".join(sorted(stops)))
        if not current.labels.intersection(plan.agent.triggers):
            raise TransitionPaused("Merge refused: assignment trigger disappeared")

    def confirm(self, current, sha):
        commit = current.merge_commit_sha
        return (current.merged and current.state == "closed" and current.head == sha
                and isinstance(commit, str) and bool(commit) and current.base_branch
                and self.github.commit_on_base(commit, current.base_branch))

    def wait(self, lease, delay, deadline, reason):
        remaining = deadline - self.coordinator.clock()
        if remaining <= 0:
            raise TransitionPaused(f"Merge refused: {reason} at the agent-timeout deadline")
        self.coordinator.deadline(lease)
        self.output(f"#{lease['assignment']}: waiting for {reason}; no agent is running")
        if self.interrupt.wait(min(delay, remaining)):
            raise KeyboardInterrupt

    def route_failure(self, lease, plan, outcome, reason):
        name = plan.agent.on_conflict
        if not name:
            raise TransitionPaused(f"Merge refused: {reason}; no on-conflict outcome is configured")
        transition = outcome_declarations(plan.agent)[name] | {"started": False}
        changes = {}
        if exhausted_rounds(lease, name):
            transition["add"] = [self.config.stop_labels[0]]
            changes["actions"] = ["Maintainer: review the open findings and decide whether to allow more rounds."]
        self.coordinator.update_outcome(lease, outcome, outcome=name, transition=transition,
                                        summary=f"Merge refused for {outcome['candidate_sha']}: {reason}", **changes)

    def prepare(self, lease, plan, outcome, deadline=None):
        sha = outcome["candidate_sha"]
        transition = outcome["transition"]
        if plan.item.kind != "pr" or outcome.get("handoff"):
            raise TransitionPaused("Merge refused: merge requires a PR assignment without a handoff")
        if set(transition["add"]).intersection(self.config.stop_labels):
            raise TransitionPaused("Merge refused: merge outcome adds a stop label")
        current = self.current(lease, plan, outcome)
        if sha != plan.item.head:
            parents = self.github.commit_parents(sha)
            if len(parents) < 2 or parents[0] != plan.item.head:
                raise TransitionPaused("Merge refused: reported head is neither the assigned candidate nor its base merge")
        if lease.get("mode") == "recovery":
            if not self.confirm(current, sha):
                raise MergeNotRecovered("Merge recovery refused: PR is not confirmed merged from the reported head; "
                                        "a new integrator run must evaluate the candidate")
        else:
            deadline = deadline if deadline is not None else seconds(lease["created"]) + plan.agent.timeout_seconds
            unknown_reads = 0
            while True:
                self.open_candidate(current, plan)
                checks = self.github.commit_checks(sha)
                failed = [name for name, state in checks if state == "fail"]
                if failed:
                    self.route_failure(lease, plan, outcome, "failing checks: " + ", ".join(failed))
                    return
                pending = [name for name, state in checks if state == "pending"]
                if not checks or pending:
                    reason = "pending checks: " + ", ".join(pending) if pending else "checks to be reported"
                    self.wait(lease, CHECK_POLL_SECONDS, deadline, reason)
                    current = self.current(lease, plan, outcome)
                    continue
                current = self.current(lease, plan, outcome)
                self.open_candidate(current, plan)
                if current.mergeable is not None:
                    break
                unknown_reads += 1
                if unknown_reads == MERGEABILITY_READS:
                    raise TransitionPaused("Merge refused: mergeability is still unknown")
                self.wait(lease, MERGEABILITY_POLL_SECONDS, deadline, "mergeability")
                # CI can be rerun while GitHub computes mergeability. Every
                # wait returns through the check gate before authorizing merge.
                current = self.current(lease, plan, outcome)
            if current.mergeable is False:
                self.route_failure(lease, plan, outcome, "PR conflicts with its base branch")
                return
            if self.coordinator.clock() >= deadline:
                raise TransitionPaused("Merge refused: agent-timeout deadline reached before merging")
            # This is the only merge mutation. A lost response is resolved by a
            # read, never by replaying the command, including during recovery.
            self.coordinator.assert_owned(lease)
            error = None
            try:
                self.github.merge_pr(current.number, transition["merge"], sha)
            except GitHubError as exc:
                error = exc
            current = self.current(lease, plan, outcome)
            if not self.confirm(current, sha):
                raise TransitionPaused(f"Merge refused: merge was not confirmed from {sha} on the base branch"
                                       + (f"; {error}" if error else ""))
        if outcome.get("merge_commit_sha") != current.merge_commit_sha or not transition["started"]:
            summary = outcome["summary"]
            if outcome.get("merge_commit_sha") != current.merge_commit_sha:
                summary += f"\n\nMerged head {sha}; merge commit {current.merge_commit_sha}."
            # The merge is irreversible and confirmed. Mark validation complete
            # in the same write as its receipt, before replayable label changes.
            self.coordinator.update_outcome(lease, outcome, merge_commit_sha=current.merge_commit_sha,
                                            transition=transition | {"started": True}, summary=summary)

    def close_issues(self, lease, number):
        # Issue closure follows acceptance and cannot change its verdict.
        try:
            issues = self.github.closing_issue_references(number)
        except LostOwnership:
            raise
        except AgentError as exc:
            self.output(f"#{number}: merged; could not read closing issues: {exc}")
            return
        for issue in issues:
            if issue.state != "open":
                continue
            self.coordinator.assert_owned(lease)
            try:
                self.github.close_issue(issue.number, issue.repository)
            except LostOwnership:
                raise
            except AgentError as exc:
                self.output(f"#{number}: merged; could not close {issue.reference(self.config.repository)}: {exc}")
