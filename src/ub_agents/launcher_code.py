"""Keep a launcher, its runs and Python helpers on one startup package copy."""

from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import shutil
import sys
import tempfile

from .state import lock, user_state_directory

_active = None


def source_directory():
    # This module is loaded before copying. Keep the original install location
    # for update banners even when their module is imported lazily from the copy.
    return Path(__file__).resolve().parent


def package_directory():
    return _active[0] / "ub_agents" if _active else source_directory()


def descriptors():
    return (_active[1].fileno(),) if _active else ()


def helper_command(module):
    # Keep a virtualenv's interpreter symlink, and ignore cwd and PYTHONPATH.
    # The bootstrap inserts the copied package ahead of editable-install finders.
    descriptor = descriptors()[0] if _active else -1
    return [os.path.abspath(sys.executable), "-I", str(package_directory() / "helper_command.py"),
            str(descriptor), module]


def _remove_unused(path):
    with lock(path / "users.lock", create=False) as held:
        if held is not None or not path.exists() or not (path / "users.lock").exists():
            shutil.rmtree(path, ignore_errors=True)


def _cleanup(path):
    with lock(path.parent / "copies.lock", blocking=True):
        _remove_unused(path)


@contextmanager
def _activate(path, stream):
    global _active
    package = sys.modules["ub_agents"]
    previous, previous_path = _active, package.__path__
    _active = path, stream
    # Launcher modules imported lazily after refresh must use the copy too.
    package.__path__ = [str(path / "ub_agents")]
    try:
        yield path
    finally:
        _active, package.__path__ = previous, previous_path


@contextmanager
def startup_copy():
    root = user_state_directory() / "launcher-code"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = None
    with ExitStack() as stack:
        try:
            # Serialize publication and pruning so an unfinished copy cannot be
            # mistaken for an unused one by another starting launcher.
            with lock(root / "copies.lock", blocking=True):
                for old in root.glob("copy-*"):
                    _remove_unused(old)
                path = Path(tempfile.mkdtemp(prefix="copy-", dir=root))
                stream = stack.enter_context(lock(path / "users.lock", shared=True))
                shutil.copytree(package_directory(), path / "ub_agents",
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            with _activate(path, stream):
                yield path
        finally:
            stack.close()
            if path is not None:
                _cleanup(path)


@contextmanager
def use_copy(descriptor=-1):
    """A copied command retains its code until it and all inherited users exit."""
    path = Path(__file__).resolve().parent.parent
    if not (path / "users.lock").is_file():
        yield
        return
    try:
        with ExitStack() as stack:
            stream = (stack.enter_context(os.fdopen(descriptor, "rb")) if descriptor >= 0 else
                      stack.enter_context(lock(path / "users.lock", shared=True)))
            with _activate(path, stream):
                yield
    finally:
        _cleanup(path)
