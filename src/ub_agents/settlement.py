"""Validate assignment reports, apply transitions and release owned runs."""

from .errors import TransitionPaused, ValidationError
from .github import links_issue
from .labels import OUTSIDE_INPUT
from .records import backoff, iso, lease_by_id, resolve_transition, same_run, validate_report_action
from .run_branches import earlier_branches
from .run_usage import usage_fields


class Settlement:
    def __init__(self, config, github, coordinator, approvals):
        self.config = config
        self.github = github
        self.coordinator = coordinator
        self.approvals = approvals

    def reported_verdict(self, lease, plan, outcome, source, what):
        """A recorded report's (result, summary, attempt effect); raises ValidationError if rejected."""
        if outcome["status"] != "success":
            self.validate_failure_report(lease, outcome, source)
            return outcome["status"], outcome["summary"], "unchanged" if outcome["status"] == "blocked" else "failure"
        self.finalize(lease, plan, outcome, what)
        return "success", outcome["summary"], "reset"

    def settle(self, plan, lease, attempt, result, summary, effect, outcome=None, diagnostics=None,
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
        if diagnostics and any(outcome.get(key) != value for key, value in diagnostics.items()):
            self.coordinator.update_outcome(lease, outcome, **diagnostics)
        self.coordinator.release(lease, result, summary, delay, attempt_effect=effect,
                                 parking_outcome=parking_outcome, max_attempts=plan.agent.max_attempts,
                                 unreported=unreported, stop_labels=self.config.stop_labels, triggers=plan.agent.triggers,
                                 **usage_fields(diagnostics or {}))

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
            source = lease_by_id(self.coordinator.history(outcome["assignment"]), outcome["lease_id"])
            for branch in earlier_branches(self.coordinator, plan.item, plan.agent, source["run"]):
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
        transition = outcome.get("transition")
        if not source or not same_run(outcome, source):
            raise ValidationError("Reported outcome does not match its source lease")
        if not outcome.get("outcome") or transition is None:
            raise ValidationError("Success report must name a declared outcome")
        agent = next((agent for agent in self.config.agents if agent.name == outcome["agent"]), None)
        if agent is None:
            raise ValidationError("Reported agent is no longer configured")
        transition = resolve_transition(transition, agent.triggers, self.config.stop_labels)
        if "sub-issues" in transition and "sub_issues" not in outcome:
            raise ValidationError("Sub-issue targets were not resolved in the outcome record")
        try:
            validate_report_action(source, outcome, self.config.stop_labels)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return transition

    def validate_failure_report(self, lease, outcome, source):
        try:
            if outcome.get("rejected"):
                raise ValueError(outcome["rejected"])
            validate_report_action(source, outcome, self.config.stop_labels)
        except ValueError as exc:
            self.coordinator.update_outcome(lease, outcome, rejected=str(exc), attempt_effect="failure")
            raise ValidationError(str(exc)) from exc

    def apply_transition(self, lease, outcome):
        transition = self.validate_report(outcome)
        self.coordinator.assert_owned(lease)
        assignment = self.github.item(outcome["assignment"])
        copied_labels = ({OUTSIDE_INPUT} if self.approvals == "on"
                         and OUTSIDE_INPUT in {label.casefold() for label in assignment.labels}
                         else set())
        target = outcome.get("handoff") or outcome["assignment"]
        destination = self.github.item(target)
        if not transition["started"]:
            stops = set(self.config.stop_labels)
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
        missing = sorted((set(transition["add"]) | copied_labels).difference(self.github.item(target).labels))
        if missing:
            self.github.add_labels(target, missing)
        for number in outcome.get("sub_issues", ()):
            self.coordinator.assert_owned(lease)
            missing = sorted((set(transition["sub-issues"]["add"]) | copied_labels)
                             .difference(self.github.item(number).labels))
            if missing:
                self.github.add_labels(number, missing)
        if not outcome.get("transition_complete"):
            self.coordinator.update_outcome(lease, outcome, transition_complete=True)
