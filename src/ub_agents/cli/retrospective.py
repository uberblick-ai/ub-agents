"""Post supervised retrospective notes through the registered retrospective command."""

from ..errors import AgentError
from ..run_config import supervised_run
from . import _runtime
from ._registry import command


def arguments(commands):
    retrospective = commands.add_parser("retrospective", help="post to the agent's retrospective board", overview_hidden=True,
                                        description="Post a body file as a top-level comment on the agent's configured "
                                        "retrospective discussion. Only available inside a supervised run; use the "
                                        "launcher's literal report_command. The repository and board are pinned by "
                                        "the launcher. Prints the comment URL and does not change the run's outcome.",
                                        examples=("ub-agents retrospective --body-file /run/scratch/retrospective.md",
                                                  'ub-agents retrospective --body-file "/run/scratch/run notes.md"'))
    retrospective.add_argument("--body-file", required=True, metavar="PATH", help="path to a UTF-8 retrospective body file")
    return retrospective


@command("retrospective", arguments, config_path="resolved", config_option=False)
def retrospective_run(args):
    from ..retrospective import read_body
    policy = supervised_run()
    if policy["retrospectives"] is None:
        raise AgentError(f"No retrospective board configured for agent {policy['agent']}; nothing posted")
    content = read_body(args.body_file)
    print(_runtime.GitHub(policy["repository"]).create_discussion_comment(policy["retrospectives"], content))
    return
