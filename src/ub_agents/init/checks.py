"""Fill starter check commands from explicit project task definitions."""

import json
import re
import tomllib

from ..errors import AgentError


def make_targets(text):
    rules = re.findall(r"^ *([^\s#:=]+(?:[ \t]+[^\s#:=]+)*)[ \t]*:{1,2}(?![:=])(.*)", text, re.MULTILINE)
    assignment = r"^[ \t]*(?:(?:export|override|private)[ \t]+)*[^\s:=]+[ \t]*(?::{0,3}|[?+!])="
    return {name for group, rest in rules if not re.match(assignment, rest) for name in group.split()}


def task_names(path):
    try:
        if path.name == "Makefile":
            # Make syntax and the supported names are ASCII; comments need not be UTF-8.
            return make_targets(path.read_text(encoding="utf-8", errors="surrogateescape"))
        text = path.read_text(encoding="utf-8-sig" if path.name == "package.json" else "utf-8")
        data = tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)
        tasks = data.get("tasks" if path.suffix == ".toml" else "scripts", {}) if isinstance(data, dict) else {}
        if not isinstance(tasks, dict):
            return set()
        return {name for name, value in tasks.items()
                if isinstance(value, str) and value.strip()
                or path.suffix == ".toml" and isinstance(value, (dict, list))}
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentError(f"Cannot read project checks from {path}: {exc}; fix the file and run init again") from exc


def project_checks(root):
    commands = {}
    for filename, runner in (("mise.toml", "mise run"), ("package.json", "npm run"), ("Makefile", "make")):
        path = root / filename
        if not path.exists():
            continue
        names = task_names(path)
        for check, candidates in (("build", ("build",)), ("test", ("test",)), ("lint", ("lint", "check"))):
            task = next((name for name in candidates if name in names), None)
            if task and check not in commands:
                commands[check] = f"{runner} {task}"
    checks = (("Build", "build", "<project build command>"), ("Tests", "test", "<project test command>"),
              ("Other checks", "lint", "<project lint or validation command>"))
    return ("Review these commands and replace any placeholders with the project's required checks before launch:\n\n"
            + "\n".join(f"- {label}: `{commands.get(name, placeholder)}`" for label, name, placeholder in checks))
