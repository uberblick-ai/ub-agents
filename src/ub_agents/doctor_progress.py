"""Stream Doctor progress events, rewriting only the terminal's waiting line.

Use DoctorProgress as a context manager and pass it as Doctor.run's progress
callback. Exiting the context joins the spinner and erases it on every path.
"""

import os
import threading

from rich.text import Text

from .doctor_output import (area_name, group_checks, output_console, render_area,
                            render_check, render_header, render_verdict)


class WaitingLine:
    def __init__(self, console):
        self.console = console
        self.enabled = console.file.isatty() and not os.environ.get("CI")
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.message = None
        self.frame = 0

    def update(self, message):
        if not self.enabled:
            return
        with self.lock:
            self.message = message
            self.draw()
        if self.thread is None:
            self.thread = threading.Thread(target=self.spin, daemon=True)
            self.thread.start()

    def draw(self):
        line = Text(f"  {'◒◐◓◑'[self.frame % 4]} {self.message}")
        # Keep the spinner on one physical line, including narrow terminals.
        line.truncate(max(0, self.console.width - 1), overflow="ellipsis")
        self.console.file.write("\r\x1b[2K" + line.plain)
        self.console.file.flush()
        self.frame += 1

    def spin(self):
        while not self.stop.wait(0.1):
            with self.lock:
                if self.message is not None:
                    self.draw()

    def clear(self):
        with self.lock:
            if self.message is not None:
                self.message = None
                self.console.file.write("\r\x1b[2K")
                self.console.file.flush()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        self.clear()


class DoctorProgress:
    def __init__(self, areas, *, verbose=False, stream=None):
        self.areas, self.verbose = areas, verbose
        self.width = max(len(area) for area in areas)
        self.console = output_console(stream)
        self.spinner = WaitingLine(self.console)
        self.label = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.spinner.close()

    def __call__(self, event, area, value):
        self.spinner.clear()
        if event == "header":
            render_header(self.console, value)
        elif event == "area-start":
            self.label = f"{area_name(area):<{self.width}}   checking prerequisites"
            if self.verbose:
                self.console.print(f"  {area_name(area)}")
        elif event == "step":
            self.label = f"{area_name(area):<{self.width}}   {value}"
        elif event == "check" and self.verbose:
            render_check(self.console, value, True)
        elif event == "area-end":
            render_area(self.console, area, value, self.width, details=not self.verbose)
        self.console.file.flush()
        if event not in {"header", "area-end"}:
            self.spinner.update(self.label)

    def finish(self, result, elapsed):
        self.spinner.clear()
        render_verdict(self.console, result, elapsed)
        self.console.file.flush()

    def rechecked_github(self, result, elapsed):
        self.spinner.clear()
        checks = group_checks(result, self.areas)["GitHub"]
        self.console.print()
        render_area(self.console, "GitHub", checks, self.width)
        self.finish(result, elapsed)
