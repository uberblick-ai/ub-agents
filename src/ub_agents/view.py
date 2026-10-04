"""Development-checkout entrypoint; no public ub-agents command or UI imports."""

import argparse
from pathlib import Path

from .view_data import choose_session


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only terminal view of one local launcher (development checkout).')
    parser.add_argument('control_checkout', type=Path)
    parser.add_argument('--session', help='Session ID; otherwise attach to the only live session')
    args = parser.parse_args(argv)
    try:
        path, listing = choose_session(args.control_checkout.absolute(), args.session)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if path is None:
        print('Specify --session ID. Available local sessions:')
        print('\n'.join(listing) if listing else '(none)')
        return
    try:
        from .view_ui import View
    except ModuleNotFoundError as exc:
        if exc.name not in {'textual', 'rich'}:
            raise
        parser.error('Install the opt-in UI extra in the checkout venv: .venv/bin/pip install -e ".[ui]"')
    View(args.control_checkout.absolute(), path).run()


if __name__ == '__main__':
    main()
