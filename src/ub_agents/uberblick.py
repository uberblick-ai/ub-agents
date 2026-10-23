"""Find the Uberblick workspace used by a project."""

WORKSPACE_FILE = ".uberblick.json"


def workspace_binding(root):
    """The nearest .uberblick.json, where ub looks for it."""
    for directory in (root, *root.parents):
        path = directory / WORKSPACE_FILE
        if path.exists() or path.is_symlink():
            return path
    return None
