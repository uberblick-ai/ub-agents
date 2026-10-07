"""Compact help presentation derived from argparse's commands and actions."""

import argparse
from copy import copy
import sys
import textwrap


class HelpFormatter(argparse.HelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, width=80)

    def add_usage(self, usage, actions, groups, prefix=None):
        if usage is None:
            # Only positionals, required options and required alternatives belong
            # in the synopsis. Copy compatibility positionals for rendering only.
            required_groups = [group for group in groups if group.required]
            alternatives = [action for group in required_groups for action in group._group_actions]
            positionals, options = [], []
            for action in actions:
                if action.help == argparse.SUPPRESS:
                    continue
                if not action.option_strings:
                    if getattr(action, "required_for_help", False):
                        action = copy(action)
                        action.nargs = None
                        action.required = True
                    positionals.append(action)
                elif action.required or action in alternatives:
                    options.append(action)
            parts = [self._format_args(action, action.dest.upper()) for action in positionals]
            rendered_groups = []
            for action in options:
                group = next((group for group in required_groups if action in group._group_actions), None)
                if group is None:
                    parts.append(self.option_synopsis(action))
                elif group not in rendered_groups:
                    parts.append("(" + " | ".join(self.option_synopsis(alternative)
                                                 for alternative in group._group_actions
                                                 if alternative.help != argparse.SUPPRESS) + ")")
                    rendered_groups.append(group)
            synopsis = " ".join(parts)
            usage = "%(prog)s" + (f" {synopsis}" if synopsis else "")
            if any(action.option_strings and action.help != argparse.SUPPRESS
                   and not action.required and action not in alternatives for action in actions):
                usage += " [options]"
        return super().add_usage(usage, actions, groups, prefix)

    def option_synopsis(self, action):
        label = action.option_strings[0]
        if action.nargs != 0:
            label += " " + self._format_args(action, action.dest.upper())
        return label

    def rows(self, rows):
        lines = []
        for label, description in rows:
            prefix = f"  {label:<23}"
            if len(label) > 21:
                lines.append(f"  {label}")
                prefix = " " * 25
            lines.extend(textwrap.wrap(description, width=80, initial_indent=prefix,
                                       subsequent_indent=" " * 25))
        return lines

    def option_rows(self, actions):
        rows = []
        for action in actions:
            if action.option_strings and action.help != argparse.SUPPRESS:
                description = action.help or ""
                if action.required:
                    description += " (required)"
                rows.append((self._format_action_invocation(action), description))
        return self.rows(rows)


class HelpParser(argparse.ArgumentParser):
    def __init__(self, *args, examples=(), overview_options=(), run_command=False,
                 overview_hidden=False, **kwargs):
        kwargs.setdefault("formatter_class", HelpFormatter)
        super().__init__(*args, **kwargs)
        self.examples = examples
        self.overview_options = overview_options
        self.run_command = run_command
        self.overview_hidden = overview_hidden
        # argparse registers this first; display it last in command help.
        for action in self._actions:
            if isinstance(action, argparse._HelpAction):
                action.help = "show this help"

    def add_argument(self, *args, required_for_help=False, **kwargs):
        action = super().add_argument(*args, **kwargs)
        action.required_for_help = required_for_help
        return action

    def _get_formatter(self):
        formatter = super()._get_formatter()
        # Python 3.14 sets colour after constructing the formatter.
        # Keep help stable and table widths literal, even with forced colour.
        if hasattr(formatter, "_set_color"):
            formatter._set_color(False)
        return formatter

    def commands(self):
        # argparse's private metadata keeps help tied to command registration.
        return next((action.choices for action in self._actions
                     if isinstance(action, argparse._SubParsersAction)), {})

    def overview_synopsis(self, name):
        formatter = self._get_formatter()
        positionals = []
        for action in self._actions:
            if action.help == argparse.SUPPRESS or action.option_strings:
                continue
            if getattr(action, "required_for_help", False):
                action = copy(action)
                action.nargs = None
            positionals.append(action)
        synopsis = " ".join(formatter._format_args(action, action.dest.upper()) for action in positionals)
        parts = [name, synopsis] if synopsis else [name]
        parts.extend(f"[{option}]" for option in self.overview_options)
        return " ".join(parts)

    def format_help(self):
        formatter = self._get_formatter()
        commands = self.commands()
        if commands:
            descriptions = {choice.dest: choice.help for action in self._actions
                            if isinstance(action, argparse._SubParsersAction)
                            for choice in action._choices_actions}
            lines = [f"{self.prog} — {self.description}", "", self.format_usage().rstrip()]
            for run_command, title in ((False, "commands:"),
                                       (True, "inside a run, through the launcher's report_command:")):
                rows = [(command.overview_synopsis(name), descriptions[name])
                        for name, command in commands.items()
                        if command.run_command == run_command and not command.overview_hidden]
                if rows:
                    lines.extend(["", title, *formatter.rows(rows)])
            lines.extend(["", "options:", *formatter.option_rows(self._actions)])
        else:
            lines = [self.format_usage().rstrip(), "", *textwrap.wrap(self.description, width=80)]
            actions = [action for action in self._actions if not isinstance(action, argparse._HelpAction)]
            actions.extend(action for action in self._actions if isinstance(action, argparse._HelpAction))
            lines.extend(["", "options:", *formatter.option_rows(actions)])
            if self.examples:
                lines.extend(["", "examples:", *(f"  {example}" for example in self.examples)])
        return "\n".join(lines) + "\n"

    def unknown_command(self, name):
        self.exit(2, f'{self.prog}: unknown command "{name}"\n\n{self.format_help()}')

    def _check_value(self, action, value):
        if isinstance(action, argparse._SubParsersAction) and value not in action.choices:
            self.unknown_command(value)
        return super()._check_value(action, value)

    def parse_args(self, args=None, namespace=None):
        parsed, extras = self.parse_known_args(args, namespace)
        if extras:
            # argparse otherwise reports leftover command options at the root.
            command = self.commands().get(getattr(parsed, "command", None), self)
            command.error(f"unrecognized arguments: {' '.join(extras)}")
        return parsed

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")
