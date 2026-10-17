"""Clack-style setup prompts: a transcript on stdout plus one small inline question at a time."""

from contextlib import contextmanager
from dataclasses import dataclass
import textwrap
import threading

from rich.console import Console
from rich.text import Text

DIM, CYAN, GREEN, YELLOW, RED = "bright_black", "cyan", "green", "yellow", "red"


def note_lines(note):
    return [note] if isinstance(note, str) and note else [line for line in note if line] if note else []


class Cancelled(Exception):
    """The person cancelled at a question; nothing has been written."""


@dataclass(frozen=True)
class Option:
    value: object
    label: str
    hint: str = ""
    disabled: str = ""


class Transcript:
    """Everything that stays in scrollback: intro, answered questions, notes and outro."""

    def __init__(self, console=None):
        self.console = console or Console(highlight=False, soft_wrap=True)

    def line(self, *parts):
        self.console.print(Text.assemble(*parts))

    def rail(self, text=""):
        self.line(("│", DIM), *(("  ", text if isinstance(text, Text) else Text(text)) if text else ()))

    def intro(self, title, detail):
        self.line(("┌  ", DIM), (f" {title} ", "black on cyan"), (f" {detail}", DIM))
        self.rail()

    def step(self, title, lines=(), mark="◇", color=GREEN):
        self.line((mark, color), "  ", title)
        for line in lines:
            self.rail(line)
        self.rail()

    def answered(self, message, value):
        self.step(message, [Text(value, style=DIM)])

    def note(self, title, lines, width=76):
        wrapped = []
        for line in lines:
            if isinstance(line, Text):
                wrapped.append(line)
            else:
                indent = " " * (len(line) - len(line.lstrip("0123456789. ")))
                wrapped.extend(Text(part) for part in textwrap.wrap(line, width, subsequent_indent=indent,
                                                                    break_on_hyphens=False, break_long_words=False))
        lines = wrapped
        width = max([len(line.plain) for line in lines] + [len(title)]) + 4
        self.line(("◇", GREEN), "  ", title, " ", ("─" * (width - len(title) - 1) + "╮", DIM))
        self.line(("│" + " " * (width + 2) + "│", DIM))
        for line in lines:
            self.line(("│  ", DIM), line, (" " * (width - len(line.plain)) + "│", DIM))
        self.line(("│" + " " * (width + 2) + "│", DIM))
        self.line(("├" + "─" * (width + 2) + "╯", DIM))
        self.rail()

    def cancelled(self, message):
        self.line(("■", RED), "  ", message)
        self.line(("└  ", DIM), ("Setup cancelled. Nothing was written.", DIM))

    def outro(self, *parts):
        self.line(("└  ", DIM), *parts)

    @contextmanager
    def spinner(self, message):
        if not self.console.is_terminal:
            yield
            return
        stop = threading.Event()

        def spin():
            frames, index = "◒◐◓◑", 0
            while not stop.wait(0.1 if index else 0):
                self.console.file.write(f"\r\x1b[35m{frames[index % 4]}\x1b[0m  {message}...")
                self.console.file.flush()
                index += 1

        thread = threading.Thread(target=spin, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()
            self.console.file.write("\r\x1b[2K")
            self.console.file.flush()


class DefaultPrompter:
    """Unattended: every question takes its default. The flow records each answer."""

    def select(self, message, options, default=0, note=""):
        return options[default].value

    def multiselect(self, message, options, default=()):
        return [o.value for o in options if o.value in default and not o.disabled]

    def confirm(self, message, default=True, note=()):
        return default

    def text(self, message, default="", validate=None, note=()):
        return default


class LinePrompter:
    """Plain numbered prompts for terminals without cursor control (TERM=dumb)."""

    def ask(self, message, prompt):
        try:
            return input(f"{message} {prompt}").strip()
        except (EOFError, KeyboardInterrupt):
            raise Cancelled(message) from None

    def select(self, message, options, default=0, note=""):
        while True:
            print("\n  ".join([message, *note_lines(note)]))
            for number, option in enumerate(options, 1):
                print(f"  {number}. {option.label}" + (f" ({option.hint})" if option.hint else ""))
            answer = self.ask("Choice", f"[{default + 1}]: ")
            if not answer:
                return options[default].value
            if answer.isdecimal() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1].value

    def multiselect(self, message, options, default=()):
        while True:
            print(message)
            for number, option in enumerate(options, 1):
                hint = option.disabled or option.hint
                selected = " [selected]" if option.value in default and not option.disabled else ""
                print(f"  {number}. {option.label}{selected}" + (f" ({hint})" if hint else ""))
            answer = self.ask("Numbers, separated by commas", "(Enter for selected, 0 for none): " if default else
                              "(Enter for none): ")
            if not answer:
                return [o.value for o in options if o.value in default and not o.disabled]
            if answer == "0":
                return []
            parts = [part.strip() for part in answer.split(",") if part.strip()]
            if not all(part.isdecimal() and 1 <= int(part) <= len(options) for part in parts):
                continue
            chosen = [options[int(part) - 1] for part in dict.fromkeys(parts)]
            disabled = [option for option in chosen if option.disabled]
            if not disabled:
                return [o.value for o in chosen]
            for option in disabled:
                print(f"  {option.label} is unavailable: {option.disabled}")

    def confirm(self, message, default=True, note=()):
        for line in note:
            print(f"  {line}")
        while True:
            answer = self.ask(message, "[Y/n]: " if default else "[y/N]: ").casefold()
            if answer in {"", "y", "yes", "n", "no"}:
                return default if not answer else answer.startswith("y")

    def text(self, message, default="", validate=None, note=()):
        for line in note_lines(note):
            print(f"  {line}")
        while True:
            value = self.ask(message, f"[{default}]: " if default else ": ") or default
            error = validate(value) if validate else None
            if not error:
                return value
            print(f"  {error}")


class InlinePrompter:
    """One Textual inline app per question; it clears itself when answered."""

    def run(self, question):
        from .init_inline import Question
        value = Question(question).run(inline=True, mouse=False)
        if value is None:
            raise Cancelled(question["message"])
        return value

    def select(self, message, options, default=0, note=""):
        return self.run({"kind": "select", "message": message, "options": options, "cursor": default,
                         "lines": note_lines(note)})

    def multiselect(self, message, options, default=()):
        return self.run({"kind": "multi", "message": message, "options": options,
                         "cursor": next((i for i, o in enumerate(options) if not o.disabled), 0),
                         "checked": {i for i, o in enumerate(options) if o.value in default}})

    def confirm(self, message, default=True, note=()):
        return self.run({"kind": "confirm", "message": message, "options": [Option(True, "Yes"), Option(False, "No")],
                         "cursor": 0 if default else 1, "lines": note_lines(note)})

    def text(self, message, default="", validate=None, note=()):
        return self.run({"kind": "text", "message": message, "default": default, "validate": validate,
                         "lines": note_lines(note)})


def interactive_prompter(environ):
    return LinePrompter() if environ.get("TERM") in {"dumb", "unknown"} else InlinePrompter()
