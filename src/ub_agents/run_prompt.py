"""Pure builders for the run's prompt, assignment context and child environment."""

import json

from .eligibility import AgentMatches
from .execution import fork_head_ref
from .labels import OUTSIDE_INPUT
from .prompts import CONTINUATION_PROMPT, FORK_PR_PROMPT, RETROSPECTIVE_PROMPT, RUN_PROMPT
from .records import outcome_declarations


def run_context(repository, plan, lease, snapshot, *, feedback, scratch, report_command,
                branch, earlier_branches):
    context = {"repository": repository, "assignment": plan.item.number,
               "kind": plan.item.kind, "title": snapshot["title"],
               "body": snapshot["body"], "comments": snapshot["comments"],
               "withheld_counts": snapshot["withheld_counts"],
               "feedback": feedback,
               "candidate_sha": plan.item.head, "run": lease["run"],
               "scratch": str(scratch),
               "report_command": report_command,
               "agent": plan.agent.name, "branch": branch,
               "earlier_branches": earlier_branches}
    if "outcome_rounds" in lease:
        context["outcome_rounds"] = lease["outcome_rounds"]
    if plan.item.kind == "pr":
        context |= {name: snapshot[name] for name in ("reviews", "review_comments")}
    return context


def run_environment(environment, context, lease_id, context_path, run_config_path):
    env = {key: value for key, value in environment.items()
           if not key.startswith("UB_AGENTS_")}
    env.update({"UB_AGENTS_REPOSITORY": context["repository"],
                "UB_AGENTS_ASSIGNMENT": str(context["assignment"]),
                "UB_AGENTS_RUN": context["run"], "UB_AGENTS_LEASE_ID": str(lease_id),
                "UB_AGENTS_CONTEXT": str(context_path),
                "UB_AGENTS_RUN_CONFIG": str(run_config_path),
                "UB_AGENTS_REPORT": context["report_command"],
                "UB_AGENTS_SCRATCH": context["scratch"],
                "UB_AGENTS_CANDIDATE_SHA": context["candidate_sha"] or "",
                "UB_AGENTS_BRANCH": context["branch"] or ""})
    return env


def run_prompt(config, approvals, plan, lease, context, instructions, *, shared_instructions=""):
    earlier = context["earlier_branches"]
    report_command = context["report_command"]
    retrospective = (RETROSPECTIVE_PROMPT.format(report_command=report_command)
                     if plan.agent.retrospectives is not None else "")
    continuation = (CONTINUATION_PROMPT.format(earlier_branches=json.dumps(earlier)) if earlier else "")
    matches = plan.matches
    if matches is None or matches.configured != config.agents:
        matches = AgentMatches.for_item(plan.item, config.agents)
    workflow_labels = set(config.stop_labels).union(matches.workflow_triggers)
    if approvals == "on":
        workflow_labels.add(OUTSIDE_INPUT)
    for configured in config.agents:
        for changes in configured.outcomes.values():
            workflow_labels.update(changes["add"])
            workflow_labels.update(changes["remove"])
    shared = (f"Shared project policy:\n{shared_instructions}\n\n"
              if config.shared_instructions is not None else "")
    if plan.item.is_fork(config.repository):
        workspace = ("The private worktree is a detached checkout of the PR's base branch tip "
                     "from origin, with the base branch's files and project guidance."
                     if plan.agent.worktree else
                     "You are in the control checkout; it has not been moved to the PR head.")
        head_ref = (fork_head_ref(lease["run"]) if plan.agent.worktree else
                    f"origin refs/pull/{plan.item.number}/head (fetch only for reading the diff)")
        shared = FORK_PR_PROMPT.format(workspace=workspace, head_ref=head_ref,
                                      head=plan.item.head) + "\n" + shared
    return RUN_PROMPT.format(agent=plan.agent.name, report_command=report_command,
                             outcomes=json.dumps(outcome_declarations(plan.agent), sort_keys=True),
                             labels=json.dumps(sorted(workflow_labels)), continuation=continuation,
                             retrospective=retrospective, context=json.dumps(context, indent=2),
                             shared_instructions=shared, instructions=instructions)
