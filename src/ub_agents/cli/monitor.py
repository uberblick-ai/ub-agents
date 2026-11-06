"""Serve every launcher's runs in a browser page through the registered monitor command."""

import argparse
from pathlib import Path

from ..config import DEFAULT_CONFIG, load_config
from ..errors import AgentError

from ._registry import command


def port_number(value):
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise argparse.ArgumentTypeError("must be a port number from 1 to 65535")
    return int(value)


def arguments(commands):
    monitor = commands.add_parser("monitor", help="every launcher's runs in a browser, with notifications",
                                  description="Serve a page on 127.0.0.1 that shows what needs a person, what runs now, "
                                  "what runs next and what ended in the last 24 hours, on every machine that runs a launcher for the "
                                  "repositories. Give checkout folders or config files; with none, use this checkout. "
                                  "Use --config only without checkouts. "
                                  "It reads GitHub with your gh login, polls every 30 seconds and can send "
                                  "browser notifications. It never changes anything on GitHub; Ctrl-C stops it.",
                                  examples=("ub-agents monitor", "ub-agents monitor ~/code/backend ~/code/studio",
                                            "ub-agents monitor . ../studio --background"))
    monitor.add_argument("checkouts", metavar="CHECKOUT", nargs="*",
                         help="checkout folders or config files (default: this checkout)")
    monitor.add_argument("--port", type=port_number, default=4377, help="port on 127.0.0.1 (default: 4377)")
    monitor.add_argument("--no-open", action="store_true", help="serve without opening a browser")
    monitor.add_argument("--background", action="store_true",
                         help="keep serving after this command exits; prints the pid to stop it")
    return monitor


def configurations(args):
    if args.checkouts and not args.default_config:
        raise AgentError(f"--config cannot be combined with checkouts: {', '.join(args.checkouts)}")
    loaded, repositories = [], {}
    for checkout in args.checkouts or [args.config]:
        path = Path(checkout).expanduser().resolve()
        if path.is_dir():
            path /= DEFAULT_CONFIG
        try:
            config = load_config(path)
        except (AgentError, OSError) as exc:
            raise AgentError(f"Cannot load checkout {checkout}: {exc}") from exc
        repository = config.repository.casefold()
        if repository in repositories:
            raise AgentError(f"Checkouts {repositories[repository]} and {checkout} configure the same repository: "
                             f"{config.repository}")
        repositories[repository] = checkout
        loaded.append((path, config))
    return loaded


@command("monitor", arguments, config_required=lambda args: not args.checkouts)
def monitor_run(args):
    from ..monitor import monitor_background, monitor_sources, serve
    loaded = configurations(args)
    if args.background:
        return monitor_background([path for path, _ in loaded], loaded[0][1].root, args.port,
                                  open_page=not args.no_open)
    return serve(monitor_sources([config for _, config in loaded]), args.port, open_page=not args.no_open)
