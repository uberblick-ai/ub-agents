"""Render doctor records as grouped terminal diagnostics or unchanged JSON."""

from collections import Counter
from datetime import datetime
import json
import os
import sys

from rich.console import Console
from rich.text import Text

from . import __version__
from .launcher_lock import configuration_pause_summary


SYMBOLS = {"ok": ("✓", "green"), "warn": ("!", "yellow"),
           "fail": ("✗", "red"), "skip": ("–", "dim")}


def installation_summary(check):
    if check is None:
        return f"ub-agents {__version__}"
    message = check["message"].removeprefix("ub-agents: ")
    if check["status"] == "ok":
        policy = message.rsplit("automatic updates: ", 1)[-1]
        return f"ub-agents {__version__} (Homebrew, updates {policy})"
    if message.startswith("pip, source or other installation;"):
        return "ub-agents from source (updates need the Homebrew tap)"
    return f"ub-agents {__version__} ({message})"


def header(result):
    checks = {check["id"]: check for check in result["checks"]}
    config = checks.get("config")
    repository = config["message"].split(",", 1)[0] if config and config["status"] == "ok" else "repository unavailable"
    launcher = result.get("launcher")
    running = "no launcher running"
    if launcher:
        started = datetime.fromisoformat(launcher["started_at"].replace("Z", "+00:00"))
        running = f"launcher running (pid {launcher['pid']}, since {started:%H:%M})"
    return f"{repository} · {installation_summary(checks.get('launcher-installation'))} · {running}"


def check_message(check):
    message = check["message"]
    agent, runtime = check["agent"], check["runtime"]
    if agent:
        message = message.removeprefix(f"{agent}: ").removeprefix(f"{agent} ")
    if check["id"].startswith("different-runtime-from:") and runtime:
        message = message.removesuffix(f" using {runtime}")
    if check["id"].startswith("runtime-auth:") and message.startswith("probe "):
        message = "auth " + message
    server = check.get("server")
    if server and message.startswith(f"{server}: "):
        state = message[len(server) + 2:]
        separator = " is " if state in {"missing", "pending approval", "unread"} else ": "
        message = f"MCP server {server}{separator}{state}"
    scope = " · ".join(value for value in (agent, runtime) if value)
    return f"{scope}: {message}" if scope else message


def render_check(console, check, verbose):
    symbol, style = SYMBOLS[check["status"]]
    line = Text("      ")
    line.append(symbol, style=style)
    line.append(f" {check_message(check)}")
    if check["status"] == "skip":
        line.stylize("dim")
    console.print(line)
    if verbose:
        console.print(f"        id: {check['id']}", style="dim")
    if check["status"] in {"warn", "fail"} and check["remedy"]:
        console.print(f"        → {check['remedy']}", style="dim")


def skipped_summary(checks):
    messages = {check["message"] for check in checks}
    if all("offline:" in message for message in messages):
        return "offline"
    if any("gh unavailable" in message for message in messages):
        return "skipped, needs gh"
    if len(messages) == 1:
        return f"skipped, {next(iter(messages))}"
    return f"{len(checks)} skipped"


def area_summary(checks):
    counts = Counter(check["status"] for check in checks)
    if counts["skip"] == len(checks):
        return "skip", skipped_summary(checks)
    status = next(value for value in ("fail", "warn", "ok") if counts[value])
    labels = {check["id"].casefold() for check in checks
              if check["status"] == "ok" and check["id"].startswith("github-label:")}
    summary = []
    if counts["ok"] - len(labels):
        summary.append(f"{counts['ok'] - len(labels)} passed")
    if labels:
        summary.append(f"{len(labels)} label{'s' if len(labels) != 1 else ''}")
    for value, noun in (("fail", "failure"), ("warn", "warning"), ("skip", "skipped")):
        if counts[value]:
            summary.append(f"{counts[value]} {noun}" + ("s" if counts[value] != 1 and value != "skip" else ""))
    return status, ", ".join(summary)


def verdict(result, elapsed):
    checks = result["checks"]
    failures = sum(check["required"] and check["status"] == "fail" for check in checks)
    warnings = sum(check["status"] == "warn" for check in checks)
    warning_count = f"{warnings} warning{'s' if warnings != 1 else ''}"
    if failures:
        return (f"{failures} failure{'s' if failures != 1 else ''}, {warning_count}. "
                f"Launch won't start until the failures are fixed. ({elapsed:.1f}s)")
    if warnings:
        launch = "A launcher is already running." if result.get("launcher") else "Launch will start."
        return f"{warning_count}, no failures. {launch} ({elapsed:.1f}s)"
    passed = sum(check["status"] == "ok" for check in checks)
    return f"All {passed} checks passed in {elapsed:.1f}s."


def render_area(console, area, checks, width, verbose):
    summary_checks = [check for check in checks if check["id"] != "launcher-installation"]
    if not summary_checks:
        return
    status, summary = area_summary(summary_checks)
    symbol, style = SYMBOLS[status]
    name = area if area == "GitHub" else area.capitalize()
    if summary[0].isdigit() and len(summary.split(" ", 1)[0]) == 1:
        summary = " " + summary
    line = Text("  ")
    line.append(symbol, style=style)
    line.append(f" {name:<{width}}   {summary}")
    if status == "skip":
        line.stylize("dim")
    console.print(line)
    for check in checks:
        if verbose or check["status"] in {"warn", "fail"}:
            render_check(console, check, verbose)


def render(result, areas, json_output=False, verbose=False, *, elapsed=0.0):
    if json_output:
        print(json.dumps(result, indent=2))
        return
    terminal = sys.stdout.isatty() and not os.environ.get("CI") and "NO_COLOR" not in os.environ
    console = Console(file=sys.stdout, force_terminal=terminal, markup=False, highlight=False, soft_wrap=True)
    console.print(header(result))
    launcher = result.get("launcher")
    if launcher and launcher.get("configuration_pause"):
        console.print(configuration_pause_summary(launcher["configuration_pause"]))
    console.print()
    check_areas = {id: area for area, ids in areas.items() for id in ids}
    groups = {area: [] for area in areas}
    for check in result["checks"]:
        groups[check_areas[check["id"].split(":", 1)[0]]].append(check)
    width = max(len(area) for area in areas)
    for area, checks in groups.items():
        render_area(console, area, checks, width, verbose)
    console.print()
    console.print(verdict(result, elapsed))
