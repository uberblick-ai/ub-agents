"""Read filtered input using project configuration or the launcher's pinned policy."""

import json
import os

from ..run_config import supervised_run
from . import _runtime
from ._registry import command


def arguments(commands):
    read = commands.add_parser("read", help="read an issue or PR as filtered JSON", overview_hidden=True,
                               description="Read an open or closed issue or PR under the assignment input trust rules. "
                               "Withheld input is marked or counted; history and permission failures show no item content. "
                               "trusted-bots trusts listed GitHub bot feedback like write, without maintainer authority. "
                               "Inside a supervised run use the launcher's report_command followed by read N; "
                               "its repository and configuration are pinned by the launcher. This command makes no writes.",
                               examples=("ub-agents read 143", "ub-agents read 150 --config workflow.yaml"))
    read.add_argument("number", metavar="NUMBER", type=int, help="issue or PR number in the configured repository")
    return read


def needs_config(args):
    # Supervised reads use the launcher's pinned input policy.
    return "UB_AGENTS_RUN" not in os.environ


@command("read", arguments, number="required", config_required=needs_config)
def read_run(args):
    from ..read_input import read_item, read_policy
    policy = (supervised_run() if "UB_AGENTS_RUN" in os.environ else
              read_policy(_runtime.load_config(args.config)))
    print(json.dumps(read_item(_runtime.GitHub(policy["repository"]), args.number, policy), indent=2))
    return
