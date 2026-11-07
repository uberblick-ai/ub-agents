"""Post supervised retrospective notes through the registered retrospective command."""

import sys

from ..errors import AgentError
from ..retrospective import UPSTREAM_DISCUSSION, UPSTREAM_REPOSITORY, read_body, upstream_body
from ..run_config import supervised_run
from . import _runtime
from ._registry import command


def arguments(commands):
    retrospective = commands.add_parser("retrospective", help="post to the agent's retrospective board", overview_hidden=True,
                                        description="Post a body file as a top-level comment on the agent's configured "
                                        "retrospective discussion. Only available inside a supervised run; use the "
                                        "launcher's literal report_command. The repository and board are pinned by "
                                        "the launcher. With upstream-retrospectives: on, --upstream sends public "
                                        "feedback about ub-agents to its fixed Field reports board instead; omit "
                                        "project details and item links. Prints the comment URL and does not change "
                                        "the run's outcome. Upstream GitHub failures warn and exit successfully.",
                                        examples=("ub-agents retrospective --body-file /run/scratch/retrospective.md",
                                                  'ub-agents retrospective --body-file "/run/scratch/run notes.md"'))
    retrospective.add_argument("--body-file", required=True, metavar="PATH", help="path to a UTF-8 retrospective body file")
    retrospective.add_argument("--upstream", action="store_true", help="send public feedback about ub-agents (requires upstream-retrospectives: on)")
    return retrospective


@command("retrospective", arguments, config_path="resolved", config_option=False)
def retrospective_run(args):
    policy = supervised_run()
    if args.upstream:
        if policy.get("upstream-retrospectives", "off") != "on":
            raise AgentError("--upstream requires upstream-retrospectives: on in the launcher's configuration; nothing posted")
        content = upstream_body(read_body(args.body_file), policy)
        try:
            url = _runtime.GitHub(UPSTREAM_REPOSITORY).create_discussion_comment(UPSTREAM_DISCUSSION, content)
        except AgentError as exc:
            print(f"ub-agents: warning: upstream retrospective could not be posted: {exc}", file=sys.stderr)
            return
        print(url)
        return
    if policy["retrospectives"] is None:
        raise AgentError(f"No retrospective board configured for agent {policy['agent']}; nothing posted")
    content = read_body(args.body_file)
    print(_runtime.GitHub(policy["repository"]).create_discussion_comment(policy["retrospectives"], content))
    return
