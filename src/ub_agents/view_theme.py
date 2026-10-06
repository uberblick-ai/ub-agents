"""Terminal palette and theme-resolved Rich styles."""

from rich.style import Style
from rich.text import Text
from textual.color import Color
from textual.theme import Theme


VIEW_THEME = Theme(
    name='ub-agents', dark=True,
    background='#0d1016', surface='#0d1016', panel='#161a22',
    foreground='#d4d9e1', primary='#6cb6ff', secondary='#b79cff',
    accent='#b79cff', success='#7ee2a0', error='#ff8b7f', warning='#ff8b7f',
    variables={
        'view-accent': '#b79cff',
        'view-assistant': '#c8cdd6',
        'view-muted': '#6b7484',
        'view-border': '#2a303b',
        'view-selection': '#1b2030',
        'view-running': '#6cb6ff',
        'view-attention': '#ff8b7f',
        'view-eligible': '#7ee2a0',
        'view-success': '#7ee2a0',
        'view-error': '#ff8b7f',
        'view-warning': '#ff8b7f',
        'view-priority-urgent': '#e0524a',
        'view-priority-high': '#c98a86',
        'view-priority-low': '#86a891',
    },
)

SECTION_COLORS = {'Running': 'view-running', 'Needs attention': 'view-attention',
                  'Eligible': 'view-eligible'}


def variable_defaults(theme):
    """Derive readable app colors for themes without our custom variables."""
    colors = theme.to_color_system().generate()
    foreground, background = Color.parse(colors['foreground']), Color.parse(colors['background'])
    return {
        'view-accent': colors['accent' if theme.dark else 'primary'],
        'view-assistant': foreground.blend(background, 0.05).hex,
        'view-muted': foreground.blend(background, 0.4).hex,
        'view-border': colors['surface-lighten-2' if theme.dark else 'surface-darken-2'],
        'view-selection': colors['primary-muted'],
        'view-running': colors['text-primary'],
        'view-attention': colors['text-error'],
        'view-eligible': colors['text-success'],
        'view-success': colors['text-success'],
        'view-error': colors['text-error'],
        'view-warning': colors['text-warning'],
        'view-priority-urgent': colors['text-error'],
        'view-priority-high': Color.parse(colors['text-error']).blend(foreground, 0.4).hex,
        'view-priority-low': Color.parse(colors['text-success']).blend(foreground, 0.4).hex,
    }


def theme_style(app, variable, **attributes):
    variables = app.theme_variables if app else {
        **VIEW_THEME.to_color_system().generate(), **VIEW_THEME.variables}
    return Style(color=Color.parse(variables[variable]).rich_color, **attributes)


def item_reference(number, kind, *, app=None):
    reference = Text(('⌥' if kind == 'pr' else '#') + str(number))
    if kind == 'pr':
        reference.stylize(theme_style(app, 'view-accent'), 0, 1)
    return reference


def log_style(app, token):
    variables = {'assistant': 'view-assistant', 'diff-add': 'view-success', 'diff-remove': 'view-error',
                 'error': 'view-error', 'dim': 'view-muted', 'dim italic': 'view-muted'}
    if token in variables:
        return theme_style(app, variables[token], dim=token.startswith('dim'),
                           italic=token == 'assistant' or token.endswith('italic'))
    return Style.parse(token)
