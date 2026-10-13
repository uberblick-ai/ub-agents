"""Framed full-screen setup, with a line transcript for small/dumb terminals."""

import asyncio
import os
import shutil

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, VerticalScroll
from textual.widgets import Button, Checkbox, Input, Static

from .doctor import public
from .errors import AgentError
from .init_probes import PROBE_ERRORS


class SetupApp(App):
    CSS = """
    Screen { align: center middle; }
    #frame { width: 92; max-width: 100%; height: 38; max-height: 100%;
             border: round $accent; padding: 1 2; }
    #heading { height: auto; margin-bottom: 1; text-style: bold; }
    #body { height: 1fr; }
    #copy { height: auto; }
    .field-label { height: auto; margin-top: 1; }
    Input { margin-bottom: 1; }
    Checkbox { height: auto; margin-top: 1; width: 100%; }
    #error { height: auto; max-height: 4; color: $warning; }
    #actions { grid-size: 4 1; grid-gutter: 0 1; height: 3; margin-top: 1; }
    Button { min-width: 0; width: 1fr; }
    #frame.narrow { padding: 0 1; }
    .narrow #actions { grid-size: 2 2; height: 6; }
    """
    BINDINGS = [Binding("ctrl+n", "continue", "Continue / Apply", priority=True),
                Binding("ctrl+b", "back", "Back", priority=True),
                Binding("ctrl+s", "skip", "Skip", priority=True),
                Binding("escape", "cancel", "Cancel", priority=True),
                Binding("ctrl+c", "cancel", "Cancel", priority=True)]

    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard
        self.busy = False
        self.drafts = {}

    def compose(self) -> ComposeResult:
        with Container(id="frame"):
            yield Static(id="heading", markup=False)
            with VerticalScroll(id="body"):
                yield Static(id="copy", markup=False)
            yield Static(id="error", markup=False)
            with Grid(id="actions"):
                yield Button("Back ^B", id="back")
                yield Button("Continue ^N", id="continue", variant="primary")
                yield Button("Skip ^S", id="skip")
                yield Button("Cancel Esc", id="cancel")

    async def on_mount(self):
        await self.show_page()
        self.busy = True
        self.buttons()
        try:
            await asyncio.to_thread(self.wizard.probe)
        finally:
            self.busy = False
        await self.show_page()

    def on_resize(self, event):
        if self.is_mounted:
            self.query_one("#frame").set_class(event.size.width < 80, "narrow")

    def buttons(self):
        current = self.wizard.current
        self.query_one("#back", Button).disabled = self.busy or current in {"welcome", "result"}
        self.query_one("#skip", Button).disabled = self.busy or not self.wizard.page().optional
        self.query_one("#cancel", Button).disabled = self.busy or current == "result"
        forward = self.query_one("#continue", Button)
        forward.label = "Finish ^N" if current == "result" else "Apply ^N" if current == "review" else "Continue ^N"
        confirm = self.query("#field-confirm")
        forward.disabled = self.busy or (current == "review" and (not confirm or not confirm.first(Checkbox).value))

    async def show_page(self):
        page = self.wizard.page()
        pages = self.wizard.pages()
        self.query_one("#frame").set_class(self.size.width < 80, "narrow")
        self.query_one("#heading", Static).update(f"ub-agents setup  ·  {pages.index(self.wizard.current) + 1}/{len(pages)}\n{page.title}")
        body = self.query_one("#body", VerticalScroll)
        await body.remove_children()
        await body.mount(Static(page.text, id="copy", markup=False))
        draft = self.drafts.get(self.wizard.current, {})
        for field in page.fields:
            value = draft.get(field.name, field.value)
            if isinstance(field.value, bool):
                await body.mount(Checkbox(field.label, value=bool(value), id="field-" + field.name))
            else:
                await body.mount(Static(field.label, classes="field-label", markup=False),
                                 Input(str(value), id="field-" + field.name))
        body.scroll_home(animate=False)
        self.query_one("#error", Static).update("")
        self.buttons()
        if self.wizard.current == "review":
            self.query_one("#back").focus()
        else:
            fields = list(body.query(Input))
            (fields[0] if fields else self.query_one("#continue")).focus()

    def values(self):
        values = {}
        for field in self.wizard.page().fields:
            widget = self.query_one("#field-" + field.name)
            values[field.name] = widget.value
        return values

    def on_checkbox_changed(self, event):
        if event.checkbox.id == "field-confirm":
            self.buttons()

    async def on_button_pressed(self, event):
        event.stop()
        await getattr(self, "action_" + event.button.id)()

    async def action_continue(self):
        if self.busy:
            return
        if self.wizard.current == "result":
            self.exit(self.wizard.applied.ok)
            return
        values = self.values()
        self.drafts[self.wizard.current] = values
        self.busy = True
        self.buttons()
        self.query_one("#error", Static).update("Applying reviewed changes..." if self.wizard.current == "review" else "Checking choices...")
        try:
            await asyncio.to_thread(self.wizard.advance, values)
        except PROBE_ERRORS as exc:
            self.query_one("#error", Static).update(public(exc))
        else:
            # Confirmation is never retained when revisiting the review.
            self.drafts.pop("review", None)
            await self.show_page()
        finally:
            self.busy = False
            self.buttons()

    async def action_back(self):
        if self.busy or self.wizard.current in {"welcome", "result"}:
            return
        self.drafts[self.wizard.current] = self.values()
        self.wizard.back()
        await self.show_page()

    async def action_skip(self):
        if self.busy or not self.wizard.page().optional:
            return
        self.drafts.pop(self.wizard.current, None)
        self.wizard.skip()
        await self.show_page()

    async def action_cancel(self):
        if self.busy:
            return
        if self.wizard.current == "result":
            self.exit(self.wizard.applied.ok)
        else:
            self.wizard.cancelled = True
            self.exit(True)


def line_wizard(wizard):
    """Keep all content readable and all choices explicit without cursor control."""
    try:
        print(wizard.page().text)
        wizard.probe()
        while True:
            page = wizard.page()
            pages = wizard.pages()
            print(f"\n[{pages.index(wizard.current) + 1}/{len(pages)}] {page.title}\n\n{page.text}\n")
            if wizard.current == "result":
                input("Finish [Enter] ")
                return wizard.applied.ok
            action = input("[b] Back  [Enter] Continue  " + ("[s] Skip  " if page.optional else "") + "[c] Cancel: ").strip().casefold()
            if action in {"c", "cancel"}:
                wizard.cancelled = True
                return True
            if action in {"b", "back"}:
                wizard.back()
                continue
            if action in {"s", "skip"} and page.optional:
                wizard.skip()
                continue
            if action not in {"", "continue"}:
                continue
            values = {}
            for field in page.fields:
                if isinstance(field.value, bool):
                    answer = input(field.label + "? [y/N] ").strip().casefold()
                    values[field.name] = answer in {"y", "yes"}
                else:
                    answer = input(f"{field.label} [{field.value}] (- to clear): ")
                    values[field.name] = "" if answer == "-" else answer if answer else field.value
            try:
                wizard.advance(values)
            except PROBE_ERRORS as exc:
                print(public(exc))
    except (EOFError, KeyboardInterrupt):
        if wizard.applied is not None:
            return wizard.applied.ok
        wizard.cancelled = True
        return True


def run_wizard(wizard):
    size = shutil.get_terminal_size()
    if os.environ.get("TERM") in {"dumb", "unknown"} or size.columns < 40 or size.lines < 16:
        ok = line_wizard(wizard)
    else:
        ok = SetupApp(wizard).run()
    print(wizard.result_text())
    return 0 if ok else 1
