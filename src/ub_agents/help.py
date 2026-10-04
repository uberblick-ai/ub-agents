"""Help presentation derived from argparse's registered commands and actions."""

import argparse
from copy import copy
import sys


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        # Canonical positionals can be required by post-parse validation while
        # argparse accepts a hidden compatibility alias. Only change rendering.
        rendered = []
        for action in actions:
            if getattr(action, "required_for_help", False):
                action = copy(action)
                action.nargs = None
                action.required = True
            rendered.append(action)
        return super().add_usage(usage, rendered, groups, prefix)

    def _fill_text(self, text, width, indent):
        if text.startswith("Examples:"):
            return super()._fill_text(text, width, indent)
        return argparse.HelpFormatter._fill_text(self, text, width, indent)

    def _get_help_string(self, action):
        text = action.help or ""
        if action.required or getattr(action, "required_for_help", False):
            text += " (required)"
        return text


class HelpParser(argparse.ArgumentParser):
    def __init__(self, *args, examples=(), **kwargs):
        kwargs.setdefault("formatter_class", HelpFormatter)
        if examples:
            kwargs["epilog"] = "Examples:\n" + "\n".join(f"  {example}" for example in examples)
        super().__init__(*args, **kwargs)
        self.examples = examples

    def add_argument(self, *args, required_for_help=False, **kwargs):
        action = super().add_argument(*args, **kwargs)
        action.required_for_help = required_for_help
        return action

    def commands(self):
        # argparse's private metadata deliberately keeps help tied to registration.
        return next((action.choices for action in self._actions
                     if isinstance(action, argparse._SubParsersAction)), {})

    def format_help(self):
        commands = self.commands()
        if not commands:
            return super().format_help()
        rows = []
        descriptions = {choice.dest: choice.help for action in self._actions
                        if isinstance(action, argparse._SubParsersAction)
                        for choice in action._choices_actions}
        for name, command in commands.items():
            # Show one valid form for required groups; detailed help retains all
            # alternatives and choices. Copy actions to leave parsing unchanged.
            groups = [group for group in command._mutually_exclusive_groups if group.required]
            alternatives = [group._group_actions[0] for group in groups]
            actions = []
            for action in command._actions:
                if action.help == argparse.SUPPRESS:
                    continue
                if action in alternatives:
                    action = copy(action)
                    action.required = True
                    action.metavar = action.metavar or action.dest.upper()
                if not action.option_strings or action.required:
                    actions.append(action)
            formatter = command._get_formatter()
            # Python 3.14 adds colour; keep ANSI codes out of table widths.
            if hasattr(formatter, "_set_color"):
                formatter._set_color(False)
            formatter.add_usage(None, actions, [], prefix="")
            usage = " ".join(formatter.format_help().split())
            rows.append((usage, descriptions[name]))
        width = max(len(usage) for usage, _ in rows)
        lines = [self.description, "", "Commands:"]
        lines.extend(f"  {usage:<{width}} # {description}" for usage, description in rows)
        lines.extend(["", "Global options:"])
        formatter = self._get_formatter()
        formatter.start_section(None)
        formatter.add_arguments([action for action in self._actions
                                 if action.option_strings and action.help != argparse.SUPPRESS])
        formatter.end_section()
        lines.append(formatter.format_help().rstrip())
        lines.extend(["", "Run ub-agents help COMMAND or ub-agents COMMAND --help for details and examples."])
        return "\n".join(lines) + "\n"

    def error(self, message):
        self.print_usage(sys.stderr)
        command = self.prog.removeprefix("ub-agents ")
        hint = "ub-agents help" if command in {"ub-agents", "help"} else f"ub-agents help {command}"
        self.exit(2, f"{self.prog}: error: {message}\nRun {hint} for usage and examples.\n")
