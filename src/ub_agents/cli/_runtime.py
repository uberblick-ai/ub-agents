"""Build the objects requested by a command's declaration before calling its handler."""

from ..approvals import pr_author_exclusions
from ..config import DEFAULT_CONFIG, load_config
from ..coordination import Coordinator
from ..github import GitHub
from ..loop import Loop
from ..signals import SignalEvent


def build(args, declaration):
    objects = {}
    if "config" in declaration.build:
        objects["config"] = load_config(args.config)
        if declaration.validate is not None:
            declaration.validate(args, objects["config"])
    if "github" in declaration.build:
        objects["github"] = GitHub(objects["config"].repository)
    if "coordinator" in declaration.build:
        _coordination(objects, declaration.identify_actor)
    if "loop" in declaration.build:
        _loop(args, objects)
    return objects


def _coordination(objects, identify_actor):
    config, github = objects["config"], objects["github"]
    actor = github.actor() if identify_actor else None
    objects["actor"] = actor
    objects["coordinator"] = Coordinator(
        github, actor, launchers=config.launchers, trusted_bots=config.trusted_bots,
        trusted_pr_authors=pr_author_exclusions(config.trusted_pr_authors, config.approvals, github))


def _loop(args, objects):
    config = objects["config"]
    stop, interrupt = SignalEvent(), SignalEvent()
    objects.update(stop=stop, interrupt=interrupt)
    objects["loop"] = Loop(
        config, objects["github"], objects["actor"], stop,
        config_path=config.root / DEFAULT_CONFIG if args.default_config else args.config,
        default_config=args.default_config, interrupt_event=interrupt)
