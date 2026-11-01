"""Small widgets shared by the optional Textual view."""

from rich.text import Text
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Static, TabbedContent, Tabs

from ..updates import release_age
from .data import mapping, text
from .scroll import PaneScroll
from .status import pane_line
from ..launcher_lock import configuration_pause_summary

SIZE_WARNING = 'Please enlarge the terminal to at least 60×16.'


class UpdateBanner(Static):
    """A single inert row; Rich measures truncation in terminal cells."""
    def __init__(self, *, id='update'):
        super().__init__('', id=id, markup=False)
        self.banner = {}

    def set_banner(self, banner):
        banner = mapping(banner)
        self.display = (not self.app.too_small and self.app.shutdown is None and bool(banner.get('text'))
                        and isinstance(banner.get('text'), str))
        if self.banner != banner:
            self.banner = banner
            self.refresh()

    def render(self):
        width = max(0, self.content_size.width)
        line = Text(text(self.banner.get('text'), ''), no_wrap=True)
        age = release_age(self.banner.get('released_at')) if width >= 60 else ''
        room = max(0, width - len(age) - 2) if age else width
        line.truncate(room, overflow='ellipsis')
        if age:
            line.append(' ' * max(2, width - line.cell_len - len(age)) + age)
        return line


class HealthBanner(UpdateBanner):
    """The latest project health transition, including recovery, in one row."""
    def __init__(self):
        super().__init__(id='health_notice')


class ConfigurationPause(Static):
    """Keep the whole pause diagnostic and remedy readable at any width."""
    def __init__(self):
        super().__init__('', id='configuration_pause', markup=False)
        self.pause = None

    def set_pause(self, pause):
        pause = mapping(pause)
        self.pause = pause if all(isinstance(pause.get(key), str) for key in ('error', 'commit', 'next_step')) else None
        self.display = bool(self.pause) and not self.app.too_small and self.app.shutdown is None
        self.update(Text(configuration_pause_summary(self.pause)) if self.pause else '')


class ItemHeader(Static):
    @property
    def link_style(self):
        # Decorate links without replacing the title and PR marker colors.
        style = self.styles.link_style
        # Textual gives the accent marker and number separate hover IDs.
        if '@click' in self.hover_style.meta:
            style += self.link_style_hover
        return style


class ItemTabs(TabbedContent):
    """One header below the tab bar, shared even by subsequently added tabs."""

    def compose(self):
        for widget in super().compose():
            yield widget
            if isinstance(widget, Tabs):
                yield Static('', id='log_mode', markup=False)
                yield Static('', id='tab_rule', markup=False)
                yield ItemHeader('', id='item_header', markup=False)


class RawAccess(ModalScreen):
    BINDINGS = [Binding('escape', 'dismiss', 'Close', priority=True)]
    DEFAULT_CSS = '''
    RawAccess { padding: 2 4; }
    RawAccess VerticalScroll { background: $panel; padding: 1 2; }
    #raw_status { height: 1; background: $panel; }
    #raw_size_warning { height: 1fr; content-align: center middle; text-wrap: nowrap; text-overflow: ellipsis; display: none; }
    RawAccess.floor { padding: 0; }
    '''
    footer_keys = 'Esc close ? keys q quit'

    def __init__(self, message=None):
        super().__init__()
        self.message = message

    def compose(self):
        # Read the view's state now: updates that land between the push and
        # this compose find no widgets to update.
        yield Static(Text(SIZE_WARNING, no_wrap=True, overflow='ellipsis'), id='raw_size_warning')
        with PaneScroll(id='raw_content'):
            yield Static(Text(self.message if self.message is not None else self.app.raw_details()), id='raw_details')
        yield Static(self.app.footer(self.app.size.width - self.styles.padding.width, self.footer_keys),
                     id='raw_status', markup=False)

    def set_floor(self, too_small):
        self.set_class(too_small, 'floor')
        self.query_one('#raw_size_warning').display = too_small
        self.query_one('#raw_content').display = not too_small
        self.query_one('#raw_status').display = not too_small

    def on_mount(self):
        self.set_floor(self.app.too_small)

    def action_dismiss(self):
        self.dismiss()


class KeyHelp(RawAccess, inherit_bindings=False):
    BINDINGS = [Binding('escape,question_mark', 'dismiss', 'Close', priority=True)]
    footer_keys = 'Esc/? close q quit'

    def __init__(self, unblock=False, attached=False, awake_supported=False):
        unblock_keys = ('4 on Needs attention   Unblock\n'
                        'g on Unblock   Load the action-needed comment or retry a failed read\n') if unblock else ''
        poll_key = 'r   Poll GitHub now (attached launcher only; 10s cooldown)\n' if attached else ''
        awake_key = ('c   Toggle keeping the launcher machine awake (macOS only'
                     + (')\n' if awake_supported else '; unavailable on this platform)\n')) if attached else ''
        super().__init__(
            'Keys\n\n'
            'Tab / arrows / Enter   Focus a pane and select a work row\n'
            'Enter below 110×32   Open the selected item at full width\n'
            'Esc below 110×32   Return to Work; close an overlay first\n'
            '1 / 2 / 3   Agent / Issue / Runs\n'
            + unblock_keys +
            'g on Agent   Reload the selected local log at the end and follow it\n'
            'g on Issue   Load a missing description or retry a failed read\n'
            'f   Toggle follow/pause; resuming loads the latest generation\n'
            'h   Read an older bounded page toward byte zero\n'
            'u   Toggle formatted/raw projection of the same page\n'
            'p   Show the full raw path and log diagnostics; Escape closes it\n'
            + poll_key + awake_key +
            'Page Up / Page Down / Home / End   Scroll; scrolling up pauses follow\n'
            'Mouse drag   Copy selected text on release (OSC 52)\n'
            'y   Copy the current selection again\n'
            '?   Open or close this help; Escape also closes it\n'
            'q   Stop after run; close a standalone view\n'
            'Ctrl-C   Stop now; close a standalone view\n\n'
            'Local macOS also uses pbcopy when available.\n'
            'In iTerm2, enable "Applications in terminal may access clipboard" for OSC 52.\n'
            'Terminal.app does not support OSC 52; local macOS uses pbcopy instead.\n'
            "Option-drag selects with the terminal's own selection.")
