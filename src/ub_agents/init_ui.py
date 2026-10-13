"""Framed full-screen setup, with a line transcript for small/dumb terminals."""

import asyncio
import os
import shutil

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, VerticalScroll
from textual.widgets import Button, Checkbox, Input, Static

from .doctor import public
from .init_probes import PROBE_ERRORS


class SetupApp(App):
    CSS = """
    Screen { layout: vertical; }
    #frame { width: 100%; height: 1fr;
             border: round $accent; padding: 0 1; }
    #heading { height: auto; margin-bottom: 1; text-style: bold; }
    #body { height: 1fr; }
    #copy { height: auto; }
    .field-label { height: auto; margin-top: 1; }
    Input { margin-bottom: 1; }
    Checkbox { height: auto; margin-top: 1; width: 100%; }
    #error { height: auto; max-height: 4; color: $warning; }
    #actions { grid-size: 4 1; grid-gutter: 0 1; height: 1; margin-top: 1; }
    Button { min-width: 0; width: 1fr; height: 1; border: none; padding: 0; text-overflow: clip; }
    #notes { height: 3; padding: 0 1; }
    """
    BINDINGS = [Binding("ctrl+n", "continue", "Continue / Apply", priority=True),
                Binding("ctrl+b", "back", "Back", priority=True),
                Binding("ctrl+s", "skip", "Skip", priority=True),
                Binding("y", "permissions_yes", "Accept permissions", show=False),
                Binding("n", "permissions_no", "Without permissions", show=False),
                Binding("ctrl+r", "retry", "Retry", priority=True),
                Binding("ctrl+l", "start", "Finish and start loop", priority=True),
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
                yield Button("Retry ^R", id="retry")
                yield Button("Finish and start loop ^L", id="start")
        yield Static(id="notes", markup=False)

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
            self.query_one("#frame").set_class(event.size.width < 92, "narrow")
            self.buttons()

    def buttons(self):
        current = self.wizard.current
        narrow = self.size.width < 92
        permissions = current == "permissions"
        result = current == "result"
        actions = self.query_one("#actions", Grid)
        actions.styles.grid_size_columns = 3 if permissions or result else 4
        actions.styles.grid_columns = ("1fr 1fr 1fr" if narrow else "35 34 14") if permissions else (
            "1fr 1fr 1fr" if narrow else "1fr 1fr 2fr") if result else (
            "7 9 7 10" if narrow else "1fr 1fr 1fr 1fr")
        for id in ("back", "continue", "skip", "cancel", "retry", "start"):
            self.query_one("#" + id, Button).display = id in (
                {"continue", "skip", "cancel"} if permissions else
                {"continue", "retry", "start"} if result else
                {"back", "continue", "skip", "cancel"})
        self.query_one("#back", Button).disabled = self.busy or current in {"welcome", "result"}
        skip = self.query_one("#skip", Button)
        skip.disabled = self.busy or (not permissions and not self.wizard.page().optional)
        skip.label = ("Without (n)" if narrow else "[Continue without permissions (n)]") if permissions else "Skip ^S"
        self.query_one("#cancel", Button).disabled = self.busy or current == "result"
        self.query_one("#cancel", Button).label = ("Cancel esc" if narrow else "[Cancel (esc)]") if permissions else "Cancel Esc"
        forward = self.query_one("#continue", Button)
        forward.label = (("Accept (y)" if narrow else "[Accept and enable permissions (y)]") if permissions else
                         "Finish ^N" if result else "Apply ^N" if current == "review" else
                         "Retry ^N" if self.wizard.board_waiting and current == "retrospectives" else
                         "Next ^N" if narrow else "Continue ^N")
        self.query_one("#retry", Button).disabled = self.busy
        self.query_one("#retry", Button).label = "Retry ^R" if narrow else "Retry readiness ^R"
        self.query_one("#start", Button).disabled = self.busy or not (self.wizard.applied and self.wizard.applied.ok)
        self.query_one("#start", Button).label = "Start ^L" if narrow else "Finish and start loop ^L"
        confirm = self.query("#field-confirm")
        forward.disabled = self.busy or (current == "review" and (not confirm or not confirm.first(Checkbox).value))

    async def show_page(self):
        page = self.wizard.page()
        pages = self.wizard.pages()
        self.query_one("#frame").set_class(self.size.width < 92, "narrow")
        self.query_one("#heading", Static).update(f"ub-agents setup  ·  {pages.index(self.wizard.current) + 1}/{len(pages)}\n{page.title}")
        body = self.query_one("#body", VerticalScroll)
        await body.remove_children()
        preview = (self.wizard.addition_preview() if self.wizard.current in {"priority", "retrospectives"}
                   and self.wizard.current in self.wizard.option_reviews else "")
        copy = page.text.replace(preview, "", 1) if preview else page.text
        await body.mount(Static(copy, id="copy", markup=False))
        draft = self.drafts.get(self.wizard.current, {})
        for field in page.fields:
            value = draft.get(field.name, field.value)
            if isinstance(field.value, bool):
                await body.mount(Checkbox(field.label, value=bool(value), id="field-" + field.name))
            else:
                await body.mount(Static(field.label, classes="field-label", markup=False),
                                 Input(str(value), id="field-" + field.name))
        if preview:
            await body.mount(Static(preview + "\n\nContinue confirms these additions; Skip keeps the feature off.",
                                    id="option-preview", markup=False))
        body.scroll_home(animate=False)
        self.query_one("#error", Static).update("")
        notes = ("Intent: shared knowledge follow-up. Consequence: no integration is enabled.\nLink destination: https://uberblick.at"
                 if self.wizard.current == "knowledge" else
                 "Intent: lessons. Effect: public agent posts.\nLink: GitHub Docs (creating discussions)."
                 if self.wizard.current == "retrospectives" else
                 "Intent: order eligible work within queue rules. Consequence: skipped priority leaves equal ordering."
                 if self.wizard.current in {"priority", "priority-labels"} else "")
        self.query_one("#notes", Static).update(notes)
        self.buttons()
        # Reused footer buttons belong to the new page immediately, even when
        # the preceding page's button press still has its active animation.
        for button in self.query(Button):
            button.remove_class("-active")
        if self.wizard.current == "review":
            self.query_one("#back").focus()
        else:
            fields = list(body.query(Input))
            (self.query_one("#skip") if page.optional or self.wizard.current == "permissions" else
             fields[0] if fields else self.query_one("#continue")).focus()
        if preview:
            # Show the checked additions before the user can confirm, including
            # when the preceding form was taller than the viewport.
            self.call_after_refresh(self.query_one("#option-preview").scroll_visible, animate=False, top=True)

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
        if self.wizard.current == "permissions":
            values = {"permissions": True}
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
        if self.busy:
            return
        if self.wizard.current == "permissions":
            try:
                self.wizard.advance({"permissions": False})
            except PROBE_ERRORS as exc:
                self.query_one("#error", Static).update(public(exc))
                return
            await self.show_page()
            return
        if not self.wizard.page().optional:
            return
        self.drafts.pop(self.wizard.current, None)
        try:
            self.wizard.skip()
        except PROBE_ERRORS as exc:
            self.query_one("#error", Static).update(public(exc))
            return
        await self.show_page()

    async def action_permissions_yes(self):
        if self.wizard.current == "permissions":
            await self.action_continue()

    async def action_permissions_no(self):
        if self.wizard.current == "permissions":
            await self.action_skip()

    async def action_retry(self):
        if self.wizard.current == "retrospectives":
            await self.action_continue()
        elif self.wizard.current == "result" and not self.busy:
            self.busy = True
            self.buttons()
            try:
                await asyncio.to_thread(self.wizard.retry_readiness)
                await self.show_page()
            finally:
                self.busy = False
                self.buttons()

    async def action_start(self):
        if self.wizard.current != "result" or self.busy:
            return
        self.busy = True
        self.buttons()
        try:
            if await asyncio.to_thread(self.wizard.request_start):
                self.exit(True)
            else:
                await self.show_page()
        finally:
            self.busy = False
            self.buttons()

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
                action = input("[Enter] Finish  [l] Finish and start loop  [r] Retry readiness: ").strip().casefold()
                if action == "r":
                    wizard.retry_readiness()
                    continue
                if action == "l":
                    if wizard.request_start():
                        return True
                    continue
                if action == "":
                    return wizard.applied.ok
                continue
            permissions = wizard.current == "permissions"
            prompt = ("[Accept and enable permissions (y)] [Continue without permissions (n)] [Cancel (esc)] (default n): "
                      if permissions else "[b] Back  [Enter] Skip  [n] Continue  [r] Retry  [c] Cancel: "
                      if page.optional else "[b] Back  [Enter] Continue  [c] Cancel: ")
            action = input(prompt).strip().casefold()
            if action in {"c", "cancel", "esc", "\x1b"}:
                wizard.cancelled = True
                return True
            if action in {"b", "back"}:
                wizard.back()
                continue
            if permissions:
                if action in {"", "n", "y"}:
                    try:
                        wizard.advance({"permissions": action == "y"})
                    except PROBE_ERRORS as exc:
                        print(public(exc))
                continue
            if action in {"", "s", "skip"} and page.optional:
                try:
                    wizard.skip()
                except PROBE_ERRORS as exc:
                    print(public(exc))
                continue
            if action not in {"", "n", "r", "continue"}:
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
    if ok and wizard.start_loop:
        from .cli import main
        return main(["launch", "--config", str(wizard.plan.path)]) or 0
    return 0 if ok else 1
