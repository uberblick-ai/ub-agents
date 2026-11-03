"""Declare doctor ids with @check(area, *ids); areas follow declaration order."""

from collections.abc import Callable
from typing import TypeVar


CHECK_AREAS: dict[str, str] = {}
F = TypeVar("F", bound=Callable)


def check(area: str, *ids: str) -> Callable[[F], F]:
    def register(function: F) -> F:
        for id in ids:
            if id in CHECK_AREAS:
                raise ValueError(f"Doctor check {id!r} is already declared in {CHECK_AREAS[id]!r}")
            CHECK_AREAS[id] = area
        return function
    return register


def areas() -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for id, area in CHECK_AREAS.items():
        groups.setdefault(area, []).append(id)
    return groups
