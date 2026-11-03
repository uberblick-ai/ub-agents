"""Serve every launcher's runs in a browser page through the registered monitor command."""

import argparse

from ._registry import command


def port_number(value):
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise argparse.ArgumentTypeError("must be a port number from 1 to 65535")
    return int(value)


def arguments(commands):
    monitor = commands.add_parser("monitor", help="every launcher's runs in a browser, with notifications",
                                  description="Serve a page on 127.0.0.1 that shows what needs a person, what runs now "
                                  "and what ended in the last 24 hours, on every machine that runs a launcher for the "
                                  "repository. It reads GitHub with your gh login, polls every 30 seconds and can send "
                                  "browser notifications. It never changes anything on GitHub; Ctrl-C stops it.",
                                  examples=("ub-agents monitor", "ub-agents monitor --background",
                                            "ub-agents monitor --port 4400 --no-open"))
    monitor.add_argument("--port", type=port_number, default=4377, help="port on 127.0.0.1 (default: 4377)")
    monitor.add_argument("--no-open", action="store_true", help="serve without opening a browser")
    monitor.add_argument("--background", action="store_true",
                         help="keep serving after this command exits; prints the pid to stop it")
    return monitor


@command("monitor", arguments, config_required=True, build=("config", "github", "coordinator"))
def monitor_run(args, config, github, actor, coordinator):
    from ..monitor import monitor_background, serve
    if args.background:
        return monitor_background(args.config, config.root, args.port, open_page=not args.no_open)
    return serve(config, github, coordinator, args.port, open_page=not args.no_open)
