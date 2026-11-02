"""Idle-only Homebrew launcher upgrades, gated by every intervening release."""

from dataclasses import dataclass
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import re
import time

from . import __version__
from .launcher_code import source_directory
from .runtime_installations import writable
from .runtime_updates import MaintenanceFailure, RuntimeMaintenance
from .state import lock, user_state_directory
from .updates import REPOSITORY, version

FORMULA = "uberblick-ai/tap/ub-agents"
MARKER = r"<!--\s*ub-agents:launcher-upgrade=(.*?)\s*-->"


class RestartLauncher(Exception):
    def __init__(self, executable, stop_event, root):
        self.executable, self.stop_event, self.root = executable, stop_event, root


@dataclass(frozen=True)
class BrewInstallation:
    prefix: Path
    cellar: Path

    @property
    def brew(self):
        return str(self.prefix / "bin/brew")

    def current(self):
        keg = (self.prefix / "opt/ub-agents").resolve(strict=True)
        if keg.parent != self.cellar:
            raise ValueError("Homebrew opt link does not point to the ub-agents formula")
        receipt = json.loads((keg / "INSTALL_RECEIPT.json").read_text())
        if receipt.get("source", {}).get("tap") != "uberblick-ai/tap":
            raise ValueError("installed ub-agents formula is not from uberblick-ai/tap")
        return str(keg / "bin/ub-agents")


def installation(source=None):
    source = Path(source or source_directory()).resolve()
    for parent in source.parents:
        if parent.name == "ub-agents" and parent.parent.name == "Cellar":
            install = BrewInstallation(parent.parent.parent, parent)
            try:
                keg = source.relative_to(parent).parts[0]
                receipt = json.loads((parent / keg / "INSTALL_RECEIPT.json").read_text())
                if receipt.get("source", {}).get("tap") != "uberblick-ai/tap":
                    return None, "Homebrew formula is not from uberblick-ai/tap"
                if not Path(install.brew).is_file():
                    return None, "owning Homebrew executable is missing"
                if not writable(install.prefix) or not writable(parent):
                    return None, "Homebrew installation needs elevated privileges; update manually"
                return install, None
            except (OSError, ValueError, TypeError, AttributeError, IndexError):
                return None, "Homebrew installation receipt is missing or unreadable"
    return None, "pip, source or other installation; only uberblick-ai/tap/ub-agents qualifies"


def release_range(pages, running, target):
    """Missing, malformed or ambiguous markers all fail closed."""
    if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
        raise ValueError("release markers could not be read")
    found = set()
    found_target = False
    for page in pages:
        for release in page:
            match = _release_version(release)
            if match is None:
                continue
            candidate = version(match[1])
            # Both endpoints are stable versions. A prerelease with the same
            # core as running is older; one with the target's core precedes it.
            if not version(running) < candidate <= version(target):
                continue
            identity = candidate, match[2]
            if identity in found:
                raise ValueError("duplicate release versions; release markers are ambiguous")
            found.add(identity)
            if candidate == version(target) and match[2] is None:
                found_target = True
            _independent_release(release)
    if not found_target:
        raise ValueError(f"release markers through {target} could not be read")


def _release_version(release):
    if not isinstance(release, dict):
        raise ValueError("release markers could not be read")
    if release.get("draft") is True:
        return None
    if release.get("draft") is not False or not isinstance(release.get("prerelease"), bool):
        raise ValueError("release markers could not be read")
    tag = release.get("tag_name")
    match = (re.fullmatch(r"v?(\d+\.\d+\.\d+)(-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", tag)
             if isinstance(tag, str) else None)
    if match is None:
        raise ValueError("release version could not be read")
    return match


def _independent_release(release):
    body = release.get("body")
    markers = re.findall(MARKER, body) if isinstance(body, str) else []
    if markers == ["coordinated"]:
        raise ValueError(f"{release['tag_name']} requires every launcher to upgrade together")
    if markers != ["independent"]:
        raise ValueError(f"{release['tag_name']} has a missing or unreadable launcher-upgrade marker")


def _installed(install, run):
    executable = install.current()
    output = run([executable, "--version"])
    match = re.fullmatch(r"ub-agents (\d+\.\d+\.\d+)", output)
    if not match:
        raise ValueError("installed ub-agents version is unreadable")
    return executable, match[1]


def _release_markers(run, target):
    data = run(["gh", "api", "--hostname", "github.com", "--method", "GET",
                f"repos/{REPOSITORY}/releases?per_page=100", "--paginate", "--slurp"])
    release_range(json.loads(data), __version__, target)


class SelfUpdates:
    def __init__(self, output=print, stop_event=None, *, detect=installation, runner=None, clock=time.monotonic):
        self.output, self.stop_event, self.clock = output, stop_event, clock
        # The startup keg may disappear while another launcher upgrades. Retain
        # its verified ownership; subsequent version probes use the current opt.
        self.install, self.reason = detect()
        # Stop requests never interrupt Homebrew; its deadline still applies.
        self.runner = runner or partial(RuntimeMaintenance()._run, output_limit=16 * 1024 * 1024)
        self.checked = None
        self.skipped = set()

    def stopped(self):
        return self.stop_event is not None and self.stop_event.is_set()

    def skip(self, reason, updates):
        if reason not in self.skipped:
            self.output(f"Launcher maintenance ub-agents: skipped — {reason}")
            self.skipped.add(reason)
        if updates is not None and updates.banner:
            banner = dict(updates.banner)
            banner['text'] = banner['text'].split(" · automatic update skipped:", 1)[0]
            banner['text'] += f" · automatic update skipped: {reason}"
            updates.banner = banner

    def boundary(self, config, updates):
        settings = config.runtime_updates
        if settings is None or settings.policies.get("ub-agents", "off") != "auto" or self.stopped():
            return
        install, reason = self.install, self.reason
        if reason:
            if reason not in self.skipped:
                self.skip(reason, updates)
            return
        check = updates.release_check if updates is not None else None
        if not isinstance(check, tuple) or check[0] == self.checked:
            return
        self.checked = check[0]  # All failures wait for the next normal daily check.
        root = user_state_directory() / "launcher-updates"
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            key = hashlib.sha256(str(install.cellar).encode()).hexdigest()
            with lock(root / (key + ".lock")) as held:
                if held is None:
                    self.checked = None  # A peer is upgrading; inspect its result next idle boundary.
                    return
                self.check(config, updates, install, check[1], settings.timeout_seconds)
        except (MaintenanceFailure, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            detail = " ".join(str(exc).split())
            self.output(f"Launcher maintenance ub-agents: failed — {detail}; retry at next update check")
            self.skip(detail, updates)

    def _command(self, timeout):
        deadline = self.clock() + timeout
        env = dict(os.environ, HOMEBREW_NO_INSTALL_CLEANUP="1", HOMEBREW_NO_SUDO="1",
                   HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK="1", HOMEBREW_NO_AUTO_UPDATE="1",
                   HOMEBREW_NO_ASK="1", GH_PROMPT_DISABLED="1", GH_PAGER="cat")
        env.pop("GH_DEBUG", None)

        def run(command):
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise MaintenanceFailure(f"timed out after {timeout:g}s")
            return self.runner(command, env, remaining, cancellable=False)

        return run

    def _restart(self, config, executable, target):
        if self.stopped():
            self.output("Launcher maintenance ub-agents: shutdown requested; exiting without restart")
            return
        self.output(f"Launcher maintenance ub-agents: restarting {__version__} → {target}")
        raise RestartLauncher(executable, self.stop_event, config.root)

    def check(self, config, updates, install, latest, timeout):
        run = self._command(timeout)
        executable, current = _installed(install, run)
        if version(current) > version(__version__):
            if updates is not None and not updates.banner:
                updates.banner = {"text": f"⬆ ub-agents {current} is installed · you run {__version__} · "
                                          "review release notes before restarting the launcher"}
            _release_markers(run, current)
            self._restart(config, executable, current)
            return
        if latest is None or version(latest) <= version(__version__) or self.stopped():
            return
        if Path(run([install.brew, "--prefix"])).resolve() != install.prefix:
            raise ValueError("owning Homebrew prefix does not match the running installation")
        self.output(f"Launcher maintenance ub-agents: checking {FORMULA}")
        run([install.brew, "update"])
        data = json.loads(run([install.brew, "info", "--json=v2", "--formula", FORMULA]))
        formula, = data["formulae"]
        if formula["tap"] != "uberblick-ai/tap" or formula["name"] != "ub-agents":
            raise ValueError("Homebrew returned a different formula")
        target = formula["versions"]["stable"]
        if version(target) <= version(__version__):
            self.output("Launcher maintenance ub-agents: formula has no newer version; keeping running version")
            return
        _release_markers(run, target)
        if self.stopped():
            return
        self.output(f"Launcher maintenance ub-agents: upgrading to {target}")
        run([install.brew, "upgrade", "--formula", FORMULA])
        executable, current = _installed(install, run)
        if version(current) <= version(__version__):
            self.output("Launcher maintenance ub-agents: version unchanged; keeping running version")
            return
        # Another Homebrew process can change opt/the tap while ours runs.
        # Never restart on an unverified version, even after a successful command.
        if version(current) != version(target):
            _release_markers(run, current)
        self.output(f"Launcher maintenance ub-agents: upgrade succeeded ({current})")
        self._restart(config, executable, current)
