"""Declare command setup beside its handler with @command, preserving registration order."""

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
