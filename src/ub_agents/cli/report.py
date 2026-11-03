"""Record a supervised run outcome through the registered report command."""

import json
import os

from ..approvals import pr_author_exclusions
from ..coordination import Coordinator
from ..errors import AgentError
from ..records import MARKER, own_comment, record_version, records
from ..run_config import supervised_run
from . import _runtime
from ._registry import command


def arguments(commands):
    report = commands.add_parser("report", help="record the run's outcome", overview_hidden=True,
                                 description="Record a supervised run's explicit result on GitHub. "
                                 "Use inside the launcher-provided assignment environment; choose --status or --outcome (required). "
                                 "Stop reports (--status blocked or an outcome adding a configured stop label) require --action or --option.",
                                 examples=('ub-agents report --outcome handed-off --summary "Ready for review" --handoff 150',
                                           'ub-agents report --status blocked --summary "Decision pending" '
                                           '--option "Maintainer: use A." --option "Maintainer: use B."'))
    verdict = report.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--outcome", help="declared project outcome; reports success")
    verdict.add_argument("--status", choices=["retry", "blocked"], help="failure verdict; changes no labels")
    report.add_argument("--summary", required=True, help="explain the result in 1–8000 characters")
    report.add_argument("--action", action="append", help="one independent ask that is needed, at most 300 characters; repeat for each ask")
    report.add_argument("--option", action="append", help="one alternative way to unblock, at most 300 characters; repeat with the recommendation first; append : `COMMAND` for a command block")
    report.add_argument("--handoff", type=int, metavar="NUMBER", help="implementation PR number; its head is recorded")
    return report


@command("report", arguments, config_path="resolved", config_option=False)
def report_run(args):
    context = supervised_run()
    if not args.summary.strip() or len(args.summary) > 8000:
        raise AgentError("summary must contain 1–8000 characters")
    number, lease_id = context["assignment"], context["lease_id"]
    github = _runtime.GitHub(context["repository"])
    coordinator = Coordinator(github, github.actor(), trusted_bots=context["trusted-bots"],
                              trusted_pr_authors=pr_author_exclusions(context["trusted-pr-authors"],
                                                                     context["approvals"], github))
    # Only inspect the supervised comment's marker and GitHub author. Future
    # payloads are not a contract this build can validate or use for authority.
    comments = github.comments(number)
    supervised = next((c for c in comments if isinstance(c, dict) and c.get("id") == lease_id
                       and own_comment(c, coordinator.actor)), None)
    version = record_version(supervised.get("body")) if supervised else None
    supported = record_version(MARKER)
    if version is not None and version > supported:
        command = os.environ.get("UB_AGENTS_REPORT")
        instruction = f"{command} report (UB_AGENTS_REPORT)" if command else "the launcher's command in UB_AGENTS_REPORT"
        raise AgentError(f"Supervised lease uses record format v{version}, newer than this ub-agents "
                         f"reads (v{supported}). Report with {instruction}.")
    lease = next((r for r in records(comments, trusted=coordinator.trust.observation()) if r["kind"] == "lease"
                  and r["id"] == lease_id and r["run"] == context["run"]), None)
    if lease is None or lease["actor"].casefold() != coordinator.actor.casefold():
        raise AgentError("Supervised lease was not found on GitHub or is not owned by this account")
    record = coordinator.report(lease, args.status or "success", args.summary, args.handoff,
                                outcome=args.outcome, action=args.action, option=args.option,
                                declarations=context["outcomes"], stop_labels=context["stop-labels"])
    print(json.dumps({"run": lease["run"], "status": record["status"], "url": record["url"]}))
