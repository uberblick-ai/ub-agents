"""Conservative installation ownership detection; never bootstrap a runtime."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys


@dataclass(frozen=True)
class Installation:
    executable: str
    identity: str
    method: str = "unknown"
    prefix: Path | None = None
    package: Path | None = None
    token: str | None = None
    native: bool = False


def installation(cli, which=None, home=None):
    executable = (which or shutil.which)(cli)
    if not executable:
        return None
    executable = str(Path(executable).absolute())
    target = Path(executable).resolve()
    home = home or Path.home()
    versions = home / ".local/share/claude/versions"
    if cli == "claude" and target.parent == versions.resolve():
        return Installation(executable, str(versions.resolve()), "native", native=True)
    # A shim is not the installation it eventually dispatches to.
    if "shims" in Path(executable).parts or "shims" in target.parts:
        return Installation(executable, str(target), "shim")
    for parent in target.parents:
        if parent.parent.name in {"Caskroom", "Cellar"}:
            kind = "homebrew-cask" if parent.parent.name == "Caskroom" else "homebrew-formula"
            allowed = {"claude-code", "claude-code@latest"} if cli == "claude" else {"codex"}
            if parent.name in allowed and (cli != "claude" or kind == "homebrew-cask"):
                return Installation(executable, str(parent), kind, parent.parent.parent,
                                    parent, parent.name)
        if parent.parent.name in {"@openai", "@anthropic-ai"} and parent.parent.parent.name == "node_modules":
            modules = parent.parent.parent
            expected = "@openai/codex" if cli == "codex" else "@anthropic-ai/claude-code"
            if modules.parent.name != "lib":
                continue  # Project-local npm packages are not global installs.
            try:
                metadata = json.loads((parent / "package.json").read_text())
                bins = metadata.get("bin", {})
                entry = bins.get(cli) if isinstance(bins, dict) else bins
                owns = entry and (parent / entry).resolve() == target
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if metadata.get("name") == expected and owns:
                return Installation(executable, str(parent), "npm-global", modules.parent.parent,
                                    parent, expected)
    method = "system" if target.parent in {Path("/usr/bin"), Path("/bin"), Path("/usr/sbin")} else "unknown"
    # Custom launchers can atomically change their symlink target. Keep their
    # launch path stable across updates, as with the native/package identities.
    identity = str(Path(executable).parent.resolve() / Path(executable).name)
    return Installation(executable, identity, method)


def writable(path):
    # Mode bits also make the decision conservative when the launcher itself is root.
    return path.exists() and bool(path.stat().st_mode & 0o222) and os.access(path, os.W_OK)


def claude_updates_disabled(env):
    if env.get("DISABLE_UPDATES"):
        return "DISABLE_UPDATES is set in the launcher environment"
    user = Path(env.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    managed = (Path("/Library/Application Support/ClaudeCode") if sys.platform == "darwin"
               else Path("/etc/claude-code"))
    paths = [user / "settings.json", managed / "managed-settings.json"]
    paths.extend(sorted((managed / "managed-settings.d").glob("*.json")))
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            return f"cannot read Claude update policy in {path}: {exc}"
        settings_env = data.get("env", {}) if isinstance(data, dict) else None
        if not isinstance(settings_env, dict):
            return f"cannot read Claude update policy in {path}"
        if settings_env.get("DISABLE_UPDATES"):
            return f"DISABLE_UPDATES is set in {path}"
    return None


def updater(cli, install, policy, run, env, which=None):
    """Return a targeted argv and environment, or an actionable skip reason."""
    which = which or shutil.which
    if policy == "off":
        return None, env, "runtime-updates policy is off"
    if cli == "claude":
        reason = claude_updates_disabled(env)
        if reason:
            return None, env, reason
    if isinstance(policy, tuple):
        return list(policy), env, None
    if install.method == "native":
        return [install.executable, "update"], env, None
    if install.method.startswith("homebrew"):
        if not writable(install.prefix) or not writable(install.package.parent):
            return None, env, "installation needs elevated privileges; update it manually"
        candidate = install.prefix / "bin/brew"
        brew = str(candidate) if candidate.is_file() else which("brew")
        if not brew or Path(run([brew, "--prefix"], env).strip()).resolve() != install.prefix.resolve():
            return None, env, "owning Homebrew not found; configure runtime-updates command"
        kind = "--cask" if install.method == "homebrew-cask" else "--formula"
        return [brew, "upgrade", kind, install.token], env | {"HOMEBREW_NO_INSTALL_CLEANUP": "1"}, None
    if install.method == "npm-global":
        if not all(writable(p) for p in (install.package, install.package.parent, install.prefix / "bin")):
            return None, env, "npm global installation needs elevated privileges; update it manually"
        if cli == "claude":
            return [install.executable, "update"], env, None
        metadata = json.loads((install.package / "package.json").read_text())
        if "-" in metadata.get("version", ""):
            return None, env, "npm prerelease channel is ambiguous; configure runtime-updates command"
        npm_env = env | {"PATH": str(install.prefix / "bin") + os.pathsep + env.get("PATH", "")}
        candidate = install.prefix / "bin/npm"
        npm = str(candidate) if candidate.is_file() else which("npm")
        if not npm or Path(run([npm, "prefix", "-g"], npm_env).strip()).resolve() != install.prefix.resolve():
            return None, env, "npm owning this global prefix not found; configure runtime-updates command"
        return [npm, "install", "-g", "@openai/codex@latest"], npm_env, None
    if install.method == "system":
        return None, env, "system package installation needs elevated privileges; update it manually"
    return None, env, f"{install.method} installation; configure runtime-updates.{cli}.command argv"
