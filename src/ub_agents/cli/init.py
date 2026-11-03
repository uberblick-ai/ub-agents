"""Set up a project through the registered init command."""

import os
from pathlib import Path
import subprocess
import sys

from ..help import InitHelpFormatter
from . import _runtime
from ._registry import command


def arguments(commands):
    init = commands.add_parser("init", help="set up this repository: starter configuration, agent instructions and workflow labels",
                               formatter_class=InitHelpFormatter,
                               description="Ask a few setup questions in the terminal, show the changes, then write them. "
                               "Use --use-defaults for unattended setup; CI/non-TTY also uses defaults. "
                               "Existing starter files are preserved; --create-boards and --create-priorities "
                               "only add missing entries to ub-agents.yaml.",
                               examples=("ub-agents init",
                                         "ub-agents init --create-boards",
                                         "ub-agents init --create-priorities",
                                         "ub-agents init --repository org/project --runtime claude:claude-opus-5-5:high"))
    init.add_argument("--repository", metavar="OWNER/REPO", help="repository owner/name (otherwise inferred through gh)")
    init.add_argument("--runtime", help="starter cli:model:effort (default: the detected CLI)")
    init.add_argument("--use-defaults", action="store_true", help="unattended starters, even in a terminal")
    init.add_argument("--create-boards", action="store_true", help="add missing retrospective boards to an existing setup")
    init.add_argument("--create-priorities", action="store_true", help="add priority labels to an existing setup")
    return init


@command("init", arguments, config_path="absolute")
def init_project(args):
    from ..init.flow import Flow
    from ..init.prompts import DefaultPrompter, Transcript, interactive_prompter
    interactive = (not args.use_defaults and not os.environ.get("CI")
                   and sys.stdin.isatty() and sys.stdout.isatty())
    prompter = interactive_prompter(os.environ) if interactive else DefaultPrompter()
    def github_factory(repository):
        return _runtime.GitHub(repository, runner=subprocess.run)
    if args.create_boards or args.create_priorities:
        from ..init.extend import ExistingFlow
        return ExistingFlow(Path(args.config).absolute(), args.repository, args.runtime, github_factory, prompter, Transcript(),
                            unattended=not interactive, create_boards=args.create_boards,
                            create_priorities=args.create_priorities).run()
    return Flow(Path(args.config).absolute(), args.repository, args.runtime, github_factory, prompter, Transcript(),
                unattended=not interactive).run()
