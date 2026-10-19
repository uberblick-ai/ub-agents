"""The active setup question, drawn inline under the shell prompt in Clack style."""

from rich.text import Text
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Input, Static

from .init_prompts import CYAN, DIM, GREEN, RED


class Question(App):
    INLINE_PADDING = 0
    CSS = """
    Screen, Screen:inline { height: auto; min-height: 1; border: none; padding: 0; background: transparent; }
    Static { height: auto; }
    #entry { height: 1; }
    #rail { width: 3; color: $text-muted; }
    Input, Input:focus { border: none; height: 1; padding: 0; background: transparent; width: 1fr; }
    """
    BINDINGS = [Binding("up,k", "move(-1)", show=False), Binding("down,j", "move(1)", show=False),
                Binding("left,h", "move(-1)", show=False), Binding("right,l", "move(1)", show=False),
                Binding("space", "toggle", show=False), Binding("enter", "submit", show=False),
                Binding("escape,ctrl+c", "cancel", show=False, priority=True)]

    def __init__(self, question):
        super().__init__(ansi_color=True)
        self.q = question
        self.kind, self.options = question["kind"], question.get("options", [])
        self.cursor = question.get("cursor", 0)
        self.checked = set(question.get("checked", ()))
        self.error = ""

    def compose(self):
        if self.kind == "text":
            yield Static(self.head(), id="head")
            with Horizontal(id="entry"):
                yield Static(Text("│  ", style=CYAN), id="rail")
                yield Input(self.q.get("default", ""), id="value")
            yield Static(self.tail(), id="tail")
        else:
            yield Static(self.render_question(), id="question")

    def head(self):
        t = Text.assemble(("◆", CYAN), "  ", self.q["message"])
        for line in self.q.get("lines", []):
            t.append("\n"); t.append("│  ", style=CYAN); t.append(line, style=DIM)
        return t

    def tail(self):
        return Text.assemble(("│  ", CYAN), (self.error, RED), "\n", ("└", CYAN)) if self.error else Text("└", style=CYAN)

    def render_question(self):
        t = self.head()
        if self.kind == "multi":
            t.append("  (space selects, enter confirms)", style=DIM)
        t.append("\n")
        if self.kind == "confirm":
            t.append("│  ", style=CYAN)
            for index, option in enumerate(self.options):
                on = index == self.cursor
                t.append("● " if on else "○ ", style=GREEN if on else DIM)
                t.append(option.label, style="" if on else DIM)
                if index < len(self.options) - 1:
                    t.append(" / ", style=DIM)
            t.append("\n")
        else:
            for index, option in enumerate(self.options):
                on = index == self.cursor
                t.append("│  ", style=CYAN)
                if self.kind == "multi":
                    picked = index in self.checked
                    t.append("◼ " if picked else "◻ ", style=DIM if option.disabled else GREEN if picked else CYAN if on else DIM)
                else:
                    t.append("● " if on else "○ ", style=GREEN if on else DIM)
                style = DIM if option.disabled or not (on or index in self.checked) else ""
                if option.disabled and option.strike:
                    style += " strike"
                t.append(option.label, style=style)
                hint = option.disabled or (option.hint if on else "")
                if hint:
                    t.append(f" ({hint})", style=DIM)
                t.append("\n")
        t.append("└", style=CYAN)
        return t

    def redraw(self):
        self.query_one("#question", Static).update(self.render_question())

    def action_move(self, delta):
        if self.kind == "text":
            return
        self.cursor = (self.cursor + delta) % len(self.options)
        self.redraw()

    def action_toggle(self):
        if self.kind == "multi" and not self.options[self.cursor].disabled:
            self.checked ^= {self.cursor}
            self.redraw()

    def action_submit(self):
        if self.kind == "text":
            self.submit_text(self.query_one(Input).value)
        elif self.kind == "multi":
            self.exit([self.options[i].value for i in sorted(self.checked)])
        else:
            self.exit(self.options[self.cursor].value)

    def on_input_submitted(self, event):
        event.stop()
        self.submit_text(event.value)

    def submit_text(self, value):
        value = value.strip()
        validate = self.q.get("validate")
        self.error = (validate(value) if validate else None) or ""
        if self.error:
            self.query_one("#tail", Static).update(self.tail())
        else:
            self.exit(value)

    def action_cancel(self):
        self.exit(None)
