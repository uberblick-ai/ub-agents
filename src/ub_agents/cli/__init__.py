"""Parse and dispatch the standalone CLI; command modules register their own setup."""

from contextlib import ExitStack
from pathlib import Path
import sys

from .. import __version__
from ..config import DEFAULT_CONFIG, resolve_config_path
from ..errors import AgentError, CheckoutSetupInterrupted
from ..help import HelpParser
from . import _runtime
from ._registry import commands

# Registration order is the public command overview order.
from . import init, doctor, launch, status, report, retrospective, retry, read


def parser():
    result = HelpParser(prog="ub-agents", description="project-owned engineering loops on GitHub",
                        usage="%(prog)s <command> [options]")
    result.add_argument("-v", "--version", action="version", version=f"ub-agents {__version__}",
                        help="print the version")
    result.add_argument("--config", metavar="PATH", help="project configuration (default: ub-agents.yaml)")
    next(action for action in result._actions if action.dest == "help").help = (
        "show this help; after a command, that command's help")
    subparsers = result.add_subparsers(dest="command", prog="ub-agents")
    for declaration in commands.values():
        command_parser = declaration.arguments(subparsers)
        if declaration.config_option:
            command_parser.add_argument("--config", dest="command_config", metavar="PATH",
                                        help="project configuration (default: ub-agents.yaml)")
    help_command = subparsers.add_parser("help", overview_hidden=True,
                                       description="Show the command overview or detailed help for a command. "
                                       "Use anywhere without project configuration, GitHub authentication or network access.",
                                       examples=("ub-agents help", "ub-agents help launch"))
    help_command.add_argument("topic", metavar="COMMAND", nargs="?",
                              help="command to describe; omit for the overview (optional)")
    return result


def _print_help(command_line, args):
    topic = getattr(args, "topic", None)
    if topic and topic not in command_line.commands():
        command_line.unknown_command(topic)
    target = command_line.commands()[topic] if topic else command_line
    target.print_help()


def _validate_number(args, command_parser, declaration):
    if declaration.legacy_number:
        if args.number is not None and args.legacy_number is not None:
            command_parser.error(f"{args.command} accepts either N or --number N, not both")
        args.number = args.number if args.number is not None else args.legacy_number
    if declaration.number == "none":
        return
    if args.number is None and declaration.number == "optional":
        return
    if args.number is None or args.number < 1:
        command_parser.error(f"{args.command} requires a positive item number")


def _prepare_args(command_line, args):
    declaration = commands[args.command]
    command_parser = command_line.commands()[args.command]
    command_config = getattr(args, "command_config", None)
    if command_config is not None:
        if args.config is not None:
            command_parser.error("--config may be given before or after the command, not both")
        args.config = command_config
    _validate_number(args, command_parser, declaration)
    args.default_config = args.config is None
    args.config = (Path(args.config or DEFAULT_CONFIG).absolute() if declaration.config_path == "absolute" else
                   Path(args.config or DEFAULT_CONFIG).resolve() if declaration.config_path == "resolved"
                   else resolve_config_path(args.config))
    needs_config = (declaration.config_required(args) if callable(declaration.config_required)
                    else declaration.config_required)
    if needs_config and not args.config.exists():
        if declaration.missing_config is not None and declaration.missing_config(args):
            return
        name = DEFAULT_CONFIG if args.default_config else str(args.config)
        raise AgentError(f"No {name} here; run ub-agents init to set up a project")


def run(args):
    declaration = commands[args.command]
    if declaration.early_return is not None and declaration.early_return(args):
        return
    return declaration.run(args, **_runtime.build(args, declaration))


def main(argv=None):
    from ..self_updates import RestartLauncher

    try:
        return _main(argv)
    except RestartLauncher as restart:
        return launch.restart_launcher(restart, argv)


def _main(argv=None):
    with ExitStack() as stack:
        args = None
        declaration = None
        try:
            command_line = parser()
            args = command_line.parse_args(argv)
            if args.command in {None, "help"}:
                _print_help(command_line, args)
                return 0
            declaration = commands[args.command]
            _prepare_args(command_line, args)
            if declaration.resources is not None:
                declaration.resources(args, stack)
            return run(args) or 0
        except KeyboardInterrupt as exc:
            detail = (f"ub-agents: {exc}" if isinstance(exc, CheckoutSetupInterrupted)
                      else "Stopped; supervised execution terminated")
            print(detail, file=sys.stderr)
            return 130
        except (AgentError, OSError) as exc:
            detail = str(exc)
            if declaration is not None and declaration.error_message is not None and isinstance(args.config, Path):
                detail = declaration.error_message(exc, args.config.parent)
            print(f"ub-agents: {detail}", file=sys.stderr)
            return 1
