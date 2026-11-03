"""Check prerequisites through the registered doctor command."""

from time import monotonic

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
    from ..doctor.doctor import AREAS, Doctor, render
    from ..doctor.doctor_progress import DoctorProgress
    started = monotonic()
    doctor = Doctor()
    if args.json:
        result = doctor.run(args.config, offline=args.offline)
        render(result, json_output=True, elapsed=monotonic() - started)
    else:
        with DoctorProgress(AREAS, verbose=args.verbose) as progress:
            result = doctor.run(args.config, offline=args.offline, progress=progress)
            progress.finish(result, monotonic() - started)
            if not args.offline and doctor.create_missing_labels():
                result = doctor.result()
                progress.rechecked_github(result, monotonic() - started)
    return 0 if result["ok"] else 1
