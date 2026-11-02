"""Check prerequisites through the registered doctor command."""

from ..help import CommentHelpFormatter
from ._registry import command


def arguments(commands):
    doctor = commands.add_parser("doctor", help="check the machine, GitHub access, labels and agent runtimes",
                                 formatter_class=CommentHelpFormatter,
                                 overview_options=("--json",),
                                 description="Check machine, GitHub and runtime prerequisites. "
                                 "Check MCP configuration for each agent/runtime/server and probe loaded stdio servers. "
                                 "MCP problems are optional warnings. "
                                 "Use --offline for local validation without GitHub or agent runtimes. "
                                 "Create missing workflow labels only after a confirmed interactive prompt. "
                                 "Show warnings and failures with a summary per area by default; "
                                 "required failures exit nonzero.",
                                 examples=("ub-agents doctor", "ub-agents doctor --offline", "ub-agents doctor --json"))
    doctor.add_argument("--offline", action="store_true", help="validate locally without GitHub or agent runtimes")
    doctor.add_argument("--json", action="store_true", help="emit versioned prerequisite results")
    doctor.add_argument("--verbose", action="store_true", help="show the full per-check list (does not change --json)")
    return doctor


@command("doctor", arguments)
def doctor_run(args):
    from ..doctor import Doctor, render, render_check, render_counts
    doctor = Doctor()
    result = doctor.run(args.config, offline=args.offline)
    render(result, json_output=args.json, verbose=args.verbose)
    if not args.offline and not args.json and doctor.create_missing_labels():
        previous = result["checks"]
        result = doctor.result()
        for check in result["checks"]:
            if check["status"] in {"fail", "warn"} and check not in previous:
                render_check(check)
        render_counts(result)
    return 0 if result["ok"] else 1
