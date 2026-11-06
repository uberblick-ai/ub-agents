"""Serve every launcher's runs in a browser page through the registered monitor command."""

import argparse
from pathlib import Path

import yaml

from ..config import DEFAULT_CONFIG, UniqueLoader, load_config, mapping, string
from ..errors import AgentError

from ._registry import command

MONITOR_CONFIG = "ub-agents-monitor.yaml"


def port_number(value):
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise argparse.ArgumentTypeError("must be a port number from 1 to 65535")
    return int(value)


def arguments(commands):
    monitor = commands.add_parser("monitor", help="every launcher's runs in a browser, with notifications",
                                  description="Serve a page on 127.0.0.1 that shows what needs a person, what runs now, "
                                  "what runs next and what ended in the last 24 hours, on every machine that runs a launcher for the "
                                  "repositories. Give checkout folders or config files; with none, read "
                                  "ub-agents-monitor.yaml in the current folder, or use this checkout if absent. "
                                  "Use --config only without checkouts. "
                                  "It reads GitHub with your gh login, polls every 30 seconds and can send "
                                  "browser notifications. It never changes anything on GitHub; Ctrl-C stops it.",
                                  examples=("ub-agents monitor", "ub-agents monitor ~/code/backend ~/code/studio",
                                            "ub-agents monitor . ../studio --background"))
    monitor.add_argument("checkouts", metavar="CHECKOUT", nargs="*",
                         help="checkout folders or config files (default: ub-agents-monitor.yaml, then this checkout)")
    monitor.add_argument("--port", type=port_number, default=4377, help="port on 127.0.0.1 (default: 4377)")
    monitor.add_argument("--no-open", action="store_true", help="serve without opening a browser")
    monitor.add_argument("--background", action="store_true",
                         help="keep serving after this command exits; prints the pid to stop it")
    return monitor


def monitor_file(args):
    if args.checkouts or not args.default_config:
        return None
    path = Path.cwd() / MONITOR_CONFIG
    return path if path.exists() or path.is_symlink() else None


def read_checkouts(path):
    try:
        data = mapping(yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader),
                       {"checkouts"}, "monitor configuration")
        checkouts = data.get("checkouts")
        if not isinstance(checkouts, list) or not checkouts:
            raise AgentError("checkouts must be a list of at least one path")
        return [string(checkout, f"checkouts entry {index} ({checkout!r})")
                for index, checkout in enumerate(checkouts, 1)]
    except (AgentError, OSError, UnicodeError, yaml.YAMLError) as exc:
        raise AgentError(f"Cannot read monitor configuration {path}: {exc}") from exc


def configurations(args, monitor_path=None):
    if args.checkouts and not args.default_config:
        raise AgentError(f"--config cannot be combined with checkouts: {', '.join(args.checkouts)}")
    checkouts = read_checkouts(monitor_path) if monitor_path else args.checkouts or [args.config]
    root = monitor_path.parent if monitor_path else Path.cwd()
    where = f" in {monitor_path}" if monitor_path else ""
    loaded, repositories = [], {}
    for checkout in checkouts:
        try:
            path = (root / Path(checkout).expanduser()).resolve()
            if path.is_dir():
                path /= DEFAULT_CONFIG
            config = load_config(path)
        except (AgentError, OSError, RuntimeError) as exc:
            raise AgentError(f"Cannot load checkout {checkout}{where}: {exc}") from exc
        repository = config.repository.casefold()
        if repository in repositories:
            raise AgentError(f"Checkouts {repositories[repository]} and {checkout} configure the same repository: "
                             f"{config.repository}{where}")
        repositories[repository] = checkout
        loaded.append((path, config))
    return loaded


@command("monitor", arguments, config_required=lambda args: not args.checkouts and monitor_file(args) is None)
def monitor_run(args):
    from ..monitor import monitor_background, monitor_sources, serve
    monitor_path = monitor_file(args)
    loaded = configurations(args, monitor_path)
    if args.background:
        root = monitor_path.parent if monitor_path else loaded[0][1].root
        return monitor_background([path for path, _ in loaded], root, args.port,
                                  open_page=not args.no_open)
    return serve(monitor_sources([config for _, config in loaded]), args.port, open_page=not args.no_open)
