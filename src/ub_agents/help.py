"""Help presentation derived from argparse's registered commands and actions."""

import argparse
import sys


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def _fill_text(self, text, width, indent):
        if text.startswith("Examples:"):
            return super()._fill_text(text, width, indent)
        return argparse.HelpFormatter._fill_text(self, text, width, indent)

    def _get_help_string(self, action):
        text = action.help or ""
        if action.required:
            text += " (required)"
        return text


class HelpParser(argparse.ArgumentParser):
    def __init__(self, *args, examples=(), **kwargs):
        kwargs.setdefault("formatter_class", HelpFormatter)
        if examples:
            kwargs["epilog"] = "Examples:\n" + "\n".join(f"  {example}" for example in examples)
        super().__init__(*args, **kwargs)
        self.examples = examples

    def commands(self):
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
            # Keep positionals, required options and required choices in the overview.
            # The full parser usage retains secondary options in detailed help.
            groups = [group for group in command._mutually_exclusive_groups if group.required]
            actions = [action for action in command._actions
                       if action.help != argparse.SUPPRESS and
                       (not action.option_strings or action.required or
                        any(action in group._group_actions for group in groups))]
            formatter = command._get_formatter()
            formatter.add_usage(None, actions, groups, prefix="")
            usage = " ".join(formatter.format_help().split())
            rows.append((usage, descriptions[name]))
        width = max(len(usage) for usage, _ in rows)
        lines = [self.description, "", "Commands:"]
        lines.extend(f"  {usage:<{width}} # {description}" for usage, description in rows)
        lines.extend(["", "Global options (before the command):"])
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
