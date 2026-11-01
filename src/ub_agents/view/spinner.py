"""Shared terminal-view spinner frames and cadence."""

SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
SPINNER_FPS = 10


def spinner_frame(seconds):
    return SPINNER[int(seconds * SPINNER_FPS) % len(SPINNER)]
