"""Declare command setup beside its handler, preserving registration order.

For example, a config-backed command with an optional item number declares::

    @command("inspect", arguments, number="optional", config_required=True,
             build=("config", "github"))
    def inspect_run(args, config, github):
        ...

``arguments`` adds and returns the command's parser. ``build`` selects config,
github, coordinator and loop in that dependency order; callers include prerequisites.
The built objects become handler keyword arguments (loop also builds stop and
interrupt; coordinator also builds actor). ``identify_actor=False`` skips actor
lookup. ``validate(args, config)`` runs after config loading, before other builds.

``config_path`` selects absolute, resolved or project-relative discovery, and
``config_option`` controls whether the command accepts --config. ``config_required``
is a boolean or args predicate; ``missing_config(args)`` can handle an absent file.
``legacy_number`` enables retry's hidden --number alias. After argument preparation,
``resources(args, stack)`` enters command-owned contexts. ``early_return(args)``
can then stop dispatch before building objects. ``error_message(exc, root)``
customizes errors once a config path is available.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Command:
    name: str
    arguments: Callable
    run: Callable
    number: Literal["none", "optional", "required"] = "none"
    legacy_number: bool = False
    config_path: Literal["absolute", "resolved", "project"] = "project"
    config_option: bool = True
    config_required: bool | Callable = False
    missing_config: Callable | None = None
    build: tuple[str, ...] = ()
    identify_actor: bool = True
    validate: Callable | None = None
    early_return: Callable | None = None
    resources: Callable | None = None
    error_message: Callable | None = None


commands: dict[str, Command] = {}


def command(name, arguments, **setup):
    def register(run):
        commands[name] = Command(name, arguments, run, **setup)
        return run
    return register
